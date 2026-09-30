"""
admin_member_routes.py
All /admin/members/* route handlers.
Each function receives the parsed (method, path, headers, event, token) and
returns a complete API Gateway response dict.
"""
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key as DKey
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeSerializer

from admin_shared import resp, get_query, get_body, normalize_import_source, invites_table, coerce_bool
from member_store import (
    count_members_by_status, list_members_by_status_page, set_status, set_gender, set_tier_override,
    record_attendance, normalize_phone,
    search_members_page, get_member, mark_welcome_sent, claim_welcome_send, clear_welcome_send_claim,
    normalize_member_record, write_welcome_error,
    CONFIRMED_FAMILY_STATUSES,
)
from audit_log import (
    log_action,
    ACTION_MEMBER_APPROVED, ACTION_MEMBER_DENIED, ACTION_MEMBER_PENDING,
    ACTION_MEMBER_DELETED, ACTION_MEMBER_GENDER_SET, ACTION_MEMBER_TIER_SET,
    ACTION_ATTENDANCE, ACTION_MEMBER_IMPORTED,
)
from sms_adapter import maybe_send_welcome
from sms_plus_one import plus_one_reservation_key
from invite_capacity import transition_confirmed_invite
from location_resolver import InvalidZipError, resolve_us_zip

logger = logging.getLogger()

_DDB_SERIALIZER = TypeSerializer()
MAX_IMPORT_UNIQUE_ZIPS = 100
ZIP_LOOKUP_WORKERS = 10

def _ddb_av(value):
    return _DDB_SERIALIZER.serialize(value)

def _checkin_ttl_days() -> int:
    try:
        return max(1, int(os.getenv("CHECKIN_RETENTION_DAYS", "90")))
    except Exception:
        return 90

def _checkin_ttl_epoch() -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=_checkin_ttl_days())).timestamp())



def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _active_event_slug() -> str:
    try:
        ev = boto3.resource("dynamodb").Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events")).get_item(Key={"eventId": "current"}).get("Item") or {}
        slug = (ev.get("activeEventSlug") or ev.get("eventSlug") or ev.get("slug") or "").strip()
        return "" if slug == "current" else slug
    except Exception:
        logger.exception("_active_event_slug failed")
        return ""


def _invite_map_for_event(event_id: str) -> dict:
    if not event_id:
        return {}
    out = {}
    try:
        invites_t = boto3.resource("dynamodb").Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))
        kwargs = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
        while True:
            page = invites_t.query(**kwargs)
            for item in page.get("Items", []):
                phone = item.get("phone")
                if phone:
                    out[phone] = item
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except Exception:
        logger.exception("_invite_map_for_event failed event=%s", event_id)
    return out


def _enrich_members_with_current_invite(members: list[dict], event_id: str = "") -> list[dict]:
    active_event_id = event_id or _active_event_slug()
    invite_map = _invite_map_for_event(active_event_id) if active_event_id else {}
    enriched = []
    for member in members:
        item = dict(member or {})
        phone = item.get("phone")
        invite = invite_map.get(phone) or {}
        status = (invite.get("status") or "NOT_INVITED").upper()
        item.update({
            "currentEventId": active_event_id,
            "currentEventInviteStatus": status,
            "inviteStatus": status,
            "lastInvitedAt": invite.get("invitedAt") or item.get("lastInvitedAt") or "",
            "lastConfirmedAt": invite.get("confirmedAt") or item.get("lastConfirmedAt") or "",
            "lastSmsStatus": invite.get("deliveryStatus") or ("DELIVERED" if invite.get("deliveredAt") else ""),
            "deliveredAt": invite.get("deliveredAt") or "",
            "declinedAt": invite.get("declinedAt") or "",
            "attendedAt": invite.get("attendedAt") or "",
            "noShowAt": invite.get("noShowAt") or "",
            "waveNumber": invite.get("waveNumber") or "",
        })
        enriched.append(item)
    return enriched


# ── GET /admin/members/confirmed ──────────────────────────────────────────────

