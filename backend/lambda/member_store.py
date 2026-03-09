import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key as DKey

logger = logging.getLogger()
def _ddb():
    return boto3.resource("dynamodb")


def _table():
    name = os.getenv("MEMBERS_TABLE_NAME")
    if not name:
        raise RuntimeError("MEMBERS_TABLE_NAME env var is not set")
    return _ddb().Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ttl_90_days() -> int:
    return int((datetime.now(timezone.utc).timestamp()) + (90 * 24 * 60 * 60))


def split_legacy_name(name: Optional[str], last_name: Optional[str] = None) -> tuple[str, str]:
    first = (name or '').strip()
    last = (last_name or '').strip()
    if last or not first:
        return first, last
    parts = [part for part in first.split() if part]
    if len(parts) < 2:
        return first, last
    return parts[0], ' '.join(parts[1:])


def normalize_member_record(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not item:
        return item
    normalized = dict(item)
    first, last = split_legacy_name(normalized.get('name'), normalized.get('lastName'))
    normalized['name'] = first
    if last:
        normalized['lastName'] = last
    return normalized


def normalize_phone(raw: str) -> str:
    if not raw:
        raise ValueError("phone is required")
    s = raw.strip()
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
        if not (10 <= len(digits) <= 15):
            raise ValueError("phone must be valid E.164 length (10–15 digits)")
        return f"+{digits}"
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if 10 <= len(digits) <= 15:
        return f"+{digits}"
    raise ValueError("phone must be valid E.164")


def upsert_member(
    *,
    phone: str,
    name: str,
    last_name: Optional[str] = None,
    email: Optional[str] = None,
    source: str = "web",
    sms_opt_in: bool = False,
    tags: Optional[str] = None,
) -> Dict[str, Any]:
    t = _table()
    now = _now_iso()

    expr_names = {
        "#n": "name",
        "#s": "status",
        "#src": "source",
    }

    expr_vals: Dict[str, Any] = {
        ":n":       name[:120],
        ":src":     (source or "web")[:40],
        ":ls":      now,
        ":ca":      now,
        ":pending": "PENDING",
        ":soi":     sms_opt_in,
    }

    set_parts = [
        "#n = :n",
        "#src = :src",
        "lastSeenAt = :ls",
        "createdAt = if_not_exists(createdAt, :ca)",
        # Only set PENDING for NEW members. Existing members keep their
        # current status so an APPROVED member can't be downgraded by a
        # re-submission. access_request.py already gates host notification
        # on status == PENDING so only genuinely new members trigger it.
        "#s = if_not_exists(#s, :pending)",
        "smsOptIn = :soi",
    ]

    # Record WHEN sms consent was given — TCPA compliance.
    # Only stamp on the first opt-in; never overwrite an existing timestamp.
    if sms_opt_in:
        expr_vals[":oiat"] = now
        set_parts.append("smsOptInAt = if_not_exists(smsOptInAt, :oiat)")

    if last_name:
        expr_vals[":ln"] = last_name[:120]
        set_parts.append("lastName = :ln")

    if email:
        expr_vals[":e"] = email[:200]
        set_parts.append("email = :e")

    if tags:
        expr_vals[":tg"] = tags[:200]
        set_parts.append("tags = :tg")

    update_expr = "SET " + ", ".join(set_parts)

    t.update_item(
        Key={"phone": phone},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_vals,
    )

    resp = t.get_item(Key={"phone": phone})
    return resp.get("Item", {"phone": phone})


def set_status(phone: str, status: str) -> None:
    st = (status or "").upper().strip()
    if st not in ("PENDING", "APPROVED", "DENIED"):
        raise ValueError("status must be PENDING, APPROVED, or DENIED")
    phone_e164 = normalize_phone(phone)
    _table().update_item(
        Key={"phone": phone_e164},
        UpdateExpression="SET #s = :s, lastSeenAt = :ls",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": st, ":ls": _now_iso()},
    )


def list_members_by_status(status: str = "PENDING", limit: int = 200) -> List[Dict[str, Any]]:
    """
    Query the status-index GSI — O(matching members) not O(all members).
    The GSI has projection_type = ALL so all fields are available without
    a second GetItem per row.
    """
    st = (status or "PENDING").upper().strip()
    t = _table()

    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "IndexName": "status-index",
        "KeyConditionExpression": DKey("status").eq(st),
    }

    while True:
        resp = t.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    items = [normalize_member_record(item) for item in items]
    items.sort(key=lambda x: (
        (x.get("lastName") or x.get("name") or "").lower(),
        (x.get("name") or "").lower(),
    ))
    return items


