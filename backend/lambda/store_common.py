"""Shared member/attendance primitives; no application-layer imports."""
import os
import re
from datetime import datetime, timezone
import boto3

CONFIRMED_FAMILY_STATUSES = frozenset({"CONFIRMED", "ATTENDED", "NO_SHOW"})

def _ddb():
    return boto3.resource("dynamodb")


def _table():
    name = os.getenv("MEMBERS_TABLE_NAME")
    if not name:
        raise RuntimeError("MEMBERS_TABLE_NAME env var is not set")
    return _ddb().Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ttl_90_days() -> int:
    return int((datetime.now(timezone.utc).timestamp()) + (90 * 24 * 60 * 60))


def normalize_phone(raw: str) -> str:
    if not raw:
        raise ValueError("phone is required")
    s = raw.strip()
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
        if not (10 <= len(digits) <= 15):
            raise ValueError("phone must be valid E.164 length (10–15 digits)")
        return f"+{digits}"
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if 10 <= len(digits) <= 15:
        return f"+{digits}"
    raise ValueError("phone must be valid E.164")
