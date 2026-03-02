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

    # Use locked template if available
    template = (event.get("reminder_template") or "").strip()
    if template:
        return template.replace("{name}", name).strip()

    # Fallback: build from event fields
    event_label = (event.get("event_label") or event.get("eventSlug") or "the event").strip()
    start_time  = (event.get("startTime") or "").strip()
    timing_word = "Tonight" if event.get("_is_day_of") else "Tomorrow"

    parts = []
    if start_time:
        parts.append(f"Doors at {start_time}.")
    else:
        parts.append(f"{timing_word}.")
        if event_label: parts.append(f"{event_label}.")

    return " ".join(parts)

def _get_confirmed_members(event_id: str) -> list:
    """
    Query confirmed invitees for an event using the primary hash key.
    eventId is the invites table hash key — O(invited) not O(all invites ever).
    Filter CONFIRMED in Python after the key lookup.
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

def send_reminders(event: dict, is_day_of: bool, token: str = "") -> dict:
    """Send reminder SMS to all confirmed members."""
    event["_is_day_of"] = is_day_of
    event_id = event.get("eventId", "current")
    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"

    confirmed = _get_confirmed_members(event_id)
    members_t = _members_table()

    sent = 0
    failed = 0
    skipped_consent = 0

    for invite in confirmed:
        phone = invite.get("phone", "")
        if not phone:
            continue
        try:
            result = members_t.get_item(Key={"phone": phone})
            member = result.get("Item") or {}
            # Match invite_handler consent policy exactly:
            # skip if opted out OR if smsOptIn was never given
            if member.get("optOut") or not member.get("smsOptIn", False):
                skipped_consent += 1
                continue
            name = member.get("name", "")
            message = _build_reminder(name, event)
            if sms_enabled:
                send_sms(phone, message)
            sent += 1
        except Exception:
            logger.exception("reminder_handler: failed to process phone=...%s", phone[-4:])
            failed += 1

    log_action(
        token=token,
        action=ACTION_REMINDER_SENT,
        metadata={
            "eventId":        event_id,
            "isDayOf":        is_day_of,
            "sent":           sent,
            "failed":         failed,
            "skippedConsent": skipped_consent,
            "trigger":        "manual" if token else "scheduled",
            "smsEnabled":     sms_enabled,
        },
    )

    return {"sent": sent, "failed": failed, "skippedConsent": skipped_consent}

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
        # Check if this is an API Gateway call (manual blast)
        if event.get("httpMethod") or event.get("requestContext"):
            headers = event.get("headers") or {}
            origin = headers.get("origin") or headers.get("Origin") or ""
            method = (event.get("httpMethod") or
                      event.get("requestContext", {}).get("http", {}).get("method", "")).upper()

            # OPTIONS preflight
            if method == "OPTIONS":
                return {"statusCode": 200, "headers": _cors_headers(origin), "body": "{}"}

            # Manual blast from admin panel
            body = json.loads(event.get("body") or "{}")

            # Auth check
            token = headers.get("x-admin-token") or headers.get("X-Admin-Token") or ""
            expected = get_secret_string(os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token"))
            try:
                import json as _j
                j = _j.loads(expected)
                if isinstance(j, dict): expected = j.get("token", expected)
            except Exception:
                pass

            if not token or token != expected:
                return {
                    "statusCode": 401,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "unauthorized"})
                }

            current_event = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
            if not current_event:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "No current event"})
                }

            # Require explicit is_day_of — no silent default.
            # True  → "Tonight" wording; False → "Tomorrow" wording.
            if "is_day_of" not in body:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "is_day_of (true/false) required"})
                }
            is_day_of = bool(body["is_day_of"])

            result = send_reminders(current_event, is_day_of=is_day_of, token=token)
            return {
                "statusCode": 200,
                "headers": _cors_headers(origin),
                "body": json.dumps({"ok": True, **result})
            }

        # EventBridge scheduled trigger
        current_event = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        if not current_event:
            logger.info("reminder_handler: no current event — skipping")
            return {"ok": True, "skipped": True}

        reminder_timing = current_event.get("reminderTiming", "manual")
        if reminder_timing == "manual":
            logger.info("reminder_handler: timing=manual — skipping scheduled trigger")
            return {"ok": True, "skipped": True}

        # Parse the event date and decide whether today is the right fire day.
        # EventBridge fires daily — we must validate the date or we blast every day.
        event_date_str = (current_event.get("date") or "").strip()
        if not event_date_str:
            logger.warning("reminder_handler: no event date set — skipping to avoid wrong-day blast")
            return {"ok": True, "skipped": True, "reason": "no event date"}

        # Support formats: YYYY-MM-DD, MM/DD/YYYY, "Friday, June 14, 2025"
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

        today = datetime.now(timezone.utc).date()
        is_day_of = reminder_timing == "day_of"

        # day_of: fire only on the event date itself
        # day_before: fire only the day before the event
        expected_fire_date = event_date if is_day_of else event_date - timedelta(days=1)

        if today != expected_fire_date:
            logger.info(
                "reminder_handler: today=%s expected_fire=%s — skipping",
                today.isoformat(), expected_fire_date.isoformat()
            )
            return {"ok": True, "skipped": True, "reason": "not the right day"}

        result = send_reminders(current_event, is_day_of=is_day_of, token="")
        logger.info("reminder_handler: scheduled blast sent=%s failed=%s", result["sent"], result["failed"])
        return {"ok": True, **result}

    except Exception:
        logger.exception("Unhandled error in reminder_handler")
        return {"ok": False, "error": "An internal error occurred"}
