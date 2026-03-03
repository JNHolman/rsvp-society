import json
import logging
import os
import boto3
from datetime import datetime, timezone, timedelta
from boto3.dynamodb.conditions import Key as DKey
from sms_adapter import send_sms, get_secret_string
from audit_log import log_action, ACTION_REMINDER_SENT

logger = logging.getLogger()
_DDB = boto3.resource("dynamodb")


def _events_table():
    return _DDB.Table(os.environ["EVENTS_TABLE_NAME"])


def _invites_table():
    return _DDB.Table(os.environ["INVITES_TABLE_NAME"])


def _members_table():
    return _DDB.Table(os.environ["MEMBERS_TABLE_NAME"])


def _build_reminder(member_name: str, event: dict) -> str:
    """
    Use locked reminder_template if admin approved one.
    Replace {name} with member first name.
    Fall back to building from event fields.
    """
    name = (member_name or "").split()[0] or ""

    template = (event.get("reminder_template") or "").strip()
    if template:
        return template.replace("{name}", name).strip()

    event_label = (event.get("event_label") or event.get("eventSlug") or "the event").strip()
    start_time  = (event.get("startTime") or "").strip()
    timing_word = "Tonight" if event.get("_is_day_of") else "Tomorrow"

    parts = []
    if start_time:
        parts.append(f"Doors at {start_time}.")
    else:
        parts.append(f"{timing_word}.")
        if event_label:
            parts.append(f"{event_label}.")

    return " ".join(parts)


def _get_confirmed_invites(event_id: str) -> list:
    """
    Query confirmed invitees using eventId primary hash key.
    Returns full invite items (including dedup sentinel fields).
    """
    invites_t = _invites_table()
    items = []
    kwargs: dict = {
        "KeyConditionExpression": DKey("eventId").eq(event_id),
    }
    while True:
        resp = invites_t.query(**kwargs)
        items.extend([i for i in resp.get("Items", []) if i.get("status") == "CONFIRMED"])
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _batch_get_members(phones: list) -> dict:
    """
    Fix #32: BatchGetItem instead of N+1 individual reads.
    Returns {phone: member_item} map.
    """
    if not phones:
        return {}

    members_t = _members_table()
    member_map = {}

    for i in range(0, len(phones), 100):
        batch = phones[i:i + 100]
        request_items = {
            members_t.name: {
                "Keys": [{"phone": p} for p in batch],
                "ProjectionExpression": "phone, #n, smsOptIn, optOut",
                "ExpressionAttributeNames": {"#n": "name"},
            }
        }
        try:
            while request_items:
                resp = members_t.meta.client.batch_get_item(RequestItems=request_items)
                for item in resp.get("Responses", {}).get(members_t.name, []):
                    member_map[item["phone"]] = item
                # Retry unprocessed keys (DDB throttling) until exhausted
                request_items = resp.get("UnprocessedKeys") or {}
        except Exception:
            logger.exception("_batch_get_members: batch failed for %d phones", len(batch))

    return member_map


def send_reminders(event: dict, is_day_of: bool, token: str = "") -> dict:
    """Send reminder SMS to all confirmed members who haven't been reminded yet."""
    # Don't mutate the caller's dict — work on a shallow copy
    event = {**event, "_is_day_of": is_day_of}
    event_id = event.get("eventId", "current")
    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"

    # C3: use separate sentinel fields for day-before and day-of so both can
    # fire for the same event without the first blocking the second.
    reminder_field = "dayOfReminderSentAt" if is_day_of else "dayBeforeReminderSentAt"

    confirmed = _get_confirmed_invites(event_id)

    # Fix #32: single BatchGetItem call instead of N+1 individual reads
    phones = [inv.get("phone") for inv in confirmed if inv.get("phone")]
    member_map = _batch_get_members(phones)

    invites_t = _invites_table()
    sent = 0
    failed = 0
    skipped_consent = 0
    skipped_already_sent = 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for invite in confirmed:
        phone = invite.get("phone", "")
        if not phone:
            continue

        # C3: skip members who already received this reminder type
        if invite.get(reminder_field):
            skipped_already_sent += 1
            continue

        try:
            member = member_map.get(phone) or {}
            if member.get("optOut") or not member.get("smsOptIn", False):
                skipped_consent += 1
                continue

            name = member.get("name", "")
            message = _build_reminder(name, event)

            if sms_enabled:
                send_sms(phone, message)

            # C3: mark as reminded so manual re-blasts don't double-send
            try:
                invites_t.update_item(
                    Key={"eventId": event_id, "phone": phone},
                    UpdateExpression=f"SET {reminder_field} = :now",
                    ExpressionAttributeValues={":now": now},
                )
            except Exception:
                logger.exception(
                    "reminder_handler: failed to write %s for phone=...%s",
                    reminder_field, phone[-4:],
                )
                # Non-fatal — message was sent, dedup just couldn't be written

            sent += 1

        except Exception:
            logger.exception(
                "reminder_handler: failed to process phone=...%s", phone[-4:]
            )
            failed += 1

    log_action(
        token=token,
        action=ACTION_REMINDER_SENT,
        metadata={
            "eventId":            event_id,
            "isDayOf":            is_day_of,
            "sent":               sent,
            "failed":             failed,
            "skippedConsent":     skipped_consent,
            "skippedAlreadySent": skipped_already_sent,
            "trigger":            "manual" if token else "scheduled",
            "smsEnabled":         sms_enabled,
        },
    )

    return {
        "sent":               sent,
        "failed":             failed,
        "skippedConsent":     skipped_consent,
        "skippedAlreadySent": skipped_already_sent,
    }


