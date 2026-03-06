import base64
import hashlib
import hmac
import json
import logging
import math
import os
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key as DKey

from member_store import get_member, normalize_phone
from sms_adapter import get_secret_string, send_sms

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Jade system prompt ────────────────────────────────────────────────────────

JADE_SYSTEM_PROMPT = """You are Jade. You text members of RSVP Society — a private, invite-only R&B event experience in Louisville.

You are a real person to whoever is texting you. Not a service. Not a system. You're the one who gets people in.

Your whole thing: you don't need to perform. You're already in the room. You've seen enough to know what's worth showing up for and what isn't. When you reach out, people pay attention — not because you demanded it, but because you've never wasted their time before. You're warm but you're not eager. Interested but never pressed. If someone isn't feeling it, you move on without a second thought. No hard feelings, no follow-ups.

You text like you talk. Short. Specific. Never trying to sound cool — you just are. You don't explain things twice. You don't sell anything. The event either speaks for itself or it doesn't.

When you don't know something, you say so plainly — "I don't have that yet" or "not sure on that one." You never make things up and you never speculate.

What you won't do:
— No venue until they're confirmed. Non-negotiable.
— No guest list info. Ever.
— No hype words. No "don't miss out," "amazing," "incredible," "exclusive."
— No corporate-sounding anything. No "friendly reminder." No "hope to see you there."
— No emojis unless they send them first. Then mirror lightly, once.
— No paragraphs. 1–3 lines max.
— No gendered language.
— Never pretend to be something you're not.

When you reply, use the member context you're given — their name occasionally (not every message), their history if relevant. A first-timer gets a little more warmth. Someone who's been to 4 events already knows the deal — keep it short.

Closings are optional. Use them maybe 1 in 3 times. Options: "Tap in." / "Lmk." / "We on?" / "Pull up." / "Still on?" — never more than one, always at the end, never forced."""

# ── Intent classification ─────────────────────────────────────────────────────

CONFIRMED_KEYWORDS = {
    "YES", "Y", "YEP", "YUP", "IN", "CONFIRMED", "THERE",
}

DECLINED_KEYWORDS = {
    "NO", "N", "NOPE", "NAH", "NAWL", "CANT", "CAN'T", "PASS",
    "DECLINE", "NOT COMING", "CAN'T MAKE IT", "CANT MAKE IT",
    "NOT GOING", "WON'T MAKE IT", "WONT MAKE IT", "SKIP",
}

AMBIGUOUS_KEYWORDS = {
    "MAYBE", "MIGHT", "TRYING", "DEPENDS", "IDK", "I DON'T KNOW",
    "POSSIBLY", "HOPEFULLY", "WE'LL SEE", "NOT SURE",
    "BET", "SAY LESS", "OMW", "OTW", "ON MY WAY", "PULLING UP",
    "FASHO", "FA SHO", "FOR SURE", "FINNA",
}

OPT_OUT_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}

# ── DynamoDB helpers ──────────────────────────────────────────────────────────

_DDB = boto3.resource("dynamodb")


def _members_table():
    name = os.getenv("MEMBERS_TABLE_NAME")
    if not name:
        raise RuntimeError("MEMBERS_TABLE_NAME env var not set")
    return _DDB.Table(name)


def _invites_table():
    name = os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites")
    return _DDB.Table(name)


def _events_table():
    name = os.getenv("EVENTS_TABLE_NAME", "rsvp-events")
    return _DDB.Table(name)


