import base64
import hmac
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import boto3
from boto3.dynamodb.conditions import Key as DKey
from botocore.exceptions import ClientError
import logging

from audit_log import log_action, ACTION_INVITE_SENT
from member_store import normalize_phone, _table as members_table, list_members_by_status
from sms_adapter import get_secret_string, send_sms
from admin_shared import coerce_bool

_DDB = boto3.resource("dynamodb")
logger = logging.getLogger()


def _invites_table():
    name = os.getenv("INVITES_TABLE_NAME")
    if not name:
        raise RuntimeError("INVITES_TABLE_NAME env var is not set")
    return _DDB.Table(name)


def _events_table():
    name = os.getenv("EVENTS_TABLE_NAME")
    if not name:
        raise RuntimeError("EVENTS_TABLE_NAME env var is not set")
    return _DDB.Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def calc_tier(member: Dict[str, Any]) -> int:
    override = member.get("tierOverride")
    if override in (1, 2, 3):
        return int(override)
    invited = int(member.get("invitedCount", 0))
    attended = int(member.get("attendedCount", 0))
    if invited < 3:
        return 2
    rate = attended / invited
    if rate >= 0.80:
        return 1
    elif rate >= 0.40:
        return 2
    else:
        return 3


def _get_method(event: dict) -> str:
    if event.get("httpMethod"):
        return event["httpMethod"]
    return event.get("requestContext", {}).get("http", {}).get("method", "")


def _get_headers(event: dict) -> dict:
    return event.get("headers") or {}


def _resp(status: int, body: dict, origin: str = None) -> dict:
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
            "access-control-allow-methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _admin_token() -> str:
    sid = os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token")
    s = get_secret_string(sid)
    try:
        j = json.loads(s)
        if isinstance(j, dict) and j.get("token"):
            return j["token"]
    except Exception:
        pass
    return s or ""


def _get_approved_members() -> List[Dict[str, Any]]:
    items = list_members_by_status("APPROVED")
    for m in items:
        m["_tier"] = calc_tier(m)
    return items


def _get_existing_invited_phones(event_id: str) -> set:
    invites_t = _invites_table()
    phones: set = set()
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": DKey("eventId").eq(event_id),
        "ProjectionExpression": "phone",
    }
    while True:
        resp = invites_t.query(**kwargs)
        for item in resp.get("Items", []):
            phone = item.get("phone")
            if phone:
                phones.add(phone)
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return phones


def _calc_invite_suggestion(
    capacity: int,
    confirmed: int,
    already_invited: int,
    actual_confirm_rate: float | None = None,
    show_rate: float = 0.60,
) -> dict:
    """
    How many more invites does this wave need to fill the room?

    target_confirmed = capacity / show_rate
        e.g. 200 cap at 60% show rate → need 334 confirmed

    confirmation_gap = target_confirmed - currently_confirmed

    confirm_rate = actual rate from analytics if available, else default 30%

    suggested_invites = confirmation_gap / confirm_rate
    """
    confirm_rate = actual_confirm_rate if actual_confirm_rate and actual_confirm_rate > 0 else 0.30
    target_confirmed = math.ceil(capacity / show_rate)
    gap = max(0, target_confirmed - confirmed)
    suggested = math.ceil(gap / confirm_rate) if gap > 0 else 0
    return {
        "targetConfirmed":    target_confirmed,
        "currentConfirmed":   confirmed,
        "confirmationGap":    gap,
        "suggestedInvites":   suggested,
        "alreadyInvited":     already_invited,
        "assumedShowRate":    round(show_rate * 100),
        "assumedConfirmRate": round(confirm_rate * 100),
    }


def _resolve_wave_capacity(
    capacity: int,
    wave_number: int,
    wave_size: int | None,
    confirmed: int = 0,
    already_invited: int = 0,
    actual_confirm_rate: float | None = None,
) -> int:
    """
    Wave 1: cast wide at 2.5x capacity (no data yet, need to seed confirmations)
    Wave 2+: gap math — how many invites to close the confirmation shortfall
    Manual override (wave_size > 0) always wins.
    """
    if wave_size and wave_size > 0:
        return wave_size
    if wave_number == 1:
        return max(1, round(capacity * 2.5))
    suggestion = _calc_invite_suggestion(capacity, confirmed, already_invited, actual_confirm_rate)
    return max(1, suggestion["suggestedInvites"])


