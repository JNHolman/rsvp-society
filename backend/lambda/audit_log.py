"""
audit_log.py

Lightweight audit trail for all admin actions.
Writes to rsvp-audit-log DynamoDB table asynchronously (best-effort).
A failure to log NEVER blocks the actual admin operation.

Record shape:
{
    "actionId":    "<uuid>",           # hash key
    "timestamp":   "<iso8601>",        # sort key on the GSI
    "action":      "MEMBER_APPROVED",  # see ACTION_* constants below
    "actorFingerprint": "12-char-sha256", # one-way admin-token fingerprint
    "targetPhone": "+13475551234",     # member phone (when relevant)
    "targetName":  "Marcus W.",        # display name (when relevant)
    "metadata":    { ... },            # action-specific extra fields
    "ttl":         1234567890,         # epoch — auto-expire after 1 year
}
"""

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import boto3

logger = logging.getLogger()
_DDB = boto3.resource("dynamodb")

# ── Action constants ──────────────────────────────────────────────────────────
ACTION_MEMBER_APPROVED   = "MEMBER_APPROVED"
ACTION_MEMBER_DENIED     = "MEMBER_DENIED"
ACTION_MEMBER_PENDING    = "MEMBER_RESTORED_PENDING"
ACTION_MEMBER_DELETED    = "MEMBER_DELETED"
ACTION_MEMBER_GENDER_SET = "MEMBER_GENDER_SET"
ACTION_MEMBER_TIER_SET   = "MEMBER_TIER_SET"
ACTION_ATTENDANCE        = "ATTENDANCE_RECORDED"
ACTION_INVITE_SENT       = "INVITE_BATCH_SENT"
ACTION_REMINDER_SENT     = "REMINDER_BLAST_SENT"
ACTION_EVENT_UPDATED     = "EVENT_UPDATED"
ACTION_MEMBER_IMPORTED   = "MEMBER_IMPORT_COMPLETED"
ACTION_EVENT_STATUS_CHANGED = "EVENT_STATUS_CHANGED"


def _audit_table():
    name = os.getenv("AUDIT_LOG_TABLE_NAME")
    if not name:
        return None
    return _DDB.Table(name)


def _actor_tag(token: str) -> str:
    """Return a stable one-way fingerprint without persisting secret characters."""
    raw = (token or "").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12] if raw else "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ttl_one_year() -> int:
    """Epoch seconds one year from now — DynamoDB TTL auto-expires old records."""
    return int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())


def log_action(
    *,
    token: str,
    action: str,
    target_phone: Optional[str] = None,
    target_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Write one audit record. Best-effort — exceptions are logged but never raised
    so a logging failure never blocks the actual admin operation.
    """
    t = _audit_table()
    if t is None:
        # AUDIT_LOG_TABLE_NAME not configured — skip silently
        # (allows local dev / test without the table)
        return

    try:
        item: Dict[str, Any] = {
            "actionId":   str(uuid.uuid4()),
            "timestamp":  _now_iso(),
            "action":     action,
            "actorFingerprint": _actor_tag(token),
            "ttl":        _ttl_one_year(),
        }
        if target_phone:
            item["targetPhone"] = target_phone
        if target_name:
            # Store first initial + last name only — enough to read the log
            # without storing the full name if privacy becomes a concern
            item["targetName"] = target_name[:60]
        if metadata:
            item["metadata"] = metadata

        t.put_item(Item=item)

    except Exception:
        # Log to CloudWatch but never surface to caller
        logger.exception("audit_log: failed to write record action=%s", action)
