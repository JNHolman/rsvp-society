import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key as DKey
from boto3.dynamodb.types import TypeSerializer

logger = logging.getLogger()

# ── Member/Invite State Machine ──────────────────────────────────────────────
# Formal definition of every status: what causes it, what it blocks,
# what analytics it feeds, whether Jade can engage the member.
# This is the single source of truth for state behavior across the system.

INVITE_STATUS_RULES = {
    "INVITED": {
        "description":        "Invite SMS was successfully delivered",
        "entered_by":         "invite_handler after SMS confirmed sent",
        "counts_as_invited":  True,
        "blocks_future_wave": True,   # already got an invite — skip
        "jade_can_engage":    True,
        "can_receive_logistics": False,  # venue gated by revealVenue
        "can_check_in":       False,
        "counts_in_analytics": True,
    },
    "CONFIRMED": {
        "description":        "Member replied YES and confirmed attendance",
        "entered_by":         "sms_handler on YES/CONFIRM keyword",
        "counts_as_invited":  True,
        "blocks_future_wave": True,
        "jade_can_engage":    True,
        "can_receive_logistics": True,  # full venue/address unlocked
        "can_check_in":       True,
        "counts_in_analytics": True,
    },
    "DECLINED": {
        "description":        "Member replied NO and declined",
        "entered_by":         "sms_handler on NO/DECLINE keyword",
        "counts_as_invited":  True,
        "blocks_future_wave": True,   # respected their decline
        "jade_can_engage":    False,
        "can_receive_logistics": False,
        "can_check_in":       False,
        "counts_in_analytics": True,
    },
    "ATTENDED": {
        "description":        "Member checked in at the door",
        "entered_by":         "member_store.record_attendance(attended=True)",
        "counts_as_invited":  True,
        "blocks_future_wave": True,
        "jade_can_engage":    True,
        "can_receive_logistics": True,
        "can_check_in":       False,  # already checked in
        "counts_in_analytics": True,
        "feeds_invite_score": True,   # +attendedCount, improves tier
    },
    "NO_SHOW": {
        "description":        "Confirmed but did not check in by event end",
        "entered_by":         "member_store.record_attendance(attended=False)",
        "counts_as_invited":  True,
        "blocks_future_wave": True,
        "jade_can_engage":    False,
        "can_receive_logistics": False,
        "can_check_in":       False,
        "counts_in_analytics": True,
        "feeds_invite_score": True,   # +noShowCount, degrades tier
    },
    "SKIPPED_CONSENT": {
        "description":        "Member has no SMS opt-in — invite row written but SMS not sent",
        "entered_by":         "invite_handler when smsOptIn=False",
        "counts_as_invited":  False,  # never reached — does NOT count
        "blocks_future_wave": False,  # eligible for retry if they opt in
        "jade_can_engage":    False,
        "can_receive_logistics": False,
        "can_check_in":       False,
        "counts_in_analytics": False,
    },
    "FAILED": {
        "description":        "SMS send failed after all retries",
        "entered_by":         "invite_handler after rate-limit retry exhaustion",
        "counts_as_invited":  False,  # never reached — does NOT count
        "blocks_future_wave": False,  # eligible for retry in next wave
        "jade_can_engage":    False,
        "can_receive_logistics": False,
        "can_check_in":       False,
        "counts_in_analytics": False,
    },
    "DELETED": {
        "description":        "Tombstoned — PII wiped, row kept for integrity",
        "entered_by":         "admin_member_routes on DELETE",
        "counts_as_invited":  False,
        "blocks_future_wave": False,
        "jade_can_engage":    False,
        "can_receive_logistics": False,
        "can_check_in":       False,
        "counts_in_analytics": False,
    },
}

MEMBER_STATUS_RULES = {
    "PENDING": {
        "description":        "Applied — waiting for host approval",
        "entered_by":         "access_request on form submission",
        "can_be_invited":     False,
        "jade_can_engage":    True,   # Jade handles inquiries
        "can_receive_event_info": False,
    },
    "APPROVED": {
        "description":        "Approved by host — eligible for invite waves",
        "entered_by":         "admin action or host Y code",
        "can_be_invited":     True,
        "jade_can_engage":    True,
        "can_receive_event_info": False,  # no logistics until invited/confirmed
    },
    "DENIED": {
        "description":        "Denied by host — not eligible",
        "entered_by":         "admin action or host N code",
        "can_be_invited":     False,
        "jade_can_engage":    False,
        "can_receive_event_info": False,
    },
}

