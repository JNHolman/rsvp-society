"""Shared event disclosure and cutoff decisions; all times are timezone aware."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def event_start(event):
    date = str(event.get("date") or "")[:10]
    value = str(event.get("startTime") or "")
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            clock = datetime.strptime(value, fmt).time()
            return datetime.combine(datetime.strptime(date, "%Y-%m-%d").date(), clock,
                                    ZoneInfo(event.get("event_timezone") or "America/New_York"))
        except ValueError:
            continue
    raise ValueError("Event date and start time are required")


def venue_mode(event):
    mode = event.get("venueReleaseMode")
    if mode in {"invite", "confirmation", "48_hours"}:
        return mode
    legacy = event.get("revealVenue", False)
    return "confirmation" if legacy is True or str(legacy).lower() in {"true", "1"} else "48_hours"


def venue_available(event, *, confirmed=False, now=None):
    mode = venue_mode(event)
    if mode == "invite":
        return True
    if not confirmed:
        return False
    if mode == "confirmation":
        return True
    try:
        return (now or datetime.now(timezone.utc)) >= event_start(event).astimezone(timezone.utc) - timedelta(hours=48)
    except (ValueError, TypeError, KeyError):
        return False


def cutoff_reached(event, hours, *, now=None):
    try:
        return (now or datetime.now(timezone.utc)) >= event_start(event).astimezone(timezone.utc) - timedelta(hours=hours)
    except (ValueError, TypeError, KeyError):
        return True
