import base64
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from event_policy import venue_available, cutoff_reached, event_start

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key as DKey
from boto3.dynamodb.types import TypeSerializer

from member_store import (
    get_member,
    normalize_phone,
    search_members,
    set_status,
    CONFIRMED_FAMILY_STATUSES,
    LOGISTICS_ELIGIBLE_STATUSES,
    JADE_IN_WAVE_STATUSES,
    CONFIRMABLE_EVENT_STATES,
)
from sms_adapter import get_secret_string, send_sms, get_host_phones
from admin_shared import coerce_bool
from invite_capacity import transition_confirmed_invite, reconcile_confirmed_headcount
from capacity_policy import target_confirmed_headcount

logger = logging.getLogger()

# ── Jade system prompt ────────────────────────────────────────────────────────

from jade_prompt import JADE_SYSTEM_PROMPT


from sms_intent import (
    _is_status_question, _detect_rsvp_intent, AMBIGUOUS_KEYWORDS, IGNORE_KEYWORDS,
    RUNNING_LATE_KEYWORDS, COST_KEYWORDS, EXTRA_GUEST_KEYWORDS, _is_opt_out_message, _is_correction_text, _correction_reply, _normalized_tokens,
    _contains_topic,
)

# ── DynamoDB helpers ──────────────────────────────────────────────────────────

_DDB = boto3.resource("dynamodb")


def _members_table():
    name = os.getenv("MEMBERS_TABLE_NAME")
    if not name:
        raise RuntimeError("MEMBERS_TABLE_NAME env var not set")
    return _DDB.Table(name)


def _invites_table():
    name = os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites")
    return _DDB.Table(name)


def _events_table():
    name = os.getenv("EVENTS_TABLE_NAME", "rsvp-events")
    return _DDB.Table(name)


def _get_current_event() -> dict:
    try:
        table = _events_table()
        pointer = table.get_item(Key={"eventId": "current"}).get("Item") or {}
        active_slug = (pointer.get("activeEventSlug") or pointer.get("eventSlug") or pointer.get("slug") or "").strip()
        if active_slug and active_slug != "current":
            canonical = table.get_item(Key={"eventId": active_slug}).get("Item") or {}
            if canonical:
                return {**canonical, "active": True, "activeEventSlug": active_slug}
        return pointer
    except Exception:
        logger.exception("sms_handler: failed to resolve current event")
        raise

def _pending_approvals_table():
    """Dedicated table for host approval queue: pk=hostPhone, sk=memberPhone."""
    import boto3 as _b3
    return _b3.resource("dynamodb").Table(
        os.getenv("PENDING_APPROVALS_TABLE_NAME", "rsvp-pending-approvals")
    )


