import json
import os
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key as DKey

from member_store import get_member, normalize_phone
from sms_adapter import get_secret_string, send_sms

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
— "You sliding?"
— "Say less."

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
— If they reply with something ambiguous ("bet", "maybe", "might", "trying", "depends") — do not treat it as confirmed. Respond with a soft, direct confirm prompt:
  — "You in?"
  — "Want me to keep your spot?"
  — "Lock you in?"
— If they reply with a clear yes ("in", "there", "say less", "pulling up") — acknowledge briefly and move on. One line max.
— If they're being difficult — don't match the energy. One word or no response.
— Invite = 1 text. Reminder = 1 text. No follow-ups unless they reply.

You're not a bot. You're Jade. Keep it tight."""

# ── Intent classification ─────────────────────────────────────────────────────

# Clear confirmation — update invite to CONFIRMED
CONFIRMED_KEYWORDS = {
    "YES", "Y", "YEP", "YUP", "IN", "CONFIRMED", "THERE",
    "SAY LESS", "PULLING UP", "ON MY WAY", "OMW", "BET",
}

# Clear decline — update invite to DECLINED
DECLINED_KEYWORDS = {
    "NO", "N", "NOPE", "CANT", "CAN'T", "PASS", "DECLINE", "NOT COMING",
}

# Ambiguous — Jade responds with a soft confirm prompt, no status change
AMBIGUOUS_KEYWORDS = {
    "MAYBE", "MIGHT", "TRYING", "DEPENDS", "IDK", "I DON'T KNOW",
    "POSSIBLY", "HOPEFULLY", "WE'LL SEE", "NOT SURE",
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
    return _DDB.Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))


def _events_table():
    return _DDB.Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))


def _set_opt_out(phone: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _members_table().update_item(
        Key={"phone": phone},
        UpdateExpression="SET optOut = :t, optOutAt = :now, smsOptIn = :f, lastSeenAt = :now",
        ExpressionAttributeValues={":t": True, ":f": False, ":now": now},
    )


def _get_pending_invite(phone: str):
    """Return the most recent INVITED record for this phone, or None."""
    resp = _invites_table().query(
        IndexName="phone-index",
        KeyConditionExpression=DKey("phone").eq(phone),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    if items and items[0].get("status") == "INVITED":
        return items[0]
    return None


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
    """Build Jade's confirmation reply after a member confirms."""
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        parts = ["You're in."]
        if ev.get("date"):
            parts.append(f"See you {ev['date']}.")
        if ev.get("revealVenue") and ev.get("venue"):
            parts.append(f"{ev['venue']}.")
        elif ev.get("address"):
            parts.append(f"{ev['address']}.")
        return " ".join(parts)
    except Exception:
        return "You're in. See you there."


# ── Claude / Jade ─────────────────────────────────────────────────────────────

def _claude(message: str, mode: str = "general") -> str:
    """
    Call Claude (Jade). mode can be:
      'general'   — normal inbound message
      'ambiguous' — member replied with a soft/uncertain answer; Jade should
                    ask a direct yes/no confirm question
    """
    api_key = get_secret_string("rsvp/claude-api-key")

    user_content = message
    if mode == "ambiguous":
        user_content = (
            f"[CONTEXT: Member replied with an ambiguous response: '{message}'. "
            "They have not confirmed attendance. Respond with a single, short, "
            "direct yes/no confirm question. Examples: 'You in?' / "
            "'Want me to keep your spot?' / 'Lock you in?' Pick one naturally.]"
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


# ── Main handler ──────────────────────────────────────────────────────────────

def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        from_phone = normalize_phone(body.get("from") or "")
        text = (body.get("text") or "").strip()
        normalized = text.upper().strip()

        sms_enabled = (os.getenv("SEND_WELCOME_SMS", "false") or "").lower() == "true"

        # ── Opt-out ───────────────────────────────────────────────────────────
        if normalized in OPT_OUT_KEYWORDS:
            if from_phone:
                try:
                    _set_opt_out(from_phone)
                except Exception:
                    pass
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Gate: approved members only ───────────────────────────────────────
        member = get_member(from_phone)
        if not member or member.get("status") != "APPROVED":
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if member.get("optOut"):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Intent classification ─────────────────────────────────────────────

        # CONFIRMED
        if normalized in CONFIRMED_KEYWORDS:
            try:
                invite = _get_pending_invite(from_phone)
                if invite:
                    _update_invite_status(invite["eventId"], from_phone, "CONFIRMED")
                    if sms_enabled:
                        send_sms(from_phone, _build_confirmation_message(from_phone))
            except Exception:
                pass
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # DECLINED
        if normalized in DECLINED_KEYWORDS:
            try:
                invite = _get_pending_invite(from_phone)
                if invite:
                    _update_invite_status(invite["eventId"], from_phone, "DECLINED")
                    if sms_enabled:
                        send_sms(from_phone, "No worries. I'll keep you in mind.")
            except Exception:
                pass
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # AMBIGUOUS — Jade asks for a direct confirm
        if normalized in AMBIGUOUS_KEYWORDS:
            try:
                reply = _claude(text, mode="ambiguous")
                if sms_enabled and reply:
                    send_sms(from_phone, reply)
            except Exception:
                pass
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # GENERAL — everything else goes to Jade
        try:
            reply = _claude(text, mode="general")
            if sms_enabled and reply:
                send_sms(from_phone, reply)
        except Exception:
            pass

        return {"statusCode": 200, "body": json.dumps({"ok": True})}

    except Exception:
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
