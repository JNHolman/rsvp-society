"""Atomic release of confirmed invite headcount and plus-one reservations."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer

from store_common import CONFIRMED_FAMILY_STATUSES
from sms_plus_one import plus_one_reservation_key

logger = logging.getLogger(__name__)


def cancellation_timing(event: dict, now: datetime) -> str:
    """Classify against the event's local start time; exactly 24 hours is timely."""
    try:
        start = datetime.fromisoformat(f"{event['date'][:10]}T{event['startTime']}")
        start = start.replace(tzinfo=ZoneInfo(event["event_timezone"])).astimezone(timezone.utc)
    except (KeyError, ValueError, TypeError):
        # Missing legacy scheduling data cannot justify a tier exemption.
        return "UNKNOWN_TIME"
    return "TIMELY" if now <= start - timedelta(hours=24) else "LATE"


def _count_confirmed_headcount(invites_table, event_id: str) -> int:
    total = 0
    kwargs = {"KeyConditionExpression": Key("eventId").eq(event_id), "ConsistentRead": True}
    while True:
        page = invites_table.query(**kwargs)
        for invite in page.get("Items", []):
            if (invite.get("status") or "").upper() not in CONFIRMED_FAMILY_STATUSES:
                continue
            total += 1 + bool((invite.get("plusOneName") or "").strip())
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return total


def _ensure_counter(events_table, invites_table, event_id: str) -> int:
    event = events_table.get_item(
        Key={"eventId": event_id}, ProjectionExpression="confirmedHeadcount", ConsistentRead=True,
    ).get("Item") or {}
    if "confirmedHeadcount" in event:
        return int(event.get("confirmedHeadcount") or 0)
    baseline = _count_confirmed_headcount(invites_table, event_id)
    response = events_table.update_item(
        Key={"eventId": event_id},
        UpdateExpression="SET confirmedHeadcount = if_not_exists(confirmedHeadcount, :baseline)",
        ConditionExpression="attribute_exists(eventId)",
        ExpressionAttributeValues={":baseline": baseline},
        ReturnValues="ALL_NEW",
    )
    return int((response.get("Attributes") or {}).get("confirmedHeadcount", baseline) or 0)


