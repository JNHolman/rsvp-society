import hmac
import json
import logging
import os
import time
import boto3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key as DKey
from sms_adapter import send_sms, get_secret_string
from admin_shared import coerce_bool, normalize_event_date, normalize_event_time, resolve_event_slug
from audit_log import log_action, ACTION_REMINDER_SENT

logger = logging.getLogger()
_DDB = boto3.resource("dynamodb")


def _events_table():
    return _DDB.Table(os.environ["EVENTS_TABLE_NAME"])


def _invites_table():
    return _DDB.Table(os.environ["INVITES_TABLE_NAME"])


def _members_table():
    return _DDB.Table(os.environ["MEMBERS_TABLE_NAME"])


def _event_timezone_name(event: dict) -> str:
    tz_name = (event.get("event_timezone") or "America/New_York").strip()
    return tz_name or "America/New_York"


def _event_zoneinfo(event: dict):
    tz_name = _event_timezone_name(event)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("reminder_handler: invalid event_timezone=%s — defaulting to America/New_York", tz_name)
        return ZoneInfo("America/New_York")


def _scheduled_target_time(event: dict, is_day_of: bool) -> tuple[int, int]:
    default_value = "11:00" if is_day_of else "18:00"
    field_name = "day_of_send_time" if is_day_of else "day_before_send_time"
    raw_value = event.get(field_name) or default_value
    try:
        hhmm = normalize_event_time(raw_value, field_name=field_name, allow_blank=False)
    except ValueError:
        logger.warning("reminder_handler: invalid %s=%s — defaulting to %s", field_name, raw_value, default_value)
        hhmm = default_value
    hour_str, minute_str = hhmm.split(":", 1)
    return int(hour_str), int(minute_str)


def _within_scheduled_window(local_now: datetime, target_hour: int, target_minute: int, *, window_minutes: int = 5) -> bool:
    current_total = local_now.hour * 60 + local_now.minute
    target_total = target_hour * 60 + target_minute
    return target_total <= current_total < (target_total + window_minutes)


def _build_reminder(member_name: str, event: dict) -> str:
    """
    Build the reminder SMS for a member.

    Reads day_of_template or day_before_template depending on _is_day_of flag.
    Falls back to legacy reminder_template for backward compat with old saved events.
    Falls back to building from event fields if no template is saved.
    Replace {name} with member first name.
    """
    name       = (member_name or "").split()[0] or ""
    is_day_of  = event.get("_is_day_of", False)

    # Try the specific template first, then legacy single template
    if is_day_of:
        template = (event.get("day_of_template") or event.get("reminder_template") or "").strip()
    else:
        template = (event.get("day_before_template") or event.get("reminder_template") or "").strip()

    if template:
        return template.replace("{name}", name).strip()

    # Fallback: build from event fields
    event_label = (event.get("event_label") or event.get("eventSlug") or "").strip()
    start_time  = (event.get("startTime") or "").strip()
    timing_word = "Tonight" if is_day_of else "Tomorrow"

    parts = [f"{name}." if name else ""]
    parts.append(f"{timing_word}.")
    if event_label:
        parts.append(f"{event_label}.")
    if start_time:
        parts.append(f"Doors at {start_time}.")

    return " ".join(p for p in parts if p)


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
        # Include CONFIRMED and ATTENDED — ATTENDED members were confirmed and
        # may still need reminders (e.g. logistics day-of)
        items.extend([i for i in resp.get("Items", [])
                      if i.get("status") in ("CONFIRMED", "ATTENDED")])
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


