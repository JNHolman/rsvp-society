import base64
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key as DKey

logger = logging.getLogger()



def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _encode_cursor(key: Optional[Dict[str, Any]]) -> str:
    if not key:
        return ""
    raw = json.dumps(key, default=_json_default, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(token: str) -> Optional[Dict[str, Any]]:
    raw = (token or "").strip()
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        raise ValueError("invalid pagination token")


def _safe_limit(value: int, *, default: int = 50, maximum: int = 200) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(1, min(maximum, parsed))

# ── Runtime state constants ─────────────────────────────────────────────────
# States excluded from analytics invite counts
ANALYTICS_EXCLUDE_STATUSES = frozenset({"FAILED", "DELETED"})

# Event lifecycle states where invite blasts may be sent.
INVITABLE_EVENT_STATES = frozenset({"LIVE"})

# Event lifecycle states where SMS confirmations and Jade event conversations are allowed.
CONFIRMABLE_EVENT_STATES = frozenset({"LIVE"})

# States that block a member from future invite waves
WAVE_BLOCK_STATUSES = frozenset({"INVITED", "CONFIRMED", "DECLINED", "ATTENDED", "NO_SHOW"})

# States where Jade should share full logistics
LOGISTICS_ELIGIBLE_STATUSES = frozenset({"CONFIRMED", "ATTENDED"})

# Family of statuses that represent a completed confirmation (used for analytics + capacity math)
from store_common import CONFIRMED_FAMILY_STATUSES as CONFIRMED_FAMILY_STATUSES, _table, _now_iso, normalize_phone

# Invite rows in these statuses should be overwritten on retry
# (never reached the member — eligible for the next wave)
RETRYABLE_INVITE_STATUSES = frozenset({"FAILED", "DELETED"})

# Statuses that mean a member is actively in the current wave
# Used by Jade context to determine what info to share
JADE_IN_WAVE_STATUSES = frozenset({"INVITED", "CONFIRMED", "ATTENDED"})









# ── Attendance finalization: grace window + no-show settling ──────────────────
# A confirmed guest is NOT a no-show just because they have not checked in yet.
# No-show is only real once attendance is settled, which happens when either:
#   (a) the admin hits "Close Event" on the check-in page (explicit, immediate), or
#   (b) the event end time has passed by more than NO_SHOW_GRACE_HOURS (auto fallback).
# If an event has no end time, the auto fallback never fires — the check-in list is
# only final once explicitly closed. Both members and plus-ones use this same gate.
NO_SHOW_GRACE_HOURS = 4


def _event_end_dt(event: dict) -> datetime | None:
    """Return the event's end as a tz-aware UTC datetime, or None if no end time set.

    Combines the event date + endTime in the event's own timezone, then converts to
    UTC. endTime is optional; with no endTime we cannot compute an auto grace cutoff,
    so this returns None and the caller treats the event as 'not auto-finalizable'."""
    date_str = (event.get("date") or "").strip()[:10]
    end_time = (event.get("endTime") or "").strip()
    if not date_str or not end_time:
        return None
    tz_name = (event.get("event_timezone") or "America/New_York").strip() or "America/New_York"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(f"{date_str} {end_time}", fmt)
            start_time = (event.get("startTime") or "").strip()
            if start_time:
                from datetime import time as clock_time
                if naive.time() <= clock_time.fromisoformat(start_time):
                    naive += timedelta(days=1)
            return naive.replace(tzinfo=tz).astimezone(timezone.utc)
        except Exception:
            continue
    return None


def attendance_is_settled(event: dict, now: datetime | None = None) -> bool:
    """True when no-shows can be counted for this event.

    Settled if explicitly finalized (Close Event) OR the end time has passed by more
    than the grace window. Events with no end time are settled ONLY when explicitly
    finalized."""
    _fin = event.get("attendanceFinalized")
    if _fin is True or str(_fin).strip().lower() in ("true", "1", "yes"):
        return True
    end_dt = _event_end_dt(event)
    if end_dt is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= end_dt + timedelta(hours=NO_SHOW_GRACE_HOURS)


def _pending_retention_days() -> int:
    try:
        return max(1, int(os.getenv("PENDING_RETENTION_DAYS", "90")))
    except Exception:
        return 90


def _pending_expiry_iso(now: str | None = None) -> str:
    base = datetime.fromisoformat((now or _now_iso()).replace("Z", "+00:00"))
    return (base + timedelta(days=_pending_retention_days())).isoformat(timespec="seconds")


def _pending_is_expired(item: Dict[str, Any]) -> bool:
    if (item.get("status") or "").upper() != "PENDING":
        return False
    expiry = (item.get("pendingExpiresAt") or "").strip()
    if not expiry:
        submitted = (item.get("submittedAt") or item.get("createdAt") or "").strip()
        if not submitted:
            return False
        try:
            submitted_dt = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) > submitted_dt + timedelta(days=_pending_retention_days())
        except Exception:
            return False
    try:
        expiry_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > expiry_dt
    except Exception:
        return False


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