def _get_current_event() -> Dict[str, Any]:
    try:
        result = _events_table().get_item(Key={"eventId": "current"})
        return result.get("Item") or {}
    except Exception:
        return {}


def _build_invite_list(
    members: List[Dict[str, Any]],
    capacity: int,
    female_pct: int,
    tier2_buffer_pct: int = 30,
    removed_phones: List[str] = None,
) -> Dict[str, Any]:
    removed = set(removed_phones or [])
    members = [m for m in members if m.get("phone") not in removed]

    male_pct = 100 - female_pct
    target_f = round(capacity * female_pct / 100)
    target_m = capacity - target_f

    buckets: Dict[str, List] = {
        "F1": [], "F2": [], "F3": [],
        "M1": [], "M2": [], "M3": [],
        "O1": [], "O2": [], "O3": [],
    }

    for m in members:
        gender = (m.get("gender") or "O").upper()
        if gender not in ("M", "F"):
            gender = "O"
        tier = m["_tier"]
        key = f"{gender}{tier}"
        if key in buckets:
            buckets[key].append(m)

    for bucket in buckets.values():
        random.shuffle(bucket)

    def fill_gender(gender: str, target: int) -> Tuple[List, List, int]:
        t1 = buckets.get(f"{gender}1", [])
        t2 = buckets.get(f"{gender}2", [])
        tier1_invited = t1[:target]
        remaining = target - len(tier1_invited)
        if remaining > 0:
            buffer_multiplier = 1 + (tier2_buffer_pct / 100)
            tier2_needed = math.ceil(remaining * buffer_multiplier)
            tier2_invited = t2[:tier2_needed]
        else:
            tier2_invited = []
        return tier1_invited, tier2_invited, len(tier2_invited)

    f_t1, f_t2, _ = fill_gender("F", target_f)
    m_t1, m_t2, _ = fill_gender("M", target_m)

    core_invited = f_t1 + f_t2 + m_t1 + m_t2
    remaining_slots = max(0, capacity - len(core_invited))
    o1_pool = buckets.get("O1", [])
    o2_pool = buckets.get("O2", [])
    o_t1 = o1_pool[:remaining_slots]
    remaining_slots -= len(o_t1)
    o_t2 = o2_pool[:remaining_slots] if remaining_slots > 0 else []
    all_invited = core_invited + o_t1 + o_t2

    return {
        "summary": {
            "capacity":       capacity,
            "targetFemale":   target_f,
            "targetMale":     target_m,
            "femalePercent":  female_pct,
            "malePercent":    male_pct,
            "tier2BufferPct": tier2_buffer_pct,
            "totalInvites":   len(all_invited),
            "breakdown": {
                "femTier1":    len(f_t1),
                "femTier2":    len(f_t2),
                "maleTier1":   len(m_t1),
                "maleTier2":   len(m_t2),
                "otherTier1":  len(o_t1),
                "otherTier2":  len(o_t2),
                "tier3Skipped": (
                    len(buckets["F3"]) + len(buckets["M3"]) + len(buckets["O3"])
                ),
            },
        },
        "members": all_invited,
    }


def _build_sms_message(member: Dict[str, Any], event: Dict[str, Any]) -> str:
    """
    Use locked invite_template if admin approved one.
    Replace {name} with member first name.
    Fall back to building from event fields if no template set.
    """
    name = (member.get("name") or "").split()[0] or ""

    template = (event.get("invite_template") or "").strip()
    if template:
        return template.replace("{name}", name).strip()

    if event and event.get("date"):
        date = event["date"]
        time = event.get("startTime", "")
        reveal_venue = event.get("revealVenue", False)
        venue = event.get("venue", "") if reveal_venue else ""
        address = event.get("address", "") if reveal_venue else ""
        event_label = (event.get("event_label") or "").strip()
        vibe_tag = (event.get("vibe_tag") or "").strip()

        closings = ["Let me know.", "You in?", "You coming?"]
        closing = random.choice(closings) if random.random() < 0.3 else ""

        parts = []
        if name:        parts.append(f"{name}.")
        if event_label: parts.append(f"{event_label}.")
        parts.append(f"{date}.")
        if vibe_tag:    parts.append(f"{vibe_tag}.")
        if time:        parts.append(f"{time}.")
        if venue:       parts.append(f"{venue}.")
        if address:     parts.append(f"{address}.")
        if closing:     parts.append(closing)

        return " ".join(parts)
    else:
        if name:
            return f"{name}. You're on the list. You in?"
        else:
            return "You're on the list. You in?"


