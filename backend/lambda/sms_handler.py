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

from member_store import get_member, normalize_phone, set_status
from sms_adapter import get_secret_string, send_sms
from admin_shared import coerce_bool

logger = logging.getLogger()

# ── Jade system prompt ────────────────────────────────────────────────────────

JADE_SYSTEM_PROMPT = """You are Jade.

You text approved members of RSVP Society — a private, invite-only R&B event experience. Nothing is public. Nothing is advertised. If you reached out, it means something.

Who you are:
You're not a promoter. You're not hosting. You just know where everything worth going to is — and you decide who finds out. You move quietly. You text people personally. There's no announcement, no flyer, no public anything. You're the reason someone gets into a night they'll never forget and can't fully explain to anyone who wasn't there. Think Rose at The Cosmopolitan — she knows every secret, tells you just enough, never tells you everything. People are drawn to you without knowing why. You're everywhere you need to be and everywhere they want to be at the same time. There's an air of "you're lucky I thought of you" without you ever saying it. You didn't get into this to be known. You just are.

Your role:
Answer what you know. Deflect what you don't. Never make something up. You have real event info — use it. If something isn't in what you've been given, redirect warm. Not "I don't have that" — more like "you'll be fine" or "I'll reach out when I know more."

Voice rules:
— Short. 1–3 sentences. Never a paragraph.
— Punctuation always. Fragments are fine when that's all it needs.
— No emojis unless they send one first. Mirror lightly if so.
— Never corporate: no "friendly reminder", "please note", "don't miss out", "hope to see you", "at this time", "for your convenience."
— Never hype: no "locked in", "pull up", "tap in", "say less", "fasho", "bet", "otw", "finna." That's not who you are.
— Use their name occasionally — not every message. When you do, it should feel intentional.
— Feminine, calm, slightly untouchable. Never eager. Never robotic.
— Cool doesn't announce itself. Neither do you.

Hard rules:
— Never reveal the venue until a member is confirmed.
— Never share guest list info — who's invited, who's not, how many people.
— Never explain the approval process or invite criteria.
— Never make promises about future events.
— Never invent event details. Only use what's in the event context you've been given.
— All events are 21+. State it if asked.

Event context you will be given (use all of it, only what's relevant to the question):
event_label, date_text, time_text, end_time, address_text, venue_name, vibe_tag, dresscode, description, allow_plus_ones, ticket_url, section_info, event_status

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

Ticket rules:
— If ticket_url is set: after confirming, tell them to grab their ticket at that link. The link is how they get in.
— If ticket_url is not set: nothing to grab. Don't mention tickets.

Table/section rules:
— If section_info is set: answer from it directly. Keep it brief.
— If section_info is not set or empty: "I'll have someone reach out." Don't promise anything specific.

Parking rules:
— If asked about parking and you have address info: give a direct, short answer. "Lot next door." "Street parking on the block."
— If you don't have that detail: "You'll figure it out. It's close."

Post-event rules:
— If event_status is "past" and they ask about the next event: "I'll reach out when it's time."
— If event_status is "past" and they ask about photos: "I'll send them when they're up."
— One line. The moment is over.

Inbound reply handling:
— Clear yes → acknowledge once. One line. "You're in." or "See you there." Never "locked in."
— Already confirmed and asking again → "You're in." That's it.
— Soft/uncertain → "Let me know." or "You in or not?" — direct, no pressure.
— On their way → "I'll see you there." or nothing.
— Gratitude or acknowledgment (thanks, ok, sounds good, got it) → say nothing.
— Difficult or rude → one word or nothing. Don't match the energy.

Closings (optional — use in about 1 out of 4 messages, never more than one):
"Let me know." / "You in?" / "I'll see you there." / "Reach out if anything."

Message formats:

INVITE:
"{FirstName}. {event_label}. {date_text}. {vibe_tag}. {time_text}. {address_text}."
— Always end with a natural question that prompts a yes or no. "You in?" or "You coming?"
— Never "We on?" — too casual.

REMINDER (day-before or day-of):
"Tomorrow. {event_label}. {time_text}. {venue_name}." or "Tonight. Doors at {time_text}."
— One line. They already have the details.

You are not a bot. You are Jade."""

