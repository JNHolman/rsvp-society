import json
import math
import os
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import boto3
from boto3.dynamodb.conditions import Attr

from member_store import normalize_phone, _table as members_table
from sms_adapter import get_secret_string, send_sms

_DDB = boto3.resource("dynamodb")


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
    allow_origin = origin if origin in origins else (origins[0] if origins else "*")
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
    t = members_table()
    items = []
    kwargs: Dict[str, Any] = {"FilterExpression": Attr("status").eq("APPROVED")}
    while True:
        resp = t.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    for m in items:
        m["_tier"] = calc_tier(m)
    return items


def _get_current_event() -> Dict[str, Any]:
    """Fetch the current event details."""
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

    # Filter out removed phones
    members = [m for m in members if m.get("phone") not in removed]

    male_pct = 100 - female_pct
    target_f = round(capacity * female_pct / 100)
    target_m = capacity - target_f

    buckets: Dict[str, List] = {
        "F1": [], "F2": [], "F3": [],
        "M1": [], "M2": [], "M3": [],
        "O1": [], "O2": [], "O3": []
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

    f_t1, f_t2, f_buffer = fill_gender("F", target_f)
    m_t1, m_t2, m_buffer = fill_gender("M", target_m)

    # O members (gender not M or F) get the slots remaining after M and F are
    # allocated. Without this cap, every O1 and O2 member would be invited
    # regardless of capacity, causing total invites to exceed the target.
    # O1 fills first (higher tier priority), then O2 takes any leftover slots.
    slots_used = len(f_t1) + len(f_t2) + len(m_t1) + len(m_t2)
    o_budget = max(0, capacity - slots_used)
    o_t1_raw = buckets.get("O1", [])
    o_t2_raw = buckets.get("O2", [])
    o_t1 = o_t1_raw[:o_budget]
    o_t2 = o_t2_raw[:max(0, o_budget - len(o_t1))]

    all_invited = f_t1 + f_t2 + m_t1 + m_t2 + o_t1 + o_t2

    return {
        "summary": {
            "capacity": capacity,
            "targetFemale": target_f,
            "targetMale": target_m,
            "femalePercent": female_pct,
            "malePercent": male_pct,
            "tier2BufferPct": tier2_buffer_pct,
            "totalInvites": len(all_invited),
            "breakdown": {
                "femTier1": len(f_t1),
                "femTier2": len(f_t2),
                "maleTier1": len(m_t1),
                "maleTier2": len(m_t2),
                "otherTier1": len(o_t1),
                "otherTier2": len(o_t2),
                "tier3Skipped": len(buckets["F3"]) + len(buckets["M3"]) + len(buckets["O3"]),
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
    import random as _random

    name = (member.get("name") or "").split()[0] or ""

    # Use locked template if available
    template = (event.get("invite_template") or "").strip()
    if template:
        return template.replace("{name}", name).strip()

    # Fallback: build from event fields
    if event and event.get("date"):
        date = event["date"]
        time = event.get("startTime", "")
        reveal_venue = event.get("revealVenue", False)
        venue = event.get("venue", "") if reveal_venue else ""
        address = event.get("address", "") if reveal_venue else ""
        event_label = (event.get("event_label") or "").strip()
        vibe_tag = (event.get("vibe_tag") or "").strip()

        closings = ["Tap in.", "Lmk.", "We on?", "Still on?", "Pull up."]
        closing = _random.choice(closings) if _random.random() < 0.4 else ""

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
            return f"{name}. You're on the list. Lmk."
        else:
            return "You're on the list. Lmk."


def handle_preview(body: dict, origin: str) -> dict:
    event_id = (body.get("eventId") or "").strip()
    capacity = int(body.get("capacity") or 0)
    female_pct = int(body.get("femalePercent") or 60)
    tier2_buffer = int(body.get("tier2BufferPct") or 30)

    if not event_id or capacity < 1:
        return _resp(400, {"ok": False, "error": "eventId and capacity required"}, origin)

    if not (0 <= female_pct <= 100):
        return _resp(400, {"ok": False, "error": "femalePercent must be 0-100"}, origin)

    members = _get_approved_members()
    result = _build_invite_list(members, capacity, female_pct, tier2_buffer)

    # Return full phone number in preview — admin needs to see who's on the list
    preview_members = []
    for m in result["members"]:
        preview_members.append({
            "phone": m.get("phone", ""),
            "name": m.get("name", ""),
            "gender": m.get("gender", "?"),
            "tier": m["_tier"],
            "attendedCount": int(m.get("attendedCount", 0)),
            "invitedCount": int(m.get("invitedCount", 0)),
        })

    return _resp(200, {
        "ok": True,
        "eventId": event_id,
        "summary": result["summary"],
        "members": preview_members,
    }, origin)


def handle_send(body: dict, origin: str) -> dict:
    if not body.get("confirmSend"):
        return _resp(400, {"ok": False, "error": "confirmSend: true required"}, origin)

    event_id = (body.get("eventId") or "").strip()
    capacity = int(body.get("capacity") or 0)
    female_pct = int(body.get("femalePercent") or 60)
    tier2_buffer = int(body.get("tier2BufferPct") or 30)
    removed_phones = body.get("removedPhones") or []

    if not event_id or capacity < 1:
        return _resp(400, {"ok": False, "error": "eventId and capacity required"}, origin)

    members_list = _get_approved_members()
    result = _build_invite_list(members_list, capacity, female_pct, tier2_buffer, removed_phones)

    # Fetch current event for SMS message
    current_event = _get_current_event()

    invites_t = _invites_table()
    members_t = members_table()
    now = _now_iso()

    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"
    sent = 0
    failed = 0

    for m in result["members"]:
        phone = m.get("phone", "")
        if not phone:
            continue

        try:
            invites_t.put_item(Item={
                "eventId": event_id,
                "phone": phone,
                "status": "INVITED",
                "gender": m.get("gender", ""),
                "tier": m["_tier"],
                "invitedAt": now,
            })

            members_t.update_item(
                Key={"phone": phone},
                UpdateExpression="SET invitedCount = if_not_exists(invitedCount, :zero) + :one, lastSeenAt = :now",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now},
            )

            if sms_enabled:
                message = _build_sms_message(m, current_event)
                send_sms(phone, message)
                sent += 1

        except Exception:
            failed += 1
            continue

    return _resp(200, {
        "ok": True,
        "eventId": event_id,
        "summary": result["summary"],
        "invitesWritten": len(result["members"]) - failed,
        "smsSent": sent,
        "failed": failed,
    }, origin)


def handler(event, context):
    try:
        method = _get_method(event).upper()
        headers = _get_headers(event)
        origin = headers.get("origin") or headers.get("Origin")

        if method == "OPTIONS":
            return _resp(200, {"ok": True}, origin)

        token = (headers.get("x-admin-token") or headers.get("X-Admin-Token") or "").strip()
        if not token or token != _admin_token():
            return _resp(401, {"ok": False, "error": "unauthorized"}, origin)

        path = event.get("path", "")
        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        body = json.loads(raw_body) if raw_body else {}

        if method == "POST" and path.endswith("/admin/invite/preview"):
            return handle_preview(body, origin)

        if method == "POST" and path.endswith("/admin/invite/send"):
            return handle_send(body, origin)

        return _resp(404, {"ok": False, "error": "not found"}, origin)

    except Exception as e:
        return _resp(500, {"ok": False, "error": str(e)}, None)