def get_confirmed(event: dict, headers: dict, token: str) -> dict:
    ddb = boto3.resource("dynamodb")
    members_table_name = os.getenv("MEMBERS_TABLE_NAME", "rsvp-members")
    invites_t = ddb.Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))
    members_t = ddb.Table(members_table_name)

    qs = event.get("queryStringParameters") or {}
    event_id = (qs.get("eventId") or "").strip()
    if not event_id:
        return resp(headers, 400, {"ok": False, "error": "eventId query parameter required"})

    # Paginate invite query — large events can exceed a single DDB page
    confirmed_invites = []
    kwargs: dict = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
    while True:
        page = invites_t.query(**kwargs)
        confirmed_invites.extend(
            [i for i in page.get("Items", []) if (i.get("status") or "").upper() in CONFIRMED_FAMILY_STATUSES]
        )
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    # BatchGetItem — 200 confirmed = 2 batch calls instead of 200 GetItem calls
    phones = [inv["phone"] for inv in confirmed_invites if inv.get("phone")]
    member_map: dict = {}
    for i in range(0, len(phones), 100):
        batch_phones = phones[i:i + 100]
        try:
            batch_resp = members_t.meta.client.batch_get_item(
                RequestItems={
                    members_table_name: {
                        "Keys": [{"phone": p} for p in batch_phones],
                        "ProjectionExpression": "phone, #n, lastName, gender",
                        "ExpressionAttributeNames": {"#n": "name"},
                    }
                }
            )
            for item in batch_resp.get("Responses", {}).get(members_table_name, []):
                member_map[item["phone"]] = normalize_member_record(item) or {}
            # Retry throttled keys, but never spin until Lambda timeout. Missing
            # member data falls back to the immutable invite snapshot for display.
            unprocessed = batch_resp.get("UnprocessedKeys") or {}
            retries = 0
            while unprocessed and retries < 4:
                retry = members_t.meta.client.batch_get_item(RequestItems=unprocessed)
                for item in retry.get("Responses", {}).get(members_table_name, []):
                    member_map[item["phone"]] = normalize_member_record(item) or {}
                unprocessed = retry.get("UnprocessedKeys") or {}
                retries += 1
            if unprocessed:
                unresolved = len((unprocessed.get(members_table_name) or {}).get("Keys") or [])
                logger.error("get_confirmed: %d member lookup key(s) unresolved after retries", unresolved)
        except Exception:
            logger.exception("get_confirmed: batch_get_item failed batch i=%d", i)

    # Resolve +1 membership with at most one paginated members-table scan per
    # request. The old implementation called search_members() once per +1, which
    # could rescan the entire ~2,500-member table dozens of times on check-in load.
    plus_one_names = {
        (inv.get("plusOneName") or "").strip().lower()
        for inv in confirmed_invites
        if (inv.get("plusOneName") or "").strip()
    }
    member_name_keys: set[str] = set()
    if plus_one_names:
        scan_kwargs = {
            "ProjectionExpression": "#n, lastName, #s, pendingExpiresAt",
            "ExpressionAttributeNames": {"#n": "name", "#s": "status"},
        }
        while True:
            try:
                scan_page = members_t.scan(**scan_kwargs)
            except Exception:
                logger.exception("get_confirmed: single-pass +1 member lookup failed")
                break
            for raw in scan_page.get("Items", []):
                status = (raw.get("status") or "").upper()
                expires = (raw.get("pendingExpiresAt") or "").strip()
                if status == "PENDING" and expires:
                    try:
                        if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                            continue
                    except Exception:
                        pass
                first = (raw.get("name") or "").strip().lower()
                last = (raw.get("lastName") or "").strip().lower()
                full = f"{first} {last}".strip()
                member_name_keys.update(k for k in (first, last, full) if k)
            last_key = scan_page.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

    members_out = []
    for invite in confirmed_invites:
        phone = invite.get("phone", "")
        if not phone:
            continue
        m = member_map.get(phone) or {}
        # Fall back to invite snapshot if member row has no name
        # (covers deleted members and rows written before name snapshot was added)
        first_name = (m.get("name")     or invite.get("name")     or "").strip()
        last_name  = (m.get("lastName") or invite.get("lastName") or "").strip()
        full_name  = " ".join(part for part in (first_name, last_name) if part).strip()
        # Live check against the single request-scoped member-name index above.
        plus_one_name = invite.get("plusOneName", "")
        plus_one_clean = plus_one_name.strip().lower()
        plus_one_is_member = bool(plus_one_clean and plus_one_clean in member_name_keys)

        members_out.append({
            "phone":           phone,
            "name":            first_name,
            "lastName":        last_name,
            "fullName":        full_name,
            "gender":          (m.get("gender") or invite.get("gender") or "").strip(),
            "confirmedAt":     invite.get("confirmedAt", ""),
            "attendedAt":      invite.get("attendedAt", ""),
            "checkedIn":       bool(invite.get("attendedAt") or (invite.get("status") or "").upper() == "ATTENDED"),
            "plusOneName":     plus_one_name,
            "plusOneIsMember": plus_one_is_member,
            "plusOneCheckedIn": bool(invite.get("plusOneAttendedAt")),
            "plusOneAttendedAt": invite.get("plusOneAttendedAt", ""),
        })

    members_out.sort(key=lambda x: (
        (x.get("lastName") or x.get("name") or "").lower(),
        (x.get("name") or "").lower(),
    ))
    return resp(headers, 200, {"ok": True, "members": members_out})


# ── GET /admin/members ────────────────────────────────────────────────────────

def _query_limit(qs: dict, default: int = 50, maximum: int = 200) -> int:
    try:
        parsed = int(qs.get("limit") or qs.get("pageSize") or default)
    except Exception:
        parsed = default
    return max(1, min(maximum, parsed))


def _query_cursor(qs: dict) -> str:
    return (qs.get("nextPageToken") or qs.get("next_page_token") or qs.get("nextToken") or qs.get("cursor") or "").strip()


def _member_matches_admin_filters(member: dict, qs: dict) -> bool:
    q = (qs.get("q") or qs.get("search") or "").strip().lower()
    if q:
        haystack = " ".join(str(member.get(k) or "") for k in ("name", "lastName", "phone", "email", "instagram", "city", "market")).lower()
        if q not in haystack:
            return False

    gender = (qs.get("gender") or "").strip().upper()
    if gender and (member.get("gender") or "").strip().upper() != gender:
        return False

    tier = (qs.get("tier") or "").strip()
    if tier:
        member_tier = str(member.get("tierOverride", member.get("tier", "0")))
        if member_tier != tier:
            return False

    sms_filter = (qs.get("smsOptIn") or qs.get("sms") or "").strip().lower()
    if sms_filter == "yes" and not coerce_bool(member.get("smsOptIn")):
        return False
    if sms_filter == "no" and coerce_bool(member.get("smsOptIn")):
        return False

    attendance = (qs.get("attendance") or "").strip().lower()
    attended = int(member.get("attendedCount") or 0)
    no_show = int(member.get("noShowCount") or 0)
    if attendance == "attended" and attended < 1:
        return False
    if attendance == "never" and attended > 0:
        return False
    if attendance in ("noshow", "no_show") and no_show < 1:
        return False

    return True