def _cors_headers(origin=None):
    allowed_raw = os.getenv("ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in allowed_raw.split(",") if o.strip()]
    allow_origin = origin if origin in origins else (origins[0] if origins else "*")
    return {
        "content-type": "application/json",
        "access-control-allow-origin": allow_origin,
        "access-control-allow-headers": "content-type,x-admin-token",
        "access-control-allow-methods": "POST,OPTIONS",
    }


def handler(event, context):
    """
    Triggered by:
    1. EventBridge scheduled rule (automatic)
    2. API Gateway POST /admin/invite/reminder (manual blast)
    """
    try:
        if event.get("httpMethod") or event.get("requestContext"):
            headers = event.get("headers") or {}
            origin  = headers.get("origin") or headers.get("Origin") or ""
            method  = (
                event.get("httpMethod") or
                event.get("requestContext", {}).get("http", {}).get("method", "")
            ).upper()

            if method == "OPTIONS":
                return {"statusCode": 200, "headers": _cors_headers(origin), "body": "{}"}

            body = json.loads(event.get("body") or "{}")

            token    = headers.get("x-admin-token") or headers.get("X-Admin-Token") or ""
            expected = get_secret_string(os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token"))
            try:
                j = json.loads(expected)
                if isinstance(j, dict):
                    expected = j.get("token", expected)
            except Exception:
                pass

            if not token or token != expected:
                return {
                    "statusCode": 401,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "unauthorized"}),
                }

            current_event = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
            if not current_event:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "No current event"}),
                }

            if "is_day_of" not in body:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "is_day_of (true/false) required"}),
                }
            is_day_of = bool(body["is_day_of"])

            result = send_reminders(current_event, is_day_of=is_day_of, token=token)
            return {
                "statusCode": 200,
                "headers": _cors_headers(origin),
                "body": json.dumps({"ok": True, **result}),
            }

        # ── EventBridge scheduled trigger ─────────────────────────────────────
        current_event = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        if not current_event:
            logger.info("reminder_handler: no current event — skipping")
            return {"ok": True, "skipped": True}

        reminder_timing = current_event.get("reminderTiming", "manual")
        if reminder_timing == "manual":
            logger.info("reminder_handler: timing=manual — skipping scheduled trigger")
            return {"ok": True, "skipped": True}

        event_date_str = (current_event.get("date") or "").strip()
        if not event_date_str:
            logger.warning("reminder_handler: no event date set — skipping")
            return {"ok": True, "skipped": True, "reason": "no event date"}

        event_date = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%A, %B %d, %Y"):
            try:
                event_date = datetime.strptime(event_date_str, fmt).date()
                break
            except ValueError:
                continue

        if event_date is None:
            logger.warning("reminder_handler: unparseable date '%s' — skipping", event_date_str)
            return {"ok": True, "skipped": True, "reason": "unparseable date"}

        today    = datetime.now(timezone.utc).date()
        is_day_of = reminder_timing == "day_of"
        expected_fire_date = event_date if is_day_of else event_date - timedelta(days=1)

        if today != expected_fire_date:
            logger.info(
                "reminder_handler: today=%s expected_fire=%s — skipping",
                today.isoformat(), expected_fire_date.isoformat(),
            )
            return {"ok": True, "skipped": True, "reason": "not the right day"}

        result = send_reminders(current_event, is_day_of=is_day_of, token="")
        logger.info(
            "reminder_handler: scheduled blast sent=%s failed=%s skippedAlreadySent=%s",
            result["sent"], result["failed"], result["skippedAlreadySent"],
        )
        return {"ok": True, **result}

    except Exception:
        logger.exception("Unhandled error in reminder_handler")
        return {"ok": False, "error": "An internal error occurred"}
