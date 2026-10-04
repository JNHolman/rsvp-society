"""One-time reminder scheduling for the active RSVP event.

The admin Lambda owns schedule creation/deletion. EventBridge Scheduler invokes
``reminder_handler`` exactly at the saved local reminder time, so we do not poll
Lambda every five minutes all month.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from event_policy import event_start, venue_mode

import boto3
from botocore.exceptions import ClientError

from admin_shared import normalize_event_date, normalize_event_time, resolve_event_slug

logger = logging.getLogger(__name__)

_TIMINGS = ("day_before", "day_of")


def _schedule_name(event_slug: str, timing: str) -> str:
    digest = hashlib.sha1(event_slug.encode("utf-8")).hexdigest()[:16]
    return f"rsvp-reminder-{digest}-{timing}"


def _timing_enabled(mode: str, timing: str) -> bool:
    mode = (mode or "manual").strip().lower()
    return mode == "both" or mode == timing


def desired_schedule_specs(event: dict, *, now: datetime | None = None) -> list[dict]:
    """Return future one-time schedule specs for an active LIVE event."""
    slug = resolve_event_slug(event)
    if not slug or (event.get("event_status") or "DRAFT").upper() != "LIVE":
        return []

    mode = (event.get("reminderTiming") or "manual").strip().lower()
    if mode == "manual":
        return []

    event_date = datetime.strptime(
        normalize_event_date(str(event.get("date") or "")), "%Y-%m-%d"
    ).date()
    tz_name = (event.get("event_timezone") or "America/New_York").strip() or "America/New_York"
    zone = ZoneInfo(tz_name)
    local_now = now.astimezone(zone) if now else datetime.now(zone)

    specs = []
    for timing in _TIMINGS:
        if not _timing_enabled(mode, timing):
            continue
        is_day_of = timing == "day_of"
        field = "day_of_send_time" if is_day_of else "day_before_send_time"
        default_time = "11:00" if is_day_of else "18:00"
        send_time = normalize_event_time(event.get(field) or default_time, field_name=field, allow_blank=False)
        fire_date = event_date if is_day_of else event_date - timedelta(days=1)
        target_local = datetime.strptime(
            f"{fire_date.isoformat()} {send_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=zone)
        if not is_day_of and venue_mode(event) == "48_hours":
            target_local = (event_start(event).astimezone(timezone.utc) - timedelta(hours=48)).astimezone(zone)
            fire_date = target_local.date()
            send_time = target_local.strftime("%H:%M")
        if target_local <= local_now:
            continue
        specs.append({
            "name": _schedule_name(slug, timing),
            "timing": timing,
            "eventSlug": slug,
            "date": event_date.isoformat(),
            "sendTime": send_time,
            "timezone": tz_name,
            "venueReleaseMode": venue_mode(event),
            "startTime": str(event.get("startTime") or ""),
            "scheduleExpression": f"at({fire_date.isoformat()}T{send_time}:00)",
        })
    return specs


def _scheduler_config() -> tuple[str, str] | None:
    reminder_arn = (os.getenv("REMINDER_LAMBDA_ARN") or "").strip()
    role_arn = (os.getenv("REMINDER_SCHEDULER_ROLE_ARN") or "").strip()
    if not reminder_arn or not role_arn:
        return None
    return reminder_arn, role_arn


def _delete_schedule(client, name: str) -> None:
    try:
        client.delete_schedule(Name=name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


def cancel_event_reminder_schedules(event_slug: str) -> dict:
    """Delete any pending reminder schedules for an event. No-op outside AWS config."""
    if not event_slug or not _scheduler_config():
        return {"configured": False, "deleted": 0}
    client = boto3.client("scheduler")
    deleted = 0
    for timing in _TIMINGS:
        _delete_schedule(client, _schedule_name(event_slug, timing))
        deleted += 1
    return {"configured": True, "deleted": deleted}


def sync_event_reminder_schedules(event: dict, *, active: bool = True) -> dict:
    """Replace an event's pending reminder schedules with its current saved plan."""
    slug = resolve_event_slug(event)
    config = _scheduler_config()
    if not slug or not config:
        return {"configured": False, "scheduled": 0}

    reminder_arn, role_arn = config
    client = boto3.client("scheduler")

    # Deterministic names make rescheduling idempotent and remove stale timing modes.
    for timing in _TIMINGS:
        _delete_schedule(client, _schedule_name(slug, timing))

    if not active:
        return {"configured": True, "scheduled": 0}

    specs = desired_schedule_specs(event)
    for spec in specs:
        payload = {
            "source": "scheduler",
            "timing": spec["timing"],
            "eventSlug": spec["eventSlug"],
            "expectedDate": spec["date"],
            "expectedSendTime": spec["sendTime"],
            "expectedTimezone": spec["timezone"],
            "expectedVenueReleaseMode": spec["venueReleaseMode"],
            "expectedStartTime": spec["startTime"],
        }
        client.create_schedule(
            Name=spec["name"],
            Description=f"RSVP Society {spec['timing']} reminder for {slug}",
            ScheduleExpression=spec["scheduleExpression"],
            ScheduleExpressionTimezone=spec["timezone"],
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",
            Target={
                "Arn": reminder_arn,
                "RoleArn": role_arn,
                "Input": json.dumps(payload, separators=(",", ":")),
                "RetryPolicy": {
                    "MaximumEventAgeInSeconds": 3600,
                    "MaximumRetryAttempts": 2,
                },
            },
        )
    logger.info("synced %s reminder schedule(s) for %s", len(specs), slug)
    return {"configured": True, "scheduled": len(specs)}
