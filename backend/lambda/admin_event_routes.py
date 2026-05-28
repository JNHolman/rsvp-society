"""
admin_event_routes.py
All /admin/event/* route handlers plus the public /event endpoint.
"""
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key as DKey

from admin_shared import (
    resp, get_body, events_table, coerce_bool,
    archive_event_snapshot, event_identity, list_event_history, normalize_event_date, normalize_event_time, validate_schedule_time_step,
)
from audit_log import log_action, ACTION_EVENT_UPDATED, ACTION_EVENT_STATUS_CHANGED

logger = logging.getLogger()

# ── Event lifecycle states ────────────────────────────────────────────────────
EVENT_STATES = ("DRAFT", "LIVE", "INVITING", "LOCKED", "CHECK_IN_OPEN", "COMPLETED", "ARCHIVED")

REQUIRED_FOR_LIVE = {
    "eventSlug", "event_label", "date", "startTime",
    "event_timezone", "capacity", "city",
}

VALID_TRANSITIONS = {
    "DRAFT":         {"LIVE"},
    "LIVE":          {"INVITING", "DRAFT", "ARCHIVED"},
    "INVITING":      {"LIVE", "LOCKED"},
    "LOCKED":        {"CHECK_IN_OPEN", "INVITING"},
    "CHECK_IN_OPEN": {"COMPLETED"},
    "COMPLETED":     {"ARCHIVED"},
    "ARCHIVED":      set(),
}

INVITABLE_STATES  = {"LIVE", "INVITING"}
CONFIRMABLE_STATES = {"LIVE", "INVITING", "LOCKED", "CHECK_IN_OPEN"}
CHECKIN_STATES    = {"LOCKED", "CHECK_IN_OPEN"}


def _validate_for_live(item: dict) -> list:
    errors = []
    for field in REQUIRED_FOR_LIVE:
        val = item.get(field)
        if not val and val != 0:
            errors.append(field)
    try:
        if int(item.get("capacity", 0)) < 1:
            errors.append("capacity must be at least 1")
    except (TypeError, ValueError):
        errors.append("capacity must be a number")
    return errors


def _can_transition(current_state: str, new_state: str) -> bool:
    if not current_state:
        current_state = "DRAFT"
    return new_state in VALID_TRANSITIONS.get(current_state, set())


def get_current_event() -> dict | None:
    result = events_table().get_item(Key={"eventId": "current"})
    return result.get("Item")