def send_reminders(event: dict, is_day_of: bool, token: str = "", custom_message: str = None) -> dict:
    """Send reminder SMS to all confirmed members who haven't been reminded yet."""
    event = {**event, "_is_day_of": is_day_of}
    # Canonical event identity — always the slug, never "current".
    event_id = resolve_event_slug(event)
    if not event_id:
        logger.warning("send_reminders: no eventSlug on current event — cannot query invites")
        return {"sent": 0, "failed": 0, "skippedAlreadySent": 0, "skippedOptOut": 0}
    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"

    reminder_field = "dayOfReminderSentAt" if is_day_of else "dayBeforeReminderSentAt"
    claim_field = "dayOfReminderClaimedAt" if is_day_of else "dayBeforeReminderClaimedAt"

    confirmed = _get_confirmed_invites(event_id)
    invites_t = _invites_table()
    sent = 0
    failed = 0
    skipped_already_sent = 0
    skipped_opt_out = 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Batch-fetch member records so we can enforce opt-out without N+1 reads.
    all_phones = [inv.get("phone", "") for inv in confirmed if inv.get("phone")]
    member_map = _batch_get_members(all_phones)

    for invite in confirmed:
        phone = invite.get("phone", "")
        if not phone:
            continue

        # Opt-out and consent enforcement — never text someone who sent STOP
        # or who hasn't opted in, regardless of invite status.
        member = member_map.get(phone)
        if member and member.get("optOut"):
            skipped_opt_out += 1
            continue
        if member and not member.get("smsOptIn"):
            skipped_opt_out += 1
            continue

        # Manual blasts (custom_message) are free-send update texts — no dedup guard,
        # no sentinel stamp. They can fire unlimited times.
        # Scheduled reminders use the sentinel to prevent double-sends.
        if not custom_message:
            if invite.get(reminder_field):
                skipped_already_sent += 1
                continue

        try:
            name = invite.get("name", "")
            if custom_message:
                # Manual blast: free-send, no claim/stamp cycle
                message = custom_message.replace("{name}", (name or "").split()[0] or "")
                if sms_enabled:
                    send_sms(phone, message)
                    time.sleep(0.25)
                sent += 1
            else:
                # Scheduled reminder: claim row, send, stamp
                try:
                    invites_t.update_item(
                        Key={"eventId": event_id, "phone": phone},
                        UpdateExpression=f"SET {claim_field} = :now",
                        ConditionExpression=f"attribute_not_exists({reminder_field}) AND attribute_not_exists({claim_field})",
                        ExpressionAttributeValues={":now": now},
                    )
                except ClientError as exc:
                    error_code = (exc.response.get("Error") or {}).get("Code")
                    if error_code == "ConditionalCheckFailedException":
                        skipped_already_sent += 1
                        continue
                    raise

                message = _build_reminder(name, event)
                if sms_enabled:
                    send_sms(phone, message)
                    time.sleep(0.25)

                invites_t.update_item(
                    Key={"eventId": event_id, "phone": phone},
                    UpdateExpression=f"SET {reminder_field} = :now REMOVE {claim_field}",
                    ExpressionAttributeValues={":now": now},
                )
                sent += 1

        except Exception:
            logger.exception(
                "reminder_handler: failed to process phone=...%s", phone[-4:]
            )
            failed += 1
            if not custom_message:
                try:
                    invites_t.update_item(
                        Key={"eventId": event_id, "phone": phone},
                        UpdateExpression=f"REMOVE {claim_field}",
                    )
                except Exception:
                    logger.exception(
                        "reminder_handler: failed to clear %s for phone=...%s", claim_field, phone[-4:]
                    )

    log_action(
        token=token,
        action=ACTION_REMINDER_SENT,
        metadata={
            "eventId":            event_id,
            "isDayOf":            is_day_of,
            "sent":               sent,
            "failed":             failed,
            "skippedAlreadySent": skipped_already_sent,
            "skippedOptOut":      skipped_opt_out,
            "trigger":            "manual" if token else "scheduled",
            "customMessage":      bool(custom_message),
            "smsEnabled":         sms_enabled,
        },
    )

    return {
        "sent":               sent,
        "failed":             failed,
        "skippedAlreadySent": skipped_already_sent,
        "skippedOptOut":      skipped_opt_out,
    }


