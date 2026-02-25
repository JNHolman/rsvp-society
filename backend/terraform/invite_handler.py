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


# ── Reliability tier calculation ──
def calc_tier(member: Dict[str, Any]) -> int:
    """
    Auto-calculate reliability tier from attendance history.
    Returns 1 (reliable), 2 (inconsistent), or 3 (ghost).
    Requires at least 3 invites before assigning — defaults to 2 otherwise.
    Admin override (tierOverride) always wins.
    """
    # Admin override takes priority
    override = member.get("tierOverride")
    if override in (1, 2, 3):
        return int(override)

    invited = int(member.get("invitedCount", 0))
    attended = int(member.get("attendedCount", 0))

    # Not enough data — give benefit of the doubt
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
    """Scan all APPROVED members."""
    t = members_table()
    resp = t.scan(FilterExpression=Attr("status").eq("APPROVED"))
    members = resp.get("Items", [])
    # Attach calculated tier to each member
    for m in members:
        m["_tier"] = calc_tier(m)
    return members


def _build_invite_list(
    members: List[Dict[str, Any]],
    capacity: int,
    female_pct: int,
    tier2_buffer_pct: int = 30,
) -> Dict[str, Any]:
    """
    Build the invite list respecting gender ratio and reliability tiers.

    Logic:
    - Tier 1 fills first (guaranteed spots)
    - Tier 2 fills remaining slots with a buffer for ghosts
    - Tier 3 never auto-invited
    - Within each tier+gender bucket, shuffle randomly for fairness
    """
    male_pct = 100 - female_pct

    # Target counts
    target_f = round(capacity * female_pct / 100)
    target_m = capacity - target_f

    # Bucket members by gender + tier
    buckets: Dict[str, List] = {"F1": [], "F2": [], "F3": [], "M1": [], "M2": [], "M3": [], "O1": [], "O2": [], "O3": []}

    for m in members:
        gender = (m.get("gender") or "O").upper()
        if gender not in ("M", "F"):
            gender = "O"
        tier = m["_tier"]
        key = f"{gender}{tier}"
        if key in buckets:
            buckets[key].append(m)

    # Shuffle each bucket for fairness
    for bucket in buckets.values():
        random.shuffle(bucket)

    def fill_gender(gender: str, target: int) -> Tuple[List, List, int]:
        """Fill target slots for a gender. Returns (tier1_list, tier2_list, buffer_count)."""
        t1 = buckets.get(f"{gender}1", [])
        t2 = buckets.get(f"{gender}2", [])

        # Tier 1 fills first
        tier1_invited = t1[:target]
        remaining = target - len(tier1_invited)

        # Tier 2 fills the rest with ghost buffer
        if remaining > 0:
            buffer_multiplier = 1 + (tier2_buffer_pct / 100)
            tier2_needed = math.ceil(remaining * buffer_multiplier)
            tier2_invited = t2[:tier2_needed]
        else:
            tier2_invited = []
            tier2_needed = 0

        return tier1_invited, tier2_invited, len(tier2_invited)

    f_t1, f_t2, f_buffer = fill_gender("F", target_f)
    m_t1, m_t2, m_buffer = fill_gender("M", target_m)

    # Other gender gets any remaining Tier 1/2 spots (overflow)
    o_t1 = buckets.get("O1", [])
    o_t2 = buckets.get("O2", [])

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


def handle_preview(body: dict, origin: str) -> dict:
    """
    Preview who would be invited — no SMS sent, no DB writes.
    POST /admin/invite/preview
    Body: { eventId, capacity, femalePercent, tier2BufferPct? }
    """
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

    # Return summary + preview of members (phone masked for safety)
    preview_members = []
    for m in result["members"]:
        phone = m.get("phone", "")
        preview_members.append({
            "phone": phone[:6] + "****" + phone[-2:] if len(phone) > 8 else phone,
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
    """
    Execute the invite blast.
    POST /admin/invite/send
    Body: { eventId, capacity, femalePercent, tier2BufferPct?, confirmSend: true }
    Writes to EventInvites table + fires Quo SMS.
    """
    if not body.get("confirmSend"):
        return _resp(400, {"ok": False, "error": "confirmSend: true required"}, origin)

    event_id = (body.get("eventId") or "").strip()
    capacity = int(body.get("capacity") or 0)
    female_pct = int(body.get("femalePercent") or 60)
    tier2_buffer = int(body.get("tier2BufferPct") or 30)

    if not event_id or capacity < 1:
        return _resp(400, {"ok": False, "error": "eventId and capacity required"}, origin)

    members_list = _get_approved_members()
    result = _build_invite_list(members_list, capacity, female_pct, tier2_buffer)

    invites_t = _invites_table()
    members_t = members_table()
    now = _now_iso()

    sms_enabled = (os.getenv("SEND_WELCOME_SMS", "false") or "").lower() == "true"
    sent = 0
    failed = 0

    for m in result["members"]:
        phone = m.get("phone", "")
        if not phone:
            continue

        # Write to EventInvites table
        try:
            invites_t.put_item(Item={
                "eventId": event_id,
                "phone": phone,
                "status": "INVITED",
                "gender": m.get("gender", ""),
                "tier": m["_tier"],
                "invitedAt": now,
            })

            # Increment invitedCount on member record
            members_t.update_item(
                Key={"phone": phone},
                UpdateExpression="SET invitedCount = if_not_exists(invitedCount, :zero) + :one, lastSeenAt = :now",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now},
            )

            # Send SMS if Quo is live
            if sms_enabled:
                name = (m.get("name") or "").split()[0] or "there"
                send_sms(phone, f"Hey {name}, you're invited to the next RSVP Society event. Reply YES to confirm your spot. Reply STOP to opt out.")
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

        # Auth
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