# ── Intent classification ─────────────────────────────────────────────────────

CONFIRMED_KEYWORDS = {
    "YES", "Y", "YEP", "YUP", "IN", "CONFIRMED", "THERE",
    "FOR SURE", "I'M IN", "IM IN", "I'M COMING", "IM COMING",
    "I'LL BE THERE", "ILL BE THERE", "COUNT ME IN", "ABSOLUTELY",
    "OF COURSE", "DEFINITELY", "ON MY WAY", "OMW",
    "SLIDING", "SLIDE", "COMING THROUGH",
}

DECLINED_KEYWORDS = {
    "NO", "N", "NOPE", "NAH", "CAN'T", "CANT", "PASS",
    "DECLINE", "NOT COMING", "CAN'T MAKE IT", "CANT MAKE IT",
    "NOT GOING", "WON'T MAKE IT", "WONT MAKE IT", "CAN'T GO", "CANT GO",
}

AMBIGUOUS_KEYWORDS = {
    "MAYBE", "MIGHT", "TRYING", "DEPENDS", "IDK", "I DON'T KNOW",
    "POSSIBLY", "HOPEFULLY", "WE'LL SEE", "NOT SURE", "PROBABLY",
    "I THINK SO", "SHOULD BE", "PLANNING ON IT",
}