def list_members(event: dict, headers: dict, token: str) -> dict:
    qs = get_query(event)
    status = (qs.get("status") or "PENDING").upper()
    limit = _query_limit(qs)
    cursor = _query_cursor(qs)
    active_event_id = (qs.get("eventId") or "").strip() or _active_event_slug()

    # Search/filtered admin loads use the paginated search path when q is
    # present. Gender/tier/SMS/attendance filters remain post-query filters;
    # the response still returns a cursor instead of one giant payload.
    query = (qs.get("q") or qs.get("search") or "").strip()
    if query:
        page = search_members_page(query, limit=limit, next_token=cursor)
        raw_members = page.get("members", [])
    else:
        page = list_members_by_status_page(status=status, limit=limit, next_token=cursor)
        raw_members = page.get("members", [])

    members = [m for m in raw_members if _member_matches_admin_filters(m, qs)]
    members = _enrich_members_with_current_invite(members, active_event_id)
    payload = {
        "ok": True,
        "members": members,
        "pageSize": limit,
        "count": len(members),
        "currentEventId": active_event_id,
        "hasMore": bool(page.get("hasMore")),
    }
    if page.get("nextPageToken"):
        payload["nextPageToken"] = page.get("nextPageToken")

    # Exact totals are opt-in so normal list navigation stays cursor-driven.
    # The admin stats row requests this intentionally; search/filter pages do not
    # pretend a filtered scan count is exact unless a dedicated index exists.
    if (qs.get("includeTotal") or qs.get("include_total") or "").strip().lower() in {"1", "true", "yes"}:
        if not query and not any((qs.get(k) or "").strip() for k in ("gender", "tier", "smsOptIn", "sms", "attendance")):
            try:
                payload["total"] = count_members_by_status(status)
            except Exception:
                logger.exception("list_members: count failed status=%s", status)
                payload["total"] = len(members)
    return resp(headers, 200, payload)


def get_member_history(event: dict, headers: dict, token: str) -> dict:
    qs = get_query(event)
    phone_raw = (qs.get("phone") or "").strip()
    if not phone_raw:
        return resp(headers, 400, {"ok": False, "error": "phone query parameter required"})
    try:
        phone = normalize_phone(phone_raw)
    except ValueError as e:
        return resp(headers, 400, {"ok": False, "error": str(e)})

    invites_t = boto3.resource("dynamodb").Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))
    rows = []
    try:
        kwargs = {
            "IndexName": "phone-index",
            "KeyConditionExpression": DKey("phone").eq(phone),
        }
        while True:
            page = invites_t.query(**kwargs)
            rows.extend(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except Exception:
        logger.exception("get_member_history failed phone=...%s", phone[-4:])
        return resp(headers, 500, {"ok": False, "error": "history_lookup_failed"})

    def sort_key(row: dict) -> str:
        return str(row.get("attendedAt") or row.get("confirmedAt") or row.get("declinedAt") or row.get("invitedAt") or "")
    rows.sort(key=sort_key, reverse=True)
    history = [{
        "eventId": r.get("eventId", ""),
        "eventSlug": r.get("eventSlug") or r.get("eventId", ""),
        "eventLabel": r.get("eventLabel") or r.get("eventId", ""),
        "status": r.get("status", ""),
        "waveNumber": r.get("waveNumber", ""),
        "invitedAt": r.get("invitedAt", ""),
        "confirmedAt": r.get("confirmedAt", ""),
        "declinedAt": r.get("declinedAt", ""),
        "cancelledAt": r.get("cancelledAt", ""),
        "cancellationTiming": r.get("cancellationTiming", ""),
        "tierExcused": r.get("tierExcused", False),
        "attendedAt": r.get("attendedAt", ""),
        "noShowAt": r.get("noShowAt", ""),
        "deliveredAt": r.get("deliveredAt", ""),
        "deliveryStatus": r.get("deliveryStatus", ""),
        "plusOneName": r.get("plusOneName", ""),
    } for r in rows]
    return resp(headers, 200, {"ok": True, "phone": phone, "history": history, "total": len(history)})


# ── DELETE /admin/members ─────────────────────────────────────────────────────
# Soft-delete: wipes PII, sets status=DELETED. Tombstones all invite
# records for this member so they are excluded from future blasts and
# analytics. Checkin records are left intact as attendance history.

def delete_member(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    raw_phone = (data.get("phone") or "").strip()
    if not raw_phone:
        return resp(headers, 400, {"ok": False, "error": "phone required"})

    try:
        phone = normalize_phone(raw_phone)
    except ValueError as e:
        return resp(headers, 400, {"ok": False, "error": str(e)})
    confirm_phone_raw = (data.get("confirmPhone") or "").strip()
    try:
        confirm_phone = normalize_phone(confirm_phone_raw) if confirm_phone_raw else ""
    except ValueError:
        confirm_phone = ""
    if confirm_phone != phone:
        return resp(headers, 400, {"ok": False, "error": "confirmPhone must exactly match phone for delete"})

    now = _now()
    ddb = boto3.resource("dynamodb")
    members_t = ddb.Table(os.getenv("MEMBERS_TABLE_NAME", "rsvp-members"))
    events_t = ddb.Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))

    members_t.update_item(
        Key={"phone": phone},
        UpdateExpression=(
            "SET #status = :deleted, deletedAt = :now "
            "REMOVE #name, lastName, email, instagram, tags, smsOptIn, "
            "zipCode, city, #state, latitude, longitude, locationSource"
        ),
        ExpressionAttributeNames={"#status": "status", "#name": "name", "#state": "state"},
        ExpressionAttributeValues={":deleted": "DELETED", ":now": now},
    )

    # Tombstone all invite rows for this member, paginating through them all
    inv_t = invites_table()
    cleanup_failed = False
    try:
        inv_kwargs: dict = {
            "IndexName": "phone-index",
            "KeyConditionExpression": DKey("phone").eq(phone),
        }
        while True:
            page = inv_t.query(**inv_kwargs)
            for inv in page.get("Items", []):
                event_id = inv["eventId"]
                invite_cleaned = False
                guest_name = ""
                for _attempt in range(3):
                    latest_invite = inv_t.get_item(
                        Key={"eventId": event_id, "phone": phone}, ConsistentRead=True,
                    ).get("Item") or {}
                    status = (latest_invite.get("status") or "").upper()
                    if not latest_invite or status == "DELETED":
                        invite_cleaned = True
                        break
                    if status in CONFIRMED_FAMILY_STATUSES:
                        if transition_confirmed_invite(
                            invites_table=inv_t,
                            events_table=events_t,
                            ddb_client=boto3.client("dynamodb"),
                            event_id=event_id,
                            phone=phone,
                            target_status="DELETED",
                            allowed_statuses=CONFIRMED_FAMILY_STATUSES,
                        ):
                            invite_cleaned = True
                            break
                        continue
                    try:
                        inv_t.update_item(
                            Key={"eventId": event_id, "phone": phone},
                            UpdateExpression="SET #status = :deleted",
                            ConditionExpression="#status = :old_status",
                            ExpressionAttributeNames={"#status": "status"},
                            ExpressionAttributeValues={":deleted": "DELETED", ":old_status": status},
                        )
                        guest_name = (latest_invite.get("plusOneName") or "").strip()
                        invite_cleaned = True
                        break
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                            raise
                if not invite_cleaned:
                    raise RuntimeError(f"invite cleanup did not finish for event {event_id}")
                if guest_name:
                    guest_key = plus_one_reservation_key(guest_name, deps={})
                    try:
                        events_t.update_item(
                            Key={"eventId": inv["eventId"]},
                            UpdateExpression="REMOVE #reservations.#guest",
                            ConditionExpression="#reservations.#guest = :owner",
                            ExpressionAttributeNames={
                                "#reservations": "plusOneReservations",
                                "#guest": guest_key,
                            },
                            ExpressionAttributeValues={":owner": phone},
                        )
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                            logger.exception(
                                "delete_member: plus-one reservation cleanup failed event=%s phone=...%s",
                                event_id, phone[-4:],
                            )
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            inv_kwargs["ExclusiveStartKey"] = last
    except Exception:
        cleanup_failed = True
        logger.exception("delete_member: failed to tombstone invites phone=...%s", phone[-4:])

    log_action(token=token, action=ACTION_MEMBER_DELETED, target_phone=phone)
    if cleanup_failed:
        return resp(headers, 503, {"ok": False, "error": "Member disabled, but invite cleanup did not finish; retry deletion."})
    return resp(headers, 200, {"ok": True})