def _set_current_event(data: dict) -> dict:
    existing_current = get_current_event() or {}
    raw_capacity = data.get("capacity")
    try:
        capacity = int(raw_capacity or 0)
        if capacity < 0:
            raise ValueError("negative")
    except (ValueError, TypeError):
        raise ValueError(f"capacity must be a non-negative integer, got: {raw_capacity!r}")

    shared_reminder = (data.get("reminder_template") or "").strip()
    day_before_template = (data.get("day_before_template") or shared_reminder).strip()
    day_of_template = (data.get("day_of_template") or shared_reminder).strip()

    event_date = normalize_event_date(data.get("date"))
    start_time = normalize_event_time(data.get("startTime"), field_name="startTime")
    day_before_send_time = validate_schedule_time_step(data.get("day_before_send_time") or "18:00", field_name="day_before_send_time", minute_step=5)
    day_of_send_time = validate_schedule_time_step(data.get("day_of_send_time") or "11:00", field_name="day_of_send_time", minute_step=5)

    import re as _re
    event_slug = (data.get("eventSlug") or "").strip()
    if not event_slug:
        raise ValueError("eventSlug is required")
    if not _re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$', event_slug):
        raise ValueError("eventSlug must be lowercase letters, numbers, and hyphens only (e.g. rooftop-may2026)")

    # ── Event status lifecycle ───────────────────────────────────────────────
    requested_status = (data.get("event_status") or "DRAFT").strip().upper()
    if requested_status not in EVENT_STATES:
        raise ValueError(f"event_status must be one of: {', '.join(EVENT_STATES)}")

    current_status = (existing_current.get("event_status") or "DRAFT").upper()

    if requested_status != current_status:
        if not _can_transition(current_status, requested_status):
            allowed = ', '.join(VALID_TRANSITIONS.get(current_status, set())) or 'none'
            raise ValueError(
                f"Cannot transition from {current_status} to {requested_status}. "
                f"Allowed: {allowed}"
            )
        # Validate required fields before going active
        if requested_status in ("LIVE", "INVITING", "LOCKED", "CHECK_IN_OPEN"):
            check_item = {
                "eventSlug":      (data.get("eventSlug") or "").strip(),
                "event_label":    (data.get("event_label") or "").strip(),
                "date":           data.get("date"),
                "startTime":      data.get("startTime"),
                "event_timezone": data.get("event_timezone"),
                "capacity":       data.get("capacity"),
                "city":           (data.get("city") or "").strip(),
            }
            missing = _validate_for_live(check_item)
            if missing:
                raise ValueError(
                    f"Cannot set to {requested_status} — missing: {', '.join(missing)}"
                )

    item = {
        "eventId": "current",
        "event_status": requested_status,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eventSlug": event_slug,
        "date": event_date,
        "venue": (data.get("venue") or "").strip(),
        "dresscode": (data.get("dresscode") or "").strip(),
        "capacity": capacity,
        "city": (data.get("city") or "").strip(),
        "event_timezone": (data.get("event_timezone") or "America/New_York").strip() or "America/New_York",
        "address": (data.get("address") or "").strip(),
        "revealVenue": coerce_bool(data.get("revealVenue", False)),
        "vibe_tag": (data.get("vibe_tag") or "").strip(),
        "event_label": (data.get("event_label") or "").strip(),
        "startTime": start_time,
        "day_before_send_time": day_before_send_time,
        "day_of_send_time": day_of_send_time,
        "reminderTiming": (data.get("reminderTiming") or "manual").strip(),
        "description": (data.get("description") or "").strip(),
        "event_type": (data.get("event_type") or "").strip(),
        "invite_template": (data.get("invite_template") or "").strip(),
        "reminder_template": shared_reminder,
        "day_before_template": day_before_template,
        "day_of_template": day_of_template,
        "endTime": normalize_event_time(data.get("endTime"), field_name="endTime") if data.get("endTime") else "",
        "allowPlusOnes": coerce_bool(data.get("allowPlusOnes", False)),
        "ticketUrl": (data.get("ticketUrl") or "").strip(),
        "sectionInfo": (data.get("sectionInfo") or "").strip(),
        "parkingInfo": (data.get("parkingInfo") or "").strip(),
        "privateNotes": (data.get("privateNotes") or "").strip(),
    }
    previous_identity = event_identity(existing_current)
    next_identity = event_identity(item)
    has_existing_shape = existing_current and any(str(existing_current.get(k) or '').strip() for k in ("eventSlug", "date", "venue", "city", "event_label"))
    if has_existing_shape and previous_identity:
        archive_event_snapshot(existing_current)

    events_table().put_item(Item=item)
    return item


def get_public_event(headers: dict) -> dict:
    ev = get_current_event()
    if ev:
        # Only expose fields that are safe for the public landing page.
        # Templates, reminder config, capacity, and ops metadata stay hidden.
        PUBLIC_FIELDS = {
            "eventSlug", "event_label", "date", "startTime", "endTime",
            "city", "vibe_tag", "dresscode", "event_type",
            "allowPlusOnes", "event_status",
        }
        # Excluded intentionally — private fields that must not reach unauthenticated clients:
        # "description"  — Jade private ops brief (venue, capacity, bar plan)
        # "sectionInfo"  — table/section layout, admin/Jade context only
        # "ticketUrl"    — can expose Posh/Eventbrite page with venue before confirmation
        reveal = coerce_bool(ev.get("revealVenue", False))
        if reveal:
            PUBLIC_FIELDS.update({"venue", "address", "revealVenue"})
        public = {k: v for k, v in ev.items() if k in PUBLIC_FIELDS}
    else:
        public = {}
    return resp(headers, 200, {"ok": True, "event": public})


def get_admin_event(event: dict, headers: dict, token: str) -> dict:
    ev = get_current_event()
    return resp(headers, 200, {"ok": True, "event": ev or {}})


