import boto3
import json
import os
from member_store import list_members_by_status, set_status, set_gender, set_tier_override, record_attendance
from sms_adapter import get_secret_string

def _get_method(event: dict) -> str:
    if event.get("httpMethod"):
        return event["httpMethod"]
    rc = event.get("requestContext", {}).get("http", {})
    return rc.get("method", "")

def _get_headers(event: dict) -> dict:
    return event.get("headers") or {}

def _get_query(event: dict) -> dict:
    return event.get("queryStringParameters") or {}

def _resp(status: int, body: dict, origin: str | None = None) -> dict:
    allowed = os.getenv("ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in allowed.split(",") if o.strip()]
    allow_origin = origin if origin in origins else (origins[0] if origins else "*")

    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": allow_origin,
            "access-control-allow-headers": "content-type,x-admin-token",
            "access-control-allow-methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body),
    }

def _admin_token() -> str:
    sid = os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token")
    s = get_secret_string(sid)
    # allow either raw string or {"token":"..."}
    try:
        j = json.loads(s)
        if isinstance(j, dict) and j.get("token"):
            return j["token"]
    except Exception:
        pass
    return s or ""


def _events_table():
    import boto3
    ddb = boto3.resource("dynamodb")
    name = os.getenv("EVENTS_TABLE_NAME", "rsvp-events")
    return ddb.Table(name)


def get_current_event():
    resp = _events_table().get_item(Key={"eventId": "current"})
    return resp.get("Item")


def set_current_event(data: dict):
    from datetime import datetime, timezone
    item = {
        "eventId": "current",
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date":      (data.get("date") or "").strip(),
        "venue":     (data.get("venue") or "").strip(),
        "dresscode": (data.get("dresscode") or "").strip(),
        "capacity":  int(data.get("capacity") or 0),
        "city":      (data.get("city") or "").strip(),
        "notes":     (data.get("notes") or "").strip(),
    }
    _events_table().put_item(Item=item)
    return item


def handler(event, context):
    try:
        method = _get_method(event).upper()
        headers = _get_headers(event)
        origin = headers.get("origin") or headers.get("Origin")

        if method == "OPTIONS":
            return _resp(200, {"ok": True}, origin)

        token = (headers.get("x-admin-token") or headers.get("X-Admin-Token") or "").strip()
        if not token or token != _admin_token():
            return _resp(401, {"ok": False, "error": "unauthorized"}, origin)

        # Route by path (REST API uses event["path"])
        path = event.get("path", "")

        if method == "GET" and path.endswith("/admin/members"):
            qs = _get_query(event)
            status = (qs.get("status") or "PENDING").upper()
            members = list_members_by_status(status=status, limit=200)
            return _resp(200, {"ok": True, "members": members}, origin)

        if method == "POST" and path.endswith("/admin/members/status"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            status = (data.get("status") or "").strip().upper()
            if not phone or status not in ("PENDING", "APPROVED", "DENIED"):
                return _resp(400, {"ok": False, "error": "phone and valid status required"}, origin)

            set_status(phone, status)
            return _resp(200, {"ok": True}, origin)


        if method == "POST" and path.endswith("/admin/members/gender"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            gender = (data.get("gender") or "").strip().upper()
            if not phone or gender not in ("M", "F", "O"):
                return _resp(400, {"ok": False, "error": "phone and gender (M/F/O) required"}, origin)
            set_gender(phone, gender)
            return _resp(200, {"ok": True}, origin)

        if method == "POST" and path.endswith("/admin/members/tier"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            tier = data.get("tier")
            if not phone or tier is None:
                return _resp(400, {"ok": False, "error": "phone and tier required"}, origin)
            set_tier_override(phone, int(tier))
            return _resp(200, {"ok": True}, origin)

        if method == "POST" and path.endswith("/admin/members/attendance"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            attended = bool(data.get("attended", False))
            if not phone:
                return _resp(400, {"ok": False, "error": "phone required"}, origin)
            record_attendance(phone, attended)
            return _resp(200, {"ok": True}, origin)


        if method == "GET" and path.endswith("/admin/event"):
            ev = get_current_event()
            return _resp(200, {"ok": True, "event": ev or {}}, origin)

        if method == "POST" and path.endswith("/admin/event"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            ev = set_current_event(data)
            return _resp(200, {"ok": True, "event": ev}, origin)


        # Public endpoint — no auth, venue hidden
        if method == "GET" and path.endswith("/event") and not path.endswith("/admin/event"):
            ev = get_current_event()
            if ev:
                public = {k: v for k, v in ev.items() if k != "venue"}
            else:
                public = {}
            return _resp(200, {"ok": True, "event": public}, origin)

        return _resp(404, {"ok": False, "error": "not found"}, origin)

    except Exception as e:
        return _resp(500, {"ok": False, "error": str(e)}, None)