def _clear_pending_approvals_for_member(phone: str) -> None:
    """Delete every host approval queue row for one member phone."""
    table = boto3.resource("dynamodb").Table(
        os.getenv("PENDING_APPROVALS_TABLE_NAME", "rsvp-pending-approvals")
    )
    kwargs = {"ProjectionExpression": "hostPhone, memberPhone"}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            if item.get("memberPhone") == phone:
                table.delete_item(Key={"hostPhone": item.get("hostPhone"), "memberPhone": phone})
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last



# ── POST /admin/members/status ────────────────────────────────────────────────

def set_member_status(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    phone_raw = (data.get("phone") or "").strip()
    status = (data.get("status") or "").strip().upper()

    if not phone_raw or status not in ("PENDING", "APPROVED", "DENIED"):
        return resp(headers, 400, {"ok": False, "error": "phone and valid status required"})

    try:
        phone = normalize_phone(phone_raw)
    except ValueError as e:
        return resp(headers, 400, {"ok": False, "error": str(e)})

    # Capture previous status before the write so we can guard the welcome
    # on the first PENDING→APPROVED transition only.
    prev = get_member(phone) or {}
    prev_status = (prev.get("status") or "").upper()

    try:
        set_status(phone, status)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return resp(headers, 409, {"ok": False, "error": "Member was deleted or no longer exists. A fresh signup is required."})
        raise

    if status in ("APPROVED", "DENIED"):
        try:
            _clear_pending_approvals_for_member(phone)
        except Exception:
            logger.exception("set_member_status: failed to clear stale approval rows phone=...%s", phone[-4:])
            return resp(headers, 503, {"ok": False, "error": "status updated but approval queue cleanup failed"})

    action = (ACTION_MEMBER_APPROVED if status == "APPROVED"
              else ACTION_MEMBER_DENIED if status == "DENIED"
              else ACTION_MEMBER_PENDING)
    log_action(token=token, action=action, target_phone=phone, metadata={"status": status})

    # On APPROVED transition: send Jade welcome (best-effort) only on the
    # *first* approval — guard on both prev_status and welcomeSentAt so a
    # re-approval of an already-approved member never re-sends.
    if status == "APPROVED" and prev_status != "APPROVED":
        try:
            # Signing up is the consent act. First approval marks the member as
            # SMS eligible unless they explicitly STOP/opt out later.
            # This is a best-effort write; explicit opt-outs are still honored.
            # Consent was recorded by signup/import or the reviewed reapplication.
            # Approval alone must not opt an unconsented contact into messaging.
            member = get_member(phone) or {"phone": phone, "status": "APPROVED"}
            member["status"] = "APPROVED"
            if claim_welcome_send(phone):
                try:
                    sent = maybe_send_welcome(member)
                    if sent:
                        mark_welcome_sent(phone)
                    else:
                        clear_welcome_send_claim(phone)
                except Exception:
                    clear_welcome_send_claim(phone)
                    raise
        except Exception as _welcome_exc:
            import traceback
            _err = traceback.format_exc()
            logger.exception("set_member_status: welcome SMS failed phone=...%s", phone[-4:])
            write_welcome_error(phone, _err)

    return resp(headers, 200, {"ok": True})


# ── POST /admin/members/gender ────────────────────────────────────────────────

def set_member_gender(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    phone_raw = (data.get("phone") or "").strip()
    gender = (data.get("gender") or "").strip().upper()
    if not phone_raw or gender not in ("M", "F", "O"):
        return resp(headers, 400, {"ok": False, "error": "phone and gender (M/F/O) required"})
    try:
        phone = normalize_phone(phone_raw)
    except ValueError as e:
        return resp(headers, 400, {"ok": False, "error": str(e)})
    set_gender(phone, gender)
    log_action(token=token, action=ACTION_MEMBER_GENDER_SET,
               target_phone=phone, metadata={"gender": gender})
    return resp(headers, 200, {"ok": True})


# ── POST /admin/members/tier ──────────────────────────────────────────────────

def set_member_tier(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    phone_raw = (data.get("phone") or "").strip()
    tier = data.get("tier")
    if not phone_raw or tier is None:
        return resp(headers, 400, {"ok": False, "error": "phone and tier required"})
    try:
        tier_int = int(tier)
    except (TypeError, ValueError):
        return resp(headers, 400, {"ok": False, "error": "tier must be an integer (0–3)"})
    try:
        phone = normalize_phone(phone_raw)
    except ValueError as e:
        return resp(headers, 400, {"ok": False, "error": str(e)})
    set_tier_override(phone, tier_int)
    log_action(token=token, action=ACTION_MEMBER_TIER_SET,
               target_phone=phone, metadata={"tier": tier_int})
    return resp(headers, 200, {"ok": True})


# ── POST /admin/members/attendance ────────────────────────────────────────────


def _record_plus_one_attendance(event_id: str, sponsor_phone_raw: str, attended: bool) -> dict:
    """Check in a confirmed member's +1 as a separate headcount.

    This uses a DynamoDB transaction so the check-in row and sponsor invite flag
    cannot drift apart. Plus-ones are not written as fake members; they are a
    separate check-in record keyed as PLUSONE#<sponsor phone>.
    """
    if not attended:
        return {"ok": False, "result": "PLUS_ONE_NO_SHOW_UNSUPPORTED", "reason": "+1 no-show is tracked through the sponsor invite only"}
    try:
        sponsor_phone = normalize_phone(sponsor_phone_raw)
    except ValueError as e:
        return {"ok": False, "result": "BAD_PHONE", "reason": str(e)}

    now = _now()
    inv_t = invites_table()
    checkins_t = boto3.resource("dynamodb").Table(os.getenv("CHECKINS_TABLE_NAME", "rsvp-checkins"))
    invite = inv_t.get_item(Key={"eventId": event_id, "phone": sponsor_phone}).get("Item") or {}
    if not invite:
        return {"ok": False, "result": "INVITE_NOT_FOUND", "reason": "Sponsor invite not found for this event"}
    if (invite.get("status") or "").upper() not in CONFIRMED_FAMILY_STATUSES:
        return {"ok": False, "result": "NOT_CONFIRMED", "reason": "Sponsor must be confirmed before the +1 can check in"}
    plus_one_name = (invite.get("plusOneName") or "").strip()
    if not plus_one_name:
        return {"ok": False, "result": "NO_PLUS_ONE", "reason": "No +1 is attached to this invite"}
    if invite.get("plusOneAttendedAt"):
        return {"ok": False, "result": "ALREADY_CHECKED_IN", "reason": "+1 already checked in"}

    plus_key = f"PLUSONE#{sponsor_phone}"
    try:
        boto3.client("dynamodb").transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": inv_t.name,
                        "Key": {"eventId": _ddb_av(event_id), "phone": _ddb_av(sponsor_phone)},
                        "UpdateExpression": "SET plusOneAttendedAt = :now",
                        "ConditionExpression": "attribute_exists(phone) AND #s IN (:confirmed, :attended, :noshow) AND plusOneName = :expectedName AND attribute_not_exists(plusOneAttendedAt)",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":now": _ddb_av(now),
                            ":confirmed": _ddb_av("CONFIRMED"),
                            ":attended": _ddb_av("ATTENDED"),
                            ":noshow": _ddb_av("NO_SHOW"),
                            ":expectedName": _ddb_av(invite["plusOneName"]),
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": checkins_t.name,
                        "Item": {
                            "eventId": _ddb_av(event_id),
                            "phone": _ddb_av(plus_key),
                            "sponsorPhone": _ddb_av(sponsor_phone),
                            "guestType": _ddb_av("PLUS_ONE"),
                            "guestName": _ddb_av(plus_one_name),
                            "checkedInAt": _ddb_av(now),
                            "ttl": _ddb_av(_checkin_ttl_epoch()),
                        },
                        "ConditionExpression": "attribute_not_exists(phone)",
                    }
                },
            ]
        )
        return {"ok": True, "result": "ATTENDANCE_OK", "reason": ""}
    except ClientError as ce:
        code = ce.response.get("Error", {}).get("Code")
        if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
            # Re-read for a precise admin-facing reason. No partial writes occurred.
            invite_after = inv_t.get_item(Key={"eventId": event_id, "phone": sponsor_phone}).get("Item") or {}
            existing_plus_checkin = checkins_t.get_item(Key={"eventId": event_id, "phone": plus_key}).get("Item") or {}
            if invite_after.get("plusOneAttendedAt") or existing_plus_checkin:
                return {"ok": False, "result": "ALREADY_CHECKED_IN", "reason": "+1 already checked in"}
            if (invite_after.get("status") or "").upper() not in CONFIRMED_FAMILY_STATUSES:
                return {"ok": False, "result": "NOT_CONFIRMED", "reason": "Sponsor must be confirmed before the +1 can check in"}
            if not (invite_after.get("plusOneName") or "").strip():
                return {"ok": False, "result": "NO_PLUS_ONE", "reason": "No +1 is attached to this invite"}
            return {"ok": False, "result": "ATTENDANCE_CONFLICT", "reason": "Could not check in +1 because the invite changed. Refresh and try again."}
        logger.exception("_record_plus_one_attendance failed sponsor=...%s", sponsor_phone[-4:])
        return {"ok": False, "result": "ATTENDANCE_DDB_ERROR", "reason": "Database error during +1 check-in"}