def save_admin_event(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    try:
        ev = _set_current_event(data)
    except ValueError as ve:
        return resp(headers, 400, {"ok": False, "error": str(ve)})
    log_action(token=token, action=ACTION_EVENT_UPDATED,
               metadata={"date": ev.get("date"), "venue": ev.get("venue"),
                         "capacity": ev.get("capacity"), "eventSlug": ev.get("eventSlug")})
    return resp(headers, 200, {"ok": True, "event": ev})


def get_analytics(event: dict, headers: dict, token: str) -> dict:
    invites_t = boto3.resource("dynamodb").Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))
    qs = event.get("queryStringParameters") or {}
    event_id = (qs.get("eventId") or "").strip()
    if not event_id:
        return resp(headers, 400, {"ok": False, "error": "eventId query parameter required"})
    items = []
    kwargs: dict = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
    while True:
        page = invites_t.query(**kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    # Correct status accounting:
    # ATTENDED and NO_SHOW were previously CONFIRMED — they still count as confirmed.
    # Counting them separately prevents show_rate from exceeding 100% or going negative.
    EXCLUDE   = frozenset({"DELETED", "SKIPPED_CONSENT", "FAILED"})
    CONFIRMED_FAMILY = CONFIRMED_FAMILY_STATUSES  # centralized in member_store

    totals = {
        "invited": 0, "confirmed": 0, "attended": 0,
        "no_show": 0, "declined": 0, "no_response": 0,
        "failed": 0, "skipped_consent": 0,
    }
    by_gender = {g: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0, "no_show": 0} for g in ("M", "F", "O")}
    by_tier   = {t: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0, "no_show": 0} for t in (1, 2, 3)}
    by_wave   = {w: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0, "no_show": 0} for w in (0, 1, 2, 3)}

    for item in items:
        status = (item.get("status") or "INVITED").upper()
        if status in EXCLUDE:
            if status == "FAILED":          totals["failed"] += 1
            elif status == "SKIPPED_CONSENT": totals["skipped_consent"] += 1
            continue

        gender = (item.get("gender") or "O").upper()
        if gender not in ("M", "F"):
            gender = "O"
        tier = int(item.get("tier", 1))
        if tier not in (1, 2, 3):
            tier = 1
        wave = int(item.get("waveNumber", 0) or 0)
        if wave not in (1, 2, 3):
            wave = 0

        totals["invited"] += 1

        # ATTENDED and NO_SHOW were confirmed — count them in confirmed bucket too
        if status in CONFIRMED_FAMILY:
            totals["confirmed"] += 1
        if status == "ATTENDED":
            totals["attended"] += 1
        elif status == "NO_SHOW":
            totals["no_show"] += 1
        elif status == "DECLINED":
            totals["declined"] += 1
        elif status == "INVITED":
            totals["no_response"] += 1

        # by_wave
        w = by_wave[wave]
        w["invited"] += 1
        if status in CONFIRMED_FAMILY: w["confirmed"] += 1
        if status == "ATTENDED":       w["attended"] += 1
        elif status == "NO_SHOW":      w["no_show"] += 1
        elif status == "DECLINED":     w["declined"] += 1
        elif status == "INVITED":      w["no_response"] += 1

        # by_gender
        g = by_gender[gender]
        g["invited"] += 1
        if status in CONFIRMED_FAMILY: g["confirmed"] += 1
        if status == "ATTENDED":       g["attended"] += 1
        elif status == "NO_SHOW":      g["no_show"] += 1
        elif status == "DECLINED":     g["declined"] += 1

        # by_tier
        t = by_tier[tier]
        t["invited"] += 1
        if status in CONFIRMED_FAMILY: t["confirmed"] += 1
        if status == "ATTENDED":       t["attended"] += 1
        elif status == "NO_SHOW":      t["no_show"] += 1
        elif status == "DECLINED":     t["declined"] += 1

    def rate(n, d):
        return round(n / d * 100, 1) if d else 0

    summary = {
        "totals": totals,
        "rates": {
            "confirm_rate":      rate(totals["confirmed"],   totals["invited"]),
            "decline_rate":      rate(totals["declined"],    totals["invited"]),
            "no_response_rate":  rate(totals["no_response"], totals["invited"]),
            "show_rate":         rate(totals["attended"],    totals["confirmed"]),
            "no_show_rate":      rate(totals["no_show"],     totals["confirmed"]),
        },
        "by_gender": by_gender,
        "by_tier":   by_tier,
        "by_wave":   by_wave,
        "total_records": len(items),
    }
    return resp(headers, 200, {"ok": True, "eventId": event_id, "analytics": summary})


def get_events(event: dict, headers: dict, token: str) -> dict:
    history = list_event_history()
    return resp(headers, 200, {"ok": True, **history})
