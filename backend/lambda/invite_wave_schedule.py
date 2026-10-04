"""One-time, low-volume scheduling for the next automatic invite wave."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

from admin_shared import normalize_event_date, normalize_event_time, resolve_event_slug

logger = logging.getLogger(__name__)

RESPONSE_WINDOW_HOURS = 48
MIN_EVENT_LEAD_HOURS = 24


def _event_start_utc(event: dict) -> datetime:
    event_date = normalize_event_date(str(event.get("date") or ""))
    start_time = normalize_event_time(str(event.get("startTime") or ""), field_name="startTime", allow_blank=False)
    timezone_name = str(event.get("event_timezone") or "America/New_York").strip()
    local_start = datetime.strptime(f"{event_date} {start_time}", "%Y-%m-%d %H:%M").replace(
        tzinfo=ZoneInfo(timezone_name)
    )
    return local_start.astimezone(timezone.utc)


def next_wave_schedule_spec(event: dict, completed_wave: int, *, now: datetime | None = None) -> dict | None:
    """Wave 2 follows after 48h; Wave 3 after 72h. Never compress the gap."""
    if completed_wave not in (1, 2):
        return None
    slug = resolve_event_slug(event)
    if not slug or (event.get("event_status") or "DRAFT").upper() not in {"LIVE", "INVITING"}:
        return None

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    event_start = _event_start_utc(event)
    latest_safe_send = event_start - timedelta(hours=MIN_EVENT_LEAD_HOURS)
    fire_at = current + timedelta(hours=48 if completed_wave == 1 else 72)
    if fire_at > latest_safe_send:
        return None

    target_wave = completed_wave + 1
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:16]
    return {
        "name": f"rsvp-auto-wave-{digest}-w{target_wave}",
        "eventId": slug,
        "previousWaveNumber": completed_wave,
        "waveNumber": target_wave,
        "fireAt": fire_at,
        "responseWindowHours": int((fire_at - current).total_seconds() // 3600),
    }


def schedule_next_wave(event: dict, completed_wave: int, *, female_percent: int = 60,
                       audience_filters: dict | None = None, now: datetime | None = None) -> dict:
    spec = next_wave_schedule_spec(event, completed_wave, now=now)
    if not spec:
        return {"scheduled": False, "reason": "outside_wave_window_or_event_too_close"}

    role_arn = (os.getenv("AUTO_WAVE_SCHEDULER_ROLE_ARN") or "").strip()
    function_arn = (os.getenv("INVITE_HANDLER_ARN") or "").strip()
    if not role_arn or not function_arn:
        raise RuntimeError("auto-wave Scheduler role or invite Lambda ARN is not configured")

    payload = {
        "source": "rsvp.auto-wave",
        "eventId": spec["eventId"],
        "previousWaveNumber": spec["previousWaveNumber"],
        "femalePercent": int(female_percent),
        "audienceFilters": audience_filters if isinstance(audience_filters, dict) else {},
    }
    request = {
        "Name": spec["name"],
        "Description": f"RSVP Society automatic Wave {spec['waveNumber']} for {spec['eventId']}",
        "ScheduleExpression": f"at({spec['fireAt'].strftime('%Y-%m-%dT%H:%M:%S')})",
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ActionAfterCompletion": "DELETE",
        "Target": {
            "Arn": function_arn,
            "RoleArn": role_arn,
            "Input": json.dumps(payload, separators=(",", ":")),
            "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 2},
        },
    }
    client = boto3.client("scheduler")
    try:
        client.create_schedule(**request)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConflictException":
            raise
        # A retry of the completed invite job must not move the response deadline.
        logger.info("auto-wave schedule already exists name=%s", spec["name"])
        return {"scheduled": True, "alreadyExists": True, "name": spec["name"],
                "waveNumber": spec["waveNumber"], "responseWindowHours": spec["responseWindowHours"]}
    logger.info("auto-wave scheduled event=%s wave=%d hours=%d", spec["eventId"],
                spec["waveNumber"], spec["responseWindowHours"])
    return {"scheduled": True, "name": spec["name"], "waveNumber": spec["waveNumber"],
            "responseWindowHours": spec["responseWindowHours"]}