def _get_analytics(event_id: str) -> dict:
    """
    Fetch live invite analytics for an event. Used by wave 2+ to calculate gap.
    Excludes DELETED records so tombstoned members don't skew confirm rate.
    """
    try:
        invites_t = _invites_table()
        items = []
        kwargs: dict = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
        while True:
            resp = invites_t.query(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last

        # Fix: exclude DELETED so they don't inflate invited count and drag down confirm rate
        items = [i for i in items if i.get("status") != "DELETED"]

        invited = len(items)
        confirmed = sum(1 for i in items if i.get("status") == "CONFIRMED")
        declined  = sum(1 for i in items if i.get("status") == "DECLINED")
        attended  = sum(1 for i in items if i.get("attendedAt"))
        confirm_rate = (confirmed / invited) if invited > 0 else None
        show_rate    = (attended / confirmed) if confirmed > 0 else None
        return {
            "invited": invited, "confirmed": confirmed,
            "declined": declined, "attended": attended,
            "confirmRate": confirm_rate, "showRate": show_rate,
        }
    except Exception:
        logger.exception("_get_analytics failed event=%s", event_id)
        return {}


def handle_preview(body: dict, origin: str) -> dict:
    event_id  = (body.get("eventId") or "").strip()
    capacity  = int(body.get("capacity") or 0)
    female_pct = int(body.get("femalePercent") or 60)
    tier2_buffer = int(body.get("tier2BufferPct") or 30)
    wave_number  = int(body.get("waveNumber") or 1)
    wave_size    = int(body.get("waveSize") or 0)
    removed_phones = body.get("removedPhones") or []

    if not event_id or capacity < 1:
        return _resp(400, {"ok": False, "error": "eventId and capacity required"}, origin)

    if not (0 <= female_pct <= 100):
        return _resp(400, {"ok": False, "error": "femalePercent must be 0–100"}, origin)

    if not (0 <= tier2_buffer <= 200):
        return _resp(400, {"ok": False, "error": "tier2BufferPct must be 0–200"}, origin)

    existing_invites = _get_existing_invited_phones(event_id)

    analytics = {}
    gap_info = None
    if wave_number >= 2:
        analytics = _get_analytics(event_id)
        confirmed = analytics.get("confirmed", 0)
        actual_confirm_rate = analytics.get("confirmRate")
        effective_capacity = _resolve_wave_capacity(
            capacity, wave_number, wave_size,
            confirmed=confirmed,
            already_invited=len(existing_invites),
            actual_confirm_rate=actual_confirm_rate,
        )
        gap_info = _calc_invite_suggestion(
            capacity, confirmed, len(existing_invites), actual_confirm_rate
        )
    else:
        effective_capacity = _resolve_wave_capacity(capacity, wave_number, wave_size)

    members = [m for m in _get_approved_members() if m.get("phone") not in existing_invites]
    result = _build_invite_list(members, effective_capacity, female_pct, tier2_buffer, removed_phones)

    preview_members = []
    for m in result["members"]:
        preview_members.append({
            "phone":        m.get("phone", ""),
            "name":         m.get("name", ""),
            "gender":       m.get("gender", "?"),
            "tier":         m["_tier"],
            "attendedCount": int(m.get("attendedCount", 0)),
            "invitedCount":  int(m.get("invitedCount", 0)),
        })

    result["summary"]["waveNumber"]             = wave_number
    result["summary"]["waveSize"]               = effective_capacity
    result["summary"]["alreadyInvitedExcluded"] = len(existing_invites)
    result["summary"]["poolRemaining"]          = len(members)
    if gap_info:
        result["summary"]["gapAnalysis"] = gap_info

    return _resp(200, {
        "ok":      True,
        "eventId": event_id,
        "summary": result["summary"],
        "members": preview_members,
    }, origin)


def handle_send(body: dict, origin: str, token: str) -> dict:
    if not body.get("confirmSend"):
        return _resp(400, {"ok": False, "error": "confirmSend: true required"}, origin)

    event_id   = (body.get("eventId") or "").strip()
    capacity   = int(body.get("capacity") or 0)
    wave_number = int(body.get("waveNumber") or 1)
    wave_size   = int(body.get("waveSize") or 0)
    removed_phones = body.get("removedPhones") or []

    # selected_phones: normalize each phone, skip malformed ones rather than crashing the send
    raw_phones = body.get("phones") or []
    selected_phones = []
    for p in raw_phones:
        if p:
            try:
                selected_phones.append(normalize_phone(p))
            except ValueError:
                logger.warning("handle_send: skipping malformed phone in selected list: %r", p)

    # Fix #29: validate these server-side — don't trust client values
    try:
        female_pct   = int(body.get("femalePercent") or 60)
        tier2_buffer = int(body.get("tier2BufferPct") or 30)
    except (ValueError, TypeError):
        return _resp(400, {"ok": False, "error": "femalePercent and tier2BufferPct must be integers"}, origin)

    if not (0 <= female_pct <= 100):
        return _resp(400, {"ok": False, "error": "femalePercent must be 0–100"}, origin)
    if not (0 <= tier2_buffer <= 200):
        return _resp(400, {"ok": False, "error": "tier2BufferPct must be 0–200"}, origin)

    if not event_id or capacity < 1:
        return _resp(400, {"ok": False, "error": "eventId and capacity required"}, origin)

    # Fix C5: wave 2+ must use live analytics to compute effective_capacity,
    # matching exactly what handle_preview showed the admin.
    existing_invites = _get_existing_invited_phones(event_id)

    if wave_number >= 2 and not wave_size:
        analytics = _get_analytics(event_id)
        confirmed_count = analytics.get("confirmed", 0)
        actual_confirm_rate = analytics.get("confirmRate")
        effective_capacity = _resolve_wave_capacity(
            capacity, wave_number, wave_size,
            confirmed=confirmed_count,
            already_invited=len(existing_invites),
            actual_confirm_rate=actual_confirm_rate,
        )
    else:
        effective_capacity = _resolve_wave_capacity(capacity, wave_number, wave_size)

    members_list = [m for m in _get_approved_members() if m.get("phone") not in existing_invites]

    if selected_phones:
        # selected_phones from the frontend is the authoritative list — the admin explicitly
        # chose these people in preview. Build the member map from the full eligible pool
        # so no selected phone can be dropped by a reshuffle of _build_invite_list.
        full_member_map = {normalize_phone(m.get("phone", "")): m for m in members_list if m.get("phone")}
        selected_members = [full_member_map[p] for p in selected_phones if p in full_member_map]
        result = {"members": selected_members, "summary": {}}
    else:
        result = _build_invite_list(members_list, effective_capacity, female_pct, tier2_buffer, removed_phones)
        selected_members = result["members"]

    current_event = _get_current_event()
    invites_t = _invites_table()
    members_t = members_table()
    now = _now_iso()

    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"
    sent = 0
    failed = 0
    skipped_consent = 0
    invites_written = 0

    for m in selected_members:
        phone = normalize_phone(m.get("phone", ""))
        if not phone:
            continue

        try:
            try:
                invites_t.put_item(
                    Item={
                        "eventId":    event_id,
                        "phone":      phone,
                        "status":     "INVITED",
                        "gender":     m.get("gender", ""),
                        "tier":       m["_tier"],
                        "invitedAt":  now,
                        "waveNumber": wave_number,
                        "waveSentAt": now,
                        # Snapshot names at blast time so door list works even if member is later deleted
                        "name":       m.get("name", ""),
                        "lastName":   m.get("lastName", ""),
                    },
                    ConditionExpression="attribute_not_exists(phone)",
                )
                invites_written += 1
            except ClientError as ce:
                if ce.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    # Already invited — idempotent, skip silently
                    continue
                raise

            sms_succeeded = False
            if sms_enabled:
                if coerce_bool(m.get("optOut", False)) or not coerce_bool(m.get("smsOptIn", False)):
                    skipped_consent += 1
                    sms_succeeded = True  # not a send failure, just skipped
                else:
                    message = _build_sms_message(m, current_event)
                    msg_id = None
                    for attempt in range(3):
                        try:
                            msg_id = send_sms(phone, message)
                            sent += 1
                            sms_succeeded = True
                            break
                        except RuntimeError as sms_err:
                            err_str = str(sms_err)
                            if "429" in err_str or "rate" in err_str.lower():
                                wait = (attempt + 1) * 1.5
                                logger.warning(
                                    "invite send rate limited, waiting %.1fs phone=...%s attempt=%d",
                                    wait, phone[-4:], attempt + 1,
                                )
                                time.sleep(wait)
                            else:
                                raise
                    else:
                        failed += 1
                        logger.error("invite send exhausted retries phone=...%s", phone[-4:])
                    # Store Quo message ID on invite record for delivery tracking
                    if msg_id:
                        try:
                            invites_t.update_item(
                                Key={"eventId": event_id, "phone": phone},
                                UpdateExpression="SET quoMessageId = :mid",
                                ExpressionAttributeValues={":mid": msg_id},
                            )
                        except Exception:
                            logger.exception("invite send: failed to store msg_id phone=...%s", phone[-4:])
                    time.sleep(0.25)
            else:
                sms_succeeded = True  # SMS disabled, invite row is still valid

            # Only increment invitedCount after the invite row is written AND
            # SMS either sent successfully or was intentionally skipped.
            # This prevents count drift when sends fail and rows get cleaned up.
            if sms_succeeded:
                members_t.update_item(
                    Key={"phone": phone},
                    UpdateExpression="SET invitedCount = if_not_exists(invitedCount, :zero) + :one, lastSeenAt = :now",
                    ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now},
                )

        except Exception:
            failed += 1
            logger.exception("invite send failed event=%s phone=...%s", event_id, phone[-4:])
            # Delete the invite row so a re-run of the blast can retry this member.
            # invitedCount has NOT been incremented yet (it only increments after
            # successful send), so cleanup is clean — no counter drift.
            try:
                invites_t.delete_item(Key={"eventId": event_id, "phone": phone})
            except Exception:
                logger.exception("invite send: failed to clean up invite row phone=...%s", phone[-4:])
            continue

    result["summary"]["waveNumber"] = wave_number
    result["summary"]["waveSize"]   = effective_capacity

    # Write delivery stats to the event record — visible in admin panel
    try:
        events_t = boto3.resource("dynamodb").Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))
        events_t.update_item(
            Key={"eventId": "current"},
            UpdateExpression=(
                "SET lastBlastAt = :now, "
                "lastBlastSmsSent = :sent, "
                "lastBlastFailed = :failed, "
                "lastBlastSkippedConsent = :skip, "
                "lastBlastWave = :wave, "
                "deliveredCount = :zero"
            ),
            ExpressionAttributeValues={
                ":now": _now_iso(),
                ":sent": sent,
                ":failed": failed,
                ":skip": skipped_consent,
                ":wave": wave_number,
                ":zero": 0,
            },
        )
    except Exception:
        logger.exception("invite send: failed to write blast stats to event record")

    log_action(
        token=token,
        action=ACTION_INVITE_SENT,
        metadata={
            "eventId":        event_id,
            "waveNumber":     wave_number,
            "waveSize":       effective_capacity,
            "invitesWritten": invites_written,
            "smsSent":        sent,
            "failed":         failed,
            "skippedConsent": skipped_consent,
        },
    )

    return _resp(200, {
        "ok":            True,
        "eventId":       event_id,
        "summary":       result["summary"],
        "invitesWritten": invites_written,
        "smsSent":       sent,
        "failed":        failed,
        "skippedConsent": skipped_consent,
    }, origin)


def handler(event, context):
    try:
        method  = _get_method(event).upper()
        headers = _get_headers(event)
        origin  = headers.get("origin") or headers.get("Origin")

        if method == "OPTIONS":
            return _resp(200, {"ok": True}, origin)

        token = (headers.get("x-admin-token") or headers.get("X-Admin-Token") or "").strip()
        expected = _admin_token()
        if not token or not hmac.compare_digest(token, expected):
            return _resp(401, {"ok": False, "error": "unauthorized"}, origin)

        path = event.get("path", "")
        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        body = json.loads(raw_body) if raw_body else {}

        if method == "POST" and path.endswith("/admin/invite/preview"):
            return handle_preview(body, origin)

        if method == "POST" and path.endswith("/admin/invite/send"):
            return handle_send(body, origin, token)

        return _resp(404, {"ok": False, "error": "not found"}, origin)

    except Exception:
        logger.exception("invite handler failed")
        return _resp(500, {"ok": False, "error": "internal error"}, None)