def search_members(query: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Search members by first name, last name, or phone.
    Used by the check-in page.
    Note: full-table scan — acceptable at current scale, revisit at 5k+ members.
    """
    q = (query or "").strip().lower()
    if not q:
        return []

    t = _table()
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}

    while True:
        resp = t.scan(**kwargs)
        for raw_item in resp.get("Items", []):
            item = normalize_member_record(raw_item) or {}
            first = (item.get("name") or "").lower()
            last  = (item.get("lastName") or "").lower()
            phone = (item.get("phone") or "").lower()
            full  = f"{first} {last}".strip()
            if q in first or q in last or q in full or q in phone:
                items.append(item)
                if len(items) >= limit:
                    return items
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    items.sort(key=lambda x: (
        (x.get("lastName") or x.get("name") or "").lower(),
        (x.get("name") or "").lower(),
    ))
    return items


def set_gender(phone: str, gender: str) -> None:
    g = (gender or "").upper().strip()
    if g not in ("M", "F", "O"):
        raise ValueError("gender must be M, F, or O")
    phone_e164 = normalize_phone(phone)
    _table().update_item(
        Key={"phone": phone_e164},
        UpdateExpression="SET gender = :g, lastSeenAt = :ls",
        ExpressionAttributeValues={":g": g, ":ls": _now_iso()},
    )


def set_tier_override(phone: str, tier: int) -> None:
    if tier not in (0, 1, 2, 3):
        raise ValueError("tier must be 0 (clear), 1, 2, or 3")
    phone_e164 = normalize_phone(phone)
    t = _table()
    if tier == 0:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression="REMOVE tierOverride SET lastSeenAt = :ls",
            ExpressionAttributeValues={":ls": _now_iso()},
        )
    else:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression="SET tierOverride = :t, lastSeenAt = :ls",
            ExpressionAttributeValues={":t": tier, ":ls": _now_iso()},
        )


def _checkins_table():
    name = os.getenv("CHECKINS_TABLE_NAME")
    if not name:
        raise RuntimeError(
            "CHECKINS_TABLE_NAME env var is not set. "
            "Deploy checkins.tf and add it to the Lambda environment."
        )
    return boto3.resource("dynamodb").Table(name)


def _invites_table():
    name = os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites")
    return boto3.resource("dynamodb").Table(name)


def record_attendance(phone: str, attended: bool, event_id: str = "current") -> bool:
    """
    Record attendance for a member at a specific event.

    Uses rsvp-checkins as an idempotent write guard keyed on (eventId, phone):
    - First check-in → writes member counters, returns True
    - Repeat tap     → ConditionalCheckFailedException, no counter change, returns False
    """
    from botocore.exceptions import ClientError

    phone_e164 = normalize_phone(phone)

    if attended:
        # ── Physical check-in ─────────────────────────────────────────────────
        # Write an idempotent checkin row. ConditionalCheckFailedException means
        # this person was already tapped in — return False so the caller knows
        # not to double-increment counters or show a duplicate toast.
        ct = _checkins_table()
        try:
            ct.put_item(
                Item={
                    "eventId":     event_id,
                    "phone":       phone_e164,
                    "checkedInAt": _now_iso(),
                    "ttl":         _ttl_90_days(),
                },
                ConditionExpression="attribute_not_exists(phone)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

        t = _table()
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression=(
                "SET attendedCount = if_not_exists(attendedCount, :zero) + :one, "
                "lastSeenAt = :ls"
            ),
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":ls": _now_iso()},
        )
        try:
            _invites_table().update_item(
                Key={"eventId": event_id, "phone": phone_e164},
                UpdateExpression="SET attendedAt = :now",
                ExpressionAttributeValues={":now": _now_iso()},
            )
        except Exception:
            logger.exception("record_attendance: invite attendedAt write failed phone=...%s", phone_e164[-4:])

    else:
        # ── No Show ───────────────────────────────────────────────────────────
        # Do NOT write a checkin row — that would consume the idempotent guard
        # and prevent a real check-in if the person shows up later.
        # Do NOT increment confirmedCount — they were already counted when they
        # texted YES. Just stamp the invite record so analytics can track ghosts.
        try:
            _invites_table().update_item(
                Key={"eventId": event_id, "phone": phone_e164},
                UpdateExpression="SET noShowAt = :now",
                ExpressionAttributeValues={":now": _now_iso()},
            )
        except Exception:
            logger.exception("record_attendance: no-show stamp failed phone=...%s", phone_e164[-4:])

    return True


def claim_welcome_send(phone: str) -> bool:
    """
    Acquire a best-effort one-writer claim for sending the welcome SMS.
    Returns True only for the first in-flight sender.

    welcomeSendingAt older than 5 minutes is treated as a stuck/orphaned claim
    and overwritten — a real in-flight send completes in under 15 seconds.
    """
    from botocore.exceptions import ClientError
    from datetime import timedelta

    phone_e164 = normalize_phone(phone)
    t = _table()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    expiry = (now_dt - timedelta(minutes=5)).isoformat(timespec="seconds")

    # First attempt: claim only if neither sentinel exists
    try:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression="SET welcomeSendingAt = :now",
            ExpressionAttributeValues={":now": now},
            ConditionExpression="attribute_not_exists(welcomeSentAt) AND attribute_not_exists(welcomeSendingAt)",
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise

    # welcomeSentAt or welcomeSendingAt exists.
    # If welcomeSentAt is present, welcome was already sent — do not resend.
    # If only welcomeSendingAt is present and it is stale (>5 min), overwrite it.
    try:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression="SET welcomeSendingAt = :now",
            ExpressionAttributeValues={":now": now, ":expiry": expiry},
            ConditionExpression=(
                "attribute_not_exists(welcomeSentAt) AND welcomeSendingAt <= :expiry"
            ),
        )
        logger.warning(
            "claim_welcome_send: overwrote stale claim phone=...%s", phone_e164[-4:]
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def clear_welcome_send_claim(phone: str) -> None:
    phone_e164 = normalize_phone(phone)
    _table().update_item(
        Key={"phone": phone_e164},
        UpdateExpression="REMOVE welcomeSendingAt",
    )


def set_sms_opt_in(phone: str, value: bool) -> None:
    """Explicitly set smsOptIn on a member record."""
    phone_e164 = normalize_phone(phone)
    _table().update_item(
        Key={"phone": phone_e164},
        UpdateExpression="SET smsOptIn = :v",
        ExpressionAttributeValues={":v": value},
    )


def write_welcome_error(phone: str, error: str) -> None:
    """Write the last welcome SMS error to the member record for debugging.

    Readable directly in DynamoDB console without CloudWatch access.
    Field: lastWelcomeError  — truncated error string + timestamp.
    """
    try:
        phone_e164 = normalize_phone(phone)
        _table().update_item(
            Key={"phone": phone_e164},
            UpdateExpression="SET lastWelcomeError = :e, lastWelcomeErrorAt = :t",
            ExpressionAttributeValues={
                ":e": error[:3000],
                ":t": _now_iso(),
            },
        )
    except Exception:
        # Never let debug logging block the caller
        logger.exception("write_welcome_error: failed to write error for phone=...%s", phone[-4:])


def mark_welcome_sent(phone: str) -> bool:
    """
    Mark that the welcome SMS has been sent to this member.
    Returns True if the field was set, False if it already existed.
    """
    from botocore.exceptions import ClientError

    phone_e164 = normalize_phone(phone)
    t = _table()
    now = _now_iso()

    try:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression="SET welcomeSentAt = :now REMOVE welcomeSendingAt",
            ExpressionAttributeValues={":now": now},
            ConditionExpression="attribute_not_exists(welcomeSentAt)",
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def get_member(phone: str) -> Optional[Dict[str, Any]]:
    phone_e164 = normalize_phone(phone)
    t = _table()
    resp = t.get_item(Key={"phone": phone_e164})
    return normalize_member_record(resp.get("Item"))
