"""
admin_event_routes.py
All /admin/event/* route handlers plus the public /event endpoint.

Multi-event model:
- Real event records live in rsvp-events with eventId == eventSlug.
- The special record eventId == "current" is only an active-event pointer.
- Existing /admin/event stays compatible and resolves the active event.
"""
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
from boto3.dynamodb.conditions import Key as DKey
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from admin_shared import (
    resp, get_body, events_table, coerce_bool,
    archive_event_snapshot, event_identity, normalize_event_date, normalize_event_time, validate_schedule_time_step,
)
from audit_log import log_action, ACTION_EVENT_UPDATED, ACTION_EVENT_STATUS_CHANGED
from member_store import CONFIRMED_FAMILY_STATUSES, attendance_is_settled, finalize_event_attendance
from reminder_schedule import sync_event_reminder_schedules, cancel_event_reminder_schedules, desired_schedule_specs
from location_resolver import ZipLookupUnavailable, resolve_us_zip

logger = logging.getLogger()

# ── Event lifecycle states ────────────────────────────────────────────────────
EVENT_STATES = ("DRAFT", "LIVE", "ARCHIVED")

REQUIRED_FOR_LIVE = {
    "eventSlug", "event_label", "date", "startTime",
    "event_timezone", "capacity",
}

VALID_TRANSITIONS = {
    "DRAFT":    {"LIVE", "ARCHIVED"},
    "LIVE":     {"DRAFT", "ARCHIVED"},
    "ARCHIVED": set(),
}

PUBLIC_EVENT_STATES = {"LIVE"}
ACTIVE_ELIGIBLE_STATES = PUBLIC_EVENT_STATES
LEGACY_CURRENT_DEFAULT_STATUS = (os.getenv("LEGACY_CURRENT_DEFAULT_STATUS", "LIVE") or "LIVE").strip().upper()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    try:
        zone = item.get("event_timezone") or "America/New_York"
        if zone != "UTC" and "/" not in zone:
            raise ValueError("use a regional time zone")
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        errors.append("event_timezone must be a valid IANA time zone")
    return errors


def _can_transition(current_state: str, new_state: str) -> bool:
    if not current_state:
        current_state = "DRAFT"
    if current_state == new_state:
        return True
    return new_state in VALID_TRANSITIONS.get(current_state, set())


def _event_slug_from_data(data: dict) -> str:
    event_slug = (data.get("eventSlug") or data.get("eventId") or "").strip()
    if not event_slug:
        raise ValueError("eventSlug is required")
    if event_slug == "current":
        raise ValueError("eventSlug cannot be 'current'")
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$', event_slug):
        raise ValueError("eventSlug must be lowercase letters, numbers, and hyphens only (e.g. rooftop-may2026)")
    return event_slug




def _decimal_coord(value):
    """Convert a resolved coordinate into a DynamoDB-safe Decimal."""
    return Decimal(str(value))


def _event_location(data: dict, existing: dict) -> dict:
    """Return canonical event ZIP/location/radius fields.

    Drafts may be saved before location is chosen. Once a ZIP is supplied it is
    authoritative: city/state/coordinates come from the ZIP resolver, not free
    text. Reuse existing canonical coordinates when the ZIP did not change so
    ordinary event edits do not depend on another external lookup.
    """
    if "eventZipCode" in data:
        zip_code = str(data.get("eventZipCode") or "").strip()
    else:
        zip_code = str(existing.get("eventZipCode") or "").strip()

    if "promotionRadiusMiles" in data:
        raw_radius = data.get("promotionRadiusMiles")
    else:
        raw_radius = existing.get("promotionRadiusMiles")

    radius = None
    if raw_radius not in (None, ""):
        try:
            radius = Decimal(str(raw_radius))
        except Exception as exc:
            raise ValueError("promotionRadiusMiles must be a positive number") from exc
        if radius <= 0:
            raise ValueError("promotionRadiusMiles must be a positive number")

    if not zip_code:
        return {
            "eventZipCode": "",
            "promotionRadiusMiles": radius,
            "city": str(existing.get("city") or data.get("city") or "").strip(),
            "state": str(existing.get("state") or "").strip(),
            "latitude": existing.get("latitude"),
            "longitude": existing.get("longitude"),
            "locationSource": existing.get("locationSource") or "",
        }

    same_zip = zip_code == str(existing.get("eventZipCode") or "").strip()
    if same_zip and existing.get("latitude") is not None and existing.get("longitude") is not None:
        return {
            "eventZipCode": zip_code,
            "promotionRadiusMiles": radius,
            "city": str(existing.get("city") or "").strip(),
            "state": str(existing.get("state") or "").strip(),
            "latitude": existing.get("latitude"),
            "longitude": existing.get("longitude"),
            "locationSource": existing.get("locationSource") or "zip",
        }

    resolved = resolve_us_zip(zip_code)
    return {
        "eventZipCode": resolved["zipCode"],
        "promotionRadiusMiles": radius,
        "city": resolved["city"],
        "state": resolved["state"],
        "latitude": _decimal_coord(resolved["latitude"]),
        "longitude": _decimal_coord(resolved["longitude"]),
        "locationSource": "zip",
    }


