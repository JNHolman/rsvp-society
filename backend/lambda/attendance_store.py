"""Attendance/check-in persistence for RSVP Society.

Extracted from member_store without changing its public behavior. member_store
re-exports the public attendance functions/constants for compatibility.
"""
import logging
import os
from typing import Any, Dict

import boto3
from boto3.dynamodb.conditions import Key as DKey
from boto3.dynamodb.types import TypeSerializer

from store_common import (
    CONFIRMED_FAMILY_STATUSES,
    _now_iso,
    _table,
    _ttl_90_days,
    normalize_phone,
)

logger = logging.getLogger()

def _checkins_table():
    name = os.getenv("CHECKINS_TABLE_NAME")
    if not name:
        raise RuntimeError(
            "CHECKINS_TABLE_NAME env var is not set. "
            "Deploy checkins.tf and add it to the Lambda environment."
        )
    return boto3.resource("dynamodb").Table(name)


def _invites_table():
    name = os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites")
    return boto3.resource("dynamodb").Table(name)


# Structured attendance result codes
ATTENDANCE_OK               = "OK"
ATTENDANCE_ALREADY          = "ALREADY_CHECKED_IN"
ATTENDANCE_NOT_CONFIRMED    = "NOT_CONFIRMED"
ATTENDANCE_INVITE_NOT_FOUND = "INVITE_NOT_FOUND"
ATTENDANCE_INVALID_STATUS   = "INVALID_STATUS"
ATTENDANCE_DDB_ERROR        = "DDB_ERROR"


def _ddb_av(value):
    """Serialize a Python value to a DynamoDB AttributeValue for transactions."""
    return TypeSerializer().serialize(value)


def _ddb_key(**kwargs) -> Dict[str, Any]:
    return {k: _ddb_av(v) for k, v in kwargs.items()}


def _attendance_failure_reason(phone_e164: str, event_id: str, *, attended: bool) -> dict:
    """
    Diagnose a failed transactional attendance/no-show write.
    This keeps the admin response useful without trusting partial writes.
    """
    try:
        existing = _checkins_table().get_item(
            Key={"eventId": event_id, "phone": phone_e164}
        ).get("Item")
        if existing:
            return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in"}
    except Exception:
        logger.exception("record_attendance: checkin diagnostic failed phone=...%s", phone_e164[-4:])

    try:
        invite = _invites_table().get_item(
            Key={"eventId": event_id, "phone": phone_e164}
        ).get("Item")
    except Exception:
        logger.exception("record_attendance: invite diagnostic failed phone=...%s", phone_e164[-4:])
        return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Database error while checking invite status"}

    if not invite:
        return {"ok": False, "result": ATTENDANCE_INVITE_NOT_FOUND, "reason": "Invite not found"}

    status = (invite.get("status") or "").upper()
    if attended and status == "ATTENDED":
        return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in"}
    if not attended and status == "NO_SHOW":
        return {"ok": False, "result": ATTENDANCE_INVALID_STATUS, "reason": "Already marked no-show"}

    # If the invite IS in a checkin-eligible state but the transaction still
    # cancelled, the failure was a concurrent write/transaction conflict — NOT a
    # status problem. Returning NOT_CONFIRMED here was misleading (it claimed the
    # member was unconfirmed while showing status CONFIRMED). Surface a retryable
    # conflict instead so the door operator gets an accurate, actionable message.
    if status in CONFIRMED_FAMILY_STATUSES:
        return {
            "ok": False,
            "result": "ATTENDANCE_CONFLICT",
            "reason": "Check-in collided with another update for this guest. Refresh and tap again.",
        }

    return {
        "ok": False,
        "result": ATTENDANCE_NOT_CONFIRMED,
        "reason": f"Invite status is {status or 'missing'}; CONFIRMED required",
    }



