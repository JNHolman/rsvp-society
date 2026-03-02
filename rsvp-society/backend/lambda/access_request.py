import json
import os
import logging
from member_store import upsert_member, normalize_phone
from sms_adapter import maybe_send_welcome

logger = logging.getLogger()

def _get_method(event: dict) -> str:
    if event.get("httpMethod"):
        return event["httpMethod"]
    rc = event.get("requestContext", {}).get("http", {})
    return rc.get("method", "")

def _get_headers(event: dict) -> dict:
    return event.get("headers") or {}

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
            "access-control-allow-methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }

def handler(event, context):
    try:
        method = _get_method(event).upper()
        origin = _get_headers(event).get("origin") or _get_headers(event).get("Origin")

        if method == "OPTIONS":
            return _resp(200, {"ok": True}, origin)

        if method != "POST":
            return _resp(405, {"ok": False, "error": "Method not allowed"}, origin)

        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        data = json.loads(raw_body) if raw_body else {}
        name = (data.get("name") or "").strip()
        phone = (data.get("phone") or "").strip()
        email = (data.get("email") or "").strip() or None
        source = (data.get("source") or "web").strip()
        sms_opt_in = bool(data.get("smsOptIn", False))

        if not name or not phone:
            return _resp(400, {"ok": False, "error": "name and phone required"}, origin)

        try:
            phone_e164 = normalize_phone(phone)
        except ValueError:
            # FIX: surface a safe, useful validation message — not raw exception
            return _resp(400, {"ok": False, "error": "Please enter a valid phone number"}, origin)

        member = upsert_member(phone=phone_e164, name=name, email=email, source=source, sms_opt_in=sms_opt_in)

        # Feature-flagged welcome SMS
        maybe_send_welcome(member)

        return _resp(200, {"ok": True}, origin)

    except json.JSONDecodeError:
        return _resp(400, {"ok": False, "error": "Invalid request format"}, origin)
    except Exception as e:
        # FIX: log internally, never surface raw exception strings
        logger.exception("Unhandled error in access_request")
        return _resp(500, {"ok": False, "error": "Something went wrong. Please try again."}, None)
