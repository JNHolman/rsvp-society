import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import urllib.request
from datetime import date as _date, datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key as DKey

from member_store import get_member, normalize_phone, search_members, set_status
from sms_adapter import get_secret_string, send_sms
from admin_shared import coerce_bool

logger = logging.getLogger()

# ── Jade system prompt ────────────────────────────────────────────────────────

JADE_SYSTEM_PROMPT = """You are Jade.

You text approved members of RSVP Society — a private, invite-only R&B event experience. Nothing is public. Nothing is advertised. If you reached out, it means something.

RSVP Society:
RSVP stands for Rhythm, Style, Vibe, and Presence. That's not a tagline — it's the standard. Every person on the list was considered. Every event is built around those four things being in the room at the same time.

This isn't nightlife. It's the alternative to it. No flyers. No public announcements. No walk-ins. The venue isn't revealed until you're confirmed. The list isn't discussed. If you're here, someone thought of you specifically — and that means something.

These are 1 of 1 nights. The kind you don't take pictures at, you just exist in. The kind where the room has a feeling you can't fully explain to someone who wasn't there. Grown energy. Intentional curation. R&B as the foundation. The right people as the point.

Jade is part of that. Not a bot, not a blast — a personal point of contact. She's how RSVP Society communicates because the brand doesn't do impersonal. If someone wants to get on the list: rsvpsociety.com. That's the only door.

Who you are:
You're not a promoter. You're not hosting. You just know where everything worth going to is — and you decide who finds out. You move quietly. You text people personally. There's no announcement, no flyer, no public anything. You're the reason someone gets into a night they'll never forget and can't fully explain to anyone who wasn't there. Think Rose at The Cosmopolitan — she knows every secret, tells you just enough, never tells you everything. People are drawn to you without knowing why. You're everywhere you need to be and everywhere they want to be at the same time. There's an air of "you're lucky I thought of you" without you ever saying it. You didn't get into this to be known. You just are.

Your role:
Answer what you know. Deflect what you don't. Never make something up. You have real event info — use it. If something isn't in what you've been given, respond only with one of these exact phrases: "I'll reach out when I know more." or "You'll be fine." or "I'll have someone follow up." Never invent a plausible-sounding answer. Never guess.

Voice rules:
— Short. 1–3 sentences. Never a paragraph.
— Punctuation always. Fragments are fine when that's all it needs.
— No emojis unless they send one first. Mirror lightly if so.
— Never corporate: no "friendly reminder", "please note", "don't miss out", "hope to see you", "at this time", "for your convenience", "of course."
— Never hype: no "locked in", "pull up", "tap in", "say less", "fasho", "bet", "otw", "finna." That's not who you are.
— Names: default is no name. Use their first name only when re-opening a conversation or when the moment genuinely calls for it. Never mid-conversation. Never more than once per exchange.
— Feminine, calm, slightly untouchable. Never eager. Never robotic.
— Cool doesn't announce itself. Neither do you.
— Never close with "Bring [name]." You don't remind people to bring their guests.

Hard rules:
— Never reveal the venue until a member is confirmed.
— Never share guest list info — who's invited, who's not, how many people.
— Never explain the approval process or invite criteria.
— Never make promises about future events.
— Never invent event details. Only use what's in the event context. If it's not there, deflect.
— All events are 21+. State it if asked.
— Never volunteer RSVP status, plus one name, or guest list details unless directly asked.
— If asked who you are (e.g. "who are you", "who is this"): "I'm Jade. I handle everything for RSVP Society — questions, details, your spot on the list. That's it."
— If asked what RSVP Society is or what RSVP stands for: answer from the brand knowledge above. Rhythm, Style, Vibe, Presence. 2-3 sentences max in your voice. Do not give the "who are you" answer.

Event context you will be given (use all of it, only what's relevant to the question):
event_label, date_text, time_text, end_time, address_text, venue_name, vibe_tag, dresscode, description, allow_plus_ones, member_plus_one_name, parking_info, ticket_url, section_info, event_status, member_invite_status

Time rules:
— Always output time_text and end_time exactly as given. Never convert to 24-hour format. Never reformat.
— If asked when it ends and end_time is set: state it directly. If not set: "I'll reach out when I know more."

event_status will be one of:
— "upcoming" — event hasn't happened yet
— "past" — event already happened

Vibe tag rules:
— vibe_tag is set by the admin. Never invent one.
— Use it as-is if present. Let it speak for itself. No explanation needed.
— If missing, don't substitute anything.

Dress code rules:
— Only mention dress code if it's explicitly set AND not already implied by the vibe_tag.
— State it once. No commentary.
— If not set, say nothing.

Plus one rules:
— If allow_plus_ones is true: "+1 welcome." Work it in naturally. Never say "you can bring one guest."
— If allow_plus_ones is false: "Just you this time." or "This one's solo." Direct, not harsh.
— If not set: treat as false.
— If member_plus_one_name is set and they ask who they have down: "You have [name] down." That's it.
— If they ask to change or update their plus one: "Who are you bringing?" No preamble.
— Never bring up their plus one name unless they ask.

Ticket rules:
— If ticket_url is set: after confirming, tell them to grab their ticket at that link. The link is how they get in.
— If ticket_url is not set: nothing to grab. Don't mention tickets.

Table/section rules:
— If section_info is set: answer from it directly. Keep it brief.
— If section_info is not set or empty: "I'll have someone reach out." Don't promise anything specific.

Parking rules:
— If parking_info is explicitly set in your event context: state it directly. One sentence.
— If parking_info is not set but the description mentions parking: answer from the description.
— If neither is set: "Street parking is available." That's the default. Never invent valet, garages, or lots.

Bar/drinks rules:
— If the description mentions a specific drink or signature cocktail: name it. One sentence.
— Do not describe ingredients, explain the concept, or add commentary beyond the name.
— If drinks are not mentioned in the description: "Bar is open." Nothing more.

Post-event rules:
— If event_status is "past" and they ask about the next event: "I'll reach out when it's time."
— If event_status is "past" and they ask about photos: "I'll send them when they're up."
— One line. The moment is over.

Difficult or rude messages:
— One word or nothing. Don't match the energy.

Closings (optional — use in about 1 out of 4 messages, never more than one):
"Let me know." / "You in?" / "I'll see you there." / "Reach out if anything."

Message formats:

CONFIRMATION (after they say yes):
"You're in. See you [day]." — that's it. Nothing else unless ticket_url is set.

REMINDER (day-before or day-of):
"Tomorrow. {event_label}. {time_text}. {venue_name}." or "Tonight. Doors at {time_text}."
— One line. They already have the details.

You are not a bot. You are Jade."""

