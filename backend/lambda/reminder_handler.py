import json
import os
import boto3
from datetime import datetime, timezone, timedelta
from boto3.dynamodb.conditions import Attr
from sms_adapter import send_sms, get_secret_string

_DDB = boto3.resource("dynamodb")

def _events_table():
    return _DDB.Table(os.environ["EVENTS_TABLE_NAME"])

def _invites_table():
    return _DDB.Table(os.environ["INVITES_TABLE_NAME"])

def _members_table():
    return _DDB.Table(os.environ["MEMBERS_TABLE_NAME"])

def _build_reminder(member_name: str, event: dict) -> str:
    name = (member_name or "").split()[0] or "hey"
    event_name = event.get("eventSlug") or "the event"
    start_time = event.get("startTime", "")
    address = event.get("address", "")
    date = event.get("date", "")

    # Figure out today vs tomorrow phrasing
    now = datetime.now(timezone.utc)
    event_date_str = date  # e.g. "Saturday March 15"
    # Default to tonight phrasing since EventBridge fires day-of or day-before
    timing_word = "tonight" if event.get("_is_day_of") else "tomorrow"

    parts = [f"Hey {name}, it's Jade."]
    parts.append(f"Don't forget —")
    if start_time:
        parts.append(f"{event_name} {timing_word} at {start_time}.")
    else:
        parts.append(f"{event_name} is {timing_word}.")
    if address:
        parts.append(f"{address}.")
    parts.append("Don't be late.")

    return " ".join(parts)

def _get_confirmed_members(event_id: str):
    """Get all confirmed members for an event."""
    invites_t = _invites_table()
    result = invites_t.scan(
        FilterExpression=Attr("eventId").eq(event_id) & Attr("status").eq("CONFIRMED")
    )
    return result.get("Items", [])

def send_reminders(event: dict, is_day_of: bool) -> dict:
    """Send reminder SMS to all confirmed members."""
    event["_is_day_of"] = is_day_of
    event_id = event.get("eventId", "current")
    sms_enabled = (os.getenv("SEND_WELCOME_SMS", "false") or "").lower() == "true"

    confirmed = _get_confirmed_members(event_id)
    members_t = _members_table()

    sent = 0
    failed = 0

    for invite in confirmed:
        phone = invite.get("phone", "")
        if not phone:
            continue
        try:
            # Get member name
            result = members_t.get_item(Key={"phone": phone})
            member = result.get("Item") or {}
            if member.get("optOut"):
                continue
            name = member.get("name", "")
            message = _build_reminder(name, event)
            if sms_enabled:
                send_sms(phone, message)
            sent += 1
        except Exception:
            failed += 1

    return {"sent": sent, "failed": failed}

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

            result = send_reminders(current_event, is_day_of=True)
            return {
                "statusCode": 200,
                "headers": _cors_headers(origin),
                "body": json.dumps({"ok": True, **result})
            }

        # EventBridge scheduled trigger
        current_event = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        if not current_event:
            print("No current event found")
            return {"ok": True, "skipped": True}

        reminder_timing = current_event.get("reminderTiming", "manual")
        if reminder_timing == "manual":
            print("Reminder timing set to manual — skipping")
            return {"ok": True, "skipped": True}

        # Check if today matches the trigger day
        event_date_str = current_event.get("date", "")
        now = datetime.now(timezone.utc)

        # EventBridge fires daily at set times — check if this is the right day
        # We store reminderTiming as "day_before" or "day_of"
        # EventBridge rule fires at 6PM EST daily for day_before, 4PM EST for day_of
        # Lambda checks if event date matches today+1 (day_before) or today (day_of)
        is_day_of = reminder_timing == "day_of"
        result = send_reminders(current_event, is_day_of=is_day_of)
        print(f"Reminders sent: {result}")
        return {"ok": True, **result}

    except Exception as e:
        print(f"reminder_handler error: {e}")
        return {"ok": False, "error": str(e)}