# States excluded from analytics invite counts
ANALYTICS_EXCLUDE_STATUSES = frozenset({"SKIPPED_CONSENT", "FAILED", "DELETED"})

# States that block a member from future invite waves
WAVE_BLOCK_STATUSES = frozenset({"INVITED", "CONFIRMED", "DECLINED", "ATTENDED", "NO_SHOW"})

# States where Jade should share full logistics
LOGISTICS_ELIGIBLE_STATUSES = frozenset({"CONFIRMED", "ATTENDED"})

# Family of statuses that represent a completed confirmation (used for analytics + capacity math)
CONFIRMED_FAMILY_STATUSES = frozenset({"CONFIRMED", "ATTENDED", "NO_SHOW"})

# Statuses eligible for check-in (ATTENDED transition)
# Strict RSVP Society: only CONFIRMED members can check in.
# INVITED (not yet confirmed) cannot check in — must confirm first.
# This matches the INVITE_STATUS_RULES["INVITED"]["can_check_in"] = False rule above.
CHECKIN_ELIGIBLE_STATUSES = frozenset({"CONFIRMED"})

# Only CONFIRMED can become NO_SHOW
NO_SHOW_ELIGIBLE_STATUSES = frozenset({"CONFIRMED"})

# Invite statuses that accept a confirmation (YES reply)
CONFIRMABLE_INVITE_STATUSES = frozenset({"INVITED"})

# Invite rows in these statuses should be overwritten on retry
# (never reached the member — eligible for the next wave)
RETRYABLE_INVITE_STATUSES = frozenset({"SKIPPED_CONSENT", "FAILED", "DELETED"})

# Statuses that mean a member is actively in the current wave
# Used by Jade context to determine what info to share
JADE_IN_WAVE_STATUSES = frozenset({"INVITED", "CONFIRMED", "ATTENDED"})

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
        "submittedAt = :ls",
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


# Structured attendance result codes
ATTENDANCE_OK               = "OK"
ATTENDANCE_ALREADY          = "ALREADY_CHECKED_IN"
ATTENDANCE_NOT_CONFIRMED    = "NOT_CONFIRMED"
ATTENDANCE_INVITE_NOT_FOUND = "INVITE_NOT_FOUND"
ATTENDANCE_INVALID_STATUS   = "INVALID_STATUS"
ATTENDANCE_DDB_ERROR        = "DDB_ERROR"


def _ddb_av(value):
    """Serialize a Python value to a DynamoDB AttributeValue for transactions."""
    return TypeSerializer().serialize(value)


def _ddb_key(**kwargs) -> Dict[str, Any]:
    return {k: _ddb_av(v) for k, v in kwargs.items()}


def _attendance_failure_reason(phone_e164: str, event_id: str, *, attended: bool) -> dict:
    """
    Diagnose a failed transactional attendance/no-show write.
    This keeps the admin response useful without trusting partial writes.
    """
    try:
        existing = _checkins_table().get_item(
            Key={"eventId": event_id, "phone": phone_e164}
        ).get("Item")
        if existing:
            return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in"}
    except Exception:
        logger.exception("record_attendance: checkin diagnostic failed phone=...%s", phone_e164[-4:])

    try:
        invite = _invites_table().get_item(
            Key={"eventId": event_id, "phone": phone_e164}
        ).get("Item")
    except Exception:
        logger.exception("record_attendance: invite diagnostic failed phone=...%s", phone_e164[-4:])
        return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Database error while checking invite status"}

    if not invite:
        return {"ok": False, "result": ATTENDANCE_INVITE_NOT_FOUND, "reason": "Invite not found"}

    status = (invite.get("status") or "").upper()
    if attended and status == "ATTENDED":
        return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in"}
    if not attended and status == "NO_SHOW":
        return {"ok": False, "result": ATTENDANCE_INVALID_STATUS, "reason": "Already marked no-show"}

    return {
        "ok": False,
        "result": ATTENDANCE_NOT_CONFIRMED,
        "reason": f"Invite status is {status or 'missing'}; CONFIRMED required",
    }



