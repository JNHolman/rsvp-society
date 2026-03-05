import base64
import json
import logging
import os
from member_store import upsert_member, normalize_phone

logger = logging.getLogger()


def _coerce_bool(value) -> bool:
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
            try:
                raw_body = base64.b64decode(raw_body).decode("utf-8")
            except Exception:
                return _resp(400, {"ok": False, "error": "invalid request encoding"}, origin)

        try:
            data = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            return _resp(400, {"ok": False, "error": "invalid JSON body"}, origin)

        name = (data.get("name") or "").strip()
        phone = (data.get("phone") or "").strip()
        email = (data.get("email") or "").strip() or None
        source = (data.get("source") or "web").strip()
        sms_opt_in = _coerce_bool(data.get("smsOptIn", False))

        if not name or not phone:
            return _resp(400, {"ok": False, "error": "name and phone required"}, origin)

        # Validate phone format → 400, not 500
        try:
            phone_e164 = normalize_phone(phone)
        except ValueError as e:
            return _resp(400, {"ok": False, "error": str(e)}, origin)

        # Write member — if DDB is unavailable this correctly 500s
        member = upsert_member(
            phone=phone_e164,
            name=name,
            email=email,
            source=source,
            sms_opt_in=sms_opt_in,
        )

        logger.info(
            "access_request: member saved phone=...%s status=%s smsOptIn=%s source=%s",
            phone_e164[-4:],
            member.get("status", "PENDING"),
            bool(member.get("smsOptIn", False)),
            source,
        )

        return _resp(200, {"ok": True}, origin)

    except Exception:
        logger.exception("access_request: unhandled error")
        return _resp(500, {"ok": False, "error": "internal error"}, None)