def upsert_member(
    *,
    phone: str,
    name: str,
    last_name: Optional[str] = None,
    email: Optional[str] = None,
    source: str = "web",
    sms_opt_in: bool = False,
    tags: Optional[str] = None,
    zip_code: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> Dict[str, Any]:
    t = _table()
    now = _now_iso()

    # Public re-apply rule:
    # - Brand-new members become PENDING.
    # - APPROVED members keep their status unless opted out; an opted-out
    #   reapplication returns to PENDING and requires fresh host approval.
    # - DENIED/DELETED members may submit a genuinely new request and return to
    #   PENDING so the host can make a fresh decision.
    phone = normalize_phone(phone)
    existing = t.get_item(Key={"phone": phone}, ConsistentRead=True).get("Item") or {}
    existing_status = (existing.get("status") or "").upper().strip()
    opted_out = str(existing.get("optOut", False)).lower() == "true"
    should_reset_pending = existing_status in ("", "DENIED", "DELETED") or opted_out

    expr_names = {
        "#n": "name",
        "#s": "status",
        "#src": "source",
    }

    protect_identity = existing_status == "APPROVED" and not opted_out

    expr_vals: Dict[str, Any] = {
        ":ls":      now,
        ":ca":      now,
        ":pending": "PENDING",
        ":soi":     False if opted_out else (existing.get("smsOptIn", sms_opt_in) if protect_identity else sms_opt_in),
        ":pex":     _pending_expiry_iso(now),
    }

    set_parts = [
        "lastSeenAt = :ls",
        "submittedAt = :ls",
        "createdAt = if_not_exists(createdAt, :ca)",
        "smsOptIn = :soi",
    ]
    # Public signup is unauthenticated. Once a member is approved, a person who
    # merely knows that phone number must not be able to rewrite the member's
    # identity/profile fields. STOP reapplications enter a new host review below.
    if not protect_identity:
        expr_vals[":n"] = name[:120]
        expr_vals[":src"] = (source or "web")[:40]
        set_parts[0:0] = ["#n = :n", "#src = :src"]

    remove_parts = []
    if should_reset_pending:
        set_parts.extend([
            "#s = :pending",
            "pendingExpiresAt = :pex",
        ])
        # A deleted member reapplying is a fresh approval cycle. Clear tombstone
        # and welcome markers so host approval can send the member welcome once.
        remove_parts.extend(["deletedAt", "welcomeSentAt", "welcomeSendingAt", "lastWelcomeError"])
    else:
        set_parts.extend([
            "#s = if_not_exists(#s, :pending)",
            "pendingExpiresAt = if_not_exists(pendingExpiresAt, :pex)",
        ])

    # A reapplication records fresh requested consent but cannot clear STOP.
    # Host approval promotes this request atomically; a later STOP removes it.
    if sms_opt_in and opted_out:
        expr_vals[":requested"] = now
        set_parts.append("pendingSmsConsentAt = :requested")
    elif sms_opt_in and not protect_identity:
        expr_vals[":oiat"] = now
        set_parts.append("smsOptInAt = if_not_exists(smsOptInAt, :oiat)")

    if last_name and not protect_identity:
        expr_vals[":ln"] = last_name[:120]
        set_parts.append("lastName = :ln")

    if email and not protect_identity:
        expr_vals[":e"] = email[:200]
        set_parts.append("email = :e")

    if tags and not protect_identity:
        expr_vals[":tg"] = tags[:200]
        set_parts.append("tags = :tg")

    # Location is profile data supplied by the public form. Protect it for an
    # already-approved member just like name/email so knowing a phone number is
    # not enough to move somebody into another invite geography.
    if zip_code and city and state and latitude is not None and longitude is not None and not protect_identity:
        expr_vals[":zip"] = zip_code[:10]
        expr_vals[":city"] = city[:120]
        expr_vals[":state"] = state[:40]
        expr_vals[":lat"] = Decimal(str(latitude))
        expr_vals[":lon"] = Decimal(str(longitude))
        expr_vals[":locsrc"] = "zip"
        set_parts.extend([
            "zipCode = :zip",
            "city = :city",
            "#state = :state",
            "latitude = :lat",
            "longitude = :lon",
            "locationSource = :locsrc",
        ])
        expr_names["#state"] = "state"

    update_expr = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expr += " REMOVE " + ", ".join(remove_parts)

    # Reject a stale signup if approval, STOP, deletion or consent changed.
    guards = []
    for i, field in enumerate(("status", "optOut", "optOutAt", "smsOptIn", "submittedAt", "consentRevision")):
        key, value = f"#snapshot{i}", f":snapshot{i}"
        expr_names[key] = field
        if field in existing:
            guards.append(f"{key} = {value}")
            expr_vals[value] = existing[field]
        else:
            guards.append(f"attribute_not_exists({key})")
    expression_text = update_expr + " " + " AND ".join(guards)
    expr_names = {k: v for k, v in expr_names.items() if k in re.findall(r"#[A-Za-z0-9_]+", expression_text)}
    t.update_item(
        Key={"phone": phone},
        ConditionExpression=" AND ".join(guards),
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_vals,
    )

    resp = t.get_item(Key={"phone": phone})
    return resp.get("Item", {"phone": phone})


def set_status(phone: str, status: str, *, expected_status: str | None = None) -> None:
    st = (status or "").upper().strip()
    if st not in ("PENDING", "APPROVED", "DENIED"):
        raise ValueError("status must be PENDING, APPROVED, or DENIED")
    phone_e164 = normalize_phone(phone)
    now = _now_iso()
    values = {":s": st, ":ls": now, ":deleted": "DELETED"}
    condition = "attribute_exists(phone) AND (attribute_not_exists(#s) OR #s <> :deleted)"
    if expected_status is not None:
        condition += " AND #s = :expected"
        values[":expected"] = expected_status
    if st == "PENDING":
        expression = "SET #s = :s, lastSeenAt = :ls, pendingExpiresAt = :pex"
        values[":pex"] = _pending_expiry_iso(now)
    else:
        expression = "SET #s = :s, lastSeenAt = :ls REMOVE pendingExpiresAt"
    if st == "APPROVED":
        current = _table().get_item(Key={"phone": phone_e164}, ConsistentRead=True).get("Item") or {}
        if current.get("status") == "PENDING" and current.get("pendingSmsConsentAt"):
            values.update({":pendingReview": "PENDING", ":request": current["pendingSmsConsentAt"], ":yes": True, ":consentSource": "web_reapplication_host_approved"})
            condition += " AND #s = :pendingReview AND pendingSmsConsentAt = :request"
            expression = ("SET #s = :s, lastSeenAt = :ls, smsOptIn = :yes, smsOptInAt = :request, "
                          "smsOptInConfirmedAt = :ls, smsOptInConfirmationSource = :consentSource "
                          "REMOVE pendingExpiresAt, optOut, optOutAt, pendingSmsConsentAt")
    elif st == "DENIED":
        expression += ", pendingSmsConsentAt"
    _table().update_item(
        Key={"phone": phone_e164}, UpdateExpression=expression,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=values, ConditionExpression=condition,
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
    items = [item for item in items if not _pending_is_expired(item)]
    items.sort(key=lambda x: (
        (x.get("lastName") or x.get("name") or "").lower(),
        (x.get("name") or "").lower(),
    ))
    return items






def count_members_by_status(status: str = "PENDING") -> int:
    """Return the admin-visible count for one member status.

    PENDING counts only requests that are still reviewable; logically expired
    rows are excluded using the same rule as the pending member list.
    """
    st = (status or "PENDING").upper().strip()
    if st not in {"PENDING", "APPROVED", "DENIED"}:
        st = "PENDING"

    total = 0
    kwargs: Dict[str, Any] = {
        "IndexName": "status-index",
        "KeyConditionExpression": DKey("status").eq(st),
    }
    if st != "PENDING":
        kwargs["Select"] = "COUNT"

    while True:
        page = _table().query(**kwargs)
        if st == "PENDING":
            total += sum(1 for item in page.get("Items", []) if not _pending_is_expired(item))
        else:
            total += int(page.get("Count") or 0)
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return total


def list_members_by_status_page(status: str = "PENDING", *, limit: int = 50, next_token: str = "") -> Dict[str, Any]:
    """Return one DynamoDB page for a member status.

    This is the backend-supported pagination path for the admin UI. The older
    list_members_by_status() intentionally remains for small internal jobs and
    tests that need the full list.
    """
    st = (status or "PENDING").upper().strip()
    if st not in {"PENDING", "APPROVED", "DENIED"}:
        st = "PENDING"

    kwargs: Dict[str, Any] = {
        "IndexName": "status-index",
        "KeyConditionExpression": DKey("status").eq(st),
        "Limit": _safe_limit(limit),
    }
    cursor = _decode_cursor(next_token)
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor

    response = _table().query(**kwargs)
    items = [normalize_member_record(item) for item in response.get("Items", [])]
    items = [item for item in items if not _pending_is_expired(item)]
    items.sort(key=lambda x: (
        (x.get("lastName") or x.get("name") or "").lower(),
        (x.get("name") or "").lower(),
    ))
    next_page_token = _encode_cursor(response.get("LastEvaluatedKey"))
    return {
        "members": items,
        "nextPageToken": next_page_token,
        "hasMore": bool(next_page_token),
        "pageSize": kwargs["Limit"],
    }


def search_members_page(query: str, *, limit: int = 50, next_token: str = "") -> Dict[str, Any]:
    """Search one scan page and return a cursor so results do not silently cap at 50."""
    q = (query or "").strip().lower()
    if not q:
        return {"members": [], "nextPageToken": "", "hasMore": False, "pageSize": _safe_limit(limit)}

    limit_safe = _safe_limit(limit)
    kwargs: Dict[str, Any] = {}
    cursor = _decode_cursor(next_token)
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor

    items: List[Dict[str, Any]] = []
    last_key = None

    while len(items) < limit_safe:
        response = _table().scan(**kwargs)
        for raw_item in response.get("Items", []):
            item = normalize_member_record(raw_item) or {}
            first = (item.get("name") or "").lower()
            last = (item.get("lastName") or "").lower()
            phone = (item.get("phone") or "").lower()
            full = f"{first} {last}".strip()
            if q in first or q in last or q in full or q in phone:
                items.append(item)

        last_key = response.get("LastEvaluatedKey")
        if not last_key or len(items) >= limit_safe:
            break
        kwargs["ExclusiveStartKey"] = last_key

    items = [item for item in items if not _pending_is_expired(item)]
    items.sort(key=lambda x: (
        (x.get("lastName") or x.get("name") or "").lower(),
        (x.get("name") or "").lower(),
    ))
    next_page_token = _encode_cursor(last_key)
    return {
        "members": items,
        "nextPageToken": next_page_token,
        "hasMore": bool(next_page_token),
        "pageSize": limit_safe,
    }

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
                if not _pending_is_expired(item):
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
        ExpressionAttributeValues={':g': g, ':ls': _now_iso(), ':guardDeleted': 'DELETED'},

            ExpressionAttributeNames={'#guardStatus': 'status'},
            ConditionExpression='attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted)',
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
            ExpressionAttributeValues={':ls': _now_iso(), ':guardDeleted': 'DELETED'},

            ExpressionAttributeNames={'#guardStatus': 'status'},
            ConditionExpression='attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted)',
        )
    else:
        t.update_item(
            Key={"phone": phone_e164},
            UpdateExpression="SET tierOverride = :t, lastSeenAt = :ls",
            ExpressionAttributeValues={':t': tier, ':ls': _now_iso(), ':guardDeleted': 'DELETED'},

            ExpressionAttributeNames={'#guardStatus': 'status'},
            ConditionExpression='attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted)',
        )


from attendance_store import (
    ATTENDANCE_OK as ATTENDANCE_OK, ATTENDANCE_ALREADY as ATTENDANCE_ALREADY, ATTENDANCE_NOT_CONFIRMED as ATTENDANCE_NOT_CONFIRMED,
    ATTENDANCE_INVITE_NOT_FOUND as ATTENDANCE_INVITE_NOT_FOUND, ATTENDANCE_INVALID_STATUS as ATTENDANCE_INVALID_STATUS, ATTENDANCE_DDB_ERROR as ATTENDANCE_DDB_ERROR,
    record_attendance as record_attendance, finalize_event_attendance as finalize_event_attendance,
)

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
            ExpressionAttributeValues={':now': now, ':guardDeleted': 'DELETED'},
            ConditionExpression='attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted) AND (attribute_not_exists(welcomeSentAt) AND attribute_not_exists(welcomeSendingAt))',

            ExpressionAttributeNames={'#guardStatus': 'status'},
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
            ExpressionAttributeValues={':now': now, ':expiry': expiry, ':guardDeleted': 'DELETED'},
            ConditionExpression=(
                'attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted) AND (attribute_not_exists(welcomeSentAt) AND welcomeSendingAt <= :expiry)'
            ),

            ExpressionAttributeNames={'#guardStatus': 'status'},
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

            ExpressionAttributeNames={'#guardStatus': 'status'},
            ExpressionAttributeValues={':guardDeleted': 'DELETED'},
            ConditionExpression='attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted)',
        )


def set_sms_opt_in(phone: str, value: bool) -> None:
    """Explicitly set smsOptIn on a member record."""
    phone_e164 = normalize_phone(phone)
    _table().update_item(
        Key={"phone": phone_e164},
        UpdateExpression="SET smsOptIn = :v",
        ExpressionAttributeValues={':v': value, ':guardDeleted': 'DELETED'},

            ExpressionAttributeNames={'#guardStatus': 'status'},
            ConditionExpression='attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted)',
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
            ExpressionAttributeValues={':e': error[:3000], ':t': _now_iso(), ':guardDeleted': 'DELETED'},

            ExpressionAttributeNames={'#guardStatus': 'status'},
            ConditionExpression='attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted)',
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
            ExpressionAttributeValues={':now': now, ':guardDeleted': 'DELETED'},
            ConditionExpression='attribute_exists(phone) AND (attribute_not_exists(#guardStatus) OR #guardStatus <> :guardDeleted) AND (attribute_not_exists(welcomeSentAt))',

            ExpressionAttributeNames={'#guardStatus': 'status'},
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