def _test_disable_transactions() -> bool:
    """Unit-test escape hatch: moto's TransactWriteItems condition parser is incomplete."""
    return (os.getenv("RSVP_TEST_DISABLE_DDB_TRANSACTIONS", "false") or "").lower() == "true"


def _record_attendance_non_transactional_for_tests(phone_e164: str, attended: bool, event_id: str, now: str) -> dict:
    """
    Strict non-transactional fallback used only in moto tests.
    Production path still uses DynamoDB TransactWriteItems.
    """
    from botocore.exceptions import ClientError

    if attended:
        try:
            existing = _checkins_table().get_item(Key={"eventId": event_id, "phone": phone_e164}).get("Item")
            if existing:
                return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in"}

            _invites_table().update_item(
                Key={"eventId": event_id, "phone": phone_e164},
                UpdateExpression="SET attendedAt = :now, #s = :attended",
                ConditionExpression="attribute_exists(phone) AND #s = :confirmed",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":now": now, ":attended": "ATTENDED", ":confirmed": "CONFIRMED"},
            )
            _checkins_table().put_item(
                Item={"eventId": event_id, "phone": phone_e164, "checkedInAt": now, "ttl": _ttl_90_days()},
                ConditionExpression="attribute_not_exists(phone)",
            )
            _table().update_item(
                Key={"phone": phone_e164},
                UpdateExpression="SET attendedCount = if_not_exists(attendedCount, :zero) + :one, lastSeenAt = :ls",
                ConditionExpression="attribute_exists(phone)",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":ls": now},
            )
            return {"ok": True, "result": ATTENDANCE_OK, "reason": ""}
        except ClientError as ce:
            code = ce.response.get("Error", {}).get("Code")
            if code in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
                return _attendance_failure_reason(phone_e164, event_id, attended=True)
            logger.exception("record_attendance test fallback failed phone=...%s", phone_e164[-4:])
            return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Database error during check-in"}

    try:
        existing = _checkins_table().get_item(Key={"eventId": event_id, "phone": phone_e164}).get("Item")
        if existing:
            return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in — cannot mark no-show"}

        _invites_table().update_item(
            Key={"eventId": event_id, "phone": phone_e164},
            UpdateExpression="SET noShowAt = :now, #s = :noshow",
            ConditionExpression="attribute_exists(phone) AND #s = :confirmed",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":now": now, ":noshow": "NO_SHOW", ":confirmed": "CONFIRMED"},
        )
        _table().update_item(
            Key={"phone": phone_e164},
            UpdateExpression="SET noShowCount = if_not_exists(noShowCount, :zero) + :one",
            ConditionExpression="attribute_exists(phone)",
            ExpressionAttributeValues={":zero": 0, ":one": 1},
        )
        return {"ok": True, "result": ATTENDANCE_OK, "reason": ""}
    except ClientError as ce:
        code = ce.response.get("Error", {}).get("Code")
        if code in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
            return _attendance_failure_reason(phone_e164, event_id, attended=False)
        logger.exception("record_attendance test fallback no-show failed phone=...%s", phone_e164[-4:])
        return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Database error during no-show"}


