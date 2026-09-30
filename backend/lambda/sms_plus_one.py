"""Plus-one reservation and validation logic for the inbound SMS flow.

Functions accept a dependency mapping so sms_handler can retain its stable,
patchable helper surface while the stateful guest logic lives outside the main
webhook router.
"""
from __future__ import annotations

import hashlib
import os
import re

import boto3
from capacity_policy import target_confirmed_headcount
from boto3.dynamodb.conditions import Key as DKey
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError


def norm_name_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def plus_one_reservation_key(name: str, *, deps: dict) -> str:
    normalized = deps.get("norm_name_for_match", norm_name_for_match)(name)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ensure_plus_one_reservation_map(event_id: str, *, deps: dict) -> None:
    deps["events_table"]().update_item(
        Key={"eventId": event_id},
        UpdateExpression="SET plusOneReservations = if_not_exists(plusOneReservations, :empty)",
        ConditionExpression="attribute_exists(eventId)",
        ExpressionAttributeValues={":empty": {}},
    )


def set_plus_one(event_id: str, phone: str, plus_one_name: str, is_member: bool, *, deps: dict) -> str:
    invites_t = deps["invites_table"]()
    current = invites_t.get_item(Key={"eventId": event_id, "phone": phone}).get("Item") or {}
    old_name = (current.get("plusOneName") or "").strip()
    key_fn = deps.get("plus_one_reservation_key")
    old_key = key_fn(old_name) if old_name else ""
    new_key = key_fn(plus_one_name)

    deps["ensure_plus_one_reservation_map"](event_id)
    deps["ensure_event_headcount_counter"](event_id)

    event_item = deps["events_table"]().get_item(
        Key={"eventId": event_id},
        ProjectionExpression="#capacity, confirmedHeadcount, plusOneReservations, event_status, expectedShowRate",
        ExpressionAttributeNames={"#capacity": "capacity"},
    ).get("Item") or {}
    capacity = int(event_item.get("capacity") or 0)
    target_headcount = target_confirmed_headcount(capacity, event_item)

    # Older deletions may have tombstoned the invite row without clearing its
    # event-level reservation. Reclaim only when the owner row is missing or no
    # longer carries this guest; the transaction rechecks that snapshot so a
    # concurrent re-confirmation or guest change cannot steal a live reservation.
    reservation_owner = (event_item.get("plusOneReservations") or {}).get(new_key)
    stale_owner = ""
    stale_owner_invite: dict = {}
    if reservation_owner and reservation_owner != phone:
        stale_owner_invite = invites_t.get_item(
            Key={"eventId": event_id, "phone": reservation_owner}
        ).get("Item") or {}
        owner_status = (stale_owner_invite.get("status") or "").upper()
        owner_guest = (stale_owner_invite.get("plusOneName") or "").strip()
        active_statuses = {"CONFIRMED", "ATTENDED", "NO_SHOW"}
        if owner_status in active_statuses and owner_guest and key_fn(owner_guest) == new_key:
            return "TAKEN"
        stale_owner = reservation_owner

    serializer = TypeSerializer()
    av = serializer.serialize
    event_names = {"#r": "plusOneReservations", "#new": new_key}
    event_values = {":phone": av(phone), ":live": av("LIVE")}
    event_condition = "event_status = :live AND (attribute_not_exists(#r.#new) OR #r.#new = :phone"
    if stale_owner:
        event_condition += " OR #r.#new = :stale_owner"
        event_values[":stale_owner"] = av(stale_owner)
    event_condition += ")"

    first_guest = not bool(old_name)
    if first_guest:
        event_update = "SET #r.#new = :phone ADD confirmedHeadcount :one, confirmedHeadcountRevision :one"
        event_values[":one"] = av(1)
        if target_headcount > 0:
            event_condition += " AND confirmedHeadcount < :target"
            event_values[":target"] = av(target_headcount)
        invite_condition = "#s = :confirmed AND (attribute_not_exists(plusOneName) OR plusOneName = :empty)"
    else:
        if old_key != new_key:
            event_names["#old"] = old_key
            event_update = "SET #r.#new = :phone REMOVE #r.#old"
            event_condition += " AND (attribute_not_exists(#r.#old) OR #r.#old = :phone)"
        else:
            event_update = "SET #r.#new = :phone"
        invite_condition = "#s = :confirmed AND plusOneName = :old"

    now_values = {
        ":n": av(plus_one_name),
        ":m": av(bool(is_member)),
        ":f": av(False),
        ":e": av(""),
        ":confirmed": av("CONFIRMED"),
    }
    if old_name:
        now_values[":old"] = av(old_name)
    else:
        now_values[":empty"] = av("")

    transact_items = [
        {
            "Update": {
                "TableName": os.getenv("EVENTS_TABLE_NAME", "rsvp-events"),
                "Key": {"eventId": av(event_id)},
                "UpdateExpression": event_update,
                "ConditionExpression": event_condition,
                "ExpressionAttributeNames": event_names,
                "ExpressionAttributeValues": event_values,
            }
        },
        {
            "Update": {
                "TableName": os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"),
                "Key": {"eventId": av(event_id), "phone": av(phone)},
                "UpdateExpression": "SET plusOneName = :n, plusOneIsMember = :m, awaitingPlusOneName = :f, awaitingPlusOneLastName = :e",
                "ConditionExpression": invite_condition,
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": now_values,
            }
        },
    ]
    if stale_owner:
        owner_name = (stale_owner_invite.get("plusOneName") or "").strip()
        owner_condition = "attribute_not_exists(phone)"
        owner_names = {}
        owner_values = {}
        if stale_owner_invite:
            owner_condition = (
                "attribute_not_exists(phone) OR attribute_not_exists(#s) OR "
                "(#s <> :confirmed AND #s <> :attended AND #s <> :no_show) OR "
                "attribute_not_exists(plusOneName) OR plusOneName = :observed_name"
            )
            owner_names = {"#s": "status"}
            owner_values = {
                ":confirmed": av("CONFIRMED"),
                ":attended": av("ATTENDED"),
                ":no_show": av("NO_SHOW"),
                ":observed_name": av(owner_name),
            }
        transact_items.append({
            "ConditionCheck": {
                "TableName": os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"),
                "Key": {"eventId": av(event_id), "phone": av(stale_owner)},
                "ConditionExpression": owner_condition,
                **({"ExpressionAttributeNames": owner_names} if owner_names else {}),
                **({"ExpressionAttributeValues": owner_values} if owner_values else {}),
            }
        })

    try:
        boto3.client("dynamodb").transact_write_items(
            TransactItems=transact_items
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        try:
            latest_event = deps["events_table"]().get_item(
                Key={"eventId": event_id},
                ProjectionExpression="confirmedHeadcount, plusOneReservations",
            ).get("Item") or {}
            reservations = latest_event.get("plusOneReservations") or {}
            owner = reservations.get(new_key)
            if owner and owner != phone:
                return "TAKEN"
            if first_guest and target_headcount > 0 and int(latest_event.get("confirmedHeadcount") or 0) >= target_headcount:
                return "FULL"
        except Exception:
            deps["logger"].exception("_set_plus_one: failed to classify transaction conflict event=%s phone=...%s", event_id, phone[-4:])
        return "NOOP"

    return "SAVED"


def set_awaiting_plus_one(event_id: str, phone: str, *, deps: dict) -> None:
    deps["invites_table"]().update_item(
        Key={"eventId": event_id, "phone": phone},
        UpdateExpression="SET awaitingPlusOneName = :t",
        ExpressionAttributeValues={":t": True},
    )


def get_confirmed_invite(phone: str, *, deps: dict):
    try:
        ev = deps["get_current_event"]()
        active_slug = (ev.get("eventSlug") or ev.get("activeEventSlug") or "").strip()
    except Exception:
        active_slug = ""
    if not active_slug:
        return None

    invites_t = deps["invites_table"]()
    kwargs = {"IndexName": "phone-index", "KeyConditionExpression": DKey("phone").eq(phone)}
    while True:
        resp = invites_t.query(**kwargs)
        for item in resp.get("Items", []):
            if item.get("status") == "CONFIRMED" and item.get("eventId") == active_slug:
                return item
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return None


def get_current_invite_status(phone: str, *, deps: dict) -> str:
    try:
        ev = deps["get_current_event"]()
        ev_slug = (ev.get("eventSlug") or ev.get("eventId") or "").strip()
        if not ev_slug or not phone:
            return "UNKNOWN"
        item = deps["invites_table"]().get_item(Key={"eventId": ev_slug, "phone": phone}).get("Item") or {}
        return (item.get("status") or "UNKNOWN").upper()
    except Exception:
        deps["logger"].exception("_get_current_invite_status failed phone=...%s", str(phone)[-4:])
        return "UNKNOWN"


def lookup_plus_one_status(name: str, event_id: str, *, deps: dict) -> dict:
    try:
        name_clean = name.strip().lower()
        if not name_clean:
            return {"is_member": False, "is_confirmed": False, "phone": None}
        results = deps["search_members"](name_clean, limit=5)
        for row in results:
            first = (row.get("name") or "").strip().lower()
            last = (row.get("lastName") or "").strip().lower()
            full = f"{first} {last}".strip()
            if name_clean in (first, last, full):
                phone = row.get("phone")
                is_confirmed = False
                if phone and event_id:
                    try:
                        inv = deps["invites_table"]().get_item(Key={"eventId": event_id, "phone": phone}).get("Item")
                        is_confirmed = inv is not None and inv.get("status") == "CONFIRMED"
                    except Exception:
                        pass
                return {"is_member": True, "is_confirmed": is_confirmed, "phone": phone}
        return {"is_member": False, "is_confirmed": False, "phone": None}
    except Exception:
        deps["logger"].exception("_lookup_plus_one_status failed name_length=%d", len(name or ""))
        return {"is_member": False, "is_confirmed": False, "phone": None}


def lookup_existing_plus_one_assignment(name: str, event_id: str, current_phone: str = "", *, deps: dict) -> dict:
    norm = deps.get("norm_name_for_match", norm_name_for_match)
    target = norm(name)
    if not target or not event_id:
        return {"exists": False}
    active_statuses = {"CONFIRMED", "ATTENDED", "NO_SHOW"}
    try:
        kwargs = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
        while True:
            page = deps["invites_table"]().query(**kwargs)
            for item in page.get("Items", []):
                if (item.get("status") or "").upper() not in active_statuses:
                    continue
                phone = item.get("phone") or ""
                if current_phone and phone == current_phone:
                    continue
                existing = norm(item.get("plusOneName") or "")
                if existing and existing == target:
                    return {"exists": True, "phone": phone, "name": item.get("plusOneName") or name}
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except Exception:
        deps["logger"].exception("_lookup_existing_plus_one_assignment failed event=%s name_length=%d", event_id, len(name or ""))
    return {"exists": False}


def plus_one_unavailable_reply(name: str = "") -> str:
    first = (name or "").strip().split()[0] if name else "They"
    return f"{first} is already confirmed with another RSVP. Send me a different name if you're bringing someone else."


def validate_plus_one_candidate(name: str, event_id: str, current_phone: str, *, deps: dict) -> tuple[bool, str, bool]:
    status_fn = deps.get("lookup_plus_one_status")
    assigned_fn = deps.get("lookup_existing_plus_one_assignment")
    unavailable_fn = deps.get("plus_one_unavailable_reply")
    status = status_fn(name, event_id) if status_fn else lookup_plus_one_status(name, event_id, deps=deps)
    if status.get("is_confirmed"):
        return False, "They're already in. Send me another name if you're bringing someone else.", True
    if status.get("is_member"):
        return False, "They have their own invite. Send me another name if you're bringing someone else.", True
    assigned = assigned_fn(name, event_id, current_phone) if assigned_fn else lookup_existing_plus_one_assignment(name, event_id, current_phone, deps=deps)
    if assigned.get("exists"):
        reply = unavailable_fn(assigned.get("name") or name) if unavailable_fn else plus_one_unavailable_reply(assigned.get("name") or name)
        return False, reply, False
    return True, "", False
