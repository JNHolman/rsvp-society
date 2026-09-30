import json
import os
import re
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def build_event_context(member: dict = None, *, deps: dict) -> str:
    JADE_IN_WAVE_STATUSES = deps['JADE_IN_WAVE_STATUSES']
    LOGISTICS_ELIGIBLE_STATUSES = deps['LOGISTICS_ELIGIBLE_STATUSES']
    _display_time = deps['_display_time']
    _get_current_event = deps['_get_current_event']
    _get_pending_invite = deps['_get_pending_invite']
    _invites_table = deps['_invites_table']
    logger = deps['logger']
    """
    Fetch the current event and build a context block to inject into every
    Jade call so she has real data to work with instead of hallucinating.
    """
    try:
        ev = _get_current_event()
        if not ev:
            return "[No active event at this time.]"

        # Field collapse: description is the single Event Intelligence source.
        # Legacy records may still carry jadeNotes — fold it into description so
        # nothing is lost, then never emit jade_notes again.
        if not (ev.get("description") or "").strip() and (ev.get("jadeNotes") or ev.get("jade_notes") or "").strip():
            ev = {**ev, "description": (ev.get("jadeNotes") or ev.get("jade_notes") or "").strip()}

        event_date_str = ev.get("date", "")
        try:
            local_now = datetime.now(ZoneInfo(ev.get("event_timezone") or "America/New_York"))
            today = local_now.date().isoformat()
            event_date = event_date_str[:10]  # normalize to YYYY-MM-DD
            status = "past" if event_date < today else "upcoming"
            if ev.get("endTime") and ev.get("startTime"):
                end = datetime.fromisoformat(f"{event_date}T{ev['endTime']}").replace(tzinfo=local_now.tzinfo)
                start = datetime.fromisoformat(f"{event_date}T{ev['startTime']}").replace(tzinfo=local_now.tzinfo)
                if end <= start:
                    end += timedelta(days=1)
                status = "past" if local_now >= end else "upcoming"
            # Format date as human-readable so Jade doesn't output raw ISO
            date_display = datetime.strptime(event_date, "%Y-%m-%d").strftime("%B %-d, %Y")
        except Exception:
            # Preserve known logistics rather than guessing a local clock or
            # discarding all event context when a legacy schedule is malformed.
            logger.warning("Jade event timing unavailable; preserving event details")
            status = "unknown"
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

        # Send both: date-based status (upcoming/past) AND admin lifecycle state
        lifecycle_state = (ev.get("event_status") or "DRAFT").upper()

        lines = [
            f"event_status: {status}",
            f"event_lifecycle_state: {lifecycle_state}",
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
                if ev_key in ("startTime", "endTime"):
                    val = _display_time(str(val))
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

        confirmed = member_status in LOGISTICS_ELIGIBLE_STATUSES
        in_wave   = member_status in JADE_IN_WAVE_STATUSES

        # ── Deterministic privacy gate (THREE tiers) ──────────────────────────
        # Code removes private fields before Claude ever sees them — never rely on
        # the prompt alone. Three tiers, strictly enforced:
        #
        #   Tier 0 — NOT in the active invite wave (uninvited, declined, no-show,
        #            or a +1 with no invite of their own): NOTHING about the event.
        #            No venue, no address, no date, no time, no label, no vibe, no
        #            dress code, no description. A plus-one is not a member — they get
        #            their own invite (different wave) or sign up at the door. The rope.
        #   Tier 1 — INVITED but unconfirmed: teaser only — label, date/time, vibe,
        #            dress code. No venue/address/description/sections/parking/ticket.
        #   Tier 2 — CONFIRMED / ATTENDED: full logistics.
        #
        # revealVenue never overrides the confirmation gate.
        # ──────────────────────────────────────────────────────────────────────

        # Private logistics — hidden from everyone below Tier 2 (confirmed).
        private_prefixes = (
            "address_text", "venue_name", "description", "jade_notes",
            "parking_info", "ticket_url",
        )
        # Event teaser fields — visible to invited members (Tier 1) but NOT to
        # non-members (Tier 0). This is what stops the "Doors at 4:00 PM" leak to
        # an uninvited / plus-one / declined number. section_info lives here (not
        # private) because section pricing helps an invited member decide to come —
        # it's an upsell, not a confidential detail like the address.
        teaser_prefixes = (
            "date_text", "time_text", "end_time", "event_label", "vibe_tag", "dresscode",
            "section_info",
        )

        if not in_wave:
            # Tier 0: strip private AND teaser — they learn nothing about the event.
            strip = private_prefixes + teaser_prefixes
            lines = [l for l in lines if not any(l.startswith(p) for p in strip)]
        elif not confirmed:
            # Tier 1: invited, unconfirmed — strip private only, keep the teaser.
            lines = [l for l in lines if not any(l.startswith(p) for p in private_prefixes)]
        # Tier 2: confirmed/attended — keep full logistics.

        return "[EVENT CONTEXT]\n" + "\n".join(lines) + "\n[END EVENT CONTEXT]"
    except Exception:
        logger.exception("_build_event_context failed")
        return ""


def claude(message: str, mode: str = "general", member: dict = None, *, deps: dict) -> str:
    JADE_SYSTEM_PROMPT = deps['JADE_SYSTEM_PROMPT']
    _build_event_context = deps['_build_event_context']
    get_secret_string = deps['get_secret_string']
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
            f"[CONTEXT: Member replied with a soft/uncertain response: '{message}'. "
            "They have not confirmed. Nudge once, in your voice, leaving the door open — "
            "do not pressure, do not over-explain. One short line.]"
        )
    else:
        user_content = f"{event_context}\n\nMember message: {message}"

    payload = {
        "model": (os.getenv("CLAUDE_MODEL") or "claude-haiku-4-5-20251001").strip(),
        "max_tokens": 200,
        "temperature": 0.6,
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
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        out = json.loads(resp.read())
    return out["content"][0]["text"]


def draft_template_messages(event: dict, *, deps: dict) -> dict:
    JADE_SYSTEM_PROMPT = deps['JADE_SYSTEM_PROMPT']
    _display_time = deps['_display_time']
    get_secret_string = deps['get_secret_string']
    """Draft invite + day-before + day-of copy in Jade's voice for admin review.

    Reuses the SAME cached JADE_SYSTEM_PROMPT block as the reply path (byte-identical,
    cache_control ephemeral) so the persona is cached across reply and draft calls.
    Returns {"invite": str, "dayBefore": str, "dayOf": str}. The admin reviews/edits
    and locks these; they then send deterministically. Hard rule enforced in the
    instruction: the INVITE must never contain venue or address (pre-confirmation)."""
    api_key = get_secret_string(
        os.getenv("CLAUDE_API_KEY_SECRET_ID", "rsvp/claude-api-key")
    )

    def _g(*keys):
        for k in keys:
            v = (event.get(k) or "")
            if isinstance(v, str):
                v = v.strip()
            if v:
                return v
        return ""

    label   = _g("event_label", "label")
    date    = _g("date")
    start   = _g("startTime")
    vibe    = _g("vibe_tag")
    dress   = _g("dresscode")
    venue   = _g("venue")
    address = _g("address")
    allow_plus = bool(event.get("allowPlusOnes"))

    # Format date/time for the instruction (never raw ISO/24h into the copy).
    try:
        date_disp = datetime.strptime(date[:10], "%Y-%m-%d").strftime("%A %B %-d") if date else ""
    except Exception:
        date_disp = date
    time_disp = _display_time(start) if start else ""

    facts = [f"Event label: {label or '(none)'}",
             f"Date: {date_disp or '(none)'}",
             f"Start time: {time_disp or '(none)'}",
             f"Vibe: {vibe or '(none)'}",
             f"Dress code: {dress or '(none)'}",
             f"Plus ones allowed: {'yes' if allow_plus else 'no'}",
             f"Venue (REMINDERS ONLY, never in invite): {venue or '(none)'}",
             f"Address (REMINDERS ONLY, never in invite): {address or '(none)'}"]

    instruction = (
        "[DRAFTING TASK - you are writing three outbound templates in your voice, to be "
        "reviewed and locked by the operator, then sent to many members. Use {name} as a "
        "literal placeholder for the member first name.\n\n"
        "Event facts:\n" + "\n".join(facts) + "\n\n"
        "Write THREE messages. Rules:\n"
        "1. INVITE - short, intriguing, your voice. Include label, date, start time, vibe, "
        "dress code if set, and plus-one welcome only if plus ones are allowed. NEVER include "
        "the venue or address - location is never revealed before confirmation.\n"
        "2. DAY-BEFORE reminder - brief, references that it is tomorrow, doors/start time. Venue ok here.\n"
        "3. DAY-OF reminder - brief, references that it is tonight/today, doors/start time, venue + address ok.\n"
        "Only state facts present above. If a fact is (none), omit it - never invent.\n"
        "Return ONLY valid JSON, no markdown, no preamble:\n"
        '{"invite": "...", "dayBefore": "...", "dayOf": "..."}]'
    )
    payload = {
        "model": (os.getenv("CLAUDE_MODEL") or "claude-haiku-4-5-20251001").strip(),
        "max_tokens": 500,
        "temperature": 0.6,
        "system": [
            {
                "type": "text",
                "text": JADE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": instruction}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        out = json.loads(resp.read())
    text = out["content"][0]["text"].strip()
    # Strip accidental markdown fences, then parse JSON.
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    parsed = json.loads(text)
    result = {
        "invite":    str(parsed.get("invite", "")).strip(),
        "dayBefore": str(parsed.get("dayBefore", "")).strip(),
        "dayOf":     str(parsed.get("dayOf", "")).strip(),
    }
    # Hard guard: scrub venue/address from the invite even if the model slipped.
    if venue and venue.lower() in result["invite"].lower():
        result["invite"] = ""  # force re-draft rather than ship a leak
    if address and address.lower() in result["invite"].lower():
        result["invite"] = ""
    return result