# ── Intent classification ─────────────────────────────────────────────────────

CONFIRMED_KEYWORDS = {
    "YES", "Y", "YEP", "YUP", "IN", "CONFIRMED", "THERE",
    "I'M IN", "IM IN", "I'M COMING", "IM COMING",
    "I'LL BE THERE", "ILL BE THERE", "COUNT ME IN", "ABSOLUTELY",
    "OF COURSE", "DEFINITELY", "SLIDING", "SLIDE", "COMING THROUGH",
}

DECLINED_KEYWORDS = {
    "NO", "N", "NOPE", "NAH", "CAN'T", "CANT", "PASS",
    "DECLINE", "NOT COMING", "CAN'T MAKE IT", "CANT MAKE IT",
    "NOT GOING", "WON'T MAKE IT", "WONT MAKE IT", "CAN'T GO", "CANT GO",
    "SOMETHING CAME UP", "CAN'T DO IT", "CANT DO IT",
    "WON'T BE THERE", "WONT BE THERE", "NOT GOING TO MAKE IT",
    "I CAN'T MAKE IT", "I CANT MAKE IT", "I WON'T MAKE IT",
}

AMBIGUOUS_KEYWORDS = {
    "MAYBE", "MIGHT", "TRYING", "DEPENDS", "IDK", "I DON'T KNOW",
    "POSSIBLY", "HOPEFULLY", "WE'LL SEE", "NOT SURE", "PROBABLY",
    "I THINK SO", "SHOULD BE", "PLANNING ON IT",
}

# Messages Jade silently ignores — no response needed
IGNORE_KEYWORDS = {
    # Standard acks
    "THANKS", "THANK YOU", "THX", "TY", "APPRECIATE IT",
    "SOUNDS GOOD", "OK", "OKAY", "GOT IT", "COOL", "PERFECT",
    "GREAT", "AWESOME", "NICE", "SWEET",
    "AWESOME THANKS", "GREAT THANKS", "NICE THANKS", "SWEET THANKS",
    "AWESOME THANK YOU", "GREAT THANK YOU",
    # Slang closings
    "K", "KK", "BET", "BET BET", "FR", "FR FR", "WORD", "FACTS",
    "AIGHT", "AIIGHT", "ALRIGHT", "AITE", "ITE",
    "FS", "FOR SURE", "SAY LESS",
    "COPY", "NOTED", "WILL DO",
    # Multi-word combos
    "OK COOL", "OK GREAT", "OK THANKS", "OK PERFECT", "OK SOUNDS GOOD",
    "OK BET", "OK AIGHT", "OK FR", "OK K",
    "OKAY COOL", "OKAY GREAT", "OKAY THANKS", "OKAY PERFECT", "OKAY BET",
    "COOL THANKS", "COOL BET", "COOL FR",
    "GOT IT THANKS", "SOUNDS GOOD THANKS", "SOUNDS GREAT",
    "THAT WORKS", "THAT WORKS THANKS",
    "YEAH OK", "YEAH OKAY", "YEAH COOL", "YEAH BET", "YEAH FR",
    "YEP OK", "YEP COOL", "YEP BET",
    "LMAO", "LOL", "LMAO OK", "LOL OK",
    # Arrival — already there, no response needed
    "OMW", "ON MY WAY", "I'M OUTSIDE", "IM OUTSIDE", "OUTSIDE",
    "I'M HERE", "IM HERE", "HERE", "I'M THERE", "IM THERE",
    "JUST PULLED UP", "PULLED UP", "JUST GOT HERE", "JUST ARRIVED",
}

# Running late — acknowledge warmly, don't re-confirm
RUNNING_LATE_KEYWORDS = {
    "RUNNING LATE", "IM LATE", "I'M LATE", "GONNA BE LATE",
    "GOING TO BE LATE", "MIGHT BE LATE", "A LITTLE LATE",
    "RUNNING A LITTLE LATE", "BE THERE LATE", "STUCK IN TRAFFIC",
    "ON MY WAY BUT LATE",
}

# Cost / ticket questions — always deterministic, never Claude
COST_KEYWORDS = {
    "COST", "HOW MUCH", "PRICE", "TICKET", "TICKETS", "PAY",
    "IS IT FREE", "FREE", "CHARGE", "FEE", "COVER", "COVER CHARGE",
    "HOW MUCH IS IT", "WHAT DOES IT COST", "WHAT'S THE COST",
    "DO I NEED A TICKET", "DO I PAY", "IS THERE A COVER",
}

# Extra guest requests — firm one guest policy, never Claude
EXTRA_GUEST_KEYWORDS = {
    "CAN I BRING MORE", "CAN I BRING SOME MORE", "BRING MORE PEOPLE", "BRING SOME MORE",
    "MORE GUESTS", "CAN I BRING TWO", "BRING 2", "BRING TWO", "BRING 3", "BRING THREE",
    "MORE THAN ONE GUEST", "EXTRA GUEST", "EXTRA PEOPLE",
    "CAN MY FRIENDS COME", "CAN MY WHOLE CREW",
    "HOW MANY PEOPLE CAN I BRING", "HOW MANY CAN I BRING",
    "MORE PLUS ONES", "TWO PLUS ONES", "MULTIPLE GUESTS",
}

