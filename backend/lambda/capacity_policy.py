"""Shared RSVP and wave capacity assumptions."""
from __future__ import annotations

import math

DEFAULT_EXPECTED_SHOW_RATE = 0.60


def expected_show_rate(event: dict | None) -> float:
    """Use the event's configured rate, falling back to 60% for a new market."""
    try:
        value = float((event or {}).get("expectedShowRate", DEFAULT_EXPECTED_SHOW_RATE))
    except (TypeError, ValueError):
        value = DEFAULT_EXPECTED_SHOW_RATE
    return min(1.0, max(0.10, value))


def target_confirmed_headcount(capacity: int, event: dict | None) -> int:
    """Seat ceiling shared by RSVP confirmations, +1 reservations and wave planning."""
    seats = max(0, int(capacity or 0))
    return math.ceil(seats / expected_show_rate(event)) if seats else 0
