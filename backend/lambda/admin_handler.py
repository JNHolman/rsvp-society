import boto3
from boto3.dynamodb.conditions import Attr
import json
from decimal import Decimal

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)
import logging
import os

from member_store import (
    list_members_by_status, set_status, set_gender, set_tier_override,
    record_attendance, delete_member, upsert_member, normalize_phone,
    search_members,
)
from sms_adapter import get_secret_string

logger = logging.getLogger()


def _get_method(event: dict) -> str:
    if event.get("httpMethod"):
        return event["httpMethod"]
    rc = event.get("requestContext", {}).get("http", {})
    return rc.get("method", "")


def _get_headers(event: dict) -> dict:
    return event.get("headers") or {}


def _get_query(event: dict) -> dict:
    return event.get("queryStringParameters") or {}


def _allowed_origins():
    csv = os.getenv("ALLOWED_ORIGINS", "")
    return [o.strip() for o in csv.split(",") if o.strip()]


def _pick_origin(headers: dict) -> str:
    origin = (headers.get("origin") or headers.get("Origin") or "").strip()
    allowed = _allowed_origins()
    if origin and origin in allowed:
        return origin
    return allowed[0] if allowed else "*"


def _cors_headers(headers: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": _pick_origin(headers),
        "Access-Control-Allow-Headers": "content-type,x-admin-token",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Vary": "Origin",
    }


def _resp(headers: dict, status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": _cors_headers(headers),
        "body": json.dumps(body, cls=DecimalEncoder) if not isinstance(body, str) else body,
    }


def _admin_token() -> str:
    sid = os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token")
    s = get_secret_string(sid)
    try:
        j = json.loads(s)
        if isinstance(j, dict) and j.get("token"):
            return j["token"]
    except Exception:
        pass
    return s or ""


def _events_table():
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
    headers = {}
    try:
        method = _get_method(event).upper()
        headers = _get_headers(event)

        if method == "OPTIONS":
            return _resp(headers, 200, {"ok": True})

        token = (headers.get("x-admin-token") or headers.get("X-Admin-Token") or "").strip()
        if not token or token != _admin_token():
            return _resp(headers, 401, {"ok": False, "error": "unauthorized"})

        path = event.get("path", "")

        # ── GET /admin/members ──
        if method == "GET" and path.endswith("/admin/members"):
            qs = _get_query(event)
            status = (qs.get("status") or "PENDING").upper()
            members = list_members_by_status(status=status)
            return _resp(headers, 200, {"ok": True, "members": members})

        # ── DELETE /admin/members ──
        if method == "DELETE" and path.endswith("/admin/members"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            if not phone:
                return _resp(headers, 400, {"ok": False, "error": "phone required"})
            delete_member(phone)
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/status ──
        if method == "POST" and path.endswith("/admin/members/status"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            status = (data.get("status") or "").strip().upper()
            if not phone or status not in ("PENDING", "APPROVED", "DENIED"):
                return _resp(headers, 400, {"ok": False, "error": "phone and valid status required"})
            set_status(phone, status)
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/gender ──
        if method == "POST" and path.endswith("/admin/members/gender"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            gender = (data.get("gender") or "").strip().upper()
            if not phone or gender not in ("M", "F", "O"):
                return _resp(headers, 400, {"ok": False, "error": "phone and gender (M/F/O) required"})
            set_gender(phone, gender)
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/tier ──
        if method == "POST" and path.endswith("/admin/members/tier"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            tier = data.get("tier")
            if not phone or tier is None:
                return _resp(headers, 400, {"ok": False, "error": "phone and tier required"})
            set_tier_override(phone, int(tier))
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/attendance ──
        if method == "POST" and path.endswith("/admin/members/attendance"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            attended = bool(data.get("attended", False))
            if not phone:
                return _resp(headers, 400, {"ok": False, "error": "phone required"})
            record_attendance(phone, attended)
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/import ──
        if method == "POST" and path.endswith("/admin/members/import"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            members_to_import = data.get("members") or []
            if not members_to_import:
                return _resp(headers, 400, {"ok": False, "error": "members array required"})

            imported = 0
            skipped = 0
            errors = []

            for idx, row in enumerate(members_to_import):
                try:
                    raw_phone = (row.get("phone") or "").strip()
                    if not raw_phone:
                        skipped += 1
                        continue

                    phone_e164 = normalize_phone(raw_phone)
                    first_name = (row.get("name") or "").strip() or "Unknown"
                    last_name = (row.get("lastName") or "").strip() or None
                    email = (row.get("email") or "").strip() or None
                    instagram = (row.get("instagram") or "").strip() or None
                    tags = (row.get("tags") or "").strip() or None
                    sms_opt_in = bool(row.get("smsOptIn", True))

                    upsert_member(
                        phone=phone_e164,
                        name=first_name,
                        last_name=last_name,
                        email=email,
                        source="import",
                        sms_opt_in=sms_opt_in,
                        tags=tags,
                    )

                    if instagram:
                        ddb = boto3.resource("dynamodb")
                        t = ddb.Table(os.getenv("MEMBERS_TABLE_NAME"))
                        t.update_item(
                            Key={"phone": phone_e164},
                            UpdateExpression="SET instagram = :ig",
                            ExpressionAttributeValues={":ig": instagram[:80]},
                        )

                    set_status(phone_e164, "APPROVED")
                    imported += 1

                except Exception as row_err:
                    msg = f"row {idx} phone={row.get('phone')}: {row_err}"
                    logger.error(msg)
                    errors.append(msg)
                    skipped += 1
                    continue

            return _resp(headers, 200, {
                "ok": True,
                "imported": imported,
                "skipped": skipped,
                "errors": errors[:10],
            })

        # ── GET /admin/members/search ──
        if method == "GET" and path.endswith("/admin/members/search"):
            qs = _get_query(event)
            query = (qs.get("q") or "").strip()
            if not query:
                return _resp(headers, 400, {"ok": False, "error": "q parameter required"})
            members = search_members(query, limit=50)
            return _resp(headers, 200, {"ok": True, "members": members})

        # ── GET /admin/event ──
        if method == "GET" and path.endswith("/admin/event"):
            ev = get_current_event()
            return _resp(headers, 200, {"ok": True, "event": ev or {}})

        # ── POST /admin/event ──
        if method == "POST" and path.endswith("/admin/event"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            ev = set_current_event(data)
            return _resp(headers, 200, {"ok": True, "event": ev})

        # ── GET /event (public) ──
        if method == "GET" and path.endswith("/event") and not path.endswith("/admin/event"):
            ev = get_current_event()
            if ev:
                public = {k: v for k, v in ev.items() if k != "venue"}
            else:
                public = {}
            return _resp(headers, 200, {"ok": True, "event": public})

        return _resp(headers, 404, {"ok": False, "error": "not found"})

    except Exception as e:
        logger.exception("Unhandled error in admin_handler")
        return _resp(headers, 500, {"ok": False, "error": str(e)})
