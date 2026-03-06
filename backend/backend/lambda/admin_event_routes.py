"""
admin_event_routes.py
All /admin/event/* route handlers plus the public /event endpoint.
"""
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key as DKey

from admin_shared import resp, get_body, events_table, coerce_bool
from audit_log import log_action, ACTION_EVENT_UPDATED

logger = logging.getLogger()


# ── Event read/write ──────────────────────────────────────────────────────────

def get_current_event() -> dict | None:
    result = events_table().get_item(Key={"eventId": "current"})
    return result.get("Item")


def _set_current_event(data: dict) -> dict:
    raw_capacity = data.get("capacity")
    try:
        capacity = int(raw_capacity or 0)
        if capacity < 0:
            raise ValueError("negative")
    except (ValueError, TypeError):
        raise ValueError(f"capacity must be a non-negative integer, got: {raw_capacity!r}")

    shared_reminder    = (data.get("reminder_template")    or "").strip()
    day_before_template = (data.get("day_before_template") or shared_reminder).strip()
    day_of_template     = (data.get("day_of_template")     or shared_reminder).strip()

    item = {
        "eventId":             "current",
        "updatedAt":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eventSlug":           (data.get("eventSlug")        or "").strip(),
        "date":                (data.get("date")             or "").strip(),
        "venue":               (data.get("venue")            or "").strip(),
        "dresscode":           (data.get("dresscode")        or "").strip(),
        "capacity":            capacity,
        "city":                (data.get("city")             or "").strip(),
        "event_timezone":      (data.get("event_timezone")   or "America/New_York").strip() or "America/New_York",
        "address":             (data.get("address")          or "").strip(),
        "revealVenue":         coerce_bool(data.get("revealVenue", False)),
        "vibe_tag":            (data.get("vibe_tag")         or "").strip(),
        "event_label":         (data.get("event_label")      or "").strip(),
        "startTime":           (data.get("startTime")        or "").strip(),
        "reminderTiming":      (data.get("reminderTiming")   or "manual").strip(),
        "description":         (data.get("description")      or "").strip(),
        "event_type":          (data.get("event_type")       or "").strip(),
        "invite_template":     (data.get("invite_template")  or "").strip(),
        "reminder_template":   shared_reminder,
        "day_before_template": day_before_template,
        "day_of_template":     day_of_template,
    }
    events_table().put_item(Item=item)
    return item


# ── Public GET /event (no auth required) ─────────────────────────────────────

def get_public_event(headers: dict) -> dict:
    ev = get_current_event()
    if ev:
        reveal = coerce_bool(ev.get("revealVenue", False))
        hidden = {"venue", "address"} if not reveal else set()
        public = {k: v for k, v in ev.items() if k not in hidden}
    else:
        public = {}
    return resp(headers, 200, {"ok": True, "event": public})


# ── GET /admin/event ──────────────────────────────────────────────────────────

def get_admin_event(event: dict, headers: dict, token: str) -> dict:
    ev = get_current_event()
    return resp(headers, 200, {"ok": True, "event": ev or {}})


# ── POST /admin/event ─────────────────────────────────────────────────────────

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


# ── GET /admin/event/analytics ────────────────────────────────────────────────

def get_analytics(event: dict, headers: dict, token: str) -> dict:
    invites_t = boto3.resource("dynamodb").Table(
        os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites")
    )
    qs = event.get("queryStringParameters") or {}
    event_id = (qs.get("eventId") or "current").strip() or "current"

    # Paginate — large events can exceed a single DDB page
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
    by_gender = {
        "M": {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
        "F": {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
        "O": {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
    }
    by_tier = {
        1: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
        2: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
        3: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
    }
    by_wave = {
        1: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0},
        2: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0},
        3: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0},
        0: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0},
    }

    for item in items:
        status = item.get("status", "INVITED")

        # Skip tombstoned records entirely — they should not affect any totals
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
            # Only genuine pending INVITED records count as no_response
            totals["no_response"] += 1

        if attended:
            totals["attended"] += 1

        w = by_wave[wave]
        w["invited"] += 1
        if status == "CONFIRMED":   w["confirmed"] += 1
        elif status == "DECLINED":  w["declined"]  += 1
        elif status == "INVITED":   w["no_response"] += 1
        if attended:                w["attended"]  += 1

        g = by_gender[gender]
        g["invited"] += 1
        if status == "CONFIRMED":  g["confirmed"] += 1
        elif status == "DECLINED": g["declined"]  += 1
        if attended:               g["attended"]  += 1

        t = by_tier[tier]
        t["invited"] += 1
        if status == "CONFIRMED":  t["confirmed"] += 1
        elif status == "DECLINED": t["declined"]  += 1
        if attended:               t["attended"]  += 1

    def rate(n, d):
        return round(n / d * 100, 1) if d else 0

    summary = {
        "totals": totals,
        "rates": {
            "confirm_rate": rate(totals["confirmed"], totals["invited"]),
            "decline_rate": rate(totals["declined"],  totals["invited"]),
            "show_rate":    rate(totals["attended"],  totals["confirmed"]),
            "ghost_rate":   rate(totals["confirmed"] - totals["attended"], totals["confirmed"]),
        },
        "by_gender":     by_gender,
        "by_tier":       by_tier,
        "by_wave":       by_wave,
        "total_records": len(items),
    }
    return resp(headers, 200, {"ok": True, "eventId": event_id, "analytics": summary})
