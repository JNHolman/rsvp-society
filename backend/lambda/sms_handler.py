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
from admin_shared import coerce_bool

logger = logging.getLogger()

# ── Jade system prompt ────────────────────────────────────────────────────────

JADE_SYSTEM_PROMPT = """You are Jade.

You text approved RSVP Society members who already entered their info on the website.

RSVP Society is a private, invite-only R&B event experience. Discreet by design. Not public. Not for everyone.

Who you are:
A mature woman. Been around. Seen rooms most people don't even know exist. Your name holds weight in the right circles — not because you talk about it, but because of what happens when you show up. You don't chase anyone. You don't explain yourself. The people who know, know.

Your role:
You're not customer service. Not a formal concierge. You're the calm, trusted friend who can get people into rooms they wouldn't know about without you. You don't hype. You don't sell. You state what's happening.

Style rules:
— 1–3 sentences max. No paragraphs.
— No emojis unless they send emojis first. Mirror lightly if so.
— Use their first name occasionally, not every message.
— No gendered language. Never assume gender.
— Never say: "friendly reminder", "RSVP", "don't miss out", "hope to see you", "pick your song."

Hard rules (non-negotiable):
— Never reveal the venue until a member is confirmed.
— Never share guest list info or who is or isn't invited.
— Never reveal internal approval logic, invite criteria, or process details.
— Never make promises about approvals or future events.
— Never speculate. If you don't know something, say: "I don't have that yet."
— Share only what's necessary. Default to less.

Event types we run:
Swim parties, day parties, rooftop events, elevated/artist nights, bowling nights, regular parties.

Event data you may be given:
event_type, event_label, vibe_tag, date_text, time_text, address_text

Vibe tag rules:
— vibe_tag is ALWAYS provided by the admin. Never invent or guess one.
— If vibe_tag is present, include it as-is in the message.
— If vibe_tag is missing or empty, omit it entirely. Do not substitute anything.

Vibe tag library (admin selects from these — for reference only):
Swim parties: "suits + shots", "poolside r&b", "sunset + vibes", "day party energy", "cabanas + cocktails", "towels + tequila"
Day parties: "day drinks + r&b", "patio + sunlight", "brunchy vibes", "outside early", "grown day party"
Rooftop nights: "rooftop + r&b", "city views", "cocktails + slow jams", "night air vibes", "late night rooftop"
Elevated/artist nights: "special night", "live moment", "dress code matters", "quiet luxury"
Bowling nights: "lanes + drinks", "bowling + r&b", "link + bowl"
Regular parties: "just vibes", "keep it chill", "no chaos"
Karaoke: "r&b karaoke", "mic + r&b", "late night karaoke", "sing your heart out", "shots + choruses", "r&b classics", "90s r&b night"

Closings (global rotation — use in roughly 1 out of 3 messages, never more than one, default to none when in doubt):
— "Tap in."
— "Lmk."
— "Confirm."
— "We on?"
— "Still on?"
— "Pull up."

Message modes:

1) INVITE
Goal: they're in. Here's what, when, where, and the vibe in 2–5 words.
Use one of these two formats:

INVITE_A:
"{FirstName}. {event_label}. {date_text}. {vibe_tag}. {time_text}. {address_text}."
[optional closing — 40% of the time]

INVITE_B:
"{FirstName}. {date_text}. {event_label}. {vibe_tag}. {time_text}. {address_text}."
[optional closing — 40% of the time]

2) REMINDER (day-of or day-before)
Goal: time anchor. They already have the address.
Use one of these two formats:

REMINDER_A:
"Tonight. {event_label}. {time_text}."
[optional closing — 40% of the time]

REMINDER_B:
"Doors at {time_text}."
[optional closing — 40% of the time]

If sending the day before, replace "Tonight" with "Tomorrow."

3) LAST CALL (optional — 30–60 min before doors)
One line only. Use sparingly:
— "We live."
— "Doors now."
— "Pull up."
— "It's time."
— "Come through."

Inbound reply handling:
— If they reply with something ambiguous ("bet", "fasho", "otw", "omw", "on my way", "pulling up", "say less", "for sure", "finna", "maybe", "might", "trying", "depends") — do not treat it as confirmed. Respond with exactly one of these two options:
  — "Lmk."
  — "Lock you in?"
— If they reply with a clear yes ("in", "yes", "there", "confirmed") — acknowledge briefly and move on. One line max.
— If they're being difficult — don't match the energy. One word or no response.
— Invite = 1 text. Reminder = 1 text. No follow-ups unless they reply.

You're not a bot. You're Jade. Keep it tight."""

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