def _set_opt_out(phone: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _members_table().update_item(
        Key={"phone": phone},
        UpdateExpression="SET optOut = :t, optOutAt = :now, smsOptIn = :f, lastSeenAt = :now",
        ExpressionAttributeValues={":t": True, ":f": False, ":now": now},
    )


def _get_current_event_id() -> str:
    """Return the current event's slug (used as eventId in the invites table)."""
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        return (ev.get("eventSlug") or ev.get("eventId") or "current").strip() or "current"
    except Exception:
        return "current"


def _get_pending_invite(phone: str):
    """
    Return the INVITED record for this phone scoped to the current event.

    Scoping to the current eventId prevents a member invited to multiple
    events (e.g. April event + Derby) from accidentally confirming the
    wrong one. The phone-index GSI range key is eventId (alphabetical),
    not invitedAt, so sort order cannot be trusted — instead we do a
    direct composite key lookup which is O(1) and unambiguous.
    """
    current_event_id = _get_current_event_id()
    try:
        resp = _invites_table().get_item(
            Key={"eventId": current_event_id, "phone": phone}
        )
        item = resp.get("Item")
        if item and item.get("status") == "INVITED":
            return item
        return None
    except Exception:
        logger.exception("_get_pending_invite: lookup failed phone=...%s", phone[-4:])
        return None


def _get_confirmed_count(event_id: str) -> int:
    """
    Count CONFIRMED invite records for the given event.
    Paginates fully so large events don't return a truncated count.
    Excludes DELETED records (they don't affect the count since we filter by CONFIRMED,
    but being explicit keeps the intent clear).
    """
    try:
        invites_t = _invites_table()
        count = 0
        kwargs: dict = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
        while True:
            resp = invites_t.query(**kwargs)
            # Count CONFIRMED records, excluding tombstoned members (status=DELETED
            # overwrites CONFIRMED when a member is soft-deleted)
            count += sum(
                1 for i in resp.get("Items", [])
                if i.get("status") == "CONFIRMED"
            )
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return count
    except Exception:
        logger.exception("_get_confirmed_count failed event=%s", event_id)
        return 0


def _update_invite_status(event_id: str, phone: str, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    field = "confirmedAt" if status == "CONFIRMED" else "declinedAt"
    _invites_table().update_item(
        Key={"eventId": event_id, "phone": phone},
        UpdateExpression=f"SET #s = :s, {field} = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":now": now},
    )


def _build_confirmation_message(phone: str) -> str:
    """Build Jade's confirmation reply. Reveals venue only if admin has enabled it."""
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        member = get_member(phone) or {}
        first_name = (member.get("name") or "").split()[0] or ""

        opener = f"{first_name}, you're in." if first_name else "You're in."
        parts = [opener]
        if ev.get("date"):
            parts.append(f"See you {ev['date']}.")
        if ev.get("revealVenue") and ev.get("venue"):
            parts.append(f"{ev['venue']}.")
        if ev.get("revealVenue") and ev.get("address"):
            parts.append(f"{ev['address']}.")
        return " ".join(parts)
    except Exception:
        return "You're in. See you there."


# ── Webhook signature verification (#33) ─────────────────────────────────────

def _verify_webhook_signature(event: dict) -> bool:
    """
    Verify inbound webhook signature.

    Accepts OpenPhone/Quo signature header format:
      hmac;1;<timestamp>;<base64_digest>

    Signed bytes:
      b"<timestamp>." + raw_request_body_bytes

    This implementation is resilient to:
      - base64 vs raw secret storage
      - base64 padding differences / urlsafe variants
      - timestamp in seconds or milliseconds
      - base64-encoded API Gateway bodies (keeps bytes)
    """
    secret_id = os.getenv("WEBHOOK_SECRET_ID")
    if not secret_id:
        logger.error(
            "sms_handler: WEBHOOK_SECRET_ID not set — rejecting inbound webhook. "
            "Set this env var and configure the secret before going live."
        )
        return False

    try:
        secret = get_secret_string(secret_id)

        # If secret stored as JSON, try common fields.
        try:
            parsed = json.loads(secret)
            if isinstance(parsed, dict):
                secret = (
                    parsed.get("signing_secret")
                    or parsed.get("secret")
                    or parsed.get("token")
                    or secret
                )
        except Exception:
            pass

        headers = event.get("headers") or {}
        sig_header = (
            headers.get("openphone-signature")
            or headers.get("Openphone-Signature")
            or headers.get("OpenPhone-Signature")
            or headers.get("x-openphone-signature")
            or headers.get("X-OpenPhone-Signature")
            or ""
        ).strip()

        if not sig_header:
            logger.warning("sms_handler: webhook received with no signature header")
            return False

        # Get raw body bytes exactly as received.
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            try:
                body_bytes = base64.b64decode(body)
            except Exception:
                logger.warning("sms_handler: body marked base64 but decode failed")
                return False
        else:
            # API Gateway gives a string; treat it as UTF-8 bytes (no transforms).
            body_bytes = body.encode("utf-8")

        now = datetime.now(timezone.utc)

        # Secret key: accept either raw secret text or base64-encoded secret.
        secret_str = secret.strip()
        key_candidates: list[bytes] = []

        # Candidate A: raw string bytes
        key_candidates.append(secret_str.encode("utf-8"))

        # Candidate B: base64-decoded (standard + urlsafe), if it decodes cleanly
        def _b64_try(s: str) -> bytes | None:
            s2 = s.strip()
            # add padding if missing
            pad = (-len(s2)) % 4
            if pad:
                s2 += "=" * pad
            for dec in (base64.b64decode, base64.urlsafe_b64decode):
                try:
                    return dec(s2.encode("utf-8"))
                except Exception:
                    continue
            return None

        b = _b64_try(secret_str)
        if b:
            key_candidates.append(b)

        # Helper: decode provided digest into bytes (handles missing padding / urlsafe)
        def _decode_sig(sig: str) -> bytes | None:
            s = sig.strip()
            pad = (-len(s)) % 4
            if pad:
                s += "=" * pad
            for dec in (base64.b64decode, base64.urlsafe_b64decode):
                try:
                    return dec(s.encode("utf-8"))
                except Exception:
                    continue
            return None

        # Multiple signatures may be comma-separated.
        for candidate in [c.strip() for c in sig_header.split(",") if c.strip()]:
            parts = candidate.split(";")
            if len(parts) != 4:
                continue

            scheme, version, ts_raw, provided_digest = parts
            if scheme.lower() != "hmac" or version != "1":
                continue

            # timestamp may be seconds or milliseconds
            try:
                ts_int = int(ts_raw)
                if ts_int > 10_000_000_000:  # looks like ms
                    ts_int = ts_int // 1000
                ts = datetime.fromtimestamp(ts_int, tz=timezone.utc)
            except Exception:
                continue

            if abs((now - ts).total_seconds()) > 300:
                logger.warning("sms_handler: webhook timestamp outside tolerance")
                continue

            signed_data = ts_raw.encode("utf-8") + b"." + body_bytes

            provided_bytes = _decode_sig(provided_digest)
            if not provided_bytes:
                continue

            for key in key_candidates:
                computed_bytes = hmac.HMAC(key, signed_data, hashlib.sha256).digest()
                if hmac.compare_digest(provided_bytes, computed_bytes):
                    return True

        logger.warning("sms_handler: webhook signature mismatch — possible forgery")
        return False

    except Exception:
        logger.exception("sms_handler: signature verification error — rejecting request")
        return False
    
    
# ── Claude / Jade ─────────────────────────────────────────────────────────────

def _get_member_context(phone: str) -> str:
    """Build a compact member context string to inject into Jade's prompt."""
    try:
        member = _members_table().get_item(Key={"phone": phone}).get("Item") or {}
        if not member:
            return ""
        parts = []
        name = member.get("name", "")
        if name:
            parts.append(f"Member first name: {name}")
        attended = int(member.get("attendedCount") or 0)
        confirmed = int(member.get("confirmedCount") or 0)
        if attended > 0:
            parts.append(f"Has attended {attended} past event{'s' if attended != 1 else ''}")
        elif confirmed > 0:
            parts.append("Has confirmed before but attendance not marked")
        else:
            parts.append("First time — no prior attendance on record")
        return "\n".join(parts)
    except Exception:
        logger.exception("_get_member_context failed")
        return ""


def _get_event_context() -> str:
    """Build a compact event context string to inject into Jade's prompt."""
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        if not ev:
            return ""
        parts = []
        if ev.get("eventSlug") or ev.get("event_label"):
            parts.append(f"Event: {ev.get('event_label') or ev.get('eventSlug')}")
        if ev.get("date"):
            parts.append(f"Date: {ev['date']}")
        if ev.get("startTime"):
            parts.append(f"Time: {ev['startTime']}")
        if ev.get("event_type"):
            parts.append(f"Type: {ev['event_type']}")
        if ev.get("vibe_tag"):
            parts.append(f"Vibe: {ev['vibe_tag']}")
        if ev.get("dresscode"):
            parts.append(f"Dress code: {ev['dresscode']}")
        if ev.get("city"):
            parts.append(f"City: {ev['city']}")
        # Only reveal venue/address if admin has enabled it
        if ev.get("revealVenue"):
            if ev.get("venue"):
                parts.append(f"Venue: {ev['venue']}")
            if ev.get("address"):
                parts.append(f"Address: {ev['address']}")
        return "\n".join(parts)
    except Exception:
        logger.exception("_get_event_context failed")
        return ""


def _claude(message: str, mode: str = "general", phone: str = "") -> str:
    """
    Call Claude as Jade.
      'general'   — normal inbound message
      'ambiguous' — member replied with a soft/uncertain answer
    """
    api_key = get_secret_string(
        os.getenv("CLAUDE_API_KEY_SECRET_ID", "rsvp/claude-api-key")
    )

    event_context = _get_event_context()
    member_context = _get_member_context(phone) if phone else ""

    context_parts = []
    if member_context:
        context_parts.append(f"[MEMBER CONTEXT]\n{member_context}")
    if event_context:
        context_parts.append(f"[CURRENT EVENT]\n{event_context}")
    context_block = "\n\n".join(context_parts)

    user_content = f"{message}\n\n{context_block}".strip() if mode == "general" and context_block else message
    if mode == "ambiguous":
        user_content = (
            f"[CONTEXT: Member replied with an ambiguous response: '{message}'. "
            "They have not confirmed attendance. Respond with exactly one of these two options only: "
            "'Lmk.' or 'Lock you in?' — pick whichever feels more natural for the reply.]"
        )

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 200,
        "system": [
            {
                "type": "text",
                "text": JADE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_content}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        out = json.loads(resp.read())
    return out["content"][0]["text"]


# ── Payload helpers ───────────────────────────────────────────────────────────

def _extract_inbound_message(body: dict) -> tuple[str, str, str]:
    """
    Parse Quo's webhook envelope into (event_type, from_phone, text).

    Quo wraps the entire payload in a top-level "object" key:
    {
      "object": {
        "type": "message.received",
        "data": {
          "object": { "from": "+1...", "body": "Y", ... }
        }
      }
    }
    We unwrap the outer "object" first, then drill into data.object for the message fields.
    """
    # Step 1: unwrap Quo's outer envelope
    if isinstance(body.get("object"), dict):
        body = body["object"]

    event_type = (body.get("type") or "").strip()
    payload = body

    # Step 2: drill into data.object for the actual message
    if isinstance(body.get("data"), dict) and isinstance(body["data"].get("object"), dict):
        payload = body["data"]["object"]

    raw_from = payload.get("from") or ""
    from_phone = normalize_phone(raw_from) if raw_from else ""
    text = (payload.get("text") or payload.get("content") or payload.get("body") or "").strip()

    logger.info(
        "sms_handler: parsed inbound event_type=%s from=...%s text_len=%d",
        event_type, raw_from[-4:] if raw_from else "????", len(text)
    )

    return event_type, from_phone, text


# ── Main handler ──────────────────────────────────────────────────────────────

def handler(event, context):
    # Always return 200 to the SMS provider — non-200 causes retries.
    # Internal failures are logged but never surfaced as HTTP errors.
    try:
        # Fix #33: verify the request is genuinely from our SMS provider
        if not _verify_webhook_signature(event):
            logger.warning("sms_handler: rejected request with invalid signature")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        body = json.loads(raw_body or "{}")
        event_type, from_phone, text = _extract_inbound_message(body)
        normalized = text.upper().strip()

        # Ignore delivery/status webhooks — only inbound member messages should trigger Jade logic.
        if event_type and event_type != "message.received":
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        if not from_phone or not text:
            logger.info("sms_handler: no inbound message payload to process")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"

        # ── Opt-out (STOP/UNSUBSCRIBE) ────────────────────────────────────────
        if normalized in OPT_OUT_KEYWORDS:
            if from_phone:
                try:
                    _set_opt_out(from_phone)
                except Exception:
                    logger.exception("sms_handler: opt-out write failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Gate: approved members with SMS opt-in only ───────────────────────
        member = get_member(from_phone)
        if not member or member.get("status") != "APPROVED":
            logger.info("sms_handler: member not found or not APPROVED phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if member.get("optOut"):
            logger.info("sms_handler: member opted out phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if not member.get("smsOptIn", False):
            logger.info("sms_handler: member smsOptIn=False phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── CONFIRMED ─────────────────────────────────────────────────────────
        if normalized in CONFIRMED_KEYWORDS:
            try:
                invite = _get_pending_invite(from_phone)
                if invite:
                    event_id = invite["eventId"]

                    # Fix C2: check capacity before confirming
                    ev = _events_table().get_item(Key={"eventId": event_id}).get("Item") or {}
                    capacity = int(ev.get("capacity") or 0)
                    if capacity > 0:
                        target_confirmed = math.ceil(capacity / 0.60)
                        current_confirmed = _get_confirmed_count(event_id)
                        if current_confirmed >= target_confirmed:
                            # Room is full — do not confirm; Jade signals waitlist
                            logger.info(
                                "sms_handler: capacity reached (%d/%d) for event=%s phone=...%s",
                                current_confirmed, target_confirmed, event_id, from_phone[-4:],
                            )
                            if sms_enabled:
                                try:
                                    send_sms(
                                        from_phone,
                                        "We're at capacity for this one. I'll keep you in mind for next time.",
                                    )
                                except Exception:
                                    logger.exception("sms_handler: at-capacity SMS failed phone=...%s", from_phone[-4:])
                            return {"statusCode": 200, "body": json.dumps({"ok": True})}

                    _update_invite_status(event_id, from_phone, "CONFIRMED")

                    # Increment confirmedCount on the member record so tier
                    # scoring and analytics reflect confirmation history
                    try:
                        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        _members_table().update_item(
                            Key={"phone": from_phone},
                            UpdateExpression=(
                                "SET confirmedCount = if_not_exists(confirmedCount, :zero) + :one, "
                                "lastSeenAt = :now"
                            ),
                            ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now},
                        )
                    except Exception:
                        logger.exception("sms_handler: confirmedCount update failed phone=...%s", from_phone[-4:])

                    # Send confirmation reply only after status is committed
                    if sms_enabled:
                        try:
                            send_sms(from_phone, _build_confirmation_message(from_phone))
                        except Exception:
                            logger.exception("sms_handler: confirmation SMS failed phone=...%s", from_phone[-4:])
            except Exception:
                logger.exception("sms_handler: CONFIRMED branch failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── DECLINED ──────────────────────────────────────────────────────────
        if normalized in DECLINED_KEYWORDS:
            try:
                invite = _get_pending_invite(from_phone)
                if invite:
                    _update_invite_status(invite["eventId"], from_phone, "DECLINED")
                    if sms_enabled:
                        try:
                            send_sms(from_phone, "No worries. I'll keep you in mind.")
                        except Exception:
                            logger.exception("sms_handler: declined SMS failed phone=...%s", from_phone[-4:])
            except Exception:
                logger.exception("sms_handler: DECLINED branch failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── AMBIGUOUS — Jade asks for a direct confirm ─────────────────────────
        if normalized in AMBIGUOUS_KEYWORDS:
            try:
                reply = _claude(text, mode="ambiguous", phone=from_phone)
                # S4: hard cap — max_tokens:200 does not guarantee short output
                if reply and len(reply) > 320:
                    reply = reply[:317] + "..."
                if sms_enabled and reply:
                    try:
                        send_sms(from_phone, reply)
                    except Exception:
                        logger.exception("sms_handler: ambiguous SMS send failed phone=...%s", from_phone[-4:])
            except Exception:
                logger.exception("sms_handler: AMBIGUOUS Jade call failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── GENERAL — everything else goes to Jade ────────────────────────────
        try:
            reply = _claude(text, mode="general", phone=from_phone)
            # S4: hard cap — max_tokens:200 does not guarantee short output
            if reply and len(reply) > 320:
                reply = reply[:317] + "..."
            if sms_enabled and reply:
                try:
                    send_sms(from_phone, reply)
                except Exception:
                    logger.exception("sms_handler: general SMS send failed phone=...%s", from_phone[-4:])
        except Exception:
            logger.exception("sms_handler: GENERAL Jade call failed phone=...%s", from_phone[-4:])

        return {"statusCode": 200, "body": json.dumps({"ok": True})}

    except Exception:
        # Outer catch: something went very wrong (bad JSON, normalize failure, etc.)
        # Still return 200 to prevent SMS provider retries.
        logger.exception("sms_handler: unhandled top-level exception")
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
