"""Host Y/N access-request approval command handling."""
from __future__ import annotations

import json


def handle_host_approval(from_phone: str, text: str, sms_enabled: bool, host_phones: list[str], *, deps: dict):
    parts = text.strip().split()
    is_yn_cmd = len(parts) >= 1 and parts[0].upper() in ("Y", "N")
    if not is_yn_cmd:
        return None

    action = parts[0].upper()
    code = parts[1].strip() if len(parts) >= 2 else None
    logger = deps["logger"]
    send_sms = deps["send_sms"]
    try:
        pending = deps["get_pending_approval"](from_phone, approval_code=code)
        if not pending:
            if sms_enabled:
                send_sms(from_phone, f"Code {code} not found. Check pending requests." if code else "No pending requests.")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        stored_code = (pending.get("approvalCode") or "").strip()
        if stored_code and not code:
            target_name = pending.get("memberName", "unknown")
            if sms_enabled:
                send_sms(from_phone, f"Include the code. Reply Y {stored_code} to approve or N {stored_code} to deny {target_name}.")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        if pending.get("requestKind"):
            from host_requests import decide_request
            reply = decide_request(pending, action == "Y", deps=deps["request_deps"]())
            if sms_enabled:
                send_sms(from_phone, reply)
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        target_phone = pending["memberPhone"]
        target_name = pending["memberName"]
        target_member = deps["get_member"](target_phone) or {}
        if (target_member.get("status") or "").upper() != "PENDING":
            for hp in host_phones:
                try:
                    deps["clear_pending_approval"](hp, target_phone)
                except Exception:
                    logger.exception("sms_handler: stale approval cleanup failed host=...%s", hp[-4:])
            if sms_enabled:
                send_sms(from_phone, f"{target_name} is already {(target_member.get('status') or 'processed').lower()}.")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        new_status = "APPROVED" if action == "Y" else "DENIED"
        deps["set_status"](target_phone, new_status, expected_status="PENDING")
        deps["clear_pending_approval"](from_phone, target_phone)

        for other_hp in host_phones:
            if other_hp == from_phone:
                continue
            try:
                deps["clear_pending_approval"](other_hp, target_phone)
                logger.info("dual-host cleanup: cleared pending_approval:%s:%s", other_hp[-4:], target_phone[-4:])
            except Exception:
                logger.exception("sms_handler: failed to clear other host pending for %s", other_hp[-4:])

        if sms_enabled:
            send_sms(from_phone, f"{target_name} has been {new_status.lower()}.")

        if new_status == "APPROVED":
            try:
                target_member = deps["get_member"](target_phone)
                if target_member and not target_member.get("welcomeSentAt"):
                    claim = deps["claim_welcome_send"]
                    clear = deps["clear_welcome_send_claim"]
                    if claim(target_phone):
                        try:
                            sent = deps["maybe_send_welcome"]({**target_member, "status": "APPROVED"})
                            if sent:
                                deps["mark_welcome_sent"](target_phone)
                            else:
                                clear(target_phone)
                        except Exception:
                            clear(target_phone)
                            raise
            except Exception:
                logger.exception("sms_handler: welcome SMS failed after host approval phone=...%s", target_phone[-4:])
    except Exception:
        logger.exception("sms_handler: Y/N approval failed from=%s", from_phone[-4:])
        # Do not acknowledge an approval command that failed before its status
        # update. The inbound receipt wrapper will leave it retryable.
        return {"statusCode": 503, "body": json.dumps({"ok": False, "retry": True})}
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