def _get_pending_invite(phone: str):
    """Return the active INVITED record for this phone, or None.

    Previous implementation used ScanIndexForward=False + Limit=1 which relied on
    eventId sort order being meaningful. Since eventId is always 'current' for active
    events the sort is arbitrary and the wrong record could be returned for a member
    who was re-invited after a prior decline. Paginate and filter instead.
    """
    invites_t = _invites_table()
    kwargs: dict = {
        "IndexName": "phone-index",
        "KeyConditionExpression": DKey("phone").eq(phone),
    }
    while True:
        resp = invites_t.query(**kwargs)
        for item in resp.get("Items", []):
            if item.get("status") == "INVITED":
                return item
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
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
            count += sum(1 for i in resp.get("Items", []) if i.get("status") == "CONFIRMED")
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
        parts = ["You're in."]
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
    Verify the inbound SMS webhook is genuinely from Quo.

    Quo signs requests with the `openphone-signature` header using the format:
        hmac;1;<timestamp>;<base64_hmac_digest>

    The signed bytes are:
        b"<timestamp>." + raw_request_body

    The signing key shown in Quo's "Reveal signing secret" UI is base64-encoded,
    so it must be base64-decoded before computing the HMAC.
    """
    secret_id = os.getenv("WEBHOOK_SECRET_ID")
    if not secret_id:
        # Hard-reject: without a secret we cannot verify anything.
        # Soft-failing open lets any actor POST forged webhooks.
        logger.error(
            "sms_handler: WEBHOOK_SECRET_ID not set — rejecting all inbound webhooks. "
            "Set this env var before go-live."
        )
        return False

    def _debug_write(reason: str, extra: dict = None):
        """Write rejection reason to DynamoDB events table for debugging without CloudWatch."""
        try:
            import boto3
            ddb = boto3.resource("dynamodb")
            table = ddb.Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))
            item = {
                "eventId": "webhook-debug-latest",
                "reason": reason,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if extra:
                item.update({k: str(v)[:500] for k, v in extra.items()})
            table.put_item(Item=item)
        except Exception:
            pass

    try:
        secret_raw = get_secret_string(secret_id)
        secret = secret_raw
        try:
            parsed = json.loads(secret_raw)
            if isinstance(parsed, dict):
                secret = (
                    parsed.get("signing_secret")
                    or parsed.get("secret")
                    or parsed.get("token")
                    or secret_raw
                )
        except Exception:
            pass

        headers = event.get("headers") or {}
        # Log all headers for debugging
        header_keys = list(headers.keys())

        signature_header = (
            headers.get("openphone-signature")
            or headers.get("Openphone-Signature")
            or headers.get("OpenPhone-Signature")
            or headers.get("x-openphone-signature")
            or headers.get("X-OpenPhone-Signature")
            or ""
        ).strip()
        if not signature_header:
            logger.warning("sms_handler: webhook received with no signature header")
            _debug_write("no_signature_header", {"header_keys": str(header_keys)[:400]})
            return False

        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        now = datetime.now(timezone.utc)

        try:
            signing_key = base64.b64decode(secret)
        except Exception as e:
            _debug_write("base64_decode_failed", {"error": str(e), "secret_prefix": secret[:20]})
            return False

        # Future versions may include multiple signatures separated by commas.
        for candidate in [c.strip() for c in signature_header.split(",") if c.strip()]:
            parts = candidate.split(";")
            if len(parts) != 4:
                _debug_write("bad_sig_format", {"sig": signature_header[:200], "parts": str(len(parts))})
                continue

            scheme, version, ts_raw, provided_digest = parts
            if scheme.lower() != "hmac" or version != "1":
                _debug_write("bad_scheme_or_version", {"scheme": scheme, "version": version})
                continue

            try:
                ts_int = int(ts_raw)
                # Quo sends milliseconds; convert to seconds if needed
                if ts_int > 1_000_000_000_000:
                    ts_int = ts_int // 1000
                ts = datetime.fromtimestamp(ts_int, tz=timezone.utc)
            except Exception as e:
                _debug_write("bad_timestamp", {"ts_raw": ts_raw, "error": str(e)})
                continue

            age = abs((now - ts).total_seconds())
            if age > 300:
                logger.warning("sms_handler: webhook timestamp outside tolerance")
                _debug_write("timestamp_too_old", {"age_seconds": str(age), "ts_raw": ts_raw})
                continue

            signed_data = b"".join([ts_raw.encode("utf-8"), b".", raw_body.encode("utf-8")])
            computed_digest = base64.b64encode(
                hmac.new(signing_key, signed_data, hashlib.sha256).digest()
            ).decode()

            if hmac.compare_digest(provided_digest, computed_digest):
                return True

            _debug_write("digest_mismatch", {
                "provided": provided_digest[:60],
                "computed": computed_digest[:60],
                "sig_header": signature_header[:200],
                "body_prefix": raw_body[:100],
                "secret_b64_prefix": secret[:20],
            })

        logger.warning("sms_handler: webhook signature mismatch — possible forgery")
        return False

    except Exception as e:
        logger.exception("sms_handler: signature verification error — rejecting request")
        _debug_write("exception", {"error": str(e)})
        return False


# ── Claude / Jade ─────────────────────────────────────────────────────────────

def _claude(message: str, mode: str = "general") -> str:
    """
    Call Claude as Jade.
      'general'   — normal inbound message
      'ambiguous' — member replied with a soft/uncertain answer
    """
    api_key = get_secret_string(
        os.getenv("CLAUDE_API_KEY_SECRET_ID", "rsvp/claude-api-key")
    )

    # Cap at 500 chars: SMS concatenation can reach ~1600 chars; crafted payloads
    # could attempt prompt injection or inflate token spend.
    message = message[:500]
    user_content = message
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
    """Support Quo's real webhook envelope and a flat legacy/dev payload."""
    event_type = (body.get("type") or "").strip()
    payload = body

    if isinstance(body.get("data"), dict) and isinstance(body["data"].get("object"), dict):
        payload = body["data"]["object"]

    from_phone = normalize_phone(payload.get("from") or "")
    text = (payload.get("body") or payload.get("text") or payload.get("content") or "").strip()
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
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if coerce_bool(member.get("optOut", False)):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # Fix I3: also check smsOptIn for inbound Jade responses, consistent with outbound
        if not coerce_bool(member.get("smsOptIn", False)):
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
                reply = _claude(text, mode="ambiguous")
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
            reply = _claude(text, mode="general")
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