def _build_event_item(data: dict, *, existing: dict | None = None) -> dict:
    existing = existing or {}
    event_timezone = data.get("event_timezone") or existing.get("event_timezone") or "America/New_York"
    if not isinstance(event_timezone, str):
        raise ValueError("event_timezone must be a valid IANA time zone")
    event_timezone = event_timezone.strip()
    try:
        if event_timezone != "UTC" and "/" not in event_timezone:
            raise ValueError("use a regional time zone")
        ZoneInfo(event_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("event_timezone must be a valid IANA time zone, such as America/New_York") from exc
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

    event_slug = _event_slug_from_data(data)
    event_date = normalize_event_date(data.get("date"))
    start_time = normalize_event_time(data.get("startTime"), field_name="startTime")
    day_before_send_time = validate_schedule_time_step(data.get("day_before_send_time") or "18:00", field_name="day_before_send_time", minute_step=5)
    day_of_send_time = validate_schedule_time_step(data.get("day_of_send_time") or "11:00", field_name="day_of_send_time", minute_step=5)
    try:
        expected_show_rate = Decimal(str(data.get("expectedShowRate", existing.get("expectedShowRate", "0.60"))))
        if expected_show_rate > 1:
            expected_show_rate /= Decimal("100")
        if not Decimal("0.10") <= expected_show_rate <= Decimal("1.00"):
            raise ValueError
    except Exception as exc:
        raise ValueError("expectedShowRate must be between 10 and 100 percent") from exc

    explicit_status = (data.get("event_status") or "").strip().upper()
    if explicit_status == "INVITING":
        raise ValueError("INVITING is retired; use LIVE")
    legacy_existing_status = (existing.get("event_status") or "DRAFT").strip().upper()
    if legacy_existing_status == "INVITING":
        legacy_existing_status = "LIVE"
    requested_status = explicit_status or legacy_existing_status or "DRAFT"
    if requested_status not in EVENT_STATES:
        raise ValueError(f"event_status must be one of: {', '.join(EVENT_STATES)}")

    current_status = legacy_existing_status
    is_new_record = not bool(existing)
    location = _event_location(data, existing)
    if not is_new_record and requested_status != current_status:
        if not _can_transition(current_status, requested_status):
            allowed = ', '.join(sorted(VALID_TRANSITIONS.get(current_status, set()))) or 'none'
            raise ValueError(
                f"Cannot transition from {current_status} to {requested_status}. "
                f"Allowed: {allowed}"
            )

    if requested_status == "LIVE":
        check_item = {
            "eventSlug": event_slug,
            "event_label": (data.get("event_label") or "").strip(),
            "date": event_date,
            "startTime": start_time,
            "event_timezone": data.get("event_timezone"),
            "capacity": capacity,
        }
        missing = _validate_for_live(check_item)
        if not location.get("eventZipCode"):
            missing.append("eventZipCode")
        if location.get("promotionRadiusMiles") is None:
            missing.append("promotionRadiusMiles")
        if missing:
            raise ValueError(f"Cannot set to {requested_status} — missing: {', '.join(missing)}")

    item = {
        **existing,
        "eventId": event_slug,
        "eventSlug": event_slug,
        "slug": event_slug,
        "event_status": requested_status,
        "updatedAt": _now(),
        "date": event_date,
        "venue": (data.get("venue") or "").strip(),
        "dresscode": (data.get("dresscode") or "").strip(),
        "capacity": capacity,
        "expectedShowRate": expected_show_rate,
        "city": location.get("city") or "",
        "state": location.get("state") or "",
        "eventZipCode": location.get("eventZipCode") or "",
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "locationSource": location.get("locationSource") or "",
        "promotionRadiusMiles": location.get("promotionRadiusMiles"),
        "event_timezone": event_timezone,
        "address": (data.get("address") or "").strip(),
        "revealVenue": coerce_bool(data.get("revealVenue", False)),
        "venueReleaseMode": data.get("venueReleaseMode") if data.get("venueReleaseMode") in {"invite", "confirmation", "48_hours"} else ("confirmation" if coerce_bool(data.get("revealVenue", False)) else "48_hours"),
        "vibe_tag": (data.get("vibe_tag") or "").strip(),
        "event_label": (data.get("event_label") or "").strip(),
        "startTime": start_time,
        "day_before_send_time": day_before_send_time,
        "day_of_send_time": day_of_send_time,
        "reminderTiming": (data.get("reminderTiming") or "manual").strip(),
        # Field collapse: "description" is the single Event Intelligence source.
        # Accept legacy jadeNotes input and fold it into description; do not persist
        # jadeNotes as a separate field anymore.
        "description": (
            (data.get("description") or "").strip()
            or (data.get("jadeNotes") or data.get("jade_notes") or "").strip()
        ),
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
        "archived": coerce_bool(data.get("archived", existing.get("archived", False))),
    }
    if is_new_record:
        item["createdAt"] = existing.get("createdAt") or item["updatedAt"]
    return item


def _get_event_by_slug(event_slug: str) -> dict | None:
    if not event_slug or event_slug == "current":
        return None
    item = events_table().get_item(Key={"eventId": event_slug}, ConsistentRead=True).get("Item")
    if item and (item.get("event_status") or "").upper() == "INVITING":
        item = {**item, "event_status": "LIVE", "legacyInviting": True, "_snapshotStoredStatus": "INVITING"}
    return item


def _current_pointer() -> dict:
    return events_table().get_item(Key={"eventId": "current"}, ConsistentRead=True).get("Item") or {}


def get_current_event() -> dict | None:
    pointer = _current_pointer()
    active_slug = (pointer.get("activeEventSlug") or "").strip()
    if active_slug:
        ev = _get_event_by_slug(active_slug)
        if ev:
            return {**ev, "active": True}
    # Legacy fallback: old current record was the full event. Treat legacy current
    # rows as public-safe by default so deploy does not blank the live site before
    # a one-time data migration. New records still default to DRAFT.
    if pointer and (pointer.get("eventSlug") or pointer.get("date") or pointer.get("event_label")):
        legacy_status = (pointer.get("event_status") or LEGACY_CURRENT_DEFAULT_STATUS or "LIVE").upper()
        if legacy_status == "INVITING":
            legacy_status = "LIVE"
        elif legacy_status not in EVENT_STATES:
            legacy_status = "LIVE"
        legacy_slug = event_identity(pointer) or (pointer.get("eventSlug") or "legacy-current")
        return {
            **pointer,
            "event_status": legacy_status,
            "eventSlug": legacy_slug,
            "slug": legacy_slug,
            "active": True,
            "legacyCurrent": True,
        }
    return None


def _snapshot_guard(snapshot: dict) -> dict:
    """CAS for legacy rows and revisioned rows; returns low-level DDB values."""
    if not snapshot:
        return {"ConditionExpression": "attribute_not_exists(eventId)"}
    names, values = {}, {}
    conditions = ["attribute_exists(eventId)"]
    for index, field in enumerate(("revision", "updatedAt", "activeEventSlug", "event_status", "active", "archived", "hardDeleting")):
        alias = f"#guard{index}"
        names[alias] = field
        if field in snapshot:
            value = f":guard{index}"
            values[value] = TypeSerializer().serialize(snapshot.get("_snapshotStoredStatus", snapshot[field]) if field == "event_status" else snapshot[field])
            conditions.append(f"{alias} = {value}")
        else:
            conditions.append(f"attribute_not_exists({alias})")
    result = {"ConditionExpression": " AND ".join(conditions), "ExpressionAttributeNames": names}
    if values:
        result["ExpressionAttributeValues"] = values
    return result


def _commit_event_snapshot(item: dict, previous: dict, pointer: dict, *, clear_pointer: bool = False) -> None:
    """Commit an event edit only if both event and active pointer still match."""
    item.pop("_snapshotStoredStatus", None)
    item.pop("legacyInviting", None)
    table = events_table().name
    av = TypeSerializer().serialize
    pointer_operation = "Delete" if clear_pointer else "ConditionCheck"
    try:
        boto3.client("dynamodb").transact_write_items(TransactItems=[
            {"Put": {"TableName": table, "Item": {k: av(v) for k, v in item.items()}, **_snapshot_guard(previous)}},
            {pointer_operation: {"TableName": table, "Key": {"eventId": av("current")}, **_snapshot_guard(pointer)}},
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ValueError("Event or active event changed while saving; refresh and retry") from exc
        raise




def _save_event_record(data: dict, *, set_active: bool = False) -> dict:
    slug = _event_slug_from_data(data)
    existing = _get_event_by_slug(slug) or {}
    if existing and (coerce_bool(existing.get("hardDeleting")) or coerce_bool(existing.get("archived")) or (existing.get("event_status") or "").upper() == "ARCHIVED"):
        raise ValueError("archived events are read-only; duplicate the event before editing")

    pointer = _current_pointer()
    pointer_slug = (pointer.get("activeEventSlug") or pointer.get("eventSlug") or pointer.get("slug") or "").strip()

    item = _build_event_item(data, existing=existing)
    status = (item.get("event_status") or "DRAFT").upper()
    is_active_safe = status in ACTIVE_ELIGIBLE_STATES and not coerce_bool(item.get("archived"))

    if set_active and not is_active_safe:
        allowed = ", ".join(sorted(ACTIVE_ELIGIBLE_STATES))
        raise ValueError(f"Only {allowed} events can be set active")

    item["revision"] = uuid.uuid4().hex
    # Preserve active state until the separate activation transaction succeeds.
    item["active"] = bool(pointer_slug == slug and is_active_safe)
    _commit_event_snapshot(item, existing, pointer, clear_pointer=pointer_slug == slug and not is_active_safe)

    if set_active:
        set_active_event_by_slug(slug)
        item = {**item, "active": True}
    elif pointer_slug == slug:
        if is_active_safe:
            sync_event_reminder_schedules(item, active=True)
        else:
            cancel_event_reminder_schedules(slug)

    return item


def set_active_event_by_slug(event_slug: str) -> dict:
    """Atomically switch the canonical active LIVE event.

    The current pointer is the concurrency guard. Two admins/tabs that race from
    the same previous pointer cannot both win: the second transaction fails its
    conditional put rather than leaving event rows and the pointer out of sync.
    """
    ev = _get_event_by_slug(event_slug)
    if not ev:
        raise ValueError("event not found")
    status = (ev.get("event_status") or "DRAFT").upper()
    if coerce_bool(ev.get("archived")) or status == "ARCHIVED":
        raise ValueError("archived events cannot be active")
    if status not in ACTIVE_ELIGIBLE_STATES:
        allowed = ", ".join(sorted(ACTIVE_ELIGIBLE_STATES))
        raise ValueError(f"Only {allowed} events can be active; current status is {status}")
    try:
        promotion_radius = Decimal(str(ev.get("promotionRadiusMiles")))
        float(ev.get("latitude"))
        float(ev.get("longitude"))
        has_geo = bool(ev.get("eventZipCode")) and promotion_radius > 0
    except (TypeError, ValueError, ArithmeticError):
        has_geo = False
    if not has_geo:
        raise ValueError("Event ZIP code and promotion radius are required before making an event live")

    previous_pointer = _current_pointer()
    previous_slug = (previous_pointer.get("activeEventSlug") or "").strip()
    previous_event = {}
    if previous_slug and previous_slug != event_slug:
        previous_event = _get_event_by_slug(previous_slug) or {}
        previous_status = (previous_event.get("event_status") or "").upper()
        finalized = coerce_bool(previous_event.get("attendanceFinalized", False))
        if previous_status == "LIVE" and not finalized:
            raise ValueError("Close the active event before switching to another event")
    now = _now()
    table_name = events_table().name
    serializer = TypeSerializer()

    def av(value):
        return serializer.serialize(value)

    tx = []
    if previous_slug and previous_slug != event_slug:
        if (previous_event.get("event_status") or "").upper() == "LIVE":
            tx.append({
                "Update": {
                    "TableName": table_name,
                    "Key": {"eventId": av(previous_slug)},
                    "UpdateExpression": "SET active = :false, event_status = :draft, updatedAt = :now",
                    "ConditionExpression": "attribute_exists(eventId) AND event_status = :live AND (attribute_not_exists(archived) OR archived = :false)",
                    "ExpressionAttributeValues": {
                        ":live": av("LIVE"), ":false": av(False), ":draft": av("DRAFT"), ":now": av(now),
                    },
                }
            })

    tx.append({
        "Update": {
            "TableName": table_name,
            "Key": {"eventId": av(event_slug)},
            "UpdateExpression": "SET active = :true, updatedAt = :now",
            "ConditionExpression": "event_status = :live AND (attribute_not_exists(archived) OR archived = :false)",
            "ExpressionAttributeValues": {
                ":true": av(True),
                ":false": av(False),
                ":live": av("LIVE"),
                ":now": av(now),
            },
        }
    })

    pointer = {
        "eventId": "current",
        "activeEventSlug": event_slug,
        "active": True,
        "pointerVersion": 2,
        "revision": uuid.uuid4().hex,
        "updatedAt": now,
    }
    pointer_put = {
        "TableName": table_name,
        "Item": {k: av(v) for k, v in pointer.items()},
    }
    pointer_put.update(_snapshot_guard(previous_pointer))
    tx.append({"Put": pointer_put})

    try:
        boto3.client("dynamodb").transact_write_items(TransactItems=tx)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ValueError("active event changed while saving; retry") from exc
        raise

    ev = {**ev, "active": True, "updatedAt": now}
    if previous_slug and previous_slug != event_slug:
        cancel_event_reminder_schedules(previous_slug)
    sync_event_reminder_schedules(ev, active=True)
    return ev


def _all_event_records() -> list[dict]:
    table = events_table()
    items = []
    kwargs = {}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            if item.get("eventId") == "current":
                continue
            if (item.get("event_status") or "").upper() == "INVITING":
                item = {**item, "event_status": "LIVE", "legacyInviting": True, "_snapshotStoredStatus": "INVITING"}
            if item.get("eventSlug") or item.get("slug") or item.get("date"):
                items.append(item)
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    active_slug = (_current_pointer().get("activeEventSlug") or "").strip()
    normalized = []
    for item in items:
        slug = event_identity(item) or item.get("eventId")
        normalized.append({
            **item,
            "eventId": slug,
            "eventSlug": slug,
            "slug": slug,
            "label": item.get("event_label") or item.get("label") or slug,
            "active": bool(active_slug and slug == active_slug),
            "source": "record",
        })
    normalized.sort(key=lambda e: (1 if e.get("active") else 0, str(e.get("date") or ""), str(e.get("updatedAt") or "")), reverse=True)
    return normalized


def get_public_event(headers: dict) -> dict:
    # Retired public discovery route. Keep an empty response for old clients.
    return resp(headers, 200, {"ok": True, "event": {}})


def get_admin_event(event: dict, headers: dict, token: str) -> dict:
    ev = get_current_event()
    schedule = []
    if ev:
        try:
            specs = desired_schedule_specs(ev)
        except (ValueError, TypeError, KeyError):
            logger.warning("Event timing needs correction before scheduling")
            specs = []
        for spec in specs:
            day_of = spec["timing"] == "day_of"
            schedule.append({"label": "Day-of reminder" if day_of else "Venue / event reminder",
                "when": spec["scheduleExpression"][3:-1].replace("T", " "), "timezone": spec["timezone"],
                "message": ev.get("day_of_template" if day_of else "day_before_template", "")})
    return resp(headers, 200, {"ok": True, "event": ev or {}, "communicationSchedule": schedule})


def save_admin_event(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    set_active = coerce_bool(data.get("setActive", False))
    try:
        ev = _save_event_record(data, set_active=set_active)
    except ZipLookupUnavailable:
        logger.exception("event ZIP lookup unavailable")
        return resp(headers, 503, {"ok": False, "error": "ZIP lookup is temporarily unavailable"})
    except ValueError as ve:
        return resp(headers, 400, {"ok": False, "error": str(ve)})
    log_action(token=token, action=ACTION_EVENT_UPDATED,
               metadata={"date": ev.get("date"), "venue": ev.get("venue"),
                         "capacity": ev.get("capacity"), "eventSlug": ev.get("eventSlug"),
                         "setActive": set_active})
    return resp(headers, 200, {"ok": True, "event": ev})


def list_admin_events(event: dict, headers: dict, token: str) -> dict:
    events = _all_event_records()
    return resp(headers, 200, {"ok": True, "events": events, "meta": {"total": len(events)}})


def create_or_update_admin_event(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    set_active = coerce_bool(data.get("setActive", False))
    try:
        ev = _save_event_record(data, set_active=set_active)
    except ZipLookupUnavailable:
        logger.exception("event ZIP lookup unavailable")
        return resp(headers, 503, {"ok": False, "error": "ZIP lookup is temporarily unavailable"})
    except ValueError as ve:
        return resp(headers, 400, {"ok": False, "error": str(ve)})
    log_action(token=token, action=ACTION_EVENT_UPDATED, metadata={"eventSlug": ev.get("eventSlug"), "setActive": set_active})
    return resp(headers, 200, {"ok": True, "event": ev})


def set_active_admin_event(event: dict, headers: dict, token: str) -> dict:
    qs = event.get("queryStringParameters") or {}
    body = get_body(event)
    event_slug = (qs.get("eventSlug") or body.get("eventSlug") or body.get("slug") or "").strip()
    if not event_slug:
        return resp(headers, 400, {"ok": False, "error": "eventSlug required"})
    try:
        ev = set_active_event_by_slug(event_slug)
    except ValueError as ve:
        return resp(headers, 400, {"ok": False, "error": str(ve)})
    log_action(token=token, action=ACTION_EVENT_STATUS_CHANGED, metadata={"eventSlug": event_slug, "nextStatus": "ACTIVE"})
    return resp(headers, 200, {"ok": True, "event": ev})


def archive_admin_event(event: dict, headers: dict, token: str) -> dict:
    qs = event.get("queryStringParameters") or {}
    body = get_body(event)
    event_slug = (qs.get("eventSlug") or body.get("eventSlug") or body.get("slug") or "").strip()
    if not event_slug:
        return resp(headers, 400, {"ok": False, "error": "eventSlug required"})
    ev = _get_event_by_slug(event_slug)
    if not ev:
        return resp(headers, 404, {"ok": False, "error": "event not found"})
    snapshot_status = "OK"
    snapshot_error = ""
    try:
        archive_event_snapshot(ev)
    except Exception as exc:
        logger.exception("failed to snapshot event before archive")
        snapshot_status = "FAILED"
        snapshot_error = str(exc)[:500]
    archived = {
        **ev,
        "archived": True,
        "active": False,
        "event_status": "ARCHIVED",
        "updatedAt": _now(),
        "archiveSnapshotStatus": snapshot_status,
        "archiveSnapshotError": snapshot_error,
    }
    archived["revision"] = uuid.uuid4().hex
    pointer = _current_pointer()
    try:
        _commit_event_snapshot(archived, ev, pointer, clear_pointer=pointer.get("activeEventSlug") == event_slug)
    except ValueError as exc:
        return resp(headers, 409, {"ok": False, "error": str(exc)})
    cancel_event_reminder_schedules(event_slug)
    log_action(token=token, action=ACTION_EVENT_STATUS_CHANGED, metadata={"eventSlug": event_slug, "nextStatus": "ARCHIVED"})
    return resp(headers, 200, {"ok": True, "event": archived})


def finalize_admin_event_attendance(event: dict, headers: dict, token: str) -> dict:
    """Close one bounded attendance page and let the admin UI resume until done."""
    qs = event.get("queryStringParameters") or {}
    body = get_body(event)
    event_slug = (qs.get("eventSlug") or body.get("eventSlug") or body.get("slug") or "").strip()
    if not event_slug:
        ev_cur = get_current_event()
        event_slug = (event_identity(ev_cur) or ev_cur.get("eventSlug") or "").strip() if ev_cur else ""
    if not event_slug:
        return resp(headers, 400, {"ok": False, "error": "eventSlug required"})
    ev = _get_event_by_slug(event_slug)
    if not ev:
        return resp(headers, 404, {"ok": False, "error": "event not found"})
    if coerce_bool(ev.get("attendanceFinalized", False)):
        return resp(headers, 200, {"ok": True, "done": True, "alreadyFinalized": True,
                                   "finalizedAt": ev.get("attendanceFinalizedAt", ""), "eventSlug": event_slug})

    if (ev.get("attendanceFinalizationState") or "").upper() != "CLOSING":
        try:
            events_table().update_item(
                Key={"eventId": event_slug},
                UpdateExpression="SET event_status = :archived, attendanceFinalizationState = :closing, attendanceFinalizationStartedAt = :now",
                ConditionExpression="attribute_exists(eventId) AND (event_status = :live OR event_status = :archived) AND (attribute_not_exists(attendanceFinalized) OR attendanceFinalized = :false)",
                ExpressionAttributeValues={":archived": "ARCHIVED", ":live": "LIVE", ":closing": "CLOSING", ":false": False, ":now": _now()},
            )
            try:
                cancel_event_reminder_schedules(event_slug)
            except Exception:
                logger.exception("failed cancelling reminders while closing event=%s", event_slug)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            ev = _get_event_by_slug(event_slug) or {}
            if (ev.get("attendanceFinalizationState") or "").upper() != "CLOSING":
                return resp(headers, 409, {"ok": False, "error": "event changed while closing; refresh and retry"})

    try:
        summary = finalize_event_attendance(ev, cursor=(body.get("cursor") or ""), limit=100)
    except ValueError as exc:
        return resp(headers, 400, {"ok": False, "error": str(exc)})
    if not summary.get("ok"):
        return resp(headers, 503, {"ok": False, "error": "finalize_page_failed", "detail": summary})

    finalized_at = ""
    if summary.get("done"):
        finalized_at = _now()
        events_table().update_item(
            Key={"eventId": event_slug},
            UpdateExpression="SET attendanceFinalized = :t, attendanceFinalizedAt = :now, attendanceFinalizationState = :complete",
            ConditionExpression="attendanceFinalizationState = :closing AND (attribute_not_exists(attendanceFinalized) OR attendanceFinalized = :false)",
            ExpressionAttributeValues={":t": True, ":false": False, ":closing": "CLOSING", ":now": finalized_at, ":complete": "COMPLETE"},
        )
        log_action(token=token, action=ACTION_EVENT_STATUS_CHANGED,
                   metadata={"eventSlug": event_slug, "action": "FINALIZE_ATTENDANCE"})
    return resp(headers, 200, {
        "ok": True, "done": bool(summary.get("done")), "cursor": summary.get("nextCursor", ""),
        "finalizedAt": finalized_at, "eventSlug": event_slug,
        "memberNoShows": summary.get("memberNoShows", 0), "plusOneNoShows": summary.get("plusOneNoShows", 0),
        "rowsScanned": summary.get("rowsScanned", 0),
    })


def draft_admin_event_message(event: dict, headers: dict, token: str) -> dict:
    """Draft invite + day-before + day-of copy in Jade's voice for the operator to
    review, edit, and lock. Does NOT send anything — returns drafts only. Reuses the
    cached Jade persona prompt. The invite is guaranteed venue/address-free."""
    qs = event.get("queryStringParameters") or {}
    body = get_body(event)
    event_slug = (qs.get("eventSlug") or body.get("eventSlug") or body.get("slug") or "").strip()
    if not event_slug:
        ev_cur = get_current_event()
        event_slug = (ev_cur.get("eventSlug") or "").strip() if ev_cur else ""

    # Prefer the live form values the operator passed (unsaved edits), else the stored event.
    ev = _get_event_by_slug(event_slug) or {}
    draft_input = {
        "venueReleaseMode": body.get("venueReleaseMode") or ev.get("venueReleaseMode"),
        "revealVenue": body.get("revealVenue", ev.get("revealVenue")),
        "description": body.get("description", ev.get("description", "")),
        "event_label": body.get("event_label") or body.get("label") or ev.get("event_label") or ev.get("label"),
        "date":        body.get("date") or ev.get("date"),
        "startTime":   body.get("startTime") or body.get("time") or ev.get("startTime"),
        "vibe_tag":    body.get("vibe_tag") or body.get("vibe") or ev.get("vibe_tag"),
        "dresscode":   body.get("dresscode") or ev.get("dresscode"),
        "venue":       body.get("venue") or ev.get("venue"),
        "address":     body.get("address") or ev.get("address"),
        "allowPlusOnes": coerce_bool(body.get("allowPlusOnes", ev.get("allowPlusOnes"))),
    }
    if not (draft_input.get("date") or draft_input.get("event_label")):
        return resp(headers, 400, {"ok": False, "error": "event needs at least a label or date to draft"})

    try:
        from sms_handler import draft_template_messages
        drafts = draft_template_messages(draft_input)
    except Exception as e:
        logger.exception("draft_admin_event_message failed")
        return resp(headers, 502, {"ok": False, "error": "draft_failed", "detail": str(e)[:200]})

    return resp(headers, 200, {"ok": True, "drafts": drafts, "eventSlug": event_slug})


def duplicate_admin_event(event: dict, headers: dict, token: str) -> dict:
    qs = event.get("queryStringParameters") or {}
    body = get_body(event)
    event_slug = (qs.get("eventSlug") or body.get("eventSlug") or body.get("slug") or "").strip()
    new_slug = (body.get("newEventSlug") or f"{event_slug}-copy").strip()
    if not event_slug:
        return resp(headers, 400, {"ok": False, "error": "eventSlug required"})
    ev = _get_event_by_slug(event_slug)
    if not ev:
        return resp(headers, 404, {"ok": False, "error": "event not found"})
    duplicate = dict(ev)
    duplicate.update({
        "expectedShowRate": Decimal(str(ev.get("expectedShowRate", "0.60"))) ,
        "eventId": new_slug,
        "eventSlug": new_slug,
        "slug": new_slug,
        "event_status": "DRAFT",
        "active": False,
        "archived": False,
        "createdAt": _now(),
        "updatedAt": _now(),
    })
    for field in ("confirmedHeadcount", "confirmedHeadcountRevision", "plusOneReservations",
                  "attendanceFinalized", "attendanceFinalizedAt", "attendanceFinalizationState",
                  "observedShowRate", "lastBlastWave", "lastBlastAt", "lastBlastSmsSent",
                  "lastBlastTotal", "waveHistory", "reminderJobId", "reminderJobStatus",
                  "reminderSentAt", "activeEventSlug", "pointerRevision"):
        duplicate.pop(field, None)
    try:
        duplicate = _build_event_item(duplicate, existing={})
    except ZipLookupUnavailable:
        logger.exception("event ZIP lookup unavailable during duplicate")
        return resp(headers, 503, {"ok": False, "error": "ZIP lookup is temporarily unavailable"})
    except ValueError as ve:
        return resp(headers, 400, {"ok": False, "error": str(ve)})
    try:
        events_table().put_item(Item=duplicate, ConditionExpression="attribute_not_exists(eventId)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return resp(headers, 409, {"ok": False, "error": "An event with that name already exists. Choose another name."})
        raise
    log_action(token=token, action=ACTION_EVENT_UPDATED, metadata={"eventSlug": event_slug, "duplicatedTo": new_slug})
    return resp(headers, 200, {"ok": True, "event": duplicate})



def _delete_items_for_event(table_name_env: str, default_name: str, event_slug: str) -> int:
    table_name = os.getenv(table_name_env, default_name)
    table = boto3.resource("dynamodb").Table(table_name)
    deleted = 0
    try:
        kwargs = {"KeyConditionExpression": DKey("eventId").eq(event_slug)}
        while True:
            page = table.query(**kwargs)
            with table.batch_writer() as batch:
                for item in page.get("Items", []):
                    phone = item.get("phone")
                    if not phone:
                        continue
                    batch.delete_item(Key={"eventId": event_slug, "phone": phone})
                    deleted += 1
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {"ResourceNotFoundException", "ValidationException"}:
            raise
    return deleted


def hard_delete_admin_event(event: dict, headers: dict, token: str) -> dict:
    qs = event.get("queryStringParameters") or {}
    body = get_body(event)
    event_slug = (qs.get("eventSlug") or body.get("eventSlug") or body.get("slug") or "").strip()
    if not event_slug:
        return resp(headers, 400, {"ok": False, "error": "eventSlug required"})
    if event_slug == "current":
        return resp(headers, 400, {"ok": False, "error": "cannot hard delete current pointer directly"})
    ev = _get_event_by_slug(event_slug)
    if not ev:
        return resp(headers, 404, {"ok": False, "error": "event not found"})
    confirm_slug = (qs.get("confirmSlug") or body.get("confirmSlug") or "").strip()
    if confirm_slug != event_slug:
        return resp(headers, 400, {"ok": False, "error": "confirmSlug must exactly match eventSlug for hard delete"})

    frozen = {**ev, "hardDeleting": True, "archived": True, "active": False,
              "event_status": "ARCHIVED", "updatedAt": _now(), "revision": uuid.uuid4().hex}
    pointer = _current_pointer()
    try:
        _commit_event_snapshot(frozen, ev, pointer, clear_pointer=pointer.get("activeEventSlug") == event_slug)
    except ValueError as exc:
        return resp(headers, 409, {"ok": False, "error": str(exc)})
    invite_rows_deleted = _delete_items_for_event("INVITES_TABLE_NAME", "rsvp-event-invites", event_slug)
    checkin_rows_deleted = _delete_items_for_event("CHECKINS_TABLE_NAME", "rsvp-checkins", event_slug)
    events_table().delete_item(Key={"eventId": event_slug},
        ConditionExpression="hardDeleting = :yes AND revision = :revision",
        ExpressionAttributeValues={":yes": True, ":revision": frozen["revision"]})
    cancel_event_reminder_schedules(event_slug)
    log_action(token=token, action=ACTION_EVENT_STATUS_CHANGED, metadata={
        "eventSlug": event_slug,
        "nextStatus": "HARD_DELETED",
        "inviteRowsDeleted": invite_rows_deleted,
        "checkinRowsDeleted": checkin_rows_deleted,
    })
    return resp(headers, 200, {"ok": True, "deleted": True, "eventSlug": event_slug, "inviteRowsDeleted": invite_rows_deleted, "checkinRowsDeleted": checkin_rows_deleted})

def delete_admin_event(event: dict, headers: dict, token: str) -> dict:
    qs = event.get("queryStringParameters") or {}
    body = get_body(event)
    hard_delete = str(qs.get("hardDelete") or body.get("hardDelete") or "").strip().lower() in {"1", "true", "yes"}
    event_slug = (qs.get("eventSlug") or body.get("eventSlug") or body.get("slug") or "").strip()
    if hard_delete:
        return hard_delete_admin_event(event, headers, token)
    if event_slug:
        return archive_admin_event(event, headers, token)

    ev = get_current_event()
    if not ev:
        return resp(headers, 404, {"ok": False, "error": "no_current_event"})
    fake_event = {**event, "queryStringParameters": {"eventSlug": event_identity(ev) or ev.get("eventSlug")}}
    return archive_admin_event(fake_event, headers, token)


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

    # No-show is only real once attendance is settled (Close Event hit, or end time
    # + grace passed). Before that, confirmed-but-not-checked-in is still pending, NOT
    # a ghost. This stops the premature 100% ghost rate.
    _event_rec = _get_event_by_slug(event_id) or {}
    settled = attendance_is_settled(_event_rec)

    # Correct status accounting:
    # ATTENDED and NO_SHOW were previously CONFIRMED — they still count as confirmed.
    # Counting them separately prevents show_rate from exceeding 100% or going negative.
    EXCLUDE   = frozenset({"DELETED", "FAILED", "GUEST"})
    CONFIRMED_FAMILY = CONFIRMED_FAMILY_STATUSES  # centralized in member_store

    totals = {
        "members_invited": 0,
        "members_confirmed": 0,
        "members_attended": 0,
        "members_no_show": 0,
        "declined": 0,
        "no_response": 0,
        "failed": 0,
        "plus_one_confirmed": 0,
        "plus_one_attended": 0,
        "plus_one_no_show": 0,
        "expected_headcount": 0,
        "checked_in_headcount": 0,
        "no_show_headcount": 0,
        # Backward-compatible aliases consumed by existing frontend code.
        "invited": 0,
        "confirmed": 0,
        "attended": 0,
        "no_show": 0,
    }
    by_gender = {g: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0, "no_show": 0} for g in ("M", "F", "O")}
    by_tier   = {t: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0, "no_show": 0} for t in (1, 2, 3)}
    # Product rule: RSVP has exactly three formal waves. Anything else is
    # Manual / Resend, not a fake Wave 4+.
    by_wave   = {str(w): {"label": f"Wave {w}", "invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0, "no_show": 0, "plus_one_confirmed": 0, "plus_one_attended": 0, "plus_one_no_show": 0, "expected_headcount": 0, "checked_in_headcount": 0, "no_show_headcount": 0} for w in (1, 2, 3)}
    by_wave["manual"] = {"label": "Manual / Resend", "invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0, "no_show": 0, "plus_one_confirmed": 0, "plus_one_attended": 0, "plus_one_no_show": 0, "expected_headcount": 0, "checked_in_headcount": 0, "no_show_headcount": 0}

    for item in items:
        status = (item.get("status") or "INVITED").upper()
        if status in EXCLUDE:
            if status == "FAILED":
                totals["failed"] += 1
            continue

        gender = (item.get("gender") or "O").upper()
        if gender not in ("M", "F"):
            gender = "O"
        tier = int(item.get("tier", 1))
        if tier not in (1, 2, 3):
            tier = 1
        try:
            wave_number = int(item.get("waveNumber", 0) or 0)
        except Exception:
            wave_number = 0
        wave_key = str(wave_number) if wave_number in (1, 2, 3) else "manual"

        totals["members_invited"] += 1
        totals["invited"] += 1

        member_confirmed = status in CONFIRMED_FAMILY
        member_attended = status == "ATTENDED" or bool(item.get("attendedAt"))
        member_no_show = settled and status == "NO_SHOW"
        plus_one_confirmed = member_confirmed and bool((item.get("plusOneName") or "").strip())
        plus_one_attended = bool(item.get("plusOneAttendedAt"))
        plus_one_no_show = settled and plus_one_confirmed and not plus_one_attended

        if member_confirmed:
            totals["members_confirmed"] += 1
            totals["confirmed"] += 1
        if plus_one_confirmed:
            totals["plus_one_confirmed"] += 1
        if member_attended:
            totals["members_attended"] += 1
        if plus_one_attended:
            totals["plus_one_attended"] += 1
        if plus_one_no_show:
            totals["plus_one_no_show"] += 1
        if member_no_show:
            totals["members_no_show"] += 1
        elif status == "DECLINED":
            totals["declined"] += 1
        elif status == "INVITED":
            totals["no_response"] += 1


        # by_wave — formal Wave 1–3 plus Manual/Resend bucket.
        w = by_wave[wave_key]
        w["invited"] += 1
        if member_confirmed:
            w["confirmed"] += 1
            w["expected_headcount"] += 1
        if plus_one_confirmed:
            w["plus_one_confirmed"] += 1
            w["expected_headcount"] += 1
        if member_attended:
            w["attended"] += 1
            w["checked_in_headcount"] += 1
        if plus_one_attended:
            w["plus_one_attended"] += 1
            w["checked_in_headcount"] += 1
        if plus_one_no_show:
            w["plus_one_no_show"] += 1
        if member_no_show:
            w["no_show"] += 1
        elif status == "DECLINED":
            w["declined"] += 1
        elif status == "INVITED":
            w["no_response"] += 1

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

    # Finalize backward-compatible aliases once, after all rows are counted.
    totals["expected_headcount"] = totals["members_confirmed"] + totals["plus_one_confirmed"]
    totals["checked_in_headcount"] = totals["members_attended"] + totals["plus_one_attended"]
    # Ghost/no-show headcount is headcount-based, not member-row based.
    # A confirmed +1 who does not check in must count as a no-show.
    # A confirmed member who never checks in also counts as a no-show/ghost.
    totals["no_show_headcount"] = max(0, totals["expected_headcount"] - totals["checked_in_headcount"]) if settled else 0
    totals["attended"] = totals["checked_in_headcount"]
    totals["no_show"] = totals["no_show_headcount"]

    for wave_data in by_wave.values():
        wave_expected = int(wave_data.get("expected_headcount", 0) or 0)
        wave_checked_in = int(wave_data.get("checked_in_headcount", 0) or 0)
        wave_data["no_show_headcount"] = max(0, wave_expected - wave_checked_in) if settled else 0

    def rate(n, d):
        return round(n / d * 100, 1) if d else 0

    summary = {
        "attendanceSettled": settled,
        "totals": totals,
        "rates": {
            "confirm_rate":      rate(totals["confirmed"],   totals["invited"]),
            "decline_rate":      rate(totals["declined"],    totals["invited"]),
            "no_response_rate":  rate(totals["no_response"], totals["invited"]),
            "show_rate":         rate(totals["checked_in_headcount"], totals["expected_headcount"]) if settled else None,
            "no_show_rate":      rate(totals["no_show_headcount"],    totals["expected_headcount"]) if settled else None,
        },
        "by_gender": by_gender,
        "by_tier":   by_tier,
        "by_wave":   by_wave,
        "total_records": len(items),
    }
    return resp(headers, 200, {"ok": True, "eventId": event_id, "analytics": summary})