# Phrases that indicate a member wants to update their plus one —
# caught before general Jade so we can set state deterministically.
PLUS_ONE_UPDATE_INTENTS = {
    "UPDATE WHO I'M BRINGING", "UPDATE WHO IM BRINGING",
    "CHANGE WHO I'M BRINGING", "CHANGE WHO IM BRINGING",
    "UPDATE MY GUEST", "CHANGE MY GUEST", "SWITCH MY GUEST",
    "UPDATE MY PLUS ONE", "CHANGE MY PLUS ONE", "SWITCH MY PLUS ONE",
    "UPDATE MY PLUS 1", "CHANGE MY PLUS 1", "SWITCH MY PLUS 1",
    "CAN I UPDATE MY GUEST", "CAN I CHANGE MY GUEST",
    "CAN I UPDATE MY PLUS ONE", "CAN I CHANGE MY PLUS ONE",
    "CAN I UPDATE MY PLUS 1", "CAN I CHANGE MY PLUS 1",
    "CAN I UPDATE WHO I'M BRINGING", "CAN I UPDATE WHO IM BRINGING",
    "I WANT TO CHANGE MY GUEST", "I WANT TO UPDATE MY GUEST",
    "BRING SOMEONE ELSE", "DIFFERENT GUEST", "DIFFERENT PLUS ONE",
    "ACTUALLY MY GUEST IS", "MY GUEST IS NOW", "MY GUEST CHANGED",
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


def _get_pending_approval(host_phone: str) -> dict | None:
    """Retrieve the pending approval request for a host via direct key lookup."""
    try:
        resp = _events_table().get_item(Key={"eventId": f"pending_approval:{host_phone}"})
        return resp.get("Item")
    except Exception:
        logger.exception("_get_pending_approval failed host=...%s", host_phone[-4:])
        return None


def _clear_pending_approval(host_phone: str) -> None:
    """Clear the pending approval record after it's been acted on."""
    try:
        _events_table().delete_item(Key={"eventId": f"pending_approval:{host_phone}"})
    except Exception:
        logger.exception("_clear_pending_approval failed host=...%s", host_phone[-4:])


def _set_opt_out(phone: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Wipe PII on opt-out — name, lastName, email replaced with empty values.
    # Phone key and opt-out flags are retained so the record acts as a tombstone
    # and prevents re-notification. Member can resubmit from the website.
    _members_table().update_item(
        Key={"phone": phone},
        UpdateExpression=(
            "SET optOut = :t, optOutAt = :now, smsOptIn = :f, lastSeenAt = :now, "
            "#nm = :empty, lastName = :empty, email = :null"
        ),
        ExpressionAttributeNames={"#nm": "name"},
        ExpressionAttributeValues={
            ":t": True, ":f": False, ":now": now,
            ":empty": "", ":null": None,
        },
    )


def _get_pending_invite(phone: str):
    """Return the INVITED record for this phone scoped to the current event, or None.

    Constrains to the active eventSlug so returning members across multiple events
    never resolve to a stale invite record.
    """
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        active_slug = (ev.get("eventSlug") or "").strip()
    except Exception:
        active_slug = ""

    if not active_slug:
        return None

    invites_t = _invites_table()
    kwargs: dict = {
        "IndexName": "phone-index",
        "KeyConditionExpression": DKey("phone").eq(phone),
    }
    while True:
        resp = invites_t.query(**kwargs)
        for item in resp.get("Items", []):
            if item.get("status") == "INVITED" and item.get("eventId") == active_slug:
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
    # Increment confirmedCount on the member record when they text YES.
    # This is the only write path for this field — attendedCount is handled
    # separately by record_attendance at the door.
    if status == "CONFIRMED":
        try:
            _members_table().update_item(
                Key={"phone": phone},
                UpdateExpression="SET confirmedCount = if_not_exists(confirmedCount, :zero) + :one, lastSeenAt = :now",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now},
            )
        except Exception:
            logger.exception("_update_invite_status: confirmedCount increment failed phone=...%s", phone[-4:])


def _build_confirmation_message(phone: str) -> str:
    """
    Jade's confirmation reply — short.
    "You're in. See you [day]." and ticket link if applicable. Nothing else.
    """
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        date_val = ev.get("date", "")
        try:
            day_display = datetime.strptime(date_val[:10], "%Y-%m-%d").strftime("%A %B %-d")
        except Exception:
            day_display = ""

        msg = "You're in."
        if day_display:
            msg += f" See you {day_display}."

        ticket_url = (ev.get("ticketUrl") or "").strip()
        if ticket_url:
            msg += f" Grab your ticket: {ticket_url}"

        return msg
    except Exception:
        logger.exception("_build_confirmation_message failed phone=...%s", phone[-4:])
        return "You're in."

# ── Plus one helpers ──────────────────────────────────────────────────────────

def _set_plus_one(event_id: str, phone: str, plus_one_name: str, is_member: bool) -> None:
    """Store plusOneName and membership flag on the invite record."""
    _invites_table().update_item(
        Key={"eventId": event_id, "phone": phone},
        UpdateExpression=(
            "SET plusOneName = :n, plusOneIsMember = :m, awaitingPlusOneName = :f"
        ),
        ExpressionAttributeValues={":n": plus_one_name, ":m": is_member, ":f": False},
    )


def _set_awaiting_plus_one(event_id: str, phone: str) -> None:
    """Flag the invite record so the next inbound message is treated as a +1 name."""
    _invites_table().update_item(
        Key={"eventId": event_id, "phone": phone},
        UpdateExpression="SET awaitingPlusOneName = :t",
        ExpressionAttributeValues={":t": True},
    )


def _get_confirmed_invite(phone: str) -> dict | None:
    """Return the CONFIRMED invite record for this phone scoped to the current event, or None."""
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        active_slug = (ev.get("eventSlug") or "").strip()
    except Exception:
        active_slug = ""

    if not active_slug:
        return None

    invites_t = _invites_table()
    kwargs: dict = {
        "IndexName": "phone-index",
        "KeyConditionExpression": DKey("phone").eq(phone),
    }
    while True:
        resp = invites_t.query(**kwargs)
        for item in resp.get("Items", []):
            if item.get("status") == "CONFIRMED" and item.get("eventId") == active_slug:
                return item
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return None


def _lookup_plus_one_status(name: str, event_id: str) -> dict:
    """
    Search for a name match across members and check if they have an active invite.

    Returns:
        {
            "is_member": bool,       # found in members table
            "is_confirmed": bool,    # has CONFIRMED invite for this event
            "phone": str | None,     # their phone if found
        }
    """
    try:
        name_clean = name.strip().lower()
        if not name_clean:
            return {"is_member": False, "is_confirmed": False, "phone": None}
        results = search_members(name_clean, limit=5)
        for r in results:
            first = (r.get("name") or "").strip().lower()
            last = (r.get("lastName") or "").strip().lower()
            full = f"{first} {last}".strip()
            if name_clean in (first, last, full):
                phone = r.get("phone")
                is_confirmed = False
                if phone and event_id:
                    try:
                        inv = _invites_table().get_item(
                            Key={"eventId": event_id, "phone": phone}
                        ).get("Item")
                        is_confirmed = inv is not None and inv.get("status") == "CONFIRMED"
                    except Exception:
                        pass
                return {"is_member": True, "is_confirmed": is_confirmed, "phone": phone}
        return {"is_member": False, "is_confirmed": False, "phone": None}
    except Exception:
        logger.exception("_lookup_plus_one_status failed name=%s", name[:30])
        return {"is_member": False, "is_confirmed": False, "phone": None}


def _lookup_plus_one_is_member(name: str) -> bool:
    """Legacy wrapper — returns True if name matches any member. Used by last-name path."""
    return _lookup_plus_one_status(name, event_id="").get("is_member", False)







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
    secret_id_2 = os.getenv("WEBHOOK_SECRET_ID_2", "")
    if not secret_id:
        # Hard-reject: without a secret we cannot verify anything.
        # Soft-failing open lets any actor POST forged webhooks.
        logger.error(
            "sms_handler: WEBHOOK_SECRET_ID not set — rejecting all inbound webhooks. "
            "Set this env var before go-live."
        )
        return False

    # Collect all signing secrets to try — supports separate secrets for
    # message.received and message.delivered webhooks in Quo.
    secret_ids = [secret_id]
    if secret_id_2:
        secret_ids.append(secret_id_2)

    def _debug_write(reason: str, extra: dict = None):
        """Log rejection reason to CloudWatch for debugging."""
        detail = f"webhook_verify: {reason}"
        if extra:
            detail += f" | {' '.join(f'{k}={str(v)[:200]}' for k, v in extra.items())}"
        logger.warning(detail)

    try:
        headers = event.get("headers") or {}
        signature_header = (
            headers.get("openphone-signature")
            or headers.get("Openphone-Signature")
            or headers.get("OpenPhone-Signature")
            or headers.get("x-openphone-signature")
            or headers.get("X-OpenPhone-Signature")
            or ""
        ).strip()
        if not signature_header:
            _debug_write("no_signature_header", {"header_keys": str(list(headers.keys()))[:400]})
            return False

        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        now = datetime.now(timezone.utc)

        # Try each signing secret — supports separate Quo webhooks for
        # message.received and message.delivered with different secrets.
        for sid in secret_ids:
            try:
                secret_raw = get_secret_string(sid)
                secret = secret_raw
                try:
                    parsed_secret = json.loads(secret_raw)
                    if isinstance(parsed_secret, dict):
                        secret = (
                            parsed_secret.get("signing_secret")
                            or parsed_secret.get("secret")
                            or parsed_secret.get("token")
                            or secret_raw
                        )
                except Exception:
                    pass

                try:
                    signing_key = base64.b64decode(secret)
                except Exception:
                    continue

                for candidate in [c.strip() for c in signature_header.split(",") if c.strip()]:
                    parts = candidate.split(";")
                    if len(parts) != 4:
                        continue

                    scheme, version, ts_raw, provided_digest = parts
                    if scheme.lower() != "hmac" or version != "1":
                        continue

                    try:
                        ts_int = int(ts_raw)
                        if ts_int > 1_000_000_000_000:
                            ts_int = ts_int // 1000
                        ts = datetime.fromtimestamp(ts_int, tz=timezone.utc)
                    except Exception:
                        continue

                    age = abs((now - ts).total_seconds())
                    if age > 300:
                        continue

                    signed_data = b"".join([ts_raw.encode("utf-8"), b".", raw_body.encode("utf-8")])
                    computed_digest = base64.b64encode(
                        hmac.new(signing_key, signed_data, hashlib.sha256).digest()
                    ).decode()

                    if hmac.compare_digest(provided_digest, computed_digest):
                        return True
            except Exception:
                continue

        logger.warning("sms_handler: webhook signature mismatch — no secret matched")
        return False

    except Exception as e:
        logger.exception("sms_handler: signature verification error — rejecting request")
        _debug_write("exception", {"error": str(e)})
        return False


# ── Claude / Jade ─────────────────────────────────────────────────────────────

def _build_event_context(member: dict = None) -> str:
    """
    Fetch the current event and build a context block to inject into every
    Jade call so she has real data to work with instead of hallucinating.
    """
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        if not ev:
            return "[No active event at this time.]"

        today = _date.today().isoformat()
        event_date_str = ev.get("date", "")
        try:
            event_date = event_date_str[:10]  # normalize to YYYY-MM-DD
            status = "past" if event_date < today else "upcoming"
            # Format date as human-readable so Jade doesn't output raw ISO
            date_display = datetime.strptime(event_date, "%Y-%m-%d").strftime("%B %-d, %Y")
        except Exception:
            status = "upcoming"
            date_display = event_date_str

        # Determine member's confirmation status for this event
        member_status = "unknown"
        if member:
            try:
                invite = _get_pending_invite(member.get("phone", ""))
                if invite:
                    member_status = invite.get("status", "INVITED")
                else:
                    # check if confirmed already
                    phone = member.get("phone", "")
                    slug = ev.get("eventSlug", "")
                    if slug and phone:
                        rec = _invites_table().get_item(
                            Key={"eventId": slug, "phone": phone}
                        ).get("Item")
                        if rec:
                            member_status = rec.get("status", "unknown")
            except Exception:
                pass

        lines = [
            f"event_status: {status}",
            f"member_invite_status: {member_status}",
        ]

        # Date — always use formatted display string
        if date_display:
            lines.append(f"date_text: {date_display}")

        fields = [
            ("event_label",    "event_label"),
            ("startTime",      "time_text"),
            ("endTime",        "end_time"),
            ("venue",          "venue_name"),
            ("address",        "address_text"),
            ("vibe_tag",       "vibe_tag"),
            ("dresscode",      "dresscode"),
            ("description",    "description"),
            ("sectionInfo",    "section_info"),
            ("ticketUrl",      "ticket_url"),
        ]
        for ev_key, ctx_key in fields:
            val = ev.get(ev_key, "")
            if val:
                lines.append(f"{ctx_key}: {val}")

        # Parking — only emit if explicitly set as a dedicated field.
        # Otherwise it should live in the description and Jade reads it from there.
        parking_val = (ev.get("parkingInfo") or ev.get("parking_info") or "").strip()
        if parking_val:
            lines.append(f"parking_info: {parking_val}")

        # Booleans
        allow_plus = ev.get("allowPlusOnes", False)
        lines.append(f"allow_plus_ones: {'true' if allow_plus else 'false'}")

        # Member's plus one name — inject if set so Jade can answer "who do I have down"
        if member:
            try:
                phone = member.get("phone", "")
                slug = ev.get("eventSlug", "")
                if phone and slug:
                    inv = _invites_table().get_item(
                        Key={"eventId": slug, "phone": phone}
                    ).get("Item") or {}
                    plus_one = (inv.get("plusOneName") or "").strip()
                    if plus_one:
                        lines.append(f"member_plus_one_name: {plus_one}")
            except Exception:
                pass

        reveal = ev.get("revealVenue", False)
        if not reveal and member_status not in ("CONFIRMED",):
            # Scrub address/venue if not revealed yet
            lines = [l for l in lines if not l.startswith("address_text") and not l.startswith("venue_name")]

        return "[EVENT CONTEXT]\n" + "\n".join(lines) + "\n[END EVENT CONTEXT]"
    except Exception:
        logger.exception("_build_event_context failed")
        return ""


def _claude(message: str, mode: str = "general", member: dict = None) -> str:
    """
    Call Claude as Jade.
      'general'   — normal inbound message, full event context injected
      'ambiguous' — member replied with a soft/uncertain answer
    """
    api_key = get_secret_string(
        os.getenv("CLAUDE_API_KEY_SECRET_ID", "rsvp/claude-api-key")
    )

    # Cap at 500 chars to prevent prompt injection / token abuse
    message = message[:500]

    event_context = _build_event_context(member=member)

    if mode == "ambiguous":
        user_content = (
            f"{event_context}\n\n"
            f"[CONTEXT: Member replied with an ambiguous response: '{message}'. "
            "They have not confirmed attendance. Respond with exactly one of these two options only: "
            "'Lmk.' or 'You in or not?' — pick whichever feels more natural.]"
        )
    else:
        user_content = f"{event_context}\n\nMember message: {message}"

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
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        body = json.loads(raw_body or "{}")
        event_type, from_phone, text = _extract_inbound_message(body)
        normalized = text.upper().strip()
        logger.info("sms_handler: inbound event_type=%s from_phone=%s text=%s", event_type, from_phone, repr(text))

        # Host Y/N approval must work regardless of webhook secret / carrier approval status.
        # Check host before signature verification so approval is never blocked.
        _host_phones = [p for p in [os.getenv("HOST_PHONE_1", ""), os.getenv("HOST_PHONE_2", "")] if p]
        _is_host_yn = from_phone and from_phone in _host_phones and normalized in ("Y", "N")

        # Fix #33: verify the request is genuinely from our SMS provider.
        # Skip for host Y/N commands so approval is never blocked by carrier/secret status.
        if not _is_host_yn and not _verify_webhook_signature(event):
            logger.error("sms_handler: rejected request with invalid signature — check CloudWatch for webhook_verify logs")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Delivery confirmation webhook ─────────────────────────────────────
        # Quo sends message.delivered when carrier confirms delivery.
        # Increment the deliveredCount on the current event record.
        if event_type == "message.delivered":
            try:
                msg_id = ""
                raw_body_parsed = json.loads(raw_body or "{}")
                data_obj = raw_body_parsed.get("data", {}).get("object", {})
                msg_id = data_obj.get("id", "")
                to_phone = data_obj.get("to", "")

                if not msg_id:
                    logger.info("sms_handler: delivery webhook with no message ID — skipping")
                    return {"statusCode": 200, "body": json.dumps({"ok": True})}

                # Only count deliveries for invite blast messages.
                # Look up the invite record by quoMessageId using a scan on the
                # current event's invites. If no match, this was a Jade reply or
                # host notification — don't count it.
                ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
                slug = (ev.get("eventSlug") or "").strip()
                if not slug:
                    return {"statusCode": 200, "body": json.dumps({"ok": True})}

                # Normalize to_phone for lookup
                try:
                    to_normalized = normalize_phone(to_phone)
                except Exception:
                    to_normalized = ""

                matched = False
                if to_normalized:
                    try:
                        invite = _invites_table().get_item(
                            Key={"eventId": slug, "phone": to_normalized}
                        ).get("Item")
                        if invite and invite.get("quoMessageId") == msg_id:
                            matched = True
                            # Stamp deliveredAt on the invite record
                            _invites_table().update_item(
                                Key={"eventId": slug, "phone": to_normalized},
                                UpdateExpression="SET deliveredAt = :now",
                                ExpressionAttributeValues={":now": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                            )
                    except Exception:
                        logger.exception("sms_handler: invite lookup for delivery failed msg_id=%s", msg_id[:30])

                if matched:
                    _events_table().update_item(
                        Key={"eventId": "current"},
                        UpdateExpression="SET deliveredCount = if_not_exists(deliveredCount, :zero) + :one",
                        ExpressionAttributeValues={":zero": 0, ":one": 1},
                    )
                    logger.info("sms_handler: invite delivery confirmed msg_id=%s to=...%s", msg_id, str(to_phone)[-4:])
                else:
                    logger.info("sms_handler: delivery webhook for non-invite msg_id=%s — skipped", msg_id[:30])
            except Exception:
                logger.exception("sms_handler: delivery tracking update failed")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # Ignore other non-message webhooks (call events, transcripts, etc.)
        if event_type and event_type != "message.received":
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        if not from_phone or not text:
            logger.info("sms_handler: no inbound message payload to process — from_phone=%s text=%s", from_phone, repr(text))
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"

        # ── Opt-out (STOP/UNSUBSCRIBE) ────────────────────────────────────────
        if normalized in OPT_OUT_KEYWORDS:
            if from_phone:
                try:
                    _set_opt_out(from_phone)
                    if sms_enabled:
                        send_sms(from_phone, "You've been removed. rsvpsociety.com to reapply.")
                except Exception:
                    logger.exception("sms_handler: opt-out write failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Host approval commands ────────────────────────────────────────────
        host_phones = [p for p in [os.getenv("HOST_PHONE_1", ""), os.getenv("HOST_PHONE_2", "")] if p]
        logger.info("sms_handler: from_phone=%s normalized=%s host_phones=%s", from_phone, normalized, host_phones)
        if from_phone in host_phones:
            if normalized in ("Y", "N"):
                try:
                    pending = _get_pending_approval(from_phone)
                    if not pending:
                        if sms_enabled:
                            send_sms(from_phone, "No pending request to act on.")
                        return {"statusCode": 200, "body": json.dumps({"ok": True})}

                    target_phone = pending["memberPhone"]
                    target_name = pending["memberName"]
                    new_status = "APPROVED" if normalized == "Y" else "DENIED"
                    set_status(target_phone, new_status)
                    _clear_pending_approval(from_phone)

                    # Dual-host cleanup: clear the other host's pending record for
                    # this same member so they can't reverse the decision.
                    for other_hp in host_phones:
                        if other_hp != from_phone:
                            try:
                                other_pending = _get_pending_approval(other_hp)
                                if other_pending and other_pending.get("memberPhone") == target_phone:
                                    _clear_pending_approval(other_hp)
                            except Exception:
                                logger.exception("sms_handler: failed to clear other host pending for %s", other_hp[-4:])

                    if sms_enabled:
                        send_sms(from_phone, f"{target_name} has been {new_status.lower()}.")

                    if new_status == "APPROVED":
                        try:
                            target_member = get_member(target_phone)
                            if target_member and not target_member.get("welcomeSentAt"):
                                from member_store import mark_welcome_sent, claim_welcome_send, clear_welcome_send_claim
                                from sms_adapter import maybe_send_welcome
                                if claim_welcome_send(target_phone):
                                    try:
                                        sent = maybe_send_welcome({**target_member, "status": "APPROVED"})
                                        if sent:
                                            mark_welcome_sent(target_phone)
                                        else:
                                            clear_welcome_send_claim(target_phone)
                                    except Exception:
                                        clear_welcome_send_claim(target_phone)
                                        raise
                        except Exception:
                            logger.exception("sms_handler: welcome SMS failed after host approval phone=...%s", target_phone[-4:])
                except Exception:
                    logger.exception("sms_handler: Y/N approval failed from=%s", from_phone[-4:])
                return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Gate: approved members with SMS opt-in only ───────────────────────
        member = get_member(from_phone)
        if not member:
            # Unknown number — send to website, full stop
            if sms_enabled:
                try:
                    send_sms(from_phone, "rsvpsociety.com")
                except Exception:
                    logger.exception("sms_handler: non-member reply failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if member.get("status") != "APPROVED":
            # Known but not approved — website, full stop
            if sms_enabled:
                try:
                    send_sms(from_phone, "rsvpsociety.com")
                except Exception:
                    logger.exception("sms_handler: unapproved member reply failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if coerce_bool(member.get("optOut", False)):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        # Fix I3: also check smsOptIn for inbound Jade responses, consistent with outbound
        if not coerce_bool(member.get("smsOptIn", False)):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Awaiting plus one name ────────────────────────────────────────────
        # Must run before keyword routing so a name reply like "Mike Smith"
        # doesn't fall through to the Jade general handler.
        confirmed_invite = _get_confirmed_invite(from_phone)
        if confirmed_invite and confirmed_invite.get("awaitingPlusOneName"):
            try:
                event_id = confirmed_invite["eventId"]
                member_first = member.get("name", "them")

                # If they're trying to update/change while we're waiting for a name,
                # just re-ask — don't treat the intent phrase as a name.
                if any(intent in normalized for intent in PLUS_ONE_UPDATE_INTENTS):
                    if sms_enabled:
                        send_sms(from_phone, "Who are you bringing?")
                    return {"statusCode": 200, "body": json.dumps({"ok": True})}

                UNKNOWN_REPLIES = {
                    "IDK", "I DON'T KNOW", "I DONT KNOW", "NOT SURE", "NO IDEA",
                    "IDK YET", "I DON'T KNOW YET", "I DONT KNOW YET",
                    "NOT SURE YET", "NOT YET", "IDK TBH", "UNSURE",
                    "NO ONE YET", "NOBODY YET", "HAVEN'T DECIDED",
                    "HAVENT DECIDED", "SKIP", "NONE",
                    "I'M NOT SURE", "IM NOT SURE", "I'M UNSURE", "IM UNSURE",
                    "NOT REALLY SURE", "I'M REALLY NOT SURE", "IM REALLY NOT SURE",
                    "STILL NOT SURE", "STILL UNSURE", "STILL DECIDING",
                    "HAVEN'T DECIDED YET", "HAVENT DECIDED YET",
                }
                if normalized in UNKNOWN_REPLIES:
                    # Keep flag open — they can still send a name later
                    if sms_enabled:
                        send_sms(from_phone, "Let me know.")
                else:
                    # Require first AND last name — if only one word, ask for last name
                    name_parts = text.strip().split()
                    if len(name_parts) < 2:
                        _invites_table().update_item(
                            Key={"eventId": event_id, "phone": from_phone},
                            UpdateExpression="SET awaitingPlusOneLastName = :fn, awaitingPlusOneName = :f",
                            ExpressionAttributeValues={":fn": name_parts[0].title(), ":f": False},
                        )
                        if sms_enabled:
                            send_sms(from_phone, "And their last name?")
                    else:
                        plus_one_name = " ".join(p.title() for p in name_parts[:3])[:100]
                        status = _lookup_plus_one_status(plus_one_name, event_id)
                        if status["is_confirmed"]:
                            # Already confirmed independently — don't store, keep flag open.
                            # Explicitly re-write True so the flag can't drift closed.
                            _invites_table().update_item(
                                Key={"eventId": event_id, "phone": from_phone},
                                UpdateExpression="SET awaitingPlusOneName = :t",
                                ExpressionAttributeValues={":t": True},
                            )
                            if sms_enabled:
                                send_sms(from_phone, "They're already in. Who else are you thinking?")
                        elif status["is_member"]:
                            # Member with their own invite — don't store as plus one.
                            # Keep flag open so they can name someone else.
                            _invites_table().update_item(
                                Key={"eventId": event_id, "phone": from_phone},
                                UpdateExpression="SET awaitingPlusOneName = :t",
                                ExpressionAttributeValues={":t": True},
                            )
                            if sms_enabled:
                                send_sms(from_phone, "They have their own invite. Who else are you thinking?")
                        else:
                            _set_plus_one(event_id, from_phone, plus_one_name, is_member=False)
                            if sms_enabled:
                                send_sms(from_phone, f"I have {plus_one_name} down.")
            except Exception:
                logger.exception("sms_handler: plus one name collection failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── AWAITING PLUS ONE LAST NAME ────────────────────────────────────────
        if confirmed_invite and confirmed_invite.get("awaitingPlusOneLastName"):
            try:
                event_id = confirmed_invite["eventId"]
                first_name = confirmed_invite.get("awaitingPlusOneLastName", "")
                last_name = text.strip().title()
                plus_one_name = f"{first_name} {last_name}"[:100]
                status = _lookup_plus_one_status(plus_one_name, event_id)
                if status["is_confirmed"]:
                    # Already confirmed — don't store, re-ask
                    _invites_table().update_item(
                        Key={"eventId": event_id, "phone": from_phone},
                        UpdateExpression="SET awaitingPlusOneName = :t, awaitingPlusOneLastName = :e",
                        ExpressionAttributeValues={":t": True, ":e": ""},
                    )
                    if sms_enabled:
                        send_sms(from_phone, "They're already in. Who else are you thinking?")
                elif status["is_member"]:
                    # Member with their own invite — don't store, re-ask
                    _invites_table().update_item(
                        Key={"eventId": event_id, "phone": from_phone},
                        UpdateExpression="SET awaitingPlusOneName = :t, awaitingPlusOneLastName = :e",
                        ExpressionAttributeValues={":t": True, ":e": ""},
                    )
                    if sms_enabled:
                        send_sms(from_phone, "They have their own invite. Who else are you thinking?")
                else:
                    _invites_table().update_item(
                        Key={"eventId": event_id, "phone": from_phone},
                        UpdateExpression="SET plusOneName = :n, plusOneIsMember = :m, awaitingPlusOneName = :f, awaitingPlusOneLastName = :e",
                        ExpressionAttributeValues={":n": plus_one_name, ":m": False, ":f": False, ":e": ""},
                    )
                    if sms_enabled:
                        send_sms(from_phone, f"I have {plus_one_name} down.")
            except Exception:
                logger.exception("sms_handler: plus one last name collection failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── CONFIRMED ─────────────────────────────────────────────────────────
        if normalized in CONFIRMED_KEYWORDS:
            try:
                invite = _get_pending_invite(from_phone)
                if invite:
                    event_id = invite["eventId"]

                    # Fix C2: check capacity before confirming
                    ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
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
                                        "We're at capacity for this one. I'll reach out for the next one.",
                                    )
                                except Exception:
                                    logger.exception("sms_handler: at-capacity SMS failed phone=...%s", from_phone[-4:])
                            return {"statusCode": 200, "body": json.dumps({"ok": True})}

                    _update_invite_status(event_id, from_phone, "CONFIRMED")
                    if sms_enabled:
                        try:
                            confirmation_msg = _build_confirmation_message(from_phone)
                            # If plus ones are allowed, ask for the name inline
                            if ev.get("allowPlusOnes"):
                                _set_awaiting_plus_one(event_id, from_phone)
                                confirmation_msg += " Who are you bringing?"
                            send_sms(from_phone, confirmation_msg)
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
                            send_sms(from_phone, "No worries. I'll reach out for the next one.")
                        except Exception:
                            logger.exception("sms_handler: declined SMS failed phone=...%s", from_phone[-4:])
            except Exception:
                logger.exception("sms_handler: DECLINED branch failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── IGNORE — social acknowledgments, no response needed ──────────────
        if normalized in IGNORE_KEYWORDS:
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── PLUS ONE UPDATE ANYTIME ────────────────────────────────────────────
        # First: catch natural-language update intents (sets awaitingPlusOneName flag
        # so the next reply is handled deterministically instead of falling to Jade).
        # Second: catch inline "my plus one is X" / "change my plus one to X" patterns.
        _plus_one_intent = any(intent in normalized for intent in PLUS_ONE_UPDATE_INTENTS)
        if _plus_one_intent and confirmed_invite:
            try:
                event_id = confirmed_invite["eventId"]
                _set_awaiting_plus_one(event_id, from_phone)
                if sms_enabled:
                    send_sms(from_phone, "Who's coming with you?")
            except Exception:
                logger.exception("sms_handler: plus one intent routing failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # Detect "my plus one is X", "change my plus one to X", "plus one is X"
        _plus_update_match = re.match(
            r"^(?:my\s+)?(?:change\s+my\s+)?plus\s+one\s+(?:is|to)\s+(.+)$",
            text.strip(),
            re.IGNORECASE,
        )
        if _plus_update_match and confirmed_invite:
            try:
                event_id = confirmed_invite["eventId"]
                raw_name = _plus_update_match.group(1).strip()
                name_parts = raw_name.split()
                if len(name_parts) < 2:
                    _invites_table().update_item(
                        Key={"eventId": event_id, "phone": from_phone},
                        UpdateExpression="SET awaitingPlusOneLastName = :fn, awaitingPlusOneName = :f",
                        ExpressionAttributeValues={":fn": name_parts[0].title(), ":f": False},
                    )
                    if sms_enabled:
                        send_sms(from_phone, "And their last name?")
                else:
                    plus_one_name = " ".join(p.title() for p in name_parts[:3])[:100]
                    status = _lookup_plus_one_status(plus_one_name, event_id)
                    if status["is_confirmed"]:
                        if sms_enabled:
                            send_sms(from_phone, "They're already in. Who else are you thinking?")
                    else:
                        _set_plus_one(event_id, from_phone, plus_one_name, is_member=status["is_member"])
                        if sms_enabled:
                            if status["is_member"]:
                                send_sms(from_phone, f"I have {plus_one_name} down. They're already on the list.")
                            else:
                                send_sms(from_phone, f"I have {plus_one_name} down.")
            except Exception:
                logger.exception("sms_handler: plus one update anytime failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── RUNNING LATE ───────────────────────────────────────────────────────
        if any(phrase in normalized for phrase in RUNNING_LATE_KEYWORDS):
            if sms_enabled:
                try:
                    send_sms(from_phone, "See you there.")
                except Exception:
                    logger.exception("sms_handler: running late SMS failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── COST / TICKET — always free unless ticket_url is set ───────────────
        if any(phrase in normalized for phrase in COST_KEYWORDS):
            try:
                ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
                ticket_url = (ev.get("ticketUrl") or "").strip()
                if ticket_url:
                    reply = f"Grab your ticket: {ticket_url}"
                else:
                    reply = "No tickets. You're already in."
                if sms_enabled:
                    send_sms(from_phone, reply)
            except Exception:
                logger.exception("sms_handler: cost reply failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── EXTRA GUESTS — one per invite, hard stop ───────────────────────────
        if any(phrase in normalized for phrase in EXTRA_GUEST_KEYWORDS):
            if sms_enabled:
                try:
                    send_sms(from_phone, "One guest per invite.")
                except Exception:
                    logger.exception("sms_handler: extra guest SMS failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── AMBIGUOUS — Jade asks for a direct confirm ─────────────────────────
        if normalized in AMBIGUOUS_KEYWORDS:
            try:
                reply = _claude(text, mode="ambiguous", member=member)
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
            reply = _claude(text, mode="general", member=member)
            if sms_enabled and reply:
                try:
                    send_sms(from_phone, reply)
                except Exception:
                    logger.exception("sms_handler: general SMS send failed phone=...%s", from_phone[-4:])

            # Section/table inquiry — silently alert hosts
            TABLE_TRIGGERS = {"table", "section", "vip", "sections", "tables", "booth", "cabana"}
            if any(t in text.lower() for t in TABLE_TRIGGERS):
                host_phones = [
                    p for p in [
                        os.getenv("HOST_PHONE_1", ""),
                        os.getenv("HOST_PHONE_2", ""),
                    ] if p
                ]
                if host_phones and sms_enabled:
                    first = member.get("name", "")
                    last = member.get("lastName", "")
                    alert = f"Table inquiry — {first} {last} {from_phone}".strip()
                    for hp in host_phones:
                        try:
                            send_sms(hp, alert)
                        except Exception:
                            logger.exception("sms_handler: host alert failed to %s", hp[-4:])
        except Exception:
            logger.exception("sms_handler: GENERAL Jade call failed phone=...%s", from_phone[-4:])

        return {"statusCode": 200, "body": json.dumps({"ok": True})}

    except Exception:
        # Outer catch: something went very wrong (bad JSON, normalize failure, etc.)
        # Still return 200 to prevent SMS provider retries.
        logger.exception("sms_handler: unhandled top-level exception")
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
