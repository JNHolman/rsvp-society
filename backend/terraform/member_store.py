import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr

_DDB = boto3.resource("dynamodb")


def _table():
    name = os.getenv("MEMBERS_TABLE_NAME")
    if not name:
        raise RuntimeError("MEMBERS_TABLE_NAME env var is not set")
    return _DDB.Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_phone(raw: str) -> str:
    """
    Normalize to E.164.
    Supported:
      - +15551234567
      - 5551234567  (assumes US +1)
      - 15551234567 (assumes US +1)
    """
    if not raw:
        raise ValueError("phone is required")

    s = raw.strip()

    # If user already provided +, keep + and strip other non-digits
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
        if not (10 <= len(digits) <= 15):
            raise ValueError("phone must be valid E.164 length (10-15 digits)")
        return f"+{digits}"

    digits = re.sub(r"\D", "", s)

    # US assumptions
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"

    # Fallback: treat as country+number without '+'
    if 10 <= len(digits) <= 15:
        return f"+{digits}"

    raise ValueError("phone must be valid E.164")


def upsert_member(*, phone: str, name: str, email: Optional[str] = None, instagram: Optional[str] = None, source: str = "web", sms_opt_in: bool = False) -> Dict[str, Any]:
    """
    Upserts a member record.
    - Always updates: name, source, lastSeenAt
    - Sets createdAt only once
    - Sets status only once (defaults to PENDING)
    - Sets email only when provided
    """
    t = _table()
    now = _now_iso()

    expr_names = {
        "#n": "name",     # reserved
        "#s": "status",   # safe alias anyway
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

    if email:
        expr_vals[":e"] = email[:200]
        set_parts.append("email = :e")
    if instagram:
        expr_vals[":ig"] = instagram.lstrip("@")[:60]
        set_parts.append("instagram = :ig")

    update_expr = "SET " + ", ".join(set_parts)

    t.update_item(
        Key={"phone": phone},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_vals,
    )

    # Return the current item (simple + reliable)
    resp = t.get_item(Key={"phone": phone})
    return resp.get("Item", {"phone": phone})


def set_status(phone: str, status: str) -> None:
    """
    Sets status to PENDING|APPROVED|DENIED.
    Accepts raw or E.164; stores E.164.
    """
    st = (status or "").upper().strip()
    if st not in ("PENDING", "APPROVED", "DENIED"):
        raise ValueError("status must be PENDING, APPROVED, or DENIED")

    phone_e164 = normalize_phone(phone)
    t = _table()

    t.update_item(
        Key={"phone": phone_e164},
        UpdateExpression="SET #s = :s, lastSeenAt = :ls",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": st, ":ls": _now_iso()},
    )


def list_members_by_status(status: str = "PENDING", limit: int = 200) -> List[Dict[str, Any]]:
    """
    No GSI yet -> scan + filter.
    Keep limit small; add GSI later when volume grows.
    """
    st = (status or "PENDING").upper().strip()
    t = _table()

    resp = t.scan(
        FilterExpression=Attr("status").eq(st),
        Limit=max(1, min(int(limit), 500)),
    )

    items = resp.get("Items", [])
    # Sort newest first if createdAt exists
    items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    return items



def set_gender(phone: str, gender: str) -> None:
    """Set gender: M, F, or O."""
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
    """
    Manually override reliability tier (1, 2, or 3).
    Pass tier=0 to clear the override and revert to auto-calculation.
    """
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


def record_attendance(phone: str, attended: bool) -> None:
    """
    Called after an event to mark whether member showed up.
    Increments attendedCount if attended=True.
    Always increments confirmedCount (assumes they confirmed).
    """
    phone_e164 = normalize_phone(phone)
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
    else:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression=(
                "SET confirmedCount = if_not_exists(confirmedCount, :zero) + :one, "
                "lastSeenAt = :ls"
            ),
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":ls": _now_iso()},
        )


def get_member(phone: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a single member by phone number. Returns None if not found.
    """
    phone_e164 = normalize_phone(phone)
    t = _table()
    resp = t.get_item(Key={"phone": phone_e164})
    return resp.get("Item")

def delete_member(phone: str) -> None:
    """Permanently delete a member from the database."""
    phone_e164 = normalize_phone(phone)
    _table().delete_item(Key={"phone": phone_e164})

def bulk_import_members(rows: list) -> dict:
    """
    Bulk import members from CSV (Posh or Eventbrite).
    All imported members are set to APPROVED with smsOptIn=True.
    Skips rows where phone already exists in the table.
    Returns { imported: int, skipped: int }
    """
    t = _table()
    now = _now_iso()
    imported = 0
    skipped = 0

    for row in rows:
        try:
            phone = normalize_phone(row.get("phone", ""))
        except ValueError:
            skipped += 1
            continue

        # Check if member already exists
        existing = t.get_item(Key={"phone": phone}).get("Item")
        if existing:
            skipped += 1
            continue

        item = {
            "phone": phone,
            "name": (row.get("name") or "")[:120],
            "status": "APPROVED",
            "source": row.get("source", "import"),
            "smsOptIn": True,
            "createdAt": now,
            "lastSeenAt": now,
        }
        if row.get("email"):
            item["email"] = row["email"][:200]
        if row.get("instagram"):
            item["instagram"] = row["instagram"].lstrip("@")[:60]

        t.put_item(Item=item)
        imported += 1

    return {"imported": imported, "skipped": skipped}

