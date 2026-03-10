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
from audit_log import log_action, ACTION_EVENT_UPDATED

logger = logging.getLogger()


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

    event_slug = (data.get("eventSlug") or "").strip()
    if not event_slug:
        raise ValueError("eventSlug is required")

    item = {
        "eventId": "current",
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
            "city", "vibe_tag", "dresscode", "description", "event_type",
            "allowPlusOnes", "ticketUrl", "sectionInfo", "event_status",
        }
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

    totals = {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0}
    by_gender = {g: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0} for g in ("M", "F", "O")}
    by_tier = {t: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0} for t in (1, 2, 3)}
    by_wave = {w: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0} for w in (0, 1, 2, 3)}

    for item in items:
        status = item.get("status", "INVITED")
        if status == "DELETED":
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
        attended = bool(item.get("attendedAt"))

        totals["invited"] += 1
        if status == "CONFIRMED":
            totals["confirmed"] += 1
        elif status == "DECLINED":
            totals["declined"] += 1
        elif status == "INVITED":
            totals["no_response"] += 1
        if attended:
            totals["attended"] += 1

        w = by_wave[wave]; w["invited"] += 1
        if status == "CONFIRMED": w["confirmed"] += 1
        elif status == "DECLINED": w["declined"] += 1
        elif status == "INVITED": w["no_response"] += 1
        if attended: w["attended"] += 1

        g = by_gender[gender]; g["invited"] += 1
        if status == "CONFIRMED": g["confirmed"] += 1
        elif status == "DECLINED": g["declined"] += 1
        if attended: g["attended"] += 1

        t = by_tier[tier]; t["invited"] += 1
        if status == "CONFIRMED": t["confirmed"] += 1
        elif status == "DECLINED": t["declined"] += 1
        if attended: t["attended"] += 1

    def rate(n, d):
        return round(n / d * 100, 1) if d else 0

    summary = {
        "totals": totals,
        "rates": {
            "confirm_rate": rate(totals["confirmed"], totals["invited"]),
            "decline_rate": rate(totals["declined"], totals["invited"]),
            "show_rate": rate(totals["attended"], totals["confirmed"]),
            "ghost_rate": rate(totals["confirmed"] - totals["attended"], totals["confirmed"]),
        },
        "by_gender": by_gender,
        "by_tier": by_tier,
        "by_wave": by_wave,
        "total_records": len(items),
    }
    return resp(headers, 200, {"ok": True, "eventId": event_id, "analytics": summary})


def get_events(event: dict, headers: dict, token: str) -> dict:
    history = list_event_history()
    return resp(headers, 200, {"ok": True, **history})