def _best_effort_corrected_no_show_counter(phone_e164: str) -> None:
    """Remove an old no-show tally after a successful NO_SHOW -> ATTENDED correction."""
    from botocore.exceptions import ClientError
    try:
        _table().update_item(
            Key={"phone": phone_e164},
            UpdateExpression="ADD noShowCount :minus_one",
            ConditionExpression="attribute_exists(phone) AND noShowCount >= :one",
            ExpressionAttributeValues={":minus_one": -1, ":one": 1},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.exception("corrected no-show counter update failed phone=...%s", phone_e164[-4:])
    except Exception:
        logger.exception("corrected no-show counter update failed phone=...%s", phone_e164[-4:])


def record_attendance(phone: str, attended: bool, event_id: str = "current") -> dict:
    """
    Record attendance/no-show for a member at a specific event.
    Returns {"ok": bool, "result": ATTENDANCE_* constant, "reason": str}.

    State-integrity rule:
      - Check-in and the attendedCount increment commit together with the invite
        and check-in rows, so a successful check-in cannot silently lose tier data.
      - No-show status and noShowCount commit together with the invite row.
      - CONFIRMED can become ATTENDED or NO_SHOW; NO_SHOW can be corrected
        to ATTENDED. An ATTENDED invite cannot count again after check-in TTL expiry.
    """
    from botocore.exceptions import ClientError

    phone_e164 = normalize_phone(phone)
    now = _now_iso()

    # Use a RAW low-level client, not _ddb().meta.client. The resource's client has
    # boto3's automatic Python<->DynamoDB serialization attached; every value below is
    # already hand-serialized via _ddb_av (TypeSerializer), so the resource client would
    # double-serialize them and DynamoDB rejects the transaction ("Invalid attribute
    # value type"), which surfaced as a deterministic check-in failure. The raw client
    # passes the pre-serialized AttributeValues through unchanged (the +1 path does the
    # same). Do NOT change this back to _ddb().meta.client.
    client = boto3.client("dynamodb")

    invites_table = _invites_table().name
    checkins_table = _checkins_table().name
    members_table = _table().name

    if attended:
        prior_status = ""
        try:
            prior_invite = _invites_table().get_item(
                Key={"eventId": event_id, "phone": phone_e164}
            ).get("Item") or {}
            prior_status = (prior_invite.get("status") or "").upper()
        except Exception:
            logger.exception("record_attendance: prior invite lookup failed phone=...%s", phone_e164[-4:])

        # Fast path for clearer door/admin UX; the transaction below also protects races.
        try:
            existing = _checkins_table().get_item(
                Key={"eventId": event_id, "phone": phone_e164}
            ).get("Item")
            if existing:
                return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in"}
        except Exception:
            logger.exception("record_attendance: checkin lookup failed phone=...%s", phone_e164[-4:])

        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": invites_table,
                            "Key": _ddb_key(eventId=event_id, phone=phone_e164),
                            "UpdateExpression": "SET attendedAt = :now, #s = :attended REMOVE noShowAt",
                            "ConditionExpression": "attribute_exists(phone) AND #s IN (:confirmed, :noshow_existing)",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":now": _ddb_av(now),
                                ":attended": _ddb_av("ATTENDED"),
                                ":confirmed": _ddb_av("CONFIRMED"),
                                ":noshow_existing": _ddb_av("NO_SHOW"),
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": checkins_table,
                            "Item": {
                                "eventId": _ddb_av(event_id),
                                "phone": _ddb_av(phone_e164),
                                "checkedInAt": _ddb_av(now),
                                "ttl": _ddb_av(_ttl_90_days()),
                            },
                            "ConditionExpression": "attribute_not_exists(phone)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": members_table,
                            "Key": _ddb_key(phone=phone_e164),
                            "UpdateExpression": (
                                "SET attendedCount = if_not_exists(attendedCount, :zero) + :one, "
                                "lastSeenAt = :ls"
                            ),
                            "ConditionExpression": "attribute_exists(phone)",
                            "ExpressionAttributeValues": {
                                ":zero": _ddb_av(0), ":one": _ddb_av(1), ":ls": _ddb_av(now),
                            },
                        }
                    },
                ]
            )
            if prior_status == "NO_SHOW":
                _best_effort_corrected_no_show_counter(phone_e164)
            return {"ok": True, "result": ATTENDANCE_OK, "reason": ""}
        except ClientError as ce:
            code = ce.response.get("Error", {}).get("Code")
            if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
                return _attendance_failure_reason(phone_e164, event_id, attended=True)
            logger.exception("record_attendance: transactional check-in failed phone=...%s", phone_e164[-4:])
            return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Database error during check-in"}
        except Exception:
            logger.exception("record_attendance: transactional check-in failed phone=...%s", phone_e164[-4:])
            return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Unexpected error during check-in"}

    # No-show path
    try:
        existing = _checkins_table().get_item(
            Key={"eventId": event_id, "phone": phone_e164}
        ).get("Item")
        if existing:
            return {"ok": False, "result": ATTENDANCE_ALREADY, "reason": "Already checked in — cannot mark no-show"}
    except Exception:
        logger.exception("record_attendance: checkin lookup failed phone=...%s", phone_e164[-4:])

    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": invites_table,
                        "Key": _ddb_key(eventId=event_id, phone=phone_e164),
                        "UpdateExpression": "SET noShowAt = :now, #s = :noshow",
                        "ConditionExpression": "attribute_exists(phone) AND #s = :confirmed",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":now": _ddb_av(now),
                            ":noshow": _ddb_av("NO_SHOW"),
                            ":confirmed": _ddb_av("CONFIRMED"),
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": members_table,
                        "Key": _ddb_key(phone=phone_e164),
                        "UpdateExpression": "SET noShowCount = if_not_exists(noShowCount, :zero) + :one",
                        "ConditionExpression": "attribute_exists(phone)",
                        "ExpressionAttributeValues": {
                            ":zero": _ddb_av(0), ":one": _ddb_av(1),
                        },
                    }
                },
            ]
        )
        return {"ok": True, "result": ATTENDANCE_OK, "reason": ""}
    except ClientError as ce:
        code = ce.response.get("Error", {}).get("Code")
        if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
            return _attendance_failure_reason(phone_e164, event_id, attended=False)
        logger.exception("record_attendance: transactional no-show failed phone=...%s", phone_e164[-4:])
        return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Database error during no-show"}
    except Exception:
        logger.exception("record_attendance: transactional no-show failed phone=...%s", phone_e164[-4:])
        return {"ok": False, "result": ATTENDANCE_DDB_ERROR, "reason": "Unexpected error during no-show"}