def _cors_headers(origin=None):
    allowed_raw = os.getenv("ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in allowed_raw.split(",") if o.strip()]
    # Fail closed: never fall back to wildcard — that opens the endpoint to any domain.
    allow_origin = origin if origin in origins else (origins[0] if origins else "")
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

            if not token or not hmac.compare_digest(token, expected):
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

            # Lifecycle guard — reminders only send for active events
            ev_state = (current_event.get("event_status") or "DRAFT").upper()
            REMINDER_ALLOWED_STATES = {"LIVE", "INVITING", "LOCKED", "CHECK_IN_OPEN"}
            if ev_state not in REMINDER_ALLOWED_STATES:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({
                        "ok": False,
                        "error": f"Cannot send reminders — event is {ev_state}. "
                                 f"Event must be LIVE, INVITING, LOCKED, or CHECK_IN_OPEN."
                    }),
                }

            manual_timing = str(body.get("timing") or "").strip().lower()
            if manual_timing in {"day_before", "day_of"}:
                is_day_of = manual_timing == "day_of"
            elif "is_day_of" in body:
                is_day_of = coerce_bool(body["is_day_of"])
            else:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "timing (day_before/day_of) or is_day_of (true/false) required"}),
                }

            custom_message = str(body.get("custom_message") or "").strip() or None
            result = send_reminders(current_event, is_day_of=is_day_of, token=token, custom_message=custom_message)
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

        # Lifecycle guard — never fire scheduled reminders for inactive events
        sched_ev_state = (current_event.get("event_status") or "DRAFT").upper()
        REMINDER_ALLOWED_STATES = {"LIVE", "INVITING", "LOCKED", "CHECK_IN_OPEN"}
        if sched_ev_state not in REMINDER_ALLOWED_STATES:
            logger.info("reminder_handler: skipping scheduled reminder — event is %s", sched_ev_state)
            return {"ok": True, "skipped": True, "reason": f"event_status={sched_ev_state}"}

        reminder_timing = current_event.get("reminderTiming", "manual")
        if reminder_timing == "manual":
            logger.info("reminder_handler: timing=manual — skipping scheduled trigger")
            return {"ok": True, "skipped": True}

        event_date_str = (current_event.get("date") or "").strip()
        if not event_date_str:
            logger.warning("reminder_handler: no event date set — skipping")
            return {"ok": True, "skipped": True, "reason": "no event date"}

        try:
            event_date = datetime.strptime(normalize_event_date(event_date_str), "%Y-%m-%d").date()
        except ValueError:
            logger.warning("reminder_handler: unparseable date '%s' — skipping", event_date_str)
            return {"ok": True, "skipped": True, "reason": "unparseable date"}

        trigger_timing = str(event.get("timing") or "").strip().lower()

        if reminder_timing == "both":
            if trigger_timing in {"day_before", "day_of"}:
                is_day_of = trigger_timing == "day_of"
            else:
                zone = _event_zoneinfo(current_event)
                local_now = datetime.now(zone)
                local_today = local_now.date()
                before_hour, before_minute = _scheduled_target_time(current_event, False)
                dayof_hour, dayof_minute = _scheduled_target_time(current_event, True)
                if local_today == (event_date - timedelta(days=1)) and _within_scheduled_window(local_now, before_hour, before_minute):
                    is_day_of = False
                    logger.info("reminder_handler: inferred day_before trigger for timing=both")
                elif local_today == event_date and _within_scheduled_window(local_now, dayof_hour, dayof_minute):
                    is_day_of = True
                    logger.info("reminder_handler: inferred day_of trigger for timing=both")
                else:
                    logger.info("reminder_handler: timing=both and trigger timing missing outside eligible windows — skipping")
                    return {"ok": True, "skipped": True, "reason": "missing trigger timing"}
        elif reminder_timing == "day_before":
            if trigger_timing and trigger_timing != "day_before":
                logger.info("reminder_handler: trigger=%s but event timing=day_before — skipping", trigger_timing)
                return {"ok": True, "skipped": True, "reason": "wrong trigger timing"}
            is_day_of = False
        elif reminder_timing == "day_of":
            if trigger_timing and trigger_timing != "day_of":
                logger.info("reminder_handler: trigger=%s but event timing=day_of — skipping", trigger_timing)
                return {"ok": True, "skipped": True, "reason": "wrong trigger timing"}
            is_day_of = True
        else:
            logger.info("reminder_handler: unsupported timing=%s — skipping", reminder_timing)
            return {"ok": True, "skipped": True, "reason": "unsupported timing"}

        zone = _event_zoneinfo(current_event)
        local_now = datetime.now(zone)
        local_today = local_now.date()
        target_hour, target_minute = _scheduled_target_time(current_event, is_day_of)
        expected_fire_date = event_date if is_day_of else event_date - timedelta(days=1)

        if local_today != expected_fire_date:
            logger.info(
                "reminder_handler: local_today=%s expected_fire=%s tz=%s — skipping",
                local_today.isoformat(), expected_fire_date.isoformat(), _event_timezone_name(current_event),
            )
            return {"ok": True, "skipped": True, "reason": "not the right day"}

        if not _within_scheduled_window(local_now, target_hour, target_minute, window_minutes=5):
            logger.info(
                "reminder_handler: local_time=%s target_time=%02d:%02d tz=%s trigger=%s — skipping",
                local_now.strftime("%H:%M"), target_hour, target_minute, _event_timezone_name(current_event), trigger_timing,
            )
            return {"ok": True, "skipped": True, "reason": "not the right minute"}

        result = send_reminders(current_event, is_day_of=is_day_of, token="")
        logger.info(
            "reminder_handler: scheduled blast sent=%s failed=%s skippedAlreadySent=%s",
            result["sent"], result["failed"], result["skippedAlreadySent"],
        )
        return {"ok": True, **result}

    except Exception:
        logger.exception("Unhandled error in reminder_handler")
        return {"ok": False, "error": "An internal error occurred"}