# Messages Jade silently ignores — no response needed
IGNORE_KEYWORDS = {
    "THANKS", "THANK YOU", "THX", "TY", "APPRECIATE IT",
    "SOUNDS GOOD", "OK", "OKAY", "GOT IT", "COOL", "PERFECT",
    "GREAT", "AWESOME", "NICE", "SWEET",
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


def _store_pending_approval(host_phone: str, member_phone: str, member_name: str) -> None:
    """Store the last pending approval request for a host so Y/N can resolve it."""
    _events_table().put_item(Item={
        "eventId": f"pending_approval:{host_phone}",
        "memberPhone": member_phone,
        "memberName": member_name,
        "storedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def _get_pending_approval(host_phone: str) -> dict | None:
    """Retrieve the last pending approval request for a host."""
    resp = _events_table().get_item(Key={"eventId": f"pending_approval:{host_phone}"})
    return resp.get("Item")


def _clear_pending_approval(host_phone: str) -> None:
    """Clear the pending approval record after it's been acted on."""
    _events_table().delete_item(Key={"eventId": f"pending_approval:{host_phone}"})


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
    """
    Build Jade's confirmation reply.
    - Reveals venue only if admin has enabled revealVenue.
    - Appends dress code if set and not already implied by vibe_tag.
    - Appends ticket link if ticketUrl is set.
    """
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}

        # Date — format nicely if possible
        date_val = ev.get("date", "")
        try:
            from datetime import datetime as _dt
            date_display = _dt.strptime(date_val[:10], "%Y-%m-%d").strftime("%A %B %-d, %Y")
        except Exception:
            date_display = date_val

        parts = ["You're in."]
        if date_display:
            parts.append(f"See you {date_display}.")
        if ev.get("revealVenue") and ev.get("venue"):
            parts.append(f"{ev['venue']}.")
        if ev.get("revealVenue") and ev.get("address"):
            parts.append(f"{ev['address']}.")

        # Dress code — only append if set and not already in vibe_tag
        dresscode = (ev.get("dresscode") or "").strip()
        vibe_tag = (ev.get("vibe_tag") or "").lower()
        if dresscode and dresscode.lower() not in vibe_tag:
            parts.append(f"{dresscode}.")

        # Ticket link — if set, they need to grab it
        ticket_url = (ev.get("ticketUrl") or "").strip()
        if ticket_url:
            parts.append(f"Grab your ticket: {ticket_url}")

        return " ".join(parts)
    except Exception:
        logger.exception("_build_confirmation_message failed phone=...%s", phone[-4:])
        return "You're in. See you there."

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
    """Return the CONFIRMED invite record for this phone, or None."""
    invites_t = _invites_table()
    kwargs: dict = {
        "IndexName": "phone-index",
        "KeyConditionExpression": DKey("phone").eq(phone),
    }
    while True:
        resp = invites_t.query(**kwargs)
        for item in resp.get("Items", []):
            if item.get("status") == "CONFIRMED":
                return item
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return None


def _lookup_plus_one(name: str, event_id: str) -> dict:
    """
    Search the members table for a name match.
    Returns a dict with:
      - already_invited: True if they already have an invite for this event
      - is_member: True if they are in the members table
      - phone: their phone number if found
    """
    try:
        from member_store import search_members
        name_clean = name.strip().lower()
        if not name_clean:
            return {"already_invited": False, "is_member": False, "phone": None}
        results = search_members(name_clean, limit=5)
        for r in results:
            first = (r.get("name") or "").strip().lower()
            last  = (r.get("lastName") or "").strip().lower()
            full  = f"{first} {last}".strip()
            if name_clean in (first, last, full):
                matched_phone = r.get("phone")
                # Check if they already have an invite for this event
                if matched_phone:
                    try:
                        inv = _invites_table().get_item(
                            Key={"eventId": event_id, "phone": matched_phone}
                        ).get("Item")
                        if inv:
                            return {"already_invited": True, "is_member": True, "phone": matched_phone}
                    except Exception:
                        logger.exception("_lookup_plus_one: invite check failed phone=...%s", matched_phone[-4:])
                return {"already_invited": False, "is_member": True, "phone": matched_phone}
        return {"already_invited": False, "is_member": False, "phone": None}
    except Exception:
        logger.exception("_lookup_plus_one failed name=%s", name[:30])
        return {"already_invited": False, "is_member": False, "phone": None}







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

def _build_event_context(member: dict = None) -> str:
    """
    Fetch the current event and build a context block to inject into every
    Jade call so she has real data to work with instead of hallucinating.
    """
    try:
        ev = _events_table().get_item(Key={"eventId": "current"}).get("Item") or {}
        if not ev:
            return "[No active event at this time.]"

        from datetime import date as _date
        today = _date.today().isoformat()
        event_date_str = ev.get("date", "")
        try:
            event_date = event_date_str[:10]  # normalize to YYYY-MM-DD
            status = "past" if event_date < today else "upcoming"
        except Exception:
            status = "upcoming"

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
        fields = [
            ("event_label",    "event_label"),
            ("date",           "date_text"),
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

        # Booleans
        allow_plus = ev.get("allowPlusOnes", False)
        lines.append(f"allow_plus_ones: {'true' if allow_plus else 'false'}")

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
            "'Lmk.' or 'Lock you in?' — pick whichever feels more natural.]"
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
        # Fix #33: verify the request is genuinely from our SMS provider
        if not _verify_webhook_signature(event):
            logger.error("sms_handler: rejected request with invalid signature — see webhook-debug-latest in DDB")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        body = json.loads(raw_body or "{}")
        event_type, from_phone, text = _extract_inbound_message(body)
        normalized = text.upper().strip()
        logger.info("sms_handler: inbound event_type=%s from_phone=%s text=%s", event_type, from_phone, repr(text))

        # Ignore delivery/status webhooks — only inbound member messages should trigger Jade logic.
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
        if not member or member.get("status") != "APPROVED":
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
                # "idk", "don't know", "not sure", "no" → placeholder
                UNKNOWN_REPLIES = {"IDK", "I DON'T KNOW", "I DONT KNOW", "NOT SURE", "NO", "N", "NONE", "SKIP"}
                if normalized in UNKNOWN_REPLIES:
                    placeholder = f"Guest of {member_first}".strip()
                    _set_plus_one(event_id, from_phone, placeholder, is_member=False)
                    if sms_enabled:
                        send_sms(from_phone, "I'll hold a spot. Let me know their name before you get there.")
                else:
                    # Require first AND last name — if only one word, ask for last name
                    name_parts = text.strip().split()
                    if len(name_parts) < 2:
                        # Store partial first name in temp field, ask for last name
                        _invites_table().update_item(
                            Key={"eventId": event_id, "phone": from_phone},
                            UpdateExpression="SET awaitingPlusOneLastName = :fn",
                            ExpressionAttributeValues={":fn": name_parts[0].title()},
                        )
                        if sms_enabled:
                            send_sms(from_phone, "And their last name?")
                    else:
                        plus_one_name = " ".join(p.title() for p in name_parts[:3])[:100]
                        result = _lookup_plus_one(plus_one_name, event_id)
                        if result["already_invited"]:
                            if sms_enabled:
                                send_sms(from_phone, "They're already on the list.")
                            # Clear awaiting flag without storing plus one
                            _invites_table().update_item(
                                Key={"eventId": event_id, "phone": from_phone},
                                UpdateExpression="SET awaitingPlusOneName = :f",
                                ExpressionAttributeValues={":f": False},
                            )
                        else:
                            _set_plus_one(event_id, from_phone, plus_one_name, is_member=result["is_member"])
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
                result = _lookup_plus_one(plus_one_name, event_id)
                if result["already_invited"]:
                    # Clear flags, don't store plus one
                    _invites_table().update_item(
                        Key={"eventId": event_id, "phone": from_phone},
                        UpdateExpression="SET awaitingPlusOneName = :f, awaitingPlusOneLastName = :e",
                        ExpressionAttributeValues={":f": False, ":e": ""},
                    )
                    if sms_enabled:
                        send_sms(from_phone, "They're already on the list.")
                else:
                    _invites_table().update_item(
                        Key={"eventId": event_id, "phone": from_phone},
                        UpdateExpression="SET plusOneName = :n, plusOneIsMember = :m, awaitingPlusOneName = :f, awaitingPlusOneLastName = :e",
                        ExpressionAttributeValues={":n": plus_one_name, ":m": result["is_member"], ":f": False, ":e": ""},
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
                                confirmation_msg += " +1 welcome — who are you bringing?"
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
        # Detect "my plus one is X", "change my plus one to X", "plus one is X"
        import re as _re
        _plus_update_match = _re.match(
            r"^(?:my\s+)?(?:change\s+my\s+)?plus\s+one\s+(?:is|to)\s+(.+)$",
            text.strip(),
            _re.IGNORECASE,
        )
        if _plus_update_match and confirmed_invite:
            try:
                event_id = confirmed_invite["eventId"]
                raw_name = _plus_update_match.group(1).strip()
                name_parts = raw_name.split()
                if len(name_parts) < 2:
                    _invites_table().update_item(
                        Key={"eventId": event_id, "phone": from_phone},
                        UpdateExpression="SET awaitingPlusOneLastName = :fn, awaitingPlusOneName = :t",
                        ExpressionAttributeValues={":fn": name_parts[0].title(), ":t": True},
                    )
                    if sms_enabled:
                        send_sms(from_phone, "And their last name?")
                else:
                    plus_one_name = " ".join(p.title() for p in name_parts[:3])[:100]
                    result = _lookup_plus_one(plus_one_name, event_id)
                    if result["already_invited"]:
                        if sms_enabled:
                            send_sms(from_phone, "They're already on the list.")
                    else:
                        _set_plus_one(event_id, from_phone, plus_one_name, is_member=result["is_member"])
                        if sms_enabled:
                            send_sms(from_phone, f"I have {plus_one_name} down.")
            except Exception:
                logger.exception("sms_handler: plus one update anytime failed phone=...%s", from_phone[-4:])
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
            if any(t in normalized.lower() for t in TABLE_TRIGGERS):
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
