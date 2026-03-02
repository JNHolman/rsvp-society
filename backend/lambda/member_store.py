import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key as DKey

logger = logging.getLogger()
_DDB = boto3.resource("dynamodb")


def _table():
    name = os.getenv("MEMBERS_TABLE_NAME")
    if not name:
        raise RuntimeError("MEMBERS_TABLE_NAME env var is not set")
    return _DDB.Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_phone(raw: str) -> str:
    if not raw:
        raise ValueError("phone is required")
    s = raw.strip()
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
        if not (10 <= len(digits) <= 15):
            raise ValueError("phone must be valid E.164 length (10-15 digits)")
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
        ":n": name[:120],
        ":src": (source or "web")[:40],
        ":ls": now,
        ":ca": now,
        ":pending": "PENDING",
        ":soi": sms_opt_in,
    }

    set_parts = [
        "#n = :n",
        "#src = :src",
        "lastSeenAt = :ls",
        "createdAt = if_not_exists(createdAt, :ca)",
        "#s = if_not_exists(#s, :pending)",
        "smsOptIn = :soi",
    ]

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

    items.sort(key=lambda x: (
        (x.get("lastName") or x.get("name") or "").lower(),
        (x.get("name") or "").lower()
    ))
    return items


def search_members(query: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Search members by first name, last name, or phone.
    Used by the check-in page.
    """
    q = (query or "").strip().lower()
    if not q:
        return []

    t = _table()
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}

    while True:
        resp = t.scan(**kwargs)
        for item in resp.get("Items", []):
            first = (item.get("name") or "").lower()
            last = (item.get("lastName") or "").lower()
            phone = (item.get("phone") or "").lower()
            full = f"{first} {last}".strip()
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
        (x.get("name") or "").lower()
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
        # Hard fail — if this env var is missing the conditional write guard
        # cannot function and we would silently allow double-counting.
        # Prefer a loud Lambda startup error over silent data corruption.
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

    Raises RuntimeError if CHECKINS_TABLE_NAME is not configured so the
    misconfiguration is caught at call time, not silently swallowed.
    """
    from botocore.exceptions import ClientError

    phone_e164 = normalize_phone(phone)
    ct = _checkins_table()  # raises if env var missing

    try:
        ct.put_item(
            Item={
                "eventId":     event_id,
                "phone":       phone_e164,
                "checkedInAt": _now_iso(),
            },
            ConditionExpression="attribute_not_exists(phone)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False  # already checked in — skip counter update
        raise

    # New check-in — update member record counters
    t = _table()
    if attended:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression=(
                "SET attendedCount = if_not_exists(attendedCount, :zero) + :one, "
                "confirmedCount = if_not_exists(confirmedCount, :zero) + :one, "
                "lastSeenAt = :ls"
            ),
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":ls": _now_iso()},
        )
        # Write attendedAt back to the invite record so post-event analytics
        # can compare confirmed vs actually attended without scanning member counters.
        try:
            _invites_table().update_item(
                Key={"eventId": event_id, "phone": phone_e164},
                UpdateExpression="SET attendedAt = :now",
                ExpressionAttributeValues={":now": _now_iso()},
            )
        except Exception:
            logger.exception("attend invite update failed phone=...%s", phone_e164[-4:])
            # Non-fatal — member counter already written
    else:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression=(
                "SET confirmedCount = if_not_exists(confirmedCount, :zero) + :one, "
                "lastSeenAt = :ls"
            ),
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":ls": _now_iso()},
        )
    return True


def get_member(phone: str) -> Optional[Dict[str, Any]]:
    phone_e164 = normalize_phone(phone)
    t = _table()
    resp = t.get_item(Key={"phone": phone_e164})
    return resp.get("Item")


def delete_member(phone: str) -> None:
    phone = normalize_phone((phone or "").strip())
    if not phone:
        raise ValueError("phone required")
    _table().delete_item(Key={"phone": phone})