def finalize_event_attendance(event: dict, *, cursor: str = "", limit: int = 100) -> dict:
    """Settle one bounded page of a closing event; safe to resume from its cursor."""
    import base64
    import json
    from botocore.exceptions import ClientError

    event_id = (event.get("eventId") or event.get("eventSlug") or "current").strip()
    if event_id == "current":
        event_id = (event.get("eventSlug") or "current").strip()
    inv_t = _invites_table()
    kwargs = {"KeyConditionExpression": DKey("eventId").eq(event_id), "Limit": max(1, min(int(limit), 100))}
    if cursor:
        try:
            raw = cursor + "=" * (-len(cursor) % 4)
            kwargs["ExclusiveStartKey"] = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid attendance finalization cursor") from exc
    page = inv_t.query(**kwargs)
    items = page.get("Items", [])
    member_no_shows = 0
    plus_one_no_shows = 0
    failures = []
    now = _now_iso()

    for invite in items:
        status = (invite.get("status") or "").upper()
        phone = invite.get("phone") or ""
        if status == "CONFIRMED" and not invite.get("attendedAt"):
            result = record_attendance(phone, attended=False, event_id=event_id)
            if result.get("ok"):
                member_no_shows += 1
            else:
                latest = inv_t.get_item(Key={"eventId": event_id, "phone": phone}, ConsistentRead=True).get("Item") or {}
                if (latest.get("status") or "").upper() not in {"NO_SHOW", "ATTENDED"}:
                    failures.append({"phone": phone, "kind": "member", "result": result.get("result"), "reason": result.get("reason")})

        plus_name = (invite.get("plusOneName") or "").strip()
        if plus_name and not invite.get("plusOneAttendedAt") and not invite.get("plusOneNoShowAt"):
            try:
                inv_t.update_item(
                    Key={"eventId": event_id, "phone": phone},
                    UpdateExpression="SET plusOneNoShowAt = :now",
                    ConditionExpression=("attribute_exists(phone) AND attribute_exists(plusOneName) AND plusOneName <> :empty "
                                         "AND attribute_not_exists(plusOneAttendedAt) AND attribute_not_exists(plusOneNoShowAt)"),
                    ExpressionAttributeValues={":now": now, ":empty": ""},
                )
                plus_one_no_shows += 1
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    latest = inv_t.get_item(Key={"eventId": event_id, "phone": phone}, ConsistentRead=True).get("Item") or {}
                    if latest.get("plusOneNoShowAt") or latest.get("plusOneAttendedAt"):
                        continue
                logger.exception("finalize +1 no-show stamp failed phone=...%s", str(phone)[-4:])
                failures.append({"phone": phone, "kind": "plusOne", "reason": type(exc).__name__})
            except Exception as exc:
                logger.exception("finalize +1 no-show stamp failed phone=...%s", str(phone)[-4:])
                failures.append({"phone": phone, "kind": "plusOne", "reason": type(exc).__name__})

    last_key = page.get("LastEvaluatedKey")
    next_cursor = ""
    if last_key:
        next_cursor = base64.urlsafe_b64encode(json.dumps(last_key, separators=(",", ":")).encode()).decode().rstrip("=")
    return {
        "ok": not failures,
        "eventId": event_id,
        "memberNoShows": member_no_shows,
        "plusOneNoShows": plus_one_no_shows,
        "rowsScanned": len(items),
        "failureCount": len(failures),
        "failures": failures[:20],
        "nextCursor": next_cursor,
        "done": not bool(last_key),
        "finalizedAt": now if not last_key else "",
    }
