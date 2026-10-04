from event_policy import venue_mode
import hashlib
import math
import random
from datetime import datetime
from typing import Any, Dict, List, Tuple

CONFIRM_RATE_MIN_SAMPLE = 20
DEFAULT_CONFIRM_RATE = 0.30
DEFAULT_SHOW_RATE = 0.60
MAX_AUTO_WAVE_MULTIPLIER = 2.5
TIER1_MIN_ATTENDANCE_RATE = 0.80
TIER2_MIN_ATTENDANCE_RATE = 0.40

def _fmt_invite_date(raw: str) -> str:
    """ISO date -> 'Saturday May 31'. Falls back to the raw value if unparseable."""
    raw = (raw or "").strip()
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%A %B %-d")
    except Exception:
        return raw


def _fmt_invite_time(raw: str) -> str:
    """24h 'HH:MM' -> '4 PM' / '4:30 PM'. Falls back to the raw value if unparseable."""
    raw = (raw or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.minute:
                return dt.strftime("%-I:%M %p")
            return dt.strftime("%-I %p")
        except Exception:
            continue
    return raw


def calc_tier(member: Dict[str, Any]) -> int:
    override = member.get("tierOverride")
    if override in (1, 2, 3):
        return int(override)
    invited = int(member.get("invitedCount", 0))
    # Keep lifetime invitation history intact; only timely confirmed-RSVP
    # cancellations are exempt from the attendance-rate denominator.
    invited = max(0, invited - max(0, int(member.get("timelyCancellationCount", 0))))
    attended = int(member.get("attendedCount", 0))
    if invited < 3:
        return 2
    rate = attended / invited
    if rate >= TIER1_MIN_ATTENDANCE_RATE:
        return 1
    elif rate >= TIER2_MIN_ATTENDANCE_RATE:
        return 2
    else:
        return 3


def _calc_invite_suggestion(
    capacity: int,
    confirmed: int,
    already_invited: int,
    actual_confirm_rate: float | None = None,
    show_rate: float | None = None,
    responses: int | None = None,
) -> dict:
    """
    How many more invites does this wave need to fill the room?

    target_confirmed = capacity / show_rate
        e.g. 200 cap at 60% show rate → need 334 confirmed

    confirmation_gap = target_confirmed - currently_confirmed

    invite-to-headcount rate from analytics if available, else default 30%

    uncapped estimate = confirmation_gap / confirm_rate
    suggested_invites is limited to 2.5 times capacity for one Auto wave
    """
    # WAVE-B: only trust the event's own confirm-rate once the sample is meaningful.
    # responses defaults to already_invited when not supplied (best available proxy).
    sample = responses if responses is not None else already_invited
    if actual_confirm_rate and actual_confirm_rate > 0 and sample >= CONFIRM_RATE_MIN_SAMPLE:
        confirm_rate = actual_confirm_rate
        confirm_rate_source = "actual"
    else:
        confirm_rate = DEFAULT_CONFIRM_RATE
        confirm_rate_source = "default"

    # Use the event's configured expected show rate when provided.
    if show_rate is None or show_rate <= 0:
        eff_show_rate = DEFAULT_SHOW_RATE
        show_rate_source = "default"
    else:
        eff_show_rate = show_rate
        show_rate_source = "actual"

    target_confirmed = math.ceil(capacity / eff_show_rate)
    gap = max(0, target_confirmed - confirmed)
    suggested = math.ceil(gap / confirm_rate) if gap > 0 else 0
    uncapped_suggestion = suggested
    auto_wave_limit = math.ceil(max(0, capacity) * MAX_AUTO_WAVE_MULTIPLIER)
    suggested = min(suggested, auto_wave_limit)
    return {
        "targetConfirmed":    target_confirmed,
        "currentConfirmed":   confirmed,
        "confirmationGap":    gap,
        "suggestedInvites":   suggested,
        "uncappedInviteEstimate": uncapped_suggestion,
        "autoWaveLimit": auto_wave_limit,
        "alreadyInvited":     already_invited,
        "assumedShowRate":    round(eff_show_rate * 100),
        "assumedConfirmRate": round(confirm_rate * 100),
        "showRateSource":     show_rate_source,
        "confirmRateSource":  confirm_rate_source,
    }


def _resolve_wave_capacity(
    capacity: int,
    wave_number: int,
    wave_size: int | None,
    confirmed: int = 0,
    already_invited: int = 0,
    actual_confirm_rate: float | None = None,
    actual_show_rate: float | None = None,
    responses: int | None = None,
) -> int:
    """
    Wave 1: size the Tier 1 cohort using its 80% historical attendance floor.
    Wave 2+: use RSVP/headcount data to calculate a bounded next-wave target.
    Manual override (wave_size > 0) always wins.
    """
    if wave_size and wave_size > 0:
        return wave_size
    if wave_number == 1:
        return max(1, math.ceil(capacity / TIER1_MIN_ATTENDANCE_RATE))
    suggestion = _calc_invite_suggestion(
        capacity, confirmed, already_invited,
        actual_confirm_rate=actual_confirm_rate,
        show_rate=actual_show_rate,
        responses=responses,
    )
    suggested = suggestion["suggestedInvites"]
    # Wave 2+: allow 0 when observed results show the capacity gap is closed.
    return max(0, suggested)


def _build_invite_list(
    members: List[Dict[str, Any]],
    capacity: int,
    female_pct: int,
    removed_phones: List[str] = None,
    wave_number: int = 1,
) -> Dict[str, Any]:
    removed = set(removed_phones or [])
    members = [m for m in members if m.get("phone") not in removed]

    male_pct = 100 - female_pct
    max_tier = 1 if wave_number <= 1 else 2 if wave_number == 2 else 3
    # Cold-start city: members with fewer than three invite results are Tier 2.
    # If no Tier 1 candidates exist, let the first wave reach Tier 2 instead of
    # producing an empty send. Once any Tier 1 candidate is present, keep the
    # normal staged order.
    tier_one_available = any(int(m.get("_tier", 2)) == 1 for m in members)
    cold_start_tier2_fallback = wave_number == 1 and not tier_one_available
    if cold_start_tier2_fallback:
        max_tier = 2
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
        t3 = buckets.get(f"{gender}3", [])
        tier1_invited = t1[:target]
        remaining = target - len(tier1_invited)
        tier2_invited = []
        tier3_invited = []
        if remaining > 0 and max_tier >= 2:
            tier2_invited = t2[:remaining]
            remaining -= len(tier2_invited)
        if remaining > 0 and max_tier >= 3:
            tier3_invited = t3[:remaining]
        return tier1_invited, tier2_invited, tier3_invited

    f_t1, f_t2, f_t3 = fill_gender("F", target_f)
    m_t1, m_t2, m_t3 = fill_gender("M", target_m)

    core_invited = f_t1 + f_t2 + f_t3 + m_t1 + m_t2 + m_t3
    remaining_slots = max(0, capacity - len(core_invited))
    o_t1 = buckets.get("O1", [])[:remaining_slots]
    remaining_slots -= len(o_t1)
    o_t2 = buckets.get("O2", [])[:remaining_slots] if max_tier >= 2 and remaining_slots > 0 else []
    remaining_slots -= len(o_t2)
    o_t3 = buckets.get("O3", [])[:remaining_slots] if max_tier >= 3 and remaining_slots > 0 else []
    all_invited = core_invited + o_t1 + o_t2 + o_t3

    return {
        "summary": {
            "capacity":       capacity,
            "targetFemale":   target_f,
            "targetMale":     target_m,
            "femalePercent":  female_pct,
            "malePercent":    male_pct,
            "totalInvites":   len(all_invited),
            "breakdown": {
                "femTier1":    len(f_t1),
                "femTier2":    len(f_t2),
                "femTier3":    len(f_t3),
                "maleTier1":   len(m_t1),
                "maleTier2":   len(m_t2),
                "maleTier3":   len(m_t3),
                "otherTier1":  len(o_t1),
                "otherTier2":  len(o_t2),
                "otherTier3":  len(o_t3),
                "coldStartTier2Fallback": cold_start_tier2_fallback,
                "tier3Skipped": len(buckets["F3"]) + len(buckets["M3"]) + len(buckets["O3"]) if max_tier < 3 else 0,
            },
        },
        "members": all_invited,
    }


def _text_has_url(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in ("http://", "https://", "www.", ".com", ".net", ".org", "posh.vip", "eventbrite"))


def _validate_initial_invite_text(text: str, event: Dict[str, Any] | None = None) -> None:
    """Initial invite SMS must obey Jade gates before confirmation.

    Manual overrides and locked invite templates are send-only surfaces; they
    cannot leak venue, address, ticket links, parking, or full logistics.
    """
    raw = (text or "").strip()
    if not raw:
        return
    lowered = raw.lower()
    blocked_terms = []
    if _text_has_url(raw):
        blocked_terms.append("ticket/link")
    for term in (() if venue_mode(event or {}) == "invite" else ("parking", "park at", "address", "location is", "pull up to")):
        if term in lowered:
            blocked_terms.append(term)
    ev = event or {}
    for label, value in (("venue", ev.get("venue")), ("address", ev.get("address")), ("ticketUrl", ev.get("ticketUrl"))):
        value_s = str(value or "").strip()
        if value_s and value_s.lower() in lowered and not (label in {"venue", "address"} and venue_mode(ev) == "invite"):
            blocked_terms.append(label)
    if blocked_terms:
        unique = ", ".join(sorted(set(blocked_terms)))
        raise ValueError(f"Manual/locked invite text violates Jade gates before confirmation: {unique}")


def _distance_miles(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
    """Great-circle distance in miles between two latitude/longitude pairs."""
    try:
        a_lat, a_lon, b_lat, b_lon = map(float, (lat1, lon1, lat2, lon2))
    except (TypeError, ValueError) as exc:
        raise ValueError("valid coordinates are required") from exc
    radius_miles = 3958.7613
    phi1, phi2 = math.radians(a_lat), math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlambda = math.radians(b_lon - a_lon)
    hav = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_miles * math.asin(math.sqrt(hav))


def _event_promotion_geography(event: Dict[str, Any] | None) -> tuple[float, float, float] | None:
    """Return authoritative event lat/lon/radius, or None for legacy/no-geo events."""
    ev = event or {}
    if ev.get("latitude") is None or ev.get("longitude") is None or ev.get("promotionRadiusMiles") in (None, ""):
        return None
    try:
        radius = float(ev.get("promotionRadiusMiles"))
        latitude = float(ev.get("latitude"))
        longitude = float(ev.get("longitude"))
    except (TypeError, ValueError):
        return None
    if radius <= 0:
        return None
    return latitude, longitude, radius


def _apply_audience_filters(
    members: List[Dict[str, Any]],
    filters: Dict[str, Any] | None = None,
    event: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Apply operator filters plus the event's authoritative promotion radius.

    Phone area code is never geography. When an event has ZIP-derived coordinates
    and a promotion radius, members must have ZIP-derived coordinates and fall
    within that radius. Legacy members without coordinates remain eligible only
    when no geographic event radius is being applied.
    """
    f = filters or {}
    query = str(f.get("query") or f.get("search") or "").strip().lower()
    gender = str(f.get("gender") or "").strip().upper()
    tier = str(f.get("tier") or "").strip()
    market = str(f.get("market") or "").strip().lower()
    geo = _event_promotion_geography(event)
    out: List[Dict[str, Any]] = []
    for member in members:
        if geo:
            event_lat, event_lon, radius = geo
            if member.get("latitude") is None or member.get("longitude") is None:
                # A manually assigned market is an explicit operator decision,
                # not an inferred radius or phone-area-code guess.
                member_market = str(member.get("market") or "").strip().casefold()
                event_markets = {str((event or {}).get(key) or "").strip().casefold() for key in ("market", "city")}
                if not member_market or member_market not in event_markets:
                    continue
            else:
                try:
                    if _distance_miles(event_lat, event_lon, member.get("latitude"), member.get("longitude")) > radius:
                        continue
                except ValueError:
                    continue
        if query:
            haystack = " ".join(str(member.get(k) or "") for k in ("name", "lastName", "phone", "email", "instagram", "market", "city", "state", "zipCode")).lower()
            if query not in haystack:
                continue
        if gender and str(member.get("gender") or "").strip().upper() != gender:
            continue
        if tier and str(member.get("_tier") or calc_tier(member)) != tier:
            continue
        if market and market != "all":
            member_market = " ".join(str(member.get(k) or "") for k in ("market", "city", "state")).lower()
            if not member_market or market not in member_market:
                continue
        out.append(member)
    return out


def _invite_message_metadata(event: Dict[str, Any], message_override: str = "") -> Dict[str, str]:
    raw_override = (message_override or "").strip()
    raw_template = (event.get("invite_template") or "").strip() if event else ""
    source = "manual_override" if raw_override else "event_template" if raw_template else "generated_default"
    raw = raw_override or raw_template or "generated_default"
    return {
        "inviteMessageSource": source,
        "inviteMessageHash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
    }


def _build_sms_message(member: Dict[str, Any], event: Dict[str, Any], message_override: str = "") -> str:
    """
    Use locked invite_template if admin approved one.
    Replace {name} with member first name.
    Fall back to building from event fields if no template set.
    """
    name = (member.get("name") or "").split()[0] or ""

    override = (message_override or "").strip()
    if override:
        text = override.replace("{name}", name).strip()
        _validate_initial_invite_text(text, event)
        return text

    template = (event.get("invite_template") or "").strip()
    if template:
        text = template.replace("{name}", name).strip()
        _validate_initial_invite_text(text, event)
        return text

    if event and event.get("date"):
        date_display = _fmt_invite_date(event["date"])
        time_display = _fmt_invite_time(event.get("startTime", ""))
        event_label = (event.get("event_label") or "").strip()

        # Deterministic first invite, in Jade's voice. No venue/address/ticket/parking
        # before confirmation. The vibe tag is a private admin label — it is NOT pasted
        # into the invite as a raw fragment; the event_label carries the hook.
        parts = []
        if name:         parts.append(f"{name}.")
        if event_label:  parts.append(f"{event_label}.")
        if date_display: parts.append(f"{date_display}.")
        if time_display: parts.append(f"{time_display}.")
        if venue_mode(event) == "invite":
            location = ", ".join(str(event.get(key) or "").strip() for key in ("venue", "address") if event.get(key))
            if location:
                parts.append(location + ".")
        parts.append("You coming?")

        return " ".join(parts)
    else:
        if name:
            return f"{name}. You're on the list. Let me know."
        else:
            return "You're on the list. Let me know."
