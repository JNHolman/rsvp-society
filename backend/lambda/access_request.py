import base64
import json
import logging
import os
import boto3
from member_store import upsert_member, normalize_phone
from sms_adapter import send_sms
from admin_shared import coerce_bool as _coerce_bool

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
    # Fail closed: never fall back to wildcard — that opens the endpoint to any domain.
    allow_origin = origin if origin in origins else (origins[0] if origins else "")
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

        first_name = (data.get("firstName") or "").strip()
        last_name = (data.get("lastName") or "").strip()
        legacy_name = (data.get("name") or "").strip()
        if not first_name and legacy_name:
            parts = legacy_name.split()
            first_name = parts[0].strip() if parts else ""
            last_name = " ".join(parts[1:]).strip() if len(parts) > 1 else last_name
        name = first_name or legacy_name

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
            last_name=last_name or None,
            email=email,
            source=source,
            sms_opt_in=sms_opt_in,
        )

        logger.info(
            "access_request: member saved phone=...%s status=%s smsOptIn=%s source=%s",
            phone_e164[-4:],
            member.get("status", "PENDING"),
            _coerce_bool(member.get("smsOptIn", False)),
            source,
        )

        # Notify hosts of new pending member and store pending approval record for Y/N reply
        try:
            sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"
            host_phones = [p for p in [os.getenv("HOST_PHONE_1", ""), os.getenv("HOST_PHONE_2", "")] if p]
            if sms_enabled and host_phones and (member.get("status") or "PENDING") == "PENDING":
                display_name = f"{name} {last_name}".strip()
                events_table = boto3.resource("dynamodb").Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))
                from datetime import datetime, timezone
                for hp in host_phones:
                    try:
                        # Store pending approval so Y/N can resolve it
                        events_table.put_item(Item={
                            "eventId": f"pending_approval:{hp}",
                            "memberPhone": phone_e164,
                            "memberName": display_name,
                            "storedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        })
                        send_sms(hp, f"New request: {display_name}\nY to approve, N to deny")
                    except Exception:
                        logger.exception("access_request: host notification failed to %s", hp[-4:])
        except Exception:
            logger.exception("access_request: host notification block failed")

        # Welcome SMS is sent by sms_handler.py after the host approves via Y/N reply.
        # We intentionally do not auto-send here — upsert_member always resets status
        # to PENDING on submit, so any member going through this endpoint needs host
        # approval before Jade fires, regardless of prior history.


        return _resp(200, {"ok": True}, origin)

    except Exception:
        logger.exception("access_request: unhandled error")
        return _resp(500, {"ok": False, "error": "internal error"}, None)
