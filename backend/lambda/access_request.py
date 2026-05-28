import base64
import json
import logging
import os
import boto3
from member_store import upsert_member, normalize_phone, mark_welcome_sent
from sms_adapter import maybe_send_welcome, send_sms
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

        # Duplicate name guard — same first+last submitted within 72 hours from a
        # different phone number. Silent drop with generic 200 so the submitter gets
        # no signal that anything was flagged. Paginated scan with a safety cap.
        try:
            from datetime import datetime, timezone, timedelta
            members_table = boto3.resource("dynamodb").Table(os.getenv("MEMBERS_TABLE_NAME", "rsvp-members"))
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(timespec="seconds")
            filter_expr = (
                boto3.dynamodb.conditions.Attr("name").eq(first_name) &
                boto3.dynamodb.conditions.Attr("lastName").eq(last_name or "") &
                boto3.dynamodb.conditions.Attr("submittedAt").gte(cutoff) &
                boto3.dynamodb.conditions.Attr("phone").ne(phone_e164)
            )
            found_dup = False
            scan_kwargs = {"FilterExpression": filter_expr, "ProjectionExpression": "phone", "Limit": 500}
            while not found_dup:
                scan_resp = members_table.scan(**scan_kwargs)
                if scan_resp.get("Items"):
                    found_dup = True
                    break
                last_key = scan_resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key

            if found_dup:
                logger.warning(
                    "access_request: duplicate name within 72h name=%s %s new_phone=...%s",
                    first_name, last_name, phone_e164[-4:],
                )
                return _resp(200, {"ok": True}, origin)
        except Exception:
            logger.exception("access_request: duplicate name check failed — proceeding")

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

        # Notify hosts of new pending member.
        # Writes to rsvp-pending-approvals table (pk=hostPhone, sk=memberPhone).
        # sms_handler reads from the same table via Query on hostPhone pk.
        try:
            sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"
            host_phones = [p for p in [os.getenv("HOST_PHONE_1", ""), os.getenv("HOST_PHONE_2", "")] if p]
            if sms_enabled and host_phones and (member.get("status") or "PENDING") == "PENDING":
                display_name = f"{name} {last_name}".strip()
                # Use dedicated pending-approvals table — NOT rsvp-events
                approvals_table = boto3.resource("dynamodb").Table(
                    os.getenv("PENDING_APPROVALS_TABLE_NAME", "rsvp-pending-approvals")
                )
                from datetime import datetime, timezone
                import random as _rnd
                now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
                # TTL: expire approval records after 7 days
                import time as _time
                expires_at = int(_time.time()) + (7 * 24 * 60 * 60)
                for hp in host_phones:
                    try:
                        approval_code = "".join([str(_rnd.randint(0, 9)) for _ in range(6)])
                        approvals_table.put_item(Item={
                            "hostPhone":    hp,
                            "memberPhone":  phone_e164,
                            "memberName":   display_name,
                            "approvalCode": approval_code,
                            "status":       "PENDING",
                            "storedAt":     now_iso,
                            "expiresAt":    expires_at,
                        })
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