def reconcile_confirmed_headcount(*, events_table, invites_table, event_id: str, attempts: int = 4) -> int:
    """Repair a stale seat counter using a revision-guarded source-of-truth scan.

    Every runtime counter mutation increments confirmedHeadcountRevision in the
    same transaction. Reading the counter/revision before the strongly consistent
    invite scan and requiring both values to remain unchanged prevents a stale
    scan from overwriting a concurrent RSVP or +1 change.
    """
    for _ in range(max(1, attempts)):
        event = events_table.get_item(
            Key={"eventId": event_id},
            ProjectionExpression="confirmedHeadcount, confirmedHeadcountRevision",
            ConsistentRead=True,
        ).get("Item") or {}
        if not event:
            raise RuntimeError("event not found during headcount reconciliation")
        observed_count = int(event.get("confirmedHeadcount") or 0)
        revision_present = "confirmedHeadcountRevision" in event
        observed_revision = int(event.get("confirmedHeadcountRevision") or 0)
        actual_count = _count_confirmed_headcount(invites_table, event_id)
        if actual_count == observed_count:
            return actual_count
        condition = "attribute_exists(eventId) AND confirmedHeadcount = :observed"
        values = {":observed": observed_count, ":actual": actual_count, ":one": 1}
        if revision_present:
            condition += " AND confirmedHeadcountRevision = :revision"
            values[":revision"] = observed_revision
        else:
            condition += " AND attribute_not_exists(confirmedHeadcountRevision)"
        try:
            events_table.update_item(
                Key={"eventId": event_id},
                UpdateExpression="SET confirmedHeadcount = :actual ADD confirmedHeadcountRevision :one",
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
            logger.warning("reconciled confirmed headcount event=%s old=%d actual=%d", event_id, observed_count, actual_count)
            return actual_count
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    raise RuntimeError("confirmed headcount changed during reconciliation; retry the RSVP")


def transition_confirmed_invite(
    *, invites_table, events_table, ddb_client, event_id: str, phone: str,
    target_status: str, allowed_statuses: set[str] | frozenset[str], members_table=None,
) -> bool:
    """Change an active invite and release its count/reservation in one transaction.

    Returns False if the invite is already outside the allowed source states.
    Raises on storage errors so callers can report an incomplete operation.
    """
    serializer = TypeSerializer()
    av = serializer.serialize
    stamp_field = "cancelledAt" if target_status == "DECLINED" else "deletedAt"
    for attempt in range(3):
        invite = invites_table.get_item(
            Key={"eventId": event_id, "phone": phone}, ConsistentRead=True,
        ).get("Item") or {}
        old_status = (invite.get("status") or "").upper()
        if old_status not in allowed_statuses:
            return False

        stored_plus_one_name = invite.get("plusOneName") or ""
        plus_one_name = stored_plus_one_name.strip()
        release_count = 1 + bool(plus_one_name)
        _ensure_counter(events_table, invites_table, event_id)
        names = {"#s": "status", "#stamp": stamp_field}
        transition_now = datetime.now(timezone.utc)
        values = {
            ":old": av(old_status), ":new": av(target_status), ":now": av(transition_now.isoformat(timespec="seconds")),
            ":release": av(release_count), ":phone": av(phone),
        }
        condition = "#s = :old"
        if plus_one_name:
            names.update({"#r": "plusOneReservations", "#g": plus_one_reservation_key(plus_one_name, deps={})})
            values[":guest"] = av(stored_plus_one_name)
            condition += " AND plusOneName = :guest"
            # Ensure the map exists before using a nested document path.
            events_table.update_item(
                Key={"eventId": event_id},
                UpdateExpression="SET plusOneReservations = if_not_exists(plusOneReservations, :empty)",
                ConditionExpression="attribute_exists(eventId)",
                ExpressionAttributeValues={":empty": {}},
            )
            event_condition = "attribute_exists(eventId) AND confirmedHeadcount >= :release AND (attribute_not_exists(#r.#g) OR #r.#g = :phone)"
            event_update = "SET confirmedHeadcount = confirmedHeadcount - :release REMOVE #r.#g ADD confirmedHeadcountRevision :one"
        else:
            values[":empty"] = av("")
            condition += " AND (attribute_not_exists(plusOneName) OR plusOneName = :empty)"
            event_condition = "attribute_exists(eventId) AND confirmedHeadcount >= :release"
            event_update = "SET confirmedHeadcount = confirmedHeadcount - :release ADD confirmedHeadcountRevision :one"

        try:
            transact_items = [
                {
                    "Update": {
                        "TableName": invites_table.name,
                        "Key": {"eventId": av(event_id), "phone": av(phone)},
                        "UpdateExpression": "SET #s = :new, #stamp = :now REMOVE plusOneName, plusOneMember, plusOneAttendedAt, plusOneNoShowAt, awaitingPlusOneName, awaitingPlusOneLastName",
                        "ConditionExpression": condition,
                        "ExpressionAttributeNames": {"#s": "status", "#stamp": stamp_field},
                        "ExpressionAttributeValues": {k: v for k, v in values.items() if k in {":old", ":new", ":now", ":empty", ":guest"}},
                    }
                },
                {
                    "Update": {
                        "TableName": events_table.name,
                        "Key": {"eventId": av(event_id)},
                        "UpdateExpression": event_update,
                        "ConditionExpression": event_condition,
                        **({"ExpressionAttributeNames": {"#r": names["#r"], "#g": names["#g"]}} if plus_one_name else {}),
                        "ExpressionAttributeValues": {
                            ":release": values[":release"],
                            ":one": av(1),
                            **({":phone": values[":phone"]} if plus_one_name else {}),
                        },
                    }
                },
            ]
            if target_status == "DECLINED" and members_table is not None:
                # Classification, invite transition, seat release and tier credit
                # commit together; retries cannot apply the credit twice.
                event = events_table.get_item(Key={"eventId": event_id}, ConsistentRead=True).get("Item") or {}
                timing = cancellation_timing(event, transition_now)
                invite_update = transact_items[0]["Update"]
                invite_update["UpdateExpression"] = invite_update["UpdateExpression"].replace(
                    " REMOVE ", ", cancellationTiming = :timing, tierExcused = :excused REMOVE ", 1)
                invite_update["ExpressionAttributeValues"].update({":timing": av(timing), ":excused": av(timing == "TIMELY")})
                event_update = transact_items[1]["Update"]
                event_update["ConditionExpression"] += " AND event_status = :live AND (attribute_not_exists(attendanceFinalized) OR attendanceFinalized = :false)"
                event_update["ExpressionAttributeValues"].update({":live": av("LIVE"), ":false": av(False)})
                for i, field in enumerate(("date", "startTime", "event_timezone")):
                    name, value = f"#schedule{i}", f":schedule{i}"
                    event_update.setdefault("ExpressionAttributeNames", {})[name] = field
                    if field in event:
                        event_update["ConditionExpression"] += f" AND {name} = {value}"
                        event_update["ExpressionAttributeValues"][value] = av(event[field])
                    else:
                        event_update["ConditionExpression"] += f" AND attribute_not_exists({name})"
                if timing == "TIMELY":
                    transact_items.append({"Update": {
                        "TableName": members_table.name,
                        "Key": {"phone": av(phone)},
                        "UpdateExpression": "SET timelyCancellationCount = if_not_exists(timelyCancellationCount, :zero) + :one",
                        "ConditionExpression": "attribute_exists(phone)",
                        "ExpressionAttributeValues": {":zero": av(0), ":one": av(1)},
                    }})
            ddb_client.transact_write_items(TransactItems=transact_items)
            return True
        except ClientError as exc:
            if (exc.response.get("Error") or {}).get("Code") != "TransactionCanceledException":
                raise
            logger.info("invite transition conflict event=%s phone=...%s attempt=%d", event_id, phone[-4:], attempt + 1)
    raise RuntimeError("could not atomically release confirmed invite headcount")