def record_attendance(phone: str, attended: bool, event_id: str = "current") -> dict:
    """
    Record attendance/no-show for a member at a specific event.
    Returns {"ok": bool, "result": ATTENDANCE_* constant, "reason": str}.

    10/10 state-integrity rule:
      - Check-in is transactional: invite status, check-in row, and member counter
        succeed together or fail together.
      - No-show is transactional: invite status and noShowCount succeed together
        or fail together.
      - Only CONFIRMED can become ATTENDED or NO_SHOW.
    """
    from botocore.exceptions import ClientError

    phone_e164 = normalize_phone(phone)
    now = _now_iso()

    if _test_disable_transactions():
        return _record_attendance_non_transactional_for_tests(phone_e164, attended, event_id, now)

    client = _ddb().meta.client

    members_table = _table().name
    invites_table = _invites_table().name
    checkins_table = _checkins_table().name

    if attended:
        # Fast path for clearer door/admin UX; the transaction below also protects races.
        try:
            existing = _checkins_table().get_item(
                Key={"eventId": event_id, "phone": phone_e164}
            ).get("Item")
            if existing:
                return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in"}
        except Exception:
            logger.exception("record_attendance: checkin lookup failed phone=...%s", phone_e164[-4:])

        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": invites_table,
                            "Key": _ddb_key(eventId=event_id, phone=phone_e164),
                            "UpdateExpression": "SET attendedAt = :now, #s = :attended",
                            "ConditionExpression": "attribute_exists(phone) AND #s = :confirmed",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":now": _ddb_av(now),
                                ":attended": _ddb_av("ATTENDED"),
                                ":confirmed": _ddb_av("CONFIRMED"),
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": checkins_table,
                            "Item": {
                                "eventId": _ddb_av(event_id),
                                "phone": _ddb_av(phone_e164),
                                "checkedInAt": _ddb_av(now),
                                "ttl": _ddb_av(_ttl_90_days()),
                            },
                            "ConditionExpression": "attribute_not_exists(phone)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": members_table,
                            "Key": _ddb_key(phone=phone_e164),
                            "UpdateExpression": (
                                "SET attendedCount = if_not_exists(attendedCount, :zero) + :one, "
                                "lastSeenAt = :ls"
                            ),
                            "ConditionExpression": "attribute_exists(phone)",
                            "ExpressionAttributeValues": {
                                ":zero": _ddb_av(0),
                                ":one": _ddb_av(1),
                                ":ls": _ddb_av(now),
                            },
                        }
                    },
                ]
            )
            return {"ok": True, "result": ATTENDANCE_OK, "reason": ""}
        except ClientError as ce:
            code = ce.response.get("Error", {}).get("Code")
            if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
                return _attendance_failure_reason(phone_e164, event_id, attended=True)
            logger.exception("record_attendance: transactional check-in failed phone=...%s", phone_e164[-4:])
            return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Database error during check-in"}
        except Exception:
            logger.exception("record_attendance: transactional check-in failed phone=...%s", phone_e164[-4:])
            return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Unexpected error during check-in"}

    # No-show path
    try:
        existing = _checkins_table().get_item(
            Key={"eventId": event_id, "phone": phone_e164}
        ).get("Item")
        if existing:
            return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in — cannot mark no-show"}
    except Exception:
        logger.exception("record_attendance: checkin lookup failed phone=...%s", phone_e164[-4:])

    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": invites_table,
                        "Key": _ddb_key(eventId=event_id, phone=phone_e164),
                        "UpdateExpression": "SET noShowAt = :now, #s = :noshow",
                        "ConditionExpression": "attribute_exists(phone) AND #s = :confirmed",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":now": _ddb_av(now),
                            ":noshow": _ddb_av("NO_SHOW"),
                            ":confirmed": _ddb_av("CONFIRMED"),
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": members_table,
                        "Key": _ddb_key(phone=phone_e164),
                        "UpdateExpression": "SET noShowCount = if_not_exists(noShowCount, :zero) + :one",
                        "ConditionExpression": "attribute_exists(phone)",
                        "ExpressionAttributeValues": {
                            ":zero": _ddb_av(0),
                            ":one": _ddb_av(1),
                        },
                    }
                },
            ]
        )
        return {"ok": True, "result": ATTENDANCE_OK, "reason": ""}
    except ClientError as ce:
        code = ce.response.get("Error", {}).get("Code")
        if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
            return _attendance_failure_reason(phone_e164, event_id, attended=False)
        logger.exception("record_attendance: transactional no-show failed phone=...%s", phone_e164[-4:])
        return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Database error during no-show"}
    except Exception:
        logger.exception("record_attendance: transactional no-show failed phone=...%s", phone_e164[-4:])
        return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Unexpected error during no-show"}


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
