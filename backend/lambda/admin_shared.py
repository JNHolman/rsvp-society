"""
admin_shared.py
Shared helpers used by admin_handler, admin_member_routes, and admin_event_routes.
Nothing in here has side effects — pure utilities only.
"""
import json
import logging
import os
from datetime import datetime
from decimal import Decimal

import boto3
from sms_adapter import get_secret_string

logger = logging.getLogger()


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", ""}:
            return False
    return bool(value)


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def get_method(event: dict) -> str:
    if event.get("httpMethod"):
        return event["httpMethod"]
    rc = event.get("requestContext", {}).get("http", {})
    return rc.get("method", "")


def get_headers(event: dict) -> dict:
    return event.get("headers") or {}


def get_query(event: dict) -> dict:
    return event.get("queryStringParameters") or {}


def get_body(event: dict) -> dict:
    raw = event.get("body") or ""
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def _allowed_origins() -> list:
    csv = os.getenv("ALLOWED_ORIGINS", "")
    return [o.strip() for o in csv.split(",") if o.strip()]


def _pick_origin(headers: dict) -> str:
    origin = (headers.get("origin") or headers.get("Origin") or "").strip()
    allowed = _allowed_origins()
    if origin and origin in allowed:
        return origin
    return allowed[0] if allowed else ""


def cors_headers(headers: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": _pick_origin(headers),
        "Access-Control-Allow-Headers": "content-type,x-admin-token",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Vary": "Origin",
    }


def resp(headers: dict, status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": cors_headers(headers),
        "body": json.dumps(body, cls=DecimalEncoder) if not isinstance(body, str) else body,
    }


def get_admin_token() -> str:
    sid = os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token")
    s = get_secret_string(sid)
    try:
        j = json.loads(s)
        if isinstance(j, dict) and j.get("token"):
            return j["token"]
    except Exception:
        pass
    return s or ""


def events_table():
    return boto3.resource("dynamodb").Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))


def event_history_table():
    return boto3.resource("dynamodb").Table(os.getenv("EVENT_HISTORY_TABLE_NAME", "rsvp-event-history"))


def members_table():
    return boto3.resource("dynamodb").Table(os.getenv("MEMBERS_TABLE_NAME", "rsvp-members"))


def invites_table():
    return boto3.resource("dynamodb").Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))




def normalize_event_date(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%A, %B %d, %Y", "%A %B %d, %Y", "%a, %B %d, %Y", "%a %B %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"date must be YYYY-MM-DD, got: {raw!r}")


def normalize_event_time(value: str, *, field_name: str = "time", allow_blank: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        if allow_blank:
            return ""
        raise ValueError(f"{field_name} is required")
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(raw.upper(), fmt).strftime("%H:%M")
        except ValueError:
            continue
    raise ValueError(f"{field_name} must be HH:MM, got: {raw!r}")




def validate_schedule_time_step(value: str, *, field_name: str = "time", minute_step: int = 5) -> str:
    hhmm = normalize_event_time(value, field_name=field_name, allow_blank=False)
    minute = int(hhmm.split(":", 1)[1])
    if minute % minute_step != 0:
        raise ValueError(f"{field_name} must align to {minute_step}-minute intervals, got: {hhmm!r}")
    return hhmm

def normalize_import_source(value: str) -> str:
    src = (value or "").strip().lower()
    if src in ("csv", "web", "import", "eventbrite", "posh", "superphone"):
        return src
    return ""


def event_identity(event: dict) -> str:
    slug = str(event.get("eventSlug") or event.get("slug") or "").strip()
    if slug:
        return slug
    date = str(event.get("date") or "").strip()
    if date:
        return f"event-{date}"
    return ""


def resolve_event_slug(event: dict) -> str:
    """Canonical event identity for invite/checkin/reminder queries.

    The operational key for all invite, checkin, and reminder lookups is
    the eventSlug — never 'current'. This helper resolves it consistently
    so every handler uses the same logic.

    Returns empty string if no slug can be resolved.
    """
    return str(event.get("eventSlug") or "").strip()


def normalize_event_record(event: dict, *, source: str = "snapshot") -> dict:
    event = dict(event or {})
    identity = event_identity(event) or str(event.get("eventId") or "").strip()
    date = str(event.get("date") or "").strip()
    venue = str(event.get("venue") or "").strip()
    label = event.get("label") or event.get("event_label") or ""
    if not label:
        bits = [identity or "event", date, venue]
        label = " · ".join([b for b in bits if b])
    raw_event_id = str(event.get("eventId") or "").strip()
    event_id = identity if raw_event_id in {"", "current"} else raw_event_id
    return {
        "eventId": event_id or identity,
        "slug": identity,
        "eventSlug": str(event.get("eventSlug") or identity).strip() or identity,
        "date": date,
        "startTime": str(event.get("startTime") or "").strip(),
        "venue": venue,
        "city": str(event.get("city") or "").strip(),
        "capacity": event.get("capacity") or "",
        "updatedAt": str(event.get("updatedAt") or "").strip(),
        "label": label,
        "event_timezone": str(event.get("event_timezone") or "America/New_York").strip() or "America/New_York",
        "day_before_send_time": str(event.get("day_before_send_time") or "18:00").strip() or "18:00",
        "day_of_send_time": str(event.get("day_of_send_time") or "11:00").strip() or "11:00",
        "revealVenue": coerce_bool(event.get("revealVenue", False)),
        "source": source,
    }


def archive_event_snapshot(event: dict) -> None:
    snapshot = normalize_event_record(event, source="snapshot")
    slug = snapshot.get("slug") or snapshot.get("eventId")
    if not slug or slug == "current":
        return

    from datetime import datetime, timezone
    archived_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    source_updated_at = str(event.get("updatedAt") or "").strip()

    item = dict(event or {})
    item.update({
        "historyPk": "EVENT",
        "eventKey": f"{slug}#{archived_at}",
        "eventId": slug,
        "eventSlug": str(event.get("eventSlug") or slug).strip() or slug,
        "slug": slug,
        "archivedFrom": "current",
        "archivedAt": archived_at,
        "sourceUpdatedAt": source_updated_at,
        "lastSeenAt": source_updated_at or archived_at,
    })
    event_history_table().put_item(Item=item)