def record_member_attendance(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    phone = (data.get("phone") or "").strip()
    event_id = (data.get("eventId") or "current").strip()
    if event_id == "current":
        event_id = _active_event_slug() or "current"

    # Close Event is a real lock. Once attendance is finalized, neither member
    # nor +1 attendance may be mutated from the admin/check-in API.
    try:
        event_table = boto3.resource("dynamodb").Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))
        event_row = event_table.get_item(Key={"eventId": event_id}).get("Item") or {}
    except Exception:
        logger.exception("attendance finalization check failed event=%s", event_id)
        return resp(headers, 503, {"ok": False, "error": "event state unavailable"})
    finalized = event_row.get("attendanceFinalized")
    if finalized is True or str(finalized).strip().lower() in ("true", "1", "yes") or (event_row.get("attendanceFinalizationState") or "").upper() == "CLOSING" or (event_row.get("event_status") or "").upper() == "ARCHIVED":
        return resp(headers, 409, {"ok": False, "error": "attendance finalized or event closing", "result": "ATTENDANCE_FINALIZED"})

    attended = coerce_bool(data.get("attended", False))
    guest_type = (data.get("guestType") or data.get("type") or "member").strip().lower()
    if guest_type in ("plus_one", "plus-one", "plusone"):
        sponsor_phone = (data.get("sponsorPhone") or phone or "").strip()
        result = _record_plus_one_attendance(event_id, sponsor_phone, attended)
        ok = result.get("ok", False)
        code = result.get("result", "UNKNOWN")
        reason = result.get("reason", "")
        log_action(token=token, action=ACTION_ATTENDANCE,
                   target_phone=sponsor_phone,
                   metadata={"attended": attended, "eventId": event_id, "ok": ok, "result": code, "reason": reason, "guestType": "PLUS_ONE"})
        if not ok:
            status_code = 409 if code == "ALREADY_CHECKED_IN" else 400
            return resp(headers, status_code, {"ok": False, "result": code, "reason": reason})
        return resp(headers, 200, {"ok": True, "result": code, "guestType": "PLUS_ONE"})

    if not phone:
        return resp(headers, 400, {"ok": False, "error": "phone required"})

    # Door rule: the event invite row is the source of truth for check-in.
    # Do not fail door check-in just because a member-table record is stale,
    # missing, or phone-normalized differently. record_attendance validates the
    # event invite transactionally before writing a check-in row.
    result = record_attendance(phone, attended, event_id=event_id)

    # record_attendance now returns {"ok": bool, "result": str, "reason": str}
    ok     = result.get("ok", False)
    code   = result.get("result", "UNKNOWN")
    reason = result.get("reason", "")

    log_action(token=token, action=ACTION_ATTENDANCE,
               target_phone=phone,
               metadata={
                   "attended":        attended,
                   "eventId":         event_id,
                   "ok":              ok,
                   "result":          code,
                   "reason":          reason,
               })

    if not ok:
        # Return specific failure reason so admin UI can show useful messages
        status_code = 409 if code == "ALREADY_CHECKED_IN" else 400
        return resp(headers, status_code, {
            "ok":     False,
            "result": code,
            "reason": reason,
        })

    return resp(headers, 200, {"ok": True, "result": code})