def _get_pending_approval(host_phone: str, approval_code: str = None) -> dict | None:
    """
    Fetch a pending approval for this host from the dedicated approvals table.
    pk = hostPhone, sk = memberPhone.
    - If approval_code provided: query all items for host, find matching code.
    - Otherwise: return oldest item.
    """
    try:
        from boto3.dynamodb.conditions import Key as _K
        resp = _pending_approvals_table().query(
            KeyConditionExpression=_K("hostPhone").eq(host_phone),
        )
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = _pending_approvals_table().query(KeyConditionExpression=_K("hostPhone").eq(host_phone), ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        if not items:
            return None

        # DynamoDB TTL deletion is asynchronous. Enforce the business expiry
        # timestamp in application code so an approval cannot remain actionable
        # merely because its TTL row has not been physically removed yet.
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        active_items = []
        for item in items:
            try:
                expires_at = int(item.get("expiresAt") or 0)
            except (TypeError, ValueError):
                expires_at = 0
            if expires_at and expires_at <= now_epoch:
                continue
            active_items.append(item)
        if not active_items:
            return None

        if approval_code:
            for item in active_items:
                if (item.get("approvalCode") or "").strip() == approval_code.strip():
                    return item
            return None
        return min(active_items, key=lambda i: i.get("storedAt", ""))
    except Exception:
        logger.exception("_get_pending_approval failed host=...%s", host_phone[-4:])
        # A failed lookup is not the same as an empty queue. Propagate the
        # error so the inbound webhook can return 503 and Quo can retry.
        raise


def _clear_pending_approval(host_phone: str, member_phone: str = None) -> None:
    """
    Delete from dedicated approvals table (pk=hostPhone, sk=memberPhone).
    If member_phone provided, deletes that specific row.
    If not, deletes all approvals for this host.
    """
    try:
        tbl = _pending_approvals_table()
        if member_phone:
            tbl.delete_item(Key={"hostPhone": host_phone, "memberPhone": member_phone})
        else:
            from boto3.dynamodb.conditions import Key as _K
            resp = tbl.query(KeyConditionExpression=_K("hostPhone").eq(host_phone))
            for item in resp.get("Items", []):
                tbl.delete_item(Key={"hostPhone": host_phone, "memberPhone": item["memberPhone"]})
    except Exception:
        logger.exception("_clear_pending_approval failed host=...%s", host_phone[-4:])
def _set_opt_out(phone: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # SMS opt-out preserves the member profile and attendance record.
    _members_table().update_item(
        Key={"phone": phone},
        UpdateExpression=(
            "SET optOut = :t, optOutAt = :now, smsOptIn = :f, lastSeenAt = :now, consentRevision = :revision "
            "REMOVE pendingSmsConsentAt"
        ),
        ExpressionAttributeValues={
            ":t": True, ":f": False, ":now": now, ":revision": uuid.uuid4().hex,
        },
    )


def _extract_inbound_message_id(body: dict, event: dict) -> str:
    payload = body
    if isinstance(body.get("data"), dict):
        for key in ("resource", "object"):
            if isinstance(body["data"].get(key), dict):
                payload = body["data"][key]
                break
    value = payload.get("id") or payload.get("messageId") or payload.get("message_id")
    if value:
        return str(value).strip()[:200]
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    for key in ("webhook-id", "x-webhook-id", "x-quo-event-id"):
        if headers.get(key):
            return str(headers[key]).strip()[:200]
    return ""


def _claim_inbound_message(message_id: str) -> str:
    """Use a short processing lease; only completed messages get long retention.

    Receipt storage outages return BUSY so the provider retries without duplicate processing.
    """
    if not message_id:
        return ""
    now = int(datetime.now(timezone.utc).timestamp())
    key = "INBOUND#" + hashlib.sha256(message_id.encode()).hexdigest()
    owner = uuid.uuid4().hex
    table = _DDB.Table(os.getenv("INVITE_JOBS_TABLE_NAME", "rsvp-invite-jobs"))
    try:
        table.put_item(
            Item={"jobId": key, "kind": "INBOUND_RECEIPT", "receiptState": "PROCESSING",
                  "owner": owner, "expiresAt": now + 60, "ttl": now + 86400},
            ConditionExpression="attribute_not_exists(jobId) OR expiresAt <= :now",
            ExpressionAttributeValues={":now": now},
        )
        return owner
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            try:
                row = table.get_item(Key={"jobId": key}, ConsistentRead=True).get("Item", {})
                # Old receipts had no state and were treated as completed.
                return "DONE" if row and row.get("receiptState", "DONE") == "DONE" else "BUSY"
            except Exception:
                logger.exception("sms_handler: inbound receipt read unavailable; retry required")
                return "BUSY"
        logger.exception("sms_handler: inbound receipt claim unavailable; retry required")
    except Exception:
        logger.exception("sms_handler: inbound receipt claim unavailable; retry required")
    return "BUSY"


def _finish_inbound_message(message_id: str, owner: str, successful: bool) -> None:
    now = int(datetime.now(timezone.utc).timestamp())
    key = "INBOUND#" + hashlib.sha256(message_id.encode()).hexdigest()
    try:
        _DDB.Table(os.getenv("INVITE_JOBS_TABLE_NAME", "rsvp-invite-jobs")).update_item(
            Key={"jobId": key},
            UpdateExpression="SET receiptState = :state, expiresAt = :expires, #ttl = :ttl",
            ConditionExpression="#owner = :owner",
            ExpressionAttributeNames={"#owner": "owner", "#ttl": "ttl"},
            ExpressionAttributeValues={":owner": owner, ":state": "DONE" if successful else "RETRY",
                                       ":expires": now + 30 * 86400 if successful else 0,
                                       ":ttl": now + 30 * 86400},
        )
    except Exception:
        logger.exception("sms_handler: inbound receipt completion failed")


def _claim_access_reply(phone: str) -> bool:
    """At most one website-link reply per phone per day, across invocations.

    Reuse the existing TTL-enabled job table without creating member records.
    Claim before sending; a storage failure suppresses this optional reply.
    """
    return _claim_optional_reply(phone, "ACCESS_REPLY", 86400)


def _claim_optional_reply(identifier: str, kind: str, retention: int) -> bool:
    """Suppress replayed optional notices without suppressing command retries."""
    now = int(datetime.now(timezone.utc).timestamp())
    key = kind + "#" + hashlib.sha256(identifier.encode()).hexdigest()
    try:
        _DDB.Table(os.getenv("INVITE_JOBS_TABLE_NAME", "rsvp-invite-jobs")).put_item(
            Item={"jobId": key, "kind": kind + "_LIMIT", "expiresAt": now + retention, "ttl": now + retention},
            ConditionExpression="attribute_not_exists(jobId) OR expiresAt <= :now",
            ExpressionAttributeValues={":now": now},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.exception("sms_handler: optional reply limit unavailable kind=%s", kind)
    except Exception:
        logger.exception("sms_handler: optional reply limit unavailable kind=%s", kind)
    return False


def _get_pending_invite(phone: str):
    """Read this event's invite directly instead of walking the phone's history."""
    ev = _get_current_event()
    slug = (ev.get("eventSlug") or ev.get("activeEventSlug") or "").strip()
    if not slug:
        return None
    item = _invites_table().get_item(Key={"eventId": slug, "phone": phone}, ConsistentRead=True).get("Item")
    return item if item and item.get("status") == "INVITED" else None


def _get_reconfirmable_invite(phone: str):
    """INVITED and DECLINED members may confirm for the active event."""
    ev = _get_current_event()
    slug = (ev.get("eventSlug") or ev.get("activeEventSlug") or "").strip()
    if not slug:
        return None
    item = _invites_table().get_item(Key={"eventId": slug, "phone": phone}, ConsistentRead=True).get("Item")
    return item if item and item.get("status") in ("INVITED", "DECLINED") else None


def _find_invite_by_message_id(msg_id: str, to_phone: str = "") -> dict | None:
    """Find the invite row that produced a Quo delivery callback.

    Primary path uses quo-message-index. Fallback uses phone-index so a late
    delivery never gets stamped on whatever event is currently active.
    """
    if not msg_id:
        return None
    invites_t = _invites_table()
    try:
        page = invites_t.query(
            IndexName="quo-message-index",
            KeyConditionExpression=DKey("quoMessageId").eq(msg_id),
            Limit=1,
        )
        items = page.get("Items") or []
        if items:
            return items[0]
    except Exception:
        logger.warning("sms_handler: quo-message-index lookup failed; falling back to phone-index", exc_info=True)

    try:
        normalized = normalize_phone(to_phone) if to_phone else ""
    except Exception:
        normalized = ""
    if not normalized:
        return None

    try:
        kwargs = {
            "IndexName": "phone-index",
            "KeyConditionExpression": DKey("phone").eq(normalized),
        }
        while True:
            page = invites_t.query(**kwargs)
            for item in page.get("Items", []):
                if item.get("quoMessageId") == msg_id:
                    return item
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except Exception:
        logger.exception("sms_handler: phone-index delivery lookup failed msg_id=%s", msg_id[:30])
    return None


def _get_confirmed_count(event_id: str) -> int:
    """Return confirmed event headcount, including confirmed +1 guests.

    Capacity is measured in people, not invite rows. A database read failure is
    not equivalent to an empty event, so errors deliberately propagate and the
    caller leaves the RSVP unchanged.
    """
    invites_t = _invites_table()
    count = 0
    kwargs: dict = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
    while True:
        resp = invites_t.query(**kwargs)
        for item in resp.get("Items", []):
            if (item.get("status") or "").upper() not in CONFIRMED_FAMILY_STATUSES:
                continue
            count += 1
            if (item.get("plusOneName") or "").strip():
                count += 1
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return count


def _ensure_event_headcount_counter(event_id: str) -> int:
    """Initialize the event's atomic confirmed-headcount counter from live invite data.

    The strongly consistent event read is the common path. Only legacy events without
    a counter need the full invite query. SET if_not_exists remains the race-safe
    initializer if concurrent first confirmations both observe a missing counter.
    """
    events_t = _events_table()
    event = events_t.get_item(
        Key={"eventId": event_id},
        ProjectionExpression="confirmedHeadcount",
        ConsistentRead=True,
    ).get("Item") or {}
    if "confirmedHeadcount" in event:
        return int(event.get("confirmedHeadcount") or 0)

    baseline = _get_confirmed_count(event_id)
    response = events_t.update_item(
        Key={"eventId": event_id},
        UpdateExpression="SET confirmedHeadcount = if_not_exists(confirmedHeadcount, :baseline)",
        ConditionExpression="attribute_exists(eventId)",
        ExpressionAttributeValues={":baseline": baseline},
        ReturnValues="ALL_NEW",
    )
    attrs = response.get("Attributes") or {}
    return int(attrs.get("confirmedHeadcount", baseline) or 0)


def _confirm_invite_with_capacity(event_id: str, phone: str, target_headcount: int) -> str:
    """Atomically confirm one invite and reserve one event headcount slot.

    Returns CONFIRMED, FULL, or NOOP. The event counter and invite transition are
    committed together, so two different YES replies cannot both claim the final
    available slot.
    """
    _ensure_event_headcount_counter(event_id)
    serializer = TypeSerializer()

    def av(value):
        return serializer.serialize(value)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client = boto3.client("dynamodb")
    try:
        prior_invite = _invites_table().get_item(
            Key={"eventId": event_id, "phone": phone}, ConsistentRead=True,
        ).get("Item") or {}
        was_excused = prior_invite.get("tierExcused") is True
        transact_items = [
                {
                    "Update": {
                        "TableName": os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"),
                        "Key": {"eventId": av(event_id), "phone": av(phone)},
                        "UpdateExpression": "SET #s = :confirmed, confirmedAt = :now",
                        "ConditionExpression": "#s IN (:invited, :declined)",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":confirmed": av("CONFIRMED"),
                            ":now": av(now),
                            ":invited": av("INVITED"),
                            ":declined": av("DECLINED"),
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": os.getenv("EVENTS_TABLE_NAME", "rsvp-events"),
                        "Key": {"eventId": av(event_id)},
                        "UpdateExpression": "SET confirmedHeadcount = confirmedHeadcount + :one ADD confirmedHeadcountRevision :one",
                        "ConditionExpression": "event_status = :live AND confirmedHeadcount < :target",
                        "ExpressionAttributeValues": {
                            ":one": av(1),
                            ":live": av("LIVE"),
                            ":target": av(int(target_headcount)),
                        },
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": os.getenv("MEMBERS_TABLE_NAME", "rsvp-members"),
                        "Key": {"phone": av(phone)},
                        "ConditionExpression": (
                            "#member_status = :approved "
                            "AND (attribute_not_exists(optOut) OR optOut = :false) "
                            "AND (attribute_not_exists(smsOptIn) "
                            "OR attribute_type(smsOptIn, :null_type) OR smsOptIn = :true)"
                        ),
                        "ExpressionAttributeNames": {"#member_status": "status"},
                        "ExpressionAttributeValues": {
                            ":approved": av("APPROVED"),
                            ":false": av(False),
                            ":null_type": av("NULL"),
                            ":true": av(True),
                        },
                    }
                },
            ]
        invite_update = transact_items[0]["Update"]
        invite_update["UpdateExpression"] += " REMOVE tierExcused, cancellationTiming"
        invite_update["ExpressionAttributeValues"][":excusedGuard"] = av(was_excused)
        invite_update["ConditionExpression"] += (
            " AND tierExcused = :excusedGuard" if was_excused else
            " AND (attribute_not_exists(tierExcused) OR tierExcused = :excusedGuard)"
        )
        if was_excused:
            member_update = transact_items[2]["ConditionCheck"]
            member_update["UpdateExpression"] = "SET timelyCancellationCount = timelyCancellationCount - :one"
            member_update["ConditionExpression"] += " AND timelyCancellationCount >= :one"
            member_update["ExpressionAttributeValues"][":one"] = av(1)
            transact_items[2] = {"Update": member_update}
        client.transact_write_items(TransactItems=transact_items)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        if any(reason.get("Code") not in (None, "None", "ConditionalCheckFailed") for reason in exc.response.get("CancellationReasons", [])):
            raise
        # Determine whether the atomic claim lost to capacity or was simply a
        # duplicate/invalid transition. Both are safe no-write outcomes.
        try:
            event_item = _events_table().get_item(
                Key={"eventId": event_id}, ProjectionExpression="confirmedHeadcount"
            ).get("Item") or {}
            if int(event_item.get("confirmedHeadcount") or 0) >= int(target_headcount):
                return "FULL"
            invite_item = _invites_table().get_item(
                Key={"eventId": event_id, "phone": phone}, ProjectionExpression="#s",
                ExpressionAttributeNames={"#s": "status"},
            ).get("Item") or {}
            if (invite_item.get("status") or "").upper() == "CONFIRMED":
                return "NOOP"
        except Exception:
            logger.exception("_confirm_invite_with_capacity: failed to classify transaction conflict event=%s phone=...%s", event_id, phone[-4:])
            raise
        return "NOOP"

    try:
        _members_table().update_item(
            Key={"phone": phone},
            UpdateExpression="SET confirmedCount = if_not_exists(confirmedCount, :zero) + :one, lastSeenAt = :now",
            ConditionExpression="attribute_exists(phone) AND #status = :approved",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now, ":approved": "APPROVED"},
        )
    except Exception:
        logger.exception("_confirm_invite_with_capacity: confirmedCount increment failed phone=...%s", phone[-4:])
    return "CONFIRMED"


def _update_invite_status(event_id: str, phone: str, status: str) -> bool:
    """Apply one RSVP transition exactly once.

    Conditional writes make duplicate/replayed webhooks harmless and prevent
    confirmedCount from being incremented twice under concurrent delivery.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    normalized_status = (status or "").upper()
    field = "confirmedAt" if normalized_status == "CONFIRMED" else "declinedAt"

    if normalized_status == "CONFIRMED":
        condition = "#s IN (:invited, :declined) AND (attribute_not_exists(tierExcused) OR tierExcused = :notExcused)"
        values = {
            ":s": "CONFIRMED",
            ":notExcused": False,
            ":now": now,
            ":invited": "INVITED",
            ":declined": "DECLINED",
        }
    elif normalized_status == "DECLINED":
        condition = "#s = :invited"
        values = {
            ":s": "DECLINED",
            ":now": now,
            ":invited": "INVITED",
        }
    else:
        raise ValueError(f"unsupported invite status transition: {status}")

    try:
        _invites_table().update_item(
            Key={"eventId": event_id, "phone": phone},
            UpdateExpression=f"SET #s = :s, {field} = :now",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(
                "_update_invite_status: transition already applied/invalid event=%s phone=...%s target=%s",
                event_id, phone[-4:], normalized_status,
            )
            return False
        raise

    if normalized_status == "CONFIRMED":
        try:
            _members_table().update_item(
                Key={"phone": phone},
                UpdateExpression="SET confirmedCount = if_not_exists(confirmedCount, :zero) + :one, lastSeenAt = :now",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now},
            )
        except Exception:
            logger.exception("_update_invite_status: confirmedCount increment failed phone=...%s", phone[-4:])
    return True


def _cancel_confirmed_invite(event_id: str, phone: str) -> bool:
    """Cancel a confirmed RSVP and atomically release member/+1 headcount."""
    options = dict(
        invites_table=_invites_table(), events_table=_events_table(),
        ddb_client=boto3.client("dynamodb"), event_id=event_id, phone=phone,
        target_status="DECLINED", allowed_statuses={"CONFIRMED"},
        members_table=_members_table(),
    )
    try:
        return transition_confirmed_invite(**options)
    except RuntimeError as exc:
        if "headcount" not in str(exc):
            raise
        reconcile_confirmed_headcount(
            events_table=options["events_table"], invites_table=options["invites_table"], event_id=event_id,
        )
        return transition_confirmed_invite(**options)


def _build_confirmation_message(phone: str) -> str:
    """
    Jade's confirmation reply.
    "You're in. See you [day]." + venue/address when revealVenue=True + dresscode + ticket link.
    """
    try:
        ev = _get_current_event()
        date_val = ev.get("date", "")
        try:
            day_display = datetime.strptime(date_val[:10], "%Y-%m-%d").strftime("%A %B %-d")
        except Exception:
            day_display = ""

        msg = "You're in."
        if day_display:
            msg += f" See you {day_display}."

        # Confirmation should only reveal venue/address when the event is explicitly
        # marked ready to reveal. This protects draft/test/live mismatches from
        # leaking sensitive logistics immediately after a YES response.
        reveal = venue_available(ev, confirmed=True)
        venue = (ev.get("venue") or "").strip()
        address = (ev.get("address") or "").strip()
        if reveal:
            if venue and address:
                msg += f" {venue} — {address}."
            elif venue:
                msg += f" {venue}."
            elif address:
                msg += f" {address}."
        else:
            msg += " Location comes once it's released."

        # Always include dress code if set — confirmed guests need to know.
        # Keep guest instructions on their own paragraph in the caller so event
        # notes/dress-code copy never runs directly into the plus-one prompt.
        details = []
        dresscode = (ev.get("dresscode") or "").strip()
        if dresscode:
            details.append(f"Dress code: {dresscode}.")

        ticket_url = (ev.get("ticketUrl") or "").strip()
        if ticket_url:
            details.append(f"Grab your ticket: {ticket_url}")

        if details:
            msg = msg.rstrip() + "\n\n" + "\n".join(details)

        return msg
    except Exception:
        logger.exception("_build_confirmation_message failed phone=...%s", phone[-4:])
        return "You're in."


def _display_time(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        match = re.match(r"^(\d{1,2}):(\d{2})$", raw)
        if not match:
            return raw
        hours = int(match.group(1))
        minutes = match.group(2)
        suffix = "AM" if hours < 12 else "PM"
        hour12 = hours % 12 or 12
        return f"{hour12}:{minutes} {suffix}"
    except Exception:
        return raw

# ── Plus one helpers ──────────────────────────────────────────────────────────

from sms_plus_one import (
    plus_one_reservation_key as _po_reservation_key,
    ensure_plus_one_reservation_map as _po_ensure_reservation_map,
    set_plus_one as _po_set_plus_one, set_awaiting_plus_one as _po_set_awaiting,
    get_confirmed_invite as _po_get_confirmed_invite,
    get_current_invite_status as _po_get_current_invite_status,
    lookup_plus_one_status as _po_lookup_status,
    norm_name_for_match as _po_norm_name,
    lookup_existing_plus_one_assignment as _po_lookup_assignment,
    plus_one_unavailable_reply as _po_unavailable_reply,
    validate_plus_one_candidate as _po_validate_candidate,
)

def _plus_one_deps():
    return {
        "invites_table": _invites_table,
        "events_table": _events_table,
        "get_current_event": _get_current_event,
        "ensure_event_headcount_counter": _ensure_event_headcount_counter,
        "ensure_plus_one_reservation_map": globals().get("_ensure_plus_one_reservation_map"),
        "plus_one_reservation_key": globals().get("_plus_one_reservation_key"),
        "search_members": search_members,
        "logger": logger,
        "norm_name_for_match": globals().get("_norm_name_for_match"),
        "lookup_plus_one_status": globals().get("_lookup_plus_one_status"),
        "lookup_existing_plus_one_assignment": globals().get("_lookup_existing_plus_one_assignment"),
        "plus_one_unavailable_reply": globals().get("_plus_one_unavailable_reply"),
    }

def _plus_one_reservation_key(name: str) -> str:
    return _po_reservation_key(name, deps=_plus_one_deps())

def _ensure_plus_one_reservation_map(event_id: str) -> None:
    return _po_ensure_reservation_map(event_id, deps=_plus_one_deps())

def _set_plus_one(event_id: str, phone: str, plus_one_name: str, is_member: bool, *, expected_name=None, capacity_override=None) -> str:
    return _po_set_plus_one(event_id, phone, plus_one_name, is_member, deps=_plus_one_deps(), expected_name=expected_name, capacity_override=capacity_override)

def _set_awaiting_plus_one(event_id: str, phone: str) -> None:
    return _po_set_awaiting(event_id, phone, deps=_plus_one_deps())

def _get_confirmed_invite(phone: str):
    return _po_get_confirmed_invite(phone, deps=_plus_one_deps())

def _get_current_invite_status(phone: str) -> str:
    return _po_get_current_invite_status(phone, deps=_plus_one_deps())

def _lookup_plus_one_status(name: str, event_id: str) -> dict:
    return _po_lookup_status(name, event_id, deps=_plus_one_deps())

def _norm_name_for_match(value: str) -> str:
    return _po_norm_name(value)

def _lookup_existing_plus_one_assignment(name: str, event_id: str, current_phone: str = "") -> dict:
    return _po_lookup_assignment(name, event_id, current_phone, deps=_plus_one_deps())

def _plus_one_unavailable_reply(name: str = "") -> str:
    return _po_unavailable_reply(name)

def _validate_plus_one_candidate(name: str, event_id: str, current_phone: str) -> tuple[bool, str, bool]:
    return _po_validate_candidate(name, event_id, current_phone, deps=_plus_one_deps())


from sms_webhook import _verify_webhook_signature

# ── Claude / Jade ─────────────────────────────────────────────────────────────

def _build_event_context(member: dict = None) -> str:
    from jade_service import build_event_context
    return build_event_context(member, deps={
        "JADE_IN_WAVE_STATUSES": JADE_IN_WAVE_STATUSES,
        "LOGISTICS_ELIGIBLE_STATUSES": LOGISTICS_ELIGIBLE_STATUSES,
        "_display_time": _display_time,
        "_get_current_event": _get_current_event,
        "_get_pending_invite": _get_pending_invite,
        "_invites_table": _invites_table,
        "logger": logger,
    })



def _claude(message: str, mode: str = "general", member: dict = None) -> str:
    from jade_service import claude
    reply = claude(message, mode, member, deps={
        "JADE_SYSTEM_PROMPT": JADE_SYSTEM_PROMPT,
        "_build_event_context": _build_event_context,
        "get_secret_string": get_secret_string,
    }).strip()
    if reply == "[NO_REPLY]":
        return ""
    if reply == "[HANDOFF]":
        _queue_host_request(member["phone"], member, "HANDOFF", message[:500])
        return "Let me check on that."
    if reply.startswith("[FEEDBACK]"):
        _queue_host_request(member["phone"], member, "HANDOFF", "Event feedback: " + message[:400])
        return reply.removeprefix("[FEEDBACK]").strip() or "Appreciate you."
    return reply


def draft_template_messages(event: dict) -> dict:
    from jade_service import draft_template_messages as _draft
    return _draft(event, deps={
        "JADE_SYSTEM_PROMPT": JADE_SYSTEM_PROMPT,
        "_display_time": _display_time,
        "get_secret_string": get_secret_string,
    })



from sms_delivery import handle_delivery as _handle_delivery_impl
from sms_host_approval import handle_host_approval as _handle_host_approval_impl

def _delivery_deps():
    return {
        "find_invite_by_message_id": _find_invite_by_message_id,
        "invites_table": _invites_table,
        "events_table": _events_table,
        "logger": logger,
    }

def _handle_delivery(raw_body: str) -> dict:
    return _handle_delivery_impl(raw_body, deps=_delivery_deps())

def _host_approval_deps():
    from member_store import mark_welcome_sent, claim_welcome_send, clear_welcome_send_claim
    from sms_adapter import maybe_send_welcome
    return {
        "request_deps": _request_deps,
        "get_pending_approval": _get_pending_approval,
        "clear_pending_approval": _clear_pending_approval,
        "get_member": get_member,
        "set_status": set_status,
        "send_sms": send_sms,
        "mark_welcome_sent": mark_welcome_sent,
        "claim_welcome_send": claim_welcome_send,
        "clear_welcome_send_claim": clear_welcome_send_claim,
        "maybe_send_welcome": maybe_send_welcome,
        "logger": logger,
    }

def _handle_host_approval(from_phone: str, text: str, sms_enabled: bool, host_phones):
    return _handle_host_approval_impl(from_phone, text, sms_enabled, host_phones, deps=_host_approval_deps())


# ── Payload helpers ───────────────────────────────────────────────────────────

def _extract_inbound_message(body: dict) -> tuple[str, str, str]:
    """Support Quo's real webhook envelope and a flat legacy/dev payload."""
    event_type = (body.get("type") or "").strip()
    payload = body

    if isinstance(body.get("data"), dict):
        if isinstance(body["data"].get("resource"), dict):
            payload = body["data"]["resource"]
        elif isinstance(body["data"].get("object"), dict):
            payload = body["data"]["object"]

    from_phone = normalize_phone(payload.get("from") or "")
    text = (payload.get("body") or payload.get("text") or payload.get("content") or "").strip()
    return event_type, from_phone, text


# ── Main handler ──────────────────────────────────────────────────────────────

def _queue_host_request(phone, member, kind, detail):
    from host_requests import queue_request
    return queue_request(_get_current_event(), phone,
                         " ".join(str(member.get(k) or "") for k in ("name", "lastName")).strip(),
                         kind, detail, hosts=get_host_phones(), send_sms=send_sms,
                         current_guest=str((_get_confirmed_invite(phone) or {}).get('plusOneName') or '') if kind.startswith('GUEST_') else None)


def _request_deps():
    return {"get_member": get_member, "get_current_event": _get_current_event, "validate_candidate": _validate_plus_one_candidate,
            "set_plus_one": _set_plus_one, "plus_one_deps": _plus_one_deps,
            "cancel_invite": _cancel_confirmed_invite, "confirm_invite": _confirm_invite_with_capacity,
            "confirmation_message": _build_confirmation_message, "send_sms": send_sms}


def _handle_message(event, context, *, verified=False):
    request_id = (context.aws_request_id if context and hasattr(context, "aws_request_id") else "local")
    logger.info("handler_start request_id=%s", request_id)
    try:
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        body = json.loads(raw_body or "{}")
        event_type, from_phone, text = _extract_inbound_message(body)
        normalized = text.upper().strip()
        logger.info("sms_handler: inbound event_type=%s from_phone=...%s text_len=%d", event_type, from_phone[-4:], len(text))

        # Always verify webhook signature. Missing secret rejects by default; only local dev can bypass with ALLOW_UNSIGNED_WEBHOOK_DEV=true.
        if not verified and not _verify_webhook_signature(event):
            logger.error("sms_handler: rejected request with invalid webhook signature")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Delivery confirmation webhook ─────────────────────────────────────
        if event_type == "message.delivered":
            return _handle_delivery(raw_body)

        # Ignore other non-message webhooks (call events, transcripts, etc.)
        if event_type and event_type != "message.received":
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        if not from_phone or not text:
            logger.info("sms_handler: no inbound message payload to process — from_phone=...%s text_len=%d", from_phone[-4:], len(text))
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"

        # ── Opt-out (STOP/UNSUBSCRIBE) ────────────────────────────────────────
        # Accept punctuation/case/spacing variants of one keyword while
        # also accept explicit full-message requests such as "please stop texting me".
        if _is_opt_out_message(text):
            if from_phone:
                try:
                    _set_opt_out(from_phone)
                    if sms_enabled:
                        send_sms(from_phone, "You're opted out of texts. rsvpsociety.com to reapply.")
                except Exception:
                    logger.exception("sms_handler: opt-out write failed phone=...%s", from_phone[-4:])
                    return {"statusCode": 503, "body": json.dumps({"ok": False})}
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Host approval commands ────────────────────────────────────────────
        host_phones = get_host_phones()
        logger.info("sms_handler: from_phone=...%s text_len=%d host_count=%d", from_phone[-4:], len(normalized), len(host_phones))
        if from_phone in host_phones:
            host_result = _handle_host_approval(from_phone, text, sms_enabled, host_phones)
            if host_result is not None:
                return host_result

        # ── Gate: approved members with SMS opt-in only ───────────────────────
        member = get_member(from_phone)
        if not member:
            # Unknown number — website link, at most once per day.
            if sms_enabled and _claim_access_reply(from_phone):
                try:
                    send_sms(from_phone, "rsvpsociety.com")
                except Exception:
                    logger.exception("sms_handler: non-member reply failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if member.get("status") != "APPROVED":
            # Known but not approved — suppress opted-out contacts.
            if sms_enabled and not coerce_bool(member.get("optOut", False)) and _claim_access_reply(from_phone):
                try:
                    send_sms(from_phone, "rsvpsociety.com")
                except Exception:
                    logger.exception("sms_handler: unapproved member reply failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if coerce_bool(member.get("optOut", False)):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        # Legacy approved/imported members may predate smsOptIn. Only an
        # explicit false value blocks SMS; STOP remains authoritative via optOut.
        if member.get("smsOptIn") is not None and not coerce_bool(member.get("smsOptIn")):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        if re.search(r"\b(kill myself|commit suicide|end my life)\b", text, re.I):
            if sms_enabled:
                send_sms(from_phone, "I'm sorry you're feeling this way. If you might act on this now, call 911 or go to the nearest emergency department. You can call or text 988 in the US. Please reach out to someone you trust who can stay with you.")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if re.search(r"(how old are you|are you single|date you|love you|lawn work|mow my|terrible day)", text, re.I):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Corrections / hallucination accusations must never be saved as +1 names ──
        if _is_correction_text(text, normalized):
            if sms_enabled:
                try:
                    send_sms(from_phone, _correction_reply())
                except Exception:
                    logger.exception("sms_handler: correction fallback SMS failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── Awaiting plus one name ────────────────────────────────────────────
        # Must run before keyword routing so a name reply like "Mike Smith"
        # doesn't fall through to the Jade general handler.
        confirmed_invite = _get_confirmed_invite(from_phone)
        from sms_guest_flow import handle_guest
        guest_reply = handle_guest(from_phone, text, normalized, confirmed_invite, _get_current_event() if confirmed_invite else {}, deps={
            **_request_deps(),
            "request": lambda kind, detail: _queue_host_request(from_phone, member, kind, detail),
            "set_awaiting": _set_awaiting_plus_one, "invites_table": _invites_table,
        })
        if guest_reply is not None:
            if sms_enabled and guest_reply:
                send_sms(from_phone, guest_reply.strip())
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── STATUS QUESTION (not a fresh confirm) ────────────────────────────
        # "I'm confirmed", "am I in", "did I confirm" are questions about state, not a
        # new RSVP. If already confirmed, answer; never re-trigger the confirmation flow.
        if _is_status_question(normalized):
            _cur_status = _get_current_invite_status(from_phone)
            if _cur_status in JADE_IN_WAVE_STATUSES:
                if sms_enabled:
                    if _cur_status in LOGISTICS_ELIGIBLE_STATUSES:
                        send_sms(from_phone, "You're in. You're good.")
                    else:
                        send_sms(from_phone, "Not yet — reply yes and I'll lock you in.")
                return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── CONFIRMED ─────────────────────────────────────────────────────────
        _rsvp_intent = _detect_rsvp_intent(text, normalized)
        if _rsvp_intent == "confirm":
            try:
                # Guard: block confirmations if event is not in a confirmable state
                ev_current = _get_current_event()
                ev_state = (ev_current.get("event_status") or "DRAFT").upper()
                if ev_state not in CONFIRMABLE_EVENT_STATES:
                    if sms_enabled:
                        send_sms(from_phone, "Confirmations are closed for this event.")
                    return {"statusCode": 200, "body": json.dumps({"ok": True})}

                invite = _get_reconfirmable_invite(from_phone)
                if invite:
                    event_id = invite["eventId"]

                    ev = ev_current  # reuse already-fetched event
                    capacity = int(ev.get("capacity") or 0)
                    confirmed_now = False
                    if capacity > 0:
                        # Preserve the existing RSVP Society show-rate rule while
                        # enforcing its headcount target atomically.
                        target_confirmed = target_confirmed_headcount(capacity, ev)
                        confirmation_result = _confirm_invite_with_capacity(
                            event_id, from_phone, target_confirmed
                        )
                        if confirmation_result == "FULL":
                            _queue_host_request(from_phone, member, "WAITLIST", "RSVP at capacity")
                            _invites_table().update_item(Key={"eventId": event_id, "phone": from_phone},
                                UpdateExpression="SET waitlisted = :yes", ExpressionAttributeValues={":yes": True})
                            logger.info(
                                "sms_handler: capacity reached target=%d event=%s phone=...%s",
                                target_confirmed, event_id, from_phone[-4:],
                            )
                            if sms_enabled:
                                try:
                                    send_sms(
                                        from_phone,
                                        "I have you on the waitlist. Let me see what I can do.",
                                    )
                                except Exception:
                                    logger.exception("sms_handler: at-capacity SMS failed phone=...%s", from_phone[-4:])
                            return {"statusCode": 200, "body": json.dumps({"ok": True})}
                        confirmed_now = confirmation_result == "CONFIRMED"
                    else:
                        confirmed_now = _update_invite_status(event_id, from_phone, "CONFIRMED")

                    if confirmed_now and sms_enabled:
                        try:
                            confirmation_msg = _build_confirmation_message(from_phone)
                            # If plus ones are allowed, ask for the guest name as a
                            # separate sentence so Jade/details copy does not get mangled.
                            if ev.get("allowPlusOnes"):
                                _set_awaiting_plus_one(event_id, from_phone)
                                from followups import schedule
                                try:
                                    schedule(ev, 'guest', from_phone, after_hours=72)
                                except Exception:
                                    logger.exception('Guest follow-up scheduling failed; RSVP remains confirmed')
                                confirmation_msg = "You’re in. Bringing someone? Send their first and last name, or let me know if it’s just you."
                            send_sms(from_phone, confirmation_msg)
                        except Exception:
                            logger.exception("sms_handler: confirmation SMS failed phone=...%s", from_phone[-4:])
            except Exception:
                logger.exception("sms_handler: CONFIRMED branch failed phone=...%s", from_phone[-4:])
                return {"statusCode": 503, "body": json.dumps({"ok": False})}
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── DECLINED ──────────────────────────────────────────────────────────
        if _rsvp_intent == "decline":
            try:
                invite = _get_pending_invite(from_phone)
                if invite:
                    _update_invite_status(invite["eventId"], from_phone, "DECLINED")
                    if sms_enabled:
                        try:
                            send_sms(from_phone, "No worries. You'll hear from me for the next.")
                        except Exception:
                            logger.exception("sms_handler: declined SMS failed phone=...%s", from_phone[-4:])
                else:
                    current_event = _get_current_event()
                    event_id = (current_event.get("eventSlug") or current_event.get("activeEventSlug") or "").strip()
                    finalized = coerce_bool(current_event.get("attendanceFinalized", False))
                    if event_id and (current_event.get("event_status") or "DRAFT").upper() in CONFIRMABLE_EVENT_STATES and not finalized:
                        current_invite = _invites_table().get_item(
                            Key={"eventId": event_id, "phone": from_phone}, ConsistentRead=True,
                        ).get("Item") or {}
                        if current_invite.get("status") == "CONFIRMED" and cutoff_reached(current_event, 24):
                            _queue_host_request(from_phone, member, "CANCEL", "Cancel RSVP")
                            if sms_enabled:
                                send_sms(from_phone, "Let me see what I can do.")
                            return {"statusCode": 200, "body": json.dumps({"ok": True})}
                        if (current_invite.get("status") or "").upper() == "CONFIRMED" and _cancel_confirmed_invite(event_id, from_phone):
                            if sms_enabled:
                                try:
                                    send_sms(from_phone, "Got it — your RSVP is canceled and your spot is open.")
                                except Exception:
                                    logger.exception("sms_handler: cancellation SMS failed phone=...%s", from_phone[-4:])
            except Exception:
                logger.exception("sms_handler: DECLINED branch failed phone=...%s", from_phone[-4:])
                message_id = _extract_inbound_message_id(body, event)
                notice_id = message_id or from_phone
                retention = 30 * 86400 if message_id else 86400
                if sms_enabled and _claim_optional_reply(notice_id, "CANCEL_FAILURE_REPLY", retention):
                    try:
                        send_sms(from_phone, "I couldn't confirm your RSVP update. Please contact the host if you don't receive a cancellation confirmation.")
                    except Exception:
                        logger.exception("sms_handler: cancellation failure reply failed phone=...%s", from_phone[-4:])
                return {"statusCode": 503, "body": json.dumps({"ok": False})}
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── IGNORE — social acknowledgments, no response needed ──────────────
        if normalized in IGNORE_KEYWORDS:
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── CORRECTION / HALLUCINATION ACCUSATION ─────────────────────────────
        if any(phrase in normalized for phrase in ("HALLUCINAT", "YOU'RE WRONG", "YOURE WRONG", "THAT'S WRONG", "THATS WRONG", "NOT TRUE", "MAKING IT UP")):
            if sms_enabled:
                try:
                    send_sms(from_phone, "You're right — I'll stick to confirmed event details. What do you want to know?")
                except Exception:
                    logger.exception("sms_handler: correction SMS failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── RUNNING LATE ───────────────────────────────────────────────────────
        if any(phrase in normalized for phrase in RUNNING_LATE_KEYWORDS):
            if sms_enabled:
                try:
                    ev = _get_current_event()
                    started = datetime.now(timezone.utc) >= event_start(ev)
                    if started and not ev.get("endTime"):
                        _queue_host_request(from_phone, member, "HANDOFF", text[:500])
                        send_sms(from_phone, "Let me check on that.")
                    elif started:
                        send_sms(from_phone, "It ends at " + _display_time(str(ev["endTime"])) + ".")
                    else:
                        send_sms(from_phone, "See you there.")
                except Exception:
                    logger.exception("sms_handler: running late SMS failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── COST / TICKET — only expose ticket URL to invited/confirmed members ────
        if any(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", normalized) for phrase in COST_KEYWORDS):
            try:
                ev = _get_current_event()
                ticket_url = (ev.get("ticketUrl") or "").strip()

                # Look up the member's actual current invite status for this event
                # member_status is only available inside _build_event_context — not here
                current_ev   = ev
                ev_slug      = (current_ev.get("eventSlug") or current_ev.get("eventId") or "").strip()
                invite_record = None
                if ev_slug and from_phone:
                    try:
                        invite_record = _invites_table().get_item(
                            Key={"eventId": ev_slug, "phone": from_phone}
                        ).get("Item")
                    except Exception:
                        pass
                invite_status = ((invite_record or {}).get("status") or "").upper()

                if ticket_url and invite_status in LOGISTICS_ELIGIBLE_STATUSES:
                    reply = f"Grab your ticket: {ticket_url}"
                elif invite_status == "INVITED":
                    reply = "Once you're confirmed, I'll send what you need."
                elif invite_status in LOGISTICS_ELIGIBLE_STATUSES:
                    reply = "Not up yet."
                else:
                    reply = "Not up yet."
                if sms_enabled:
                    send_sms(from_phone, reply)
            except Exception:
                logger.exception("sms_handler: cost reply failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── EXTRA GUESTS — one per invite, hard stop ───────────────────────────
        if any(phrase in normalized for phrase in EXTRA_GUEST_KEYWORDS):
            if sms_enabled:
                try:
                    send_sms(from_phone, "One guest per invite.")
                except Exception:
                    logger.exception("sms_handler: extra guest SMS failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}


        arrangements = bool(_normalized_tokens(text) & {"TABLE", "SECTION", "VIP", "SECTIONS", "TABLES", "BOOTH", "CABANA", "CABANAS", "BOTTLE", "BOTTLES", "BIRTHDAY"}) or "GROUP PRIC" in normalized
        if arrangements and (not _get_current_event().get("sectionInfo") or re.search(r"\b(book|reserve|want|need|birthday|group)\b", text, re.I)):
            _queue_host_request(from_phone, member, "HANDOFF", text[:500])
            if sms_enabled:
                send_sms(from_phone, "I'll have the person handling that get back to you.")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── OPERATIONAL DETAILS — deterministic unknown handling ──────────────
        # Legacy event records may still carry one fact in the retired Jade Answers field,
        # e.g. "Cash bar." Do not let Claude answer unrelated logistics such as
        # hookah or food from brand story.
        try:
            tokens = _normalized_tokens(normalized)

            # Privacy gate: deterministic operational answers must obey the same
            # confirmation rules as Claude context. Invited-but-unconfirmed guests
            # should not receive logistics from parkingInfo, sectionInfo, jadeNotes,
            # ticketUrl, description, or similar event-private fields.
            protected_operational_question = any((
                _contains_topic(tokens, "FOOD"),
                _contains_topic(tokens, "DRINK", "DRINKS", "BAR"),
                _contains_topic(tokens, "HOOKAH"),
                _contains_topic(tokens, "PARK", "PARKING"),
                _contains_topic(tokens, "SECTION", "SECTIONS", "TABLE", "TABLES", "VIP", "BOOTH", "CABANA", "CABANAS"),
            ))
            invite_status = _get_current_invite_status(from_phone)
            if protected_operational_question and invite_status not in LOGISTICS_ELIGIBLE_STATUSES:
                if sms_enabled:
                    send_sms(from_phone, "Once you're confirmed, I'll send what you need.")
                return {"statusCode": 200, "body": json.dumps({"ok": True})}

            ev = _get_current_event()
            # Free-text amenity notes need their full meaning, including negation.
            # Let Jade interpret them instead of inferring availability from words.
            needs_notes = _contains_topic(tokens, "FOOD", "DRINK", "DRINKS", "BAR", "HOOKAH")
            operational_reply = None
            detail_parts = []
            if _contains_topic(tokens, "TIME", "WHEN", "START", "DOORS"):
                start = _display_time(str(ev.get("startTime") or ""))
                end = _display_time(str(ev.get("endTime") or ""))
                if start and end:
                    try:
                        started = datetime.now(timezone.utc) >= event_start(ev)
                    except (ValueError, TypeError, KeyError):
                        started = False
                    detail_parts.append(f"It ends at {end}." if started else f"{start}–{end}.")
                elif start:
                    detail_parts.append(f"Doors at {start}.")
            if _contains_topic(tokens, "PARK", "PARKING"):
                parking_val = (ev.get("parkingInfo") or ev.get("parking_info") or "").strip()
                if parking_val:
                    detail_parts.append(parking_val if parking_val.endswith(".") else f"{parking_val}.")
                else:
                    needs_notes = True
            if _contains_topic(tokens, "SECTION", "SECTIONS", "TABLE", "TABLES", "VIP", "BOOTH", "CABANA", "CABANAS"):
                section_val = (ev.get("sectionInfo") or ev.get("section_info") or "").strip()
                if section_val:
                    detail_parts.append(section_val if section_val.endswith(".") else f"{section_val}.")
                else:
                    needs_notes = True
            if detail_parts and not needs_notes:
                operational_reply = " ".join(detail_parts[:4]).strip()
                if sms_enabled:
                    send_sms(from_phone, operational_reply)
                return {"statusCode": 200, "body": json.dumps({"ok": True})}
        except Exception:
            logger.exception("sms_handler: operational detail guard failed phone=...%s", from_phone[-4:])

        # ── AMBIGUOUS — Jade asks for a direct confirm ─────────────────────────
        if normalized in AMBIGUOUS_KEYWORDS:
            try:
                reply = _claude(text, mode="ambiguous", member=member)
                if sms_enabled and reply:
                    try:
                        send_sms(from_phone, reply)
                    except Exception:
                        logger.exception("sms_handler: ambiguous SMS send failed phone=...%s", from_phone[-4:])
            except Exception:
                logger.exception("sms_handler: AMBIGUOUS Jade call failed phone=...%s", from_phone[-4:])
                if sms_enabled:
                    try:
                        send_sms(from_phone, "Tell me yes if you want me to hold it.")
                    except Exception:
                        logger.exception("sms_handler: ambiguous fallback SMS failed phone=...%s", from_phone[-4:])
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── GENERAL — everything else goes to Jade ────────────────────────────
        try:
            reply = _claude(text, mode="general", member=member)
            if sms_enabled and reply:
                try:
                    send_sms(from_phone, reply)
                except Exception:
                    logger.exception("sms_handler: general SMS send failed phone=...%s", from_phone[-4:])


        except Exception:
            logger.exception("sms_handler: GENERAL Jade call failed phone=...%s", from_phone[-4:])
            if sms_enabled:
                try:
                    send_sms(from_phone, "Soon as I know.")
                except Exception:
                    logger.exception("sms_handler: general fallback SMS failed phone=...%s", from_phone[-4:])

        return {"statusCode": 200, "body": json.dumps({"ok": True})}

    except Exception:
        # Outer catch: something went very wrong (bad JSON, normalize failure, etc.)
        # Allow retry after an unexpected processing failure.
        logger.exception("sms_handler: unhandled top-level exception")
        return {"statusCode": 503, "body": json.dumps({"ok": False})}


def handler(event, context):
    # STOP bypasses receipt storage entirely. Signature checking still runs first.
    if not _verify_webhook_signature(event):
        logger.error("sms_handler: rejected request with invalid webhook signature")
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        body = json.loads(raw)
        event_type, phone, text = _extract_inbound_message(body)
    except (ValueError, TypeError, AttributeError):
        return {"statusCode": 400, "body": json.dumps({"ok": False})}
    if event_type != "message.received" or _is_opt_out_message(text):
        return _handle_message(event, context, verified=True)
    message_id = _extract_inbound_message_id(body, event)
    owner = _claim_inbound_message(message_id)
    if owner == "DONE":
        return {"statusCode": 200, "body": json.dumps({"ok": True, "duplicate": True})}
    if owner == "BUSY":
        return {"statusCode": 503, "body": json.dumps({"ok": False, "retry": True})}
    result = _handle_message(event, context, verified=True)
    if owner:
        _finish_inbound_message(message_id, owner, result.get("statusCode") == 200)
    return result
