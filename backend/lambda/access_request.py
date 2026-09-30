import base64
import json
import logging
import os
import boto3
from member_store import upsert_member, normalize_phone, mark_welcome_sent, get_member
from sms_adapter import maybe_send_welcome, send_sms, get_host_phones
from admin_shared import coerce_bool as _coerce_bool
from location_resolver import InvalidZipError, ZipLookupUnavailable, resolve_us_zip

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
        zip_code = (data.get("zipCode") or "").strip()

        if not name or not phone:
            return _resp(400, {"ok": False, "error": "name and phone required"}, origin)

        # Public access form requires SMS opt-in. Legacy/imported members enter
        # through the admin import path, not this public endpoint. STOP opt-out is
        # still honored by the SMS handler.
        if not sms_opt_in:
            return _resp(400, {"ok": False, "error": "SMS opt-in is required to request access"}, origin)

        # Validate phone format before any external ZIP lookup.
        try:
            phone_e164 = normalize_phone(phone)
        except ValueError as e:
            return _resp(400, {"ok": False, "error": str(e)}, origin)

        if not zip_code:
            return _resp(400, {"ok": False, "error": "ZIP code is required"}, origin)
        try:
            location = resolve_us_zip(zip_code)
        except InvalidZipError as exc:
            return _resp(400, {"ok": False, "error": str(exc)}, origin)
        except ZipLookupUnavailable:
            logger.exception("access_request: ZIP lookup unavailable zip=%s", zip_code)
            return _resp(503, {"ok": False, "error": "ZIP lookup is temporarily unavailable"}, origin)

        # Remember whether this exact member was already awaiting review before
        # the upsert. Re-submitting an active pending request must not create new
        # approval codes or paid host SMS messages.
        existing_before = get_member(phone_e164) or {}
        was_pending_before = (existing_before.get("status") or "").upper() == "PENDING"

        # Phone is the member identity key. Do not full-table scan by name on
        # every public signup: same-name people are legitimate, and the old check
        # only logged a warning without changing the result.

        # Write member — if DDB is unavailable this correctly 500s
        member = upsert_member(
            phone=phone_e164,
            name=name,
            last_name=last_name or None,
            email=email,
            source=source,
            sms_opt_in=sms_opt_in,
            zip_code=location["zipCode"],
            city=location["city"],
            state=location["state"],
            latitude=location["latitude"],
            longitude=location["longitude"],
        )

        logger.info(
            "access_request: member saved phone=...%s status=%s smsOptIn=%s source=%s",
            phone_e164[-4:],
            member.get("status", "PENDING"),
            _coerce_bool(member.get("smsOptIn", False)),
            source,
        )

        # Notify hosts of new pending member.
        # Writes to rsvp-pending-approvals table (pk=hostPhone, sk=memberPhone).
        # sms_handler reads from the same table via Query on hostPhone pk.
        try:
            sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"
            host_phones = get_host_phones()
            # IMPORTANT: the pending-approval record is the source of truth for the
            # host approve/deny-by-code flow. It must be written whenever a member is
            # newly PENDING and host phones are configured — even if SMS sending is
            # turned off for testing. Only the outbound text is gated by SMS_ENABLED,
            # so toggling SMS off no longer leaves rsvp-pending-approvals empty.
            if host_phones and (member.get("status") or "PENDING") == "PENDING" and not was_pending_before:
                display_name = f"{name} {last_name}".strip()
                # Use dedicated pending-approvals table — NOT rsvp-events
                approvals_table = boto3.resource("dynamodb").Table(
                    os.getenv("PENDING_APPROVALS_TABLE_NAME", "rsvp-pending-approvals")
                )
                from datetime import datetime, timezone
                import secrets as _secrets
                now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
                # TTL: expire approval records after 7 days
                import time as _time
                expires_at = int(_time.time()) + (7 * 24 * 60 * 60)
                for hp in host_phones:
                    try:
                        approval_code = "".join([str(_secrets.randbelow(10)) for _ in range(6)])
                        approvals_table.put_item(Item={
                            "hostPhone":    hp,
                            "memberPhone":  phone_e164,
                            "memberName":   display_name,
                            "approvalCode": approval_code,
                            "status":       "PENDING",
                            "storedAt":     now_iso,
                            "expiresAt":    expires_at,
                        })
                        if sms_enabled:
                            send_sms(hp, f"New request: {display_name} [{approval_code}]\nReply Y {approval_code} to approve, N {approval_code} to deny")
                    except Exception:
                        logger.exception("access_request: host notification failed to %s", hp[-4:])
        except Exception:
            logger.exception("access_request: host notification block failed")

        # If this member is already APPROVED (e.g., legacy record or auto-approve flow),
        # send the Jade welcome once (best-effort) and mark welcomeSentAt.
        try:
            if (member.get("status") or "").upper() == "APPROVED" and not member.get("welcomeSentAt"):
                sent = maybe_send_welcome({**member, "status": "APPROVED"})
                if sent:
                    mark_welcome_sent(phone_e164)
        except Exception:
            logger.exception("access_request: welcome SMS failed phone=...%s", phone_e164[-4:])


        return _resp(200, {"ok": True}, origin)

    except Exception:
        logger.exception("access_request: unhandled error")
        return _resp(500, {"ok": False, "error": "internal error"}, None)