# ── POST /admin/members/import ────────────────────────────────────────────────

def _batch_get_existing(members_t, phones: list) -> dict:
    """Fetch existing member records in bounded batches.

    Import safety depends on knowing whether each phone already exists. If DynamoDB
    still has unresolved keys after the retry budget, fail the import instead of
    silently treating those records as new members.
    """
    table_name = members_t.name
    existing = {}
    for i in range(0, len(phones), 25):
        chunk = phones[i:i + 25]
        unprocessed = {table_name: {"Keys": [{"phone": p} for p in chunk], "ConsistentRead": True}}
        retries = 0
        while unprocessed and retries < 4:
            batch_resp = members_t.meta.client.batch_get_item(RequestItems=unprocessed)
            for item in batch_resp.get("Responses", {}).get(table_name, []):
                existing[item["phone"]] = item
            unprocessed = batch_resp.get("UnprocessedKeys") or {}
            retries += 1
        if unprocessed:
            unresolved = len((unprocessed.get(table_name) or {}).get("Keys") or [])
            raise RuntimeError(f"member lookup incomplete after retries ({unresolved} unresolved key(s))")
    return existing


def import_members(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    members_to_import = data.get("members") or []
    if not isinstance(members_to_import, list) or not members_to_import:
        return resp(headers, 400, {"ok": False, "error": "members array required"})

    if len(members_to_import) > 100 or any(not isinstance(row, dict) for row in members_to_import):
        return resp(headers, 400, {"ok": False, "error": "Send at most 100 member objects per request; the admin CSV importer batches automatically."})

    import_status = (data.get("status") or "APPROVED").upper()
    if import_status not in ("APPROVED", "PENDING"):
        import_status = "APPROVED"

    imported = 0
    skipped = 0
    excluded_no_location = 0
    errors = []
    request_source = normalize_import_source(data.get("source") or "csv") or "csv"
    consent_confirmed = coerce_bool(data.get("consentConfirmed", False))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    members_t = boto3.resource("dynamodb").Table(
        os.getenv("MEMBERS_TABLE_NAME", "rsvp-members")
    )

    # Step 1: normalize and de-duplicate by the canonical E.164 phone. Two CSV
    # rows that normalize to the same member must never enter one DynamoDB batch.
    valid_rows = []
    seen_phones = set()
    for idx, row in enumerate(members_to_import):
        raw_phone = (row.get("phone") or "").strip()
        if not raw_phone:
            skipped += 1
            continue
        try:
            phone_e164 = normalize_phone(raw_phone)
        except Exception as e:
            errors.append(f"row {idx}: bad phone — {e}")
            skipped += 1
            continue
        if phone_e164 in seen_phones:
            errors.append(f"row {idx}: duplicate phone {phone_e164}")
            skipped += 1
            continue
        raw_zip = str(row.get("zipCode") or row.get("zip") or row.get("postalCode") or "").strip()
        zip_digits = re.sub(r"\D", "", raw_zip)
        zip_code = zip_digits[:5] if re.fullmatch(r"\d{5}(?:-?\d{4})?", raw_zip) else ""
        if not zip_code:
            skipped += 1
            excluded_no_location += 1
            continue
        seen_phones.add(phone_e164)
        row_with_zip = dict(row)
        row_with_zip["zipCode"] = zip_code
        valid_rows.append((idx, phone_e164, row_with_zip))

    # Resolve each distinct ZIP once per import, with bounded concurrency and a
    # ceiling that keeps an admin request inside its Lambda timeout.
    unique_zips = sorted({row[2]["zipCode"] for row in valid_rows})
    if len(unique_zips) > MAX_IMPORT_UNIQUE_ZIPS:
        return resp(headers, 400, {
            "ok": False,
            "error": f"Import has {len(unique_zips)} unique ZIP codes; split it into files with no more than {MAX_IMPORT_UNIQUE_ZIPS} unique ZIPs.",
            "imported": 0,
            "skipped": skipped,
            "excludedNoLocation": excluded_no_location,
        })

    resolved_locations = {}
    if unique_zips:
        with ThreadPoolExecutor(max_workers=min(ZIP_LOOKUP_WORKERS, len(unique_zips))) as executor:
            lookup_futures = {executor.submit(resolve_us_zip, zip_code): zip_code for zip_code in unique_zips}
            for future in as_completed(lookup_futures):
                zip_code = lookup_futures[future]
                try:
                    resolved_locations[zip_code] = future.result()
                except InvalidZipError:
                    continue
                except Exception:
                    logger.exception("member import aborted: ZIP lookup unavailable")
                    return resp(headers, 503, {
                        "ok": False,
                        "error": "location lookup unavailable; import was not written",
                        "imported": 0,
                        "skipped": skipped,
                        "excludedNoLocation": excluded_no_location,
                    })

    located_rows = []
    for idx, phone_e164, row in valid_rows:
        location = resolved_locations.get(row["zipCode"])
        if not location:
            skipped += 1
            excluded_no_location += 1
            continue
        row.update(location)
        row["latitude"] = Decimal(str(location["latitude"]))
        row["longitude"] = Decimal(str(location["longitude"]))
        located_rows.append((idx, phone_e164, row))
    valid_rows = located_rows

    # Step 2: existing-member detection is a safety gate, not best-effort. If
    # it cannot complete, abort before writing anything.
    all_phones = [phone for _, phone, _ in valid_rows]
    try:
        existing_map = _batch_get_existing(members_t, all_phones)
    except Exception as e:
        logger.error("member import aborted: existing-member lookup failed: %s", e)
        return resp(headers, 503, {
            "ok": False,
            "error": "member lookup unavailable; import was not written",
            "imported": 0,
            "skipped": skipped,
            "excludedNoLocation": excluded_no_location,
            "status": import_status,
            "errors": ["existing member lookup failed"],
        })

    # Step 3: new members may be written as complete items. Existing members
    # use field-level UpdateItem writes so a stale import snapshot cannot erase
    # concurrent STOP/opt-out state, counters, timestamps, or other server-owned
    # fields.
    new_items = []
    existing_updates = []
    for idx, phone_e164, row in valid_rows:
        try:
            existing = existing_map.get(phone_e164)
            if existing and str(existing.get("status") or "").upper() == "DELETED":
                errors.append(f"row {idx}: deleted member requires explicit re-enrollment")
                skipped += 1
                continue
            first_name = (row.get("firstName") or row.get("name") or "").strip() or "Unknown"
            last_name = (row.get("lastName") or "").strip() or None
            email = (row.get("email") or "").strip() or None
            instagram = (row.get("instagram") or "").strip() or None
            tags = (row.get("tags") or "").strip() or None

            if "smsOptIn" in row and row.get("smsOptIn") is not None:
                raw_opt_in = row.get("smsOptIn")
                if isinstance(raw_opt_in, str):
                    row_sms_opt_in = raw_opt_in.strip().lower() not in ("false", "0", "no", "n", "")
                else:
                    row_sms_opt_in = bool(raw_opt_in)
            else:
                row_sms_opt_in = None

            # Imported contacts are not silently opted in. A file-level admin
            # attestation may record consent for new rows; existing recorded
            # consent remains unchanged when this import does not attest.
            if consent_confirmed:
                requested_sms_opt_in = row_sms_opt_in if row_sms_opt_in is not None else True
            elif existing is not None and "smsOptIn" in existing:
                requested_sms_opt_in = coerce_bool(existing.get("smsOptIn"))
            else:
                requested_sms_opt_in = False

            # Import never overrides a recorded STOP, even when consent is attested.
            sms_opt_in = False if existing and coerce_bool(existing.get("optOut")) else requested_sms_opt_in

            existing_source = normalize_import_source((existing or {}).get("source") or "")
            row_source = normalize_import_source(row.get("source") or "")
            requested_source = row_source or request_source
            if existing_source and existing_source != "csv":
                source_val = existing_source
            else:
                source_val = requested_source or existing_source or "csv"

            if existing is None:
                item = {
                    "phone": phone_e164,
                    "name": first_name[:120],
                    "source": (source_val or "csv")[:40],
                    "lastSeenAt": now,
                    "submittedAt": now,
                    "createdAt": now,
                    "smsOptIn": sms_opt_in,
                    "status": import_status if import_status == "APPROVED" else "PENDING",
                    "zipCode": row["zipCode"],
                    "city": row["city"],
                    "state": row["state"],
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                    "locationSource": "zip",
                }
                if sms_opt_in:
                    item["smsOptInAt"] = now
                    item["smsOptInConfirmedAt"] = now
                    item["smsOptInConfirmationSource"] = "csv_import_attestation"
                if last_name:
                    item["lastName"] = last_name[:120]
                if email:
                    item["email"] = email[:200]
                if tags:
                    item["tags"] = tags[:200]
                if instagram:
                    item["instagram"] = instagram[:80]
                new_items.append((idx, phone_e164, item))
                continue

            fields = {
                "name": first_name[:120],
                "source": (source_val or "csv")[:40],
                "lastSeenAt": now,
                "submittedAt": now,
                "smsOptIn": sms_opt_in,
            }
            location_fields = ("zipCode", "city", "state", "latitude", "longitude", "locationSource")
            if not existing.get("zipCode") or existing.get("zipCode") == row["zipCode"]:
                for field in location_fields:
                    if existing.get(field) in (None, ""):
                        fields[field] = "zip" if field == "locationSource" else row[field]
            if import_status == "APPROVED" and not coerce_bool(existing.get("optOut")):
                fields["status"] = "APPROVED"
            if not consent_confirmed:
                # A profile import is not a new consent action and must not
                # overwrite consent state or provenance from the original signup.
                fields.pop("smsOptIn")
            if consent_confirmed and sms_opt_in:
                if not existing.get("smsOptInAt"):
                    fields["smsOptInAt"] = now
                fields["smsOptInConfirmedAt"] = now
                fields["smsOptInConfirmationSource"] = "csv_import_attestation"
            if last_name:
                fields["lastName"] = last_name[:120]
            if email:
                fields["email"] = email[:200]
            if tags:
                fields["tags"] = tags[:200]
            if instagram:
                fields["instagram"] = instagram[:80]
            existing_updates.append((idx, phone_e164, fields, existing))
        except Exception as row_err:
            logger.error("import row %d failed: %s", idx, row_err)
            errors.append(f"row {idx}: {type(row_err).__name__}")
            skipped += 1

    # Existing rows: narrow updates only. This preserves any fields changed
    # after the safety read, including optOut/optOutAt and lifetime counters.
    for idx, phone_e164, fields, existing in existing_updates:
        try:
            names = {}
            values = {}
            assignments = []
            for n, (field, value) in enumerate(fields.items()):
                nk = f"#f{n}"
                vk = f":v{n}"
                names[nk] = field
                values[vk] = value
                assignments.append(f"{nk} = {vk}")
            conditions = ["attribute_exists(phone)"]
            for n, field in enumerate(("status", "optOut", "smsOptIn", "zipCode", "city", "state", "latitude", "longitude", "locationSource")):
                nk, vk = f"#guard{n}", f":guard{n}"
                names[nk] = field
                if field in existing:
                    conditions.append(f"{nk} = {vk}")
                    values[vk] = existing[field]
                else:
                    conditions.append(f"attribute_not_exists({nk})")
            members_t.update_item(
                Key={"phone": phone_e164},
                UpdateExpression="SET " + ", ".join(assignments),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=" AND ".join(conditions),
            )
            imported += 1
        except Exception as row_err:
            logger.error("import update row %d failed: %s", idx, row_err)
            errors.append(f"phone {phone_e164}: {type(row_err).__name__}")
            skipped += 1

    # BatchWriteItem cannot enforce create-only semantics. Use conditional puts
    # so a concurrent signup/STOP cannot be replaced after the safety read.
    # Share a low-level client (not a resource) across bounded I/O workers.
    if new_items:
        client = boto3.client("dynamodb")
        with ThreadPoolExecutor(max_workers=min(10, len(new_items))) as executor:
            writes = {
                executor.submit(
                    client.put_item, TableName=members_t.name,
                    Item={key: _ddb_av(value) for key, value in item.items()},
                    ConditionExpression="attribute_not_exists(phone)",
                ): idx
                for idx, _, item in new_items
            }
            for future in as_completed(writes):
                try:
                    future.result()
                    imported += 1
                except Exception as exc:
                    skipped += 1
                    code = (getattr(exc, "response", {}).get("Error") or {}).get("Code", type(exc).__name__)
                    errors.append(f"row {writes[future]}: {code}; row not imported")

    log_action(token=token, action=ACTION_MEMBER_IMPORTED,
               metadata={"imported": imported, "skipped": skipped,
                         "status": import_status, "consentConfirmed": consent_confirmed,
                         "excludedNoLocation": excluded_no_location,
                         "source": request_source, "errorCount": len(errors)})
    return resp(headers, 200, {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "excludedNoLocation": excluded_no_location,
        "status": import_status,
        "errors": errors[:10],
    })


# ── GET /admin/members/search ─────────────────────────────────────────────────

def search_members_route(event: dict, headers: dict, token: str) -> dict:
    qs = get_query(event)
    query = (qs.get("q") or "").strip()
    if not query:
        return resp(headers, 400, {"ok": False, "error": "q parameter required"})
    page = search_members_page(query, limit=_query_limit(qs), next_token=_query_cursor(qs))
    payload = {
        "ok": True,
        "members": page.get("members", []),
        "pageSize": page.get("pageSize"),
        "count": len(page.get("members", [])),
        "hasMore": bool(page.get("hasMore")),
    }
    if page.get("nextPageToken"):
        payload["nextPageToken"] = page.get("nextPageToken")
    return resp(headers, 200, payload)
