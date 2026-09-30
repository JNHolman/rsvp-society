import base64
import hmac
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta




from decimal import Decimal
from typing import Any, Dict, List

import boto3
from boto3.dynamodb.conditions import Key as DKey
from botocore.exceptions import ClientError
import logging
from capacity_policy import expected_show_rate
from invite_wave_schedule import MIN_EVENT_LEAD_HOURS, _event_start_utc, schedule_next_wave

from audit_log import log_action, ACTION_INVITE_SENT
from member_store import normalize_phone, _table as members_table, list_members_by_status, INVITABLE_EVENT_STATES
from sms_adapter import get_secret_string, send_sms
from admin_shared import coerce_bool
from invite_logic import (
    calc_tier, _calc_invite_suggestion,
    _resolve_wave_capacity, _build_invite_list,
    _validate_initial_invite_text, _apply_audience_filters, _event_promotion_geography,
    _invite_message_metadata, _build_sms_message,
)

_DDB = boto3.resource("dynamodb")
logger = logging.getLogger()

FORMAL_WAVE_NUMBERS = (1, 2, 3)
MANUAL_WAVE_NUMBER = 0



def _invites_table():
    name = os.getenv("INVITES_TABLE_NAME")
    if not name:
        raise RuntimeError("INVITES_TABLE_NAME env var is not set")
    return _DDB.Table(name)


def _events_table():
    name = os.getenv("EVENTS_TABLE_NAME")
    if not name:
        raise RuntimeError("EVENTS_TABLE_NAME env var is not set")
    return _DDB.Table(name)


def _invite_jobs_table():
    name = os.getenv("INVITE_JOBS_TABLE_NAME")
    if not name:
        raise RuntimeError("INVITE_JOBS_TABLE_NAME env var is not set")
    return _DDB.Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")




def _get_method(event: dict) -> str:
    if event.get("httpMethod"):
        return event["httpMethod"]
    return event.get("requestContext", {}).get("http", {}).get("method", "")


def _get_headers(event: dict) -> dict:
    return event.get("headers") or {}


def _json_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _resp(status: int, body: dict, origin: str = None) -> dict:
    allowed = os.getenv("ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in allowed.split(",") if o.strip()]
    # Fail closed: never fall back to wildcard — that opens the endpoint to any domain.
    allow_origin = origin if origin in origins else (origins[0] if origins else "")
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": allow_origin,
            "access-control-allow-headers": "content-type,x-admin-token",
            "access-control-allow-methods": "GET,POST,OPTIONS",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=_json_default),
    }


def _admin_token() -> str:
    sid = os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token")
    s = get_secret_string(sid)
    try:
        j = json.loads(s)
        if isinstance(j, dict) and j.get("token"):
            return j["token"]
    except Exception:
        pass
    return s or ""


def _get_approved_members() -> List[Dict[str, Any]]:
    items = list_members_by_status("APPROVED")
    clean_items: List[Dict[str, Any]] = []
    for m in items:
        # Approved active members are considered opted-in for RSVP Society.
        # STOP/opt-out/deleted records should not be in the active invite pool.
        if coerce_bool(m.get("optOut", False)):
            continue
        if (m.get("status") or "").upper() == "DELETED":
            continue
        if m.get("smsOptIn") is not None and not coerce_bool(m.get("smsOptIn")):
            continue
        m["_tier"] = calc_tier(m)
        clean_items.append(m)
    return clean_items


# Import wave-blocking statuses from member_store — single source of truth
from member_store import WAVE_BLOCK_STATUSES as _REAL_INVITE_STATUSES
from member_store import ANALYTICS_EXCLUDE_STATUSES as _ANALYTICS_EXCLUDE_STATUSES
from member_store import CONFIRMED_FAMILY_STATUSES as _CONFIRMED_FAMILY_STATUSES
from member_store import RETRYABLE_INVITE_STATUSES as _RETRYABLE_STATUSES_IMPORTED
def _get_next_wave_number(event_id: str) -> int:
    """Return the next formal invite wave number.

    RSVP Society uses exactly three formal waves. Anything outside Wave 1,
    Wave 2, or Wave 3 is an explicit Manual/Resend path and must not appear as
    a fake Wave 4 in analytics or operator copy.
    """
    max_wave = 0
    if not event_id:
        return 1
    try:
        kwargs = {
            "KeyConditionExpression": DKey("eventId").eq(event_id),
            "ProjectionExpression": "waveNumber, #s",
            "ExpressionAttributeNames": {"#s": "status"},
        }
        while True:
            page = _invites_table().query(**kwargs)
            for item in page.get("Items", []):
                status = (item.get("status") or "").upper()
                if status in _REAL_INVITE_STATUSES or status in _CONFIRMED_FAMILY_STATUSES or status == "DECLINED":
                    try:
                        wave = int(item.get("waveNumber") or 0)
                    except Exception:
                        wave = 0
                    if wave in FORMAL_WAVE_NUMBERS:
                        max_wave = max(max_wave, wave)
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except Exception:
        logger.exception("_get_next_wave_number failed event=%s", event_id)
        raise
    return max_wave + 1 if max_wave > 0 else 1


def _assert_formal_wave_available(wave_number: int) -> None:
    if wave_number not in FORMAL_WAVE_NUMBERS:
        raise ValueError("AUTO_WAVES_COMPLETE: Wave 1, Wave 2, and Wave 3 already exist. Use Manual/Resend instead of creating Wave 4.")

def _get_existing_invited_phones(event_id: str) -> set:
    invites_t = _invites_table()
    phones: set = set()
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": DKey("eventId").eq(event_id),
        "ProjectionExpression": "phone, #s",
        "ExpressionAttributeNames": {"#s": "status"},
    }
    while True:
        resp = invites_t.query(**kwargs)
        for item in resp.get("Items", []):
            phone  = item.get("phone")
            status = (item.get("status") or "").upper()
            # Only block future waves for members who actually received the SMS
            if phone and status in _REAL_INVITE_STATUSES:
                phones.add(phone)
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return phones


def _get_existing_invite_map(event_id: str) -> Dict[str, Dict[str, Any]]:
    """Return current invite row metadata by phone for preview/status display."""
    if not event_id:
        return {}
    invites_t = _invites_table()
    rows: Dict[str, Dict[str, Any]] = {}
    kwargs: Dict[str, Any] = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
    while True:
        page = invites_t.query(**kwargs)
        for item in page.get("Items", []):
            phone = item.get("phone")
            if not phone:
                continue
            rows[phone] = item
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return rows



def _confirmed_update_recipients(event_id: str, phones: List[str] | None = None) -> List[Dict[str, Any]]:
    """Return confirmed event members who can receive a one-time event update.

    This is intentionally separate from wave invitation logic. It sends only to
    confirmed-family invite rows for this event and skips opted-out, denied, and
    deleted members. Plus-ones do not receive SMS unless they are also members
    with their own confirmed invite row.
    """
    if not event_id:
        return []
    invites_t = _invites_table()
    members_t = members_table()
    invite_rows: Dict[str, Dict[str, Any]] = {}
    if phones is None:
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": DKey("eventId").eq(event_id),
            "ConsistentRead": True,
        }
        while True:
            page = invites_t.query(**kwargs)
            for item in page.get("Items", []):
                if (item.get("status") or "").upper() not in {"CONFIRMED", "ATTENDED"}:
                    continue
                try:
                    phone = normalize_phone(item.get("phone") or "")
                except ValueError:
                    continue
                invite_rows[phone] = item
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    else:
        unique_phones = list(dict.fromkeys(phones))
        for start in range(0, len(unique_phones), 100):
            batch = unique_phones[start:start + 100]
            pending = {invites_t.name: {
                "Keys": [{"eventId": event_id, "phone": phone} for phone in batch],
                "ConsistentRead": True,
            }}
            for _attempt in range(4):
                if not pending:
                    break
                try:
                    response = invites_t.meta.client.batch_get_item(RequestItems=pending)
                    for item in response.get("Responses", {}).get(invites_t.name, []):
                        if (item.get("status") or "").upper() not in {"CONFIRMED", "ATTENDED"}:
                            continue
                        invite_rows[item["phone"]] = item
                    pending = response.get("UnprocessedKeys") or {}
                except Exception as exc:
                    logger.exception("confirmed update: invite batch read failed")
                    raise RuntimeError("confirmed update invite batch read failed") from exc
            if pending:
                raise RuntimeError("confirmed update invite batch read left unprocessed keys")

    members_by_phone: Dict[str, Dict[str, Any]] = {}
    phones_to_read = list(invite_rows)
    for start in range(0, len(phones_to_read), 100):
        batch = phones_to_read[start:start + 100]
        pending = {members_t.name: {
            "Keys": [{"phone": phone} for phone in batch],
            "ConsistentRead": True,
        }}
        for _attempt in range(4):
            if not pending:
                break
            try:
                response = members_t.meta.client.batch_get_item(RequestItems=pending)
                for member in response.get("Responses", {}).get(members_t.name, []):
                    members_by_phone[member["phone"]] = member
                pending = response.get("UnprocessedKeys") or {}
            except Exception as exc:
                logger.exception("confirmed update: member batch read failed")
                raise RuntimeError("confirmed update member batch read failed") from exc
        if pending:
            raise RuntimeError("confirmed update member batch read left unprocessed keys")

    recipients: List[Dict[str, Any]] = []
    for phone, item in invite_rows.items():
        member = members_by_phone.get(phone)
        # A missing/unreadable member row means current consent cannot be
        # verified. Fail closed instead of texting from the invite snapshot.
        if not member:
            logger.warning("confirmed update: member state unavailable phone=...%s", phone[-4:])
            continue
        if (member.get("status") or "").upper() != "APPROVED":
            continue
        if coerce_bool(member.get("optOut", False)):
            continue
        if member.get("smsOptIn") is not None and not coerce_bool(member.get("smsOptIn")):
            continue
        recipients.append({
            "phone": phone,
            "name": member.get("name") or item.get("name") or "",
            "lastName": member.get("lastName") or item.get("lastName") or "",
        })
    return recipients


def _build_confirmed_update_message(member: Dict[str, Any], message: str) -> str:
    name = (member.get("name") or "").split()[0] or ""
    return (message or "").replace("{name}", name).strip()


def _execute_confirmed_update(body: dict, current_ev: Dict[str, Any], token: str, job_id: str) -> None:
    event_id = (body.get("eventId") or "").strip()
    message = (body.get("messageOverride") or "").strip()
    if not message:
        raise ValueError("One-time update message required")
    # Persist only the recipient phone keys in the job record. Batch-get current
    # invite/member state per bounded chunk to keep job items small and consent fresh.
    all_phones = body.get("confirmedUpdatePhones") or body.get("phones") or [
        row.get("phone") for row in (body.get("confirmedRecipients") or []) if row.get("phone")
    ]
    if not all_phones:
        all_phones = [row["phone"] for row in _confirmed_update_recipients(event_id)]
    try:
        # Confirmed updates make one 15-second-bounded provider call per person.
        # Keep a margin inside the 300-second worker timeout.
        max_per_invocation = min(15, max(1, int(os.getenv("CONFIRMED_UPDATE_MAX_PER_INVOCATION", "15"))))
    except (TypeError, ValueError):
        max_per_invocation = 15
    current_phones = list(all_phones[:max_per_invocation])
    remainder_phones = list(all_phones[max_per_invocation:])
    recipients = _confirmed_update_recipients(event_id, current_phones)
    progress = body.get("_continuationProgress") or {}
    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"
    sent = 0
    failed = 0
    skipped_already_sent = 0
    failures: List[str] = []
    jobs_t = _invite_jobs_table()
    marker_ttl = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    for member in recipients:
        phone = member.get("phone") or ""
        if not phone:
            continue
        marker_id = f"{job_id}:recipient:{hashlib.sha256(phone.encode('utf-8')).hexdigest()}"
        marker_now = _now_iso()
        try:
            jobs_t.put_item(
                Item={
                    "jobId": marker_id,
                    "kind": "CONFIRMED_UPDATE_RECIPIENT",
                    "status": "CLAIMED",
                    "claimedAt": marker_now,
                    "ttl": marker_ttl,
                },
                ConditionExpression="attribute_not_exists(jobId)",
            )
        except ClientError as exc:
            if (exc.response.get("Error") or {}).get("Code") != "ConditionalCheckFailedException":
                raise
            existing_marker = jobs_t.get_item(Key={"jobId": marker_id}, ConsistentRead=True).get("Item") or {}
            marker_status = (existing_marker.get("status") or "").upper()
            if marker_status == "SENT":
                skipped_already_sent += 1
            else:
                # An existing CLAIMED/FAILED marker is an ambiguous provider
                # outcome. Keep the retry from sending a possibly paid duplicate.
                failed += 1
                failures.append(phone[-4:])
                logger.warning("confirmed update delivery unresolved job=%s", job_id)
            continue

        text = _build_confirmed_update_message(member, message)
        try:
            if sms_enabled:
                send_sms(phone, text)
                time.sleep(0.25)
            jobs_t.update_item(
                Key={"jobId": marker_id},
                UpdateExpression="SET #s = :sent, sentAt = :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":sent": "SENT", ":now": _now_iso()},
            )
            sent += 1
        except Exception:
            failed += 1
            failures.append(phone[-4:])
            logger.exception("confirmed update send failed event=%s phone=...%s", event_id, phone[-4:])
            try:
                jobs_t.update_item(
                    Key={"jobId": marker_id},
                    UpdateExpression="SET #s = :failed, failedAt = :now",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":failed": "FAILED", ":now": _now_iso()},
                )
            except Exception:
                logger.exception("confirmed update marker write failed job=%s", job_id)
    cumulative = {
        "sent": int(progress.get("sent") or 0) + sent,
        "failed": int(progress.get("failed") or 0) + failed,
        "skippedAlreadySent": int(progress.get("skippedAlreadySent") or 0) + skipped_already_sent,
        "recipientCount": int(progress.get("recipientCount") or 0) + len(recipients),
        "failures": list(progress.get("failures") or []) + failures,
    }
    if remainder_phones:
        continuation_body = dict(body)
        continuation_body.pop("confirmedUpdatePhones", None)
        continuation_body["phones"] = remainder_phones
        continuation_body["_continuationProgress"] = cumulative
        _update_job(job_id, {
            "status": "PROCESSING",
            "smsSent": cumulative["sent"],
            "failed": cumulative["failed"],
            "eventId": event_id,
            "waveNumber": MANUAL_WAVE_NUMBER,
            "breakdown": json.dumps({"mode": "confirmed_update", **cumulative}, default=_json_default),
            "message": f"Continuing confirmed guest update: {len(remainder_phones)} recipients remaining",
        })
        try:
            boto3.client("lambda").invoke(
                FunctionName=os.getenv("AWS_LAMBDA_FUNCTION_NAME", "rsvp-invite-handler"),
                InvocationType="Event",
                Payload=json.dumps({
                    "asyncBlast": True,
                    "continuation": True,
                    "jobId": job_id,
                    "token": token,
                    "blastBody": continuation_body,
                }).encode(),
            )
        except Exception as exc:
            logger.exception("confirmed update continuation invoke failed job_id=%s", job_id)
            _update_job(job_id, {"status": "FAILED", "completedAt": _now_iso(), "error": f"continuation invoke failed: {str(exc)[:300]}"})
            raise
        return

    log_action(
        token=token,
        action=ACTION_INVITE_SENT,
        metadata={
            "eventId": event_id,
            "mode": "confirmed_update",
            "recipientCount": cumulative["recipientCount"],
            "smsSent": cumulative["sent"],
            "failed": cumulative["failed"],
        },
    )
    _update_job(job_id, {
        "status": "COMPLETE",
        "completedAt": _now_iso(),
        "eventId": event_id,
        "waveNumber": MANUAL_WAVE_NUMBER,
        "recipientCount": cumulative["recipientCount"],
        "invitesWritten": 0,
        "smsSent": cumulative["sent"],
        "failed": cumulative["failed"],
        "breakdown": json.dumps({"mode": "confirmed_update", "queued": int(body.get("confirmedUpdateRecipientCount") or len(all_phones)), "sent": cumulative["sent"], "failed": cumulative["failed"], "skippedAlreadySent": cumulative.get("skippedAlreadySent", 0), "failures": cumulative["failures"][:20]}, default=_json_default),
    })







def _get_current_event() -> Dict[str, Any]:
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
        logger.exception("_get_current_event failed")
        raise

def _resolve_active_invitable_event(expected_event_id: str) -> Dict[str, Any]:
    """Return active event only when the submitted eventId matches it exactly.

    This prevents stale admin tabs from sending current-event SMS content while
    writing invite analytics under an older eventId.
    """
    event_id = (expected_event_id or "").strip()
    if not event_id or event_id == "current":
        raise ValueError("Stable eventId/eventSlug required; 'current' is not valid for invite sends")

    current_ev = _get_current_event()
    active_slug = (current_ev.get("activeEventSlug") or current_ev.get("eventSlug") or current_ev.get("slug") or current_ev.get("eventId") or "").strip()
    if not active_slug:
        raise ValueError("No active event is set")
    if event_id != active_slug:
        raise ValueError(f"EVENT_MISMATCH: submitted eventId '{event_id}' does not match active event '{active_slug}'")

    ev_status = (current_ev.get("event_status") or "DRAFT").upper()
    if ev_status not in INVITABLE_EVENT_STATES:
        raise ValueError(
            f"Cannot send invites — event is in state '{ev_status}'. "
            f"Event must be Inviting or Live to send invites."
        )
    return {**current_ev, "eventSlug": active_slug, "eventId": active_slug}


# ── Async blast job helpers ───────────────────────────────────────────────────

from invite_job_store import (
    write_job as _job_write, update_job as _job_update,
    preview_member_phones as _job_preview_member_phones,
    write_preview_lock as _job_write_preview_lock,
    read_preview_lock as _job_read_preview_lock,
    claim_preview_lock as _job_claim_preview_lock,
    resolve_locked_send_body as _job_resolve_locked_send_body,
    handle_job_status as _job_handle_status,
)

def _job_deps():
    return {
        "invite_jobs_table": _invite_jobs_table,
        "invites_table": _invites_table,
        "now_iso": _now_iso,
        "json_default": _json_default,
        "normalize_phone": normalize_phone,
        "logger": logger,
        "resp": _resp,
        "read_preview_lock": globals().get("_read_preview_lock"),
        "claim_preview_lock": globals().get("_claim_preview_lock"),
    }

def _write_job(job_id: str, body: dict, origin: str) -> None:
    return _job_write(job_id, body, origin, deps=_job_deps())

def _update_job(job_id: str, updates: dict) -> None:
    return _job_update(job_id, updates, deps=_job_deps())

def _preview_member_phones(preview_members: List[Dict[str, Any]]) -> List[str]:
    return _job_preview_member_phones(preview_members, deps=_job_deps())

def _write_preview_lock(*, event_id: str, wave_number: int, wave_size: int, locked_phones: List[str], summary: Dict[str, Any]) -> str:
    return _job_write_preview_lock(event_id=event_id, wave_number=wave_number, wave_size=wave_size, locked_phones=locked_phones, summary=summary, deps=_job_deps())

def _read_preview_lock(lock_id: str) -> Dict[str, Any]:
    return _job_read_preview_lock(lock_id, deps=_job_deps())

def _claim_preview_lock(lock_id: str, job_id: str) -> Dict[str, Any]:
    return _job_claim_preview_lock(lock_id, job_id, deps=_job_deps())


def _resolve_locked_send_body(body: dict, job_id: str) -> dict:
    return _job_resolve_locked_send_body(body, job_id, deps=_job_deps())



def handle_job_status(qs: dict, origin: str) -> dict:
    return _job_handle_status(qs, origin, deps=_job_deps())


def _event_for_guard(event_id: str) -> Dict[str, Any]:
    """Fetch the canonical event for invite-text gating."""
    try:
        if event_id:
            item = _events_table().get_item(Key={"eventId": event_id}).get("Item") or {}
            if item:
                return item
    except Exception:
        logger.exception("_event_for_guard failed event=%s", event_id)
    return _get_current_event() or {}













def _get_analytics(event_id: str) -> dict:
    """
    Fetch live invite analytics for an event. Used by wave 2+ to calculate gap.
    Excludes DELETED records so tombstoned members don't skew confirm rate.
    """
    try:
        invites_t = _invites_table()
        items = []
        kwargs: dict = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
        while True:
            resp = invites_t.query(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last

        # Exclude non-real invite statuses from all analytics (uses central constant)
        items = [i for i in items if (i.get("status") or "").upper() not in _ANALYTICS_EXCLUDE_STATUSES]

        invited = len(items)
        # ATTENDED and NO_SHOW were confirmed — use centralized constant
        confirmed = sum(1 for i in items if (i.get("status") or "").upper() in _CONFIRMED_FAMILY_STATUSES)
        declined  = sum(1 for i in items if (i.get("status") or "").upper() == "DECLINED")
        attended  = sum(1 for i in items if i.get("attendedAt"))
        plus_one_risk = sum(1 for i in items if (i.get("status") or "").upper() in _CONFIRMED_FAMILY_STATUSES and (i.get("plusOneName") or "").strip())
        plus_one_attended = sum(1 for i in items if i.get("plusOneAttendedAt"))
        by_wave = {1: 0, 2: 0, 3: 0, "manual": 0}
        for i in items:
            try:
                wave = int(i.get("waveNumber") or 0)
            except Exception:
                wave = 0
            key = wave if wave in FORMAL_WAVE_NUMBERS else "manual"
            by_wave[key] = by_wave.get(key, 0) + 1
        confirmed_headcount = confirmed + plus_one_risk
        # Wave planning is seat-based: a confirmed member may consume two seats.
        # Measure confirmed people per invite so later waves account for +1s.
        confirm_rate = (confirmed_headcount / invited) if invited > 0 else None
        show_rate    = ((attended + plus_one_attended) / max(confirmed_headcount, 1)) if confirmed_headcount > 0 else None
        return {
            "invited": invited, "confirmed": confirmed,
            "confirmedHeadcount": confirmed_headcount,
            "declined": declined, "attended": attended,
            "plusOneRisk": plus_one_risk,
            "plusOneAttended": plus_one_attended,
            "byWave": by_wave,
            "confirmRate": confirm_rate, "showRate": show_rate,
        }
    except Exception:
        logger.exception("_get_analytics failed event=%s", event_id)
        raise


def handle_preview(body: dict, origin: str) -> dict:
    event_id  = (body.get("eventId") or "").strip()
    capacity  = int(body.get("capacity") or 0)
    female_pct = int(body.get("femalePercent") or 60)
    auto_wave    = coerce_bool(body.get("autoWave", True))
    wave_number  = int(body.get("waveNumber") or 0)
    if auto_wave or wave_number < 1:
        wave_number = _get_next_wave_number(event_id)
    try:
        _assert_formal_wave_available(wave_number)
    except ValueError as ve:
        return _resp(409, {"ok": False, "error": str(ve), "waveLimitReached": True, "manualResendAvailable": True}, origin)
    wave_size    = int(body.get("waveSize") or 0)
    removed_phones = body.get("removedPhones") or []
    include_existing = coerce_bool(body.get("includeExisting", False))

    if not event_id or capacity < 1:
        return _resp(400, {"ok": False, "error": "eventId and capacity required"}, origin)

    if not (0 <= female_pct <= 100):
        return _resp(400, {"ok": False, "error": "femalePercent must be 0–100"}, origin)

    message_override = (body.get("messageOverride") or "").strip()
    guard_event = _event_for_guard(event_id)
    if not _event_promotion_geography(guard_event):
        return _resp(409, {
            "ok": False,
            "error": "Event ZIP code and promotion radius are required before building an invite audience",
        }, origin)
    try:
        _validate_initial_invite_text(message_override, guard_event)
    except ValueError as ve:
        return _resp(400, {"ok": False, "error": str(ve)}, origin)

    existing_invites = _get_existing_invited_phones(event_id)
    all_existing_invite_map = _get_existing_invite_map(event_id)
    existing_invite_map = all_existing_invite_map if include_existing else {}

    analytics = _get_analytics(event_id)
    gap_info = None
    if wave_number >= 2:
        confirmed = analytics.get("confirmedHeadcount", analytics.get("confirmed", 0) + analytics.get("plusOneRisk", 0))
        actual_confirm_rate = analytics.get("confirmRate")
        actual_show_rate = expected_show_rate(guard_event)
        responses = analytics.get("confirmed", 0) + analytics.get("declined", 0)  # WAVE-B: sample size
        effective_capacity = _resolve_wave_capacity(
            capacity, wave_number, wave_size,
            confirmed=confirmed,
            already_invited=len(existing_invites),
            actual_confirm_rate=actual_confirm_rate,
            actual_show_rate=actual_show_rate,
            responses=responses,
        )
        gap_info = _calc_invite_suggestion(
            capacity, confirmed, len(existing_invites),
            actual_confirm_rate=actual_confirm_rate,
            show_rate=actual_show_rate,
            responses=responses,
        )
    else:
        effective_capacity = _resolve_wave_capacity(capacity, wave_number, wave_size)

    pool_members = _get_approved_members()
    filtered_pool_members = _apply_audience_filters(pool_members, body.get("audienceFilters") or {}, guard_event)
    members = filtered_pool_members if include_existing else [m for m in filtered_pool_members if m.get("phone") not in existing_invites]
    result = _build_invite_list(members, effective_capacity, female_pct, removed_phones, wave_number)

    preview_members = []
    for m in result["members"]:
        phone = m.get("phone", "")
        invite_row = existing_invite_map.get(phone, {})
        current_status = (invite_row.get("status") or "NOT_INVITED").upper()
        preview_members.append({
            "phone":        phone,
            "name":         m.get("name", ""),
            "lastName":     m.get("lastName", ""),
            "displayName":  " ".join([x for x in [m.get("name", ""), m.get("lastName", "")] if x]).strip(),
            "gender":       m.get("gender", "?"),
            "tier":         m["_tier"],
            "market":       m.get("market") or m.get("city") or m.get("state") or "",
            "attendedCount": int(m.get("attendedCount", 0)),
            "invitedCount":  int(m.get("invitedCount", 0)),
            "noShowCount":   int(m.get("noShowCount", 0)),
            "currentEventInviteStatus": current_status,
            "smsSendStatus": invite_row.get("smsSendStatus") or ("LOCKED" if current_status != "NOT_INVITED" else "ELIGIBLE"),
            "lastInviteStatus": current_status if current_status != "NOT_INVITED" else "",
            "confirmedAt": invite_row.get("confirmedAt", ""),
            "declinedAt": invite_row.get("declinedAt", ""),
            "attendedAt": invite_row.get("attendedAt", ""),
            "noShowAt": invite_row.get("noShowAt", ""),
            "smsOptIn": coerce_bool(m.get("smsOptIn", True)),
        })

    result["summary"]["coldStartTier2Fallback"] = bool((result["summary"].get("breakdown") or {}).get("coldStartTier2Fallback"))
    result["summary"]["waveNumber"]             = wave_number
    result["summary"]["autoWave"]               = auto_wave
    result["summary"]["waveSize"]               = effective_capacity
    result["summary"]["alreadyInvitedExcluded"] = 0 if include_existing else len(existing_invites)
    result["summary"]["alreadyInvitedVisible"]  = len(existing_invite_map) if include_existing else 0
    pool_remaining = len([m for m in filtered_pool_members if m.get("phone") not in existing_invites])
    by_wave = analytics.get("byWave") or {}
    wave1_sent = int(by_wave.get(1, 0) or by_wave.get("1", 0) or 0)
    plus_one_risk = int(analytics.get("plusOneRisk") or 0)
    confirmed_count = int(analytics.get("confirmed") or 0)
    safe_capacity_remaining = max(0, capacity - confirmed_count - plus_one_risk)
    result["summary"]["poolRemaining"]          = pool_remaining
    result["summary"]["includeExisting"]        = include_existing
    result["summary"]["confirmedCount"]         = confirmed_count
    result["summary"]["attendedCount"]          = int(analytics.get("attended") or 0) + int(analytics.get("plusOneAttended") or 0)
    result["summary"]["plusOneRisk"]            = plus_one_risk
    result["summary"]["safeCapacityRemaining"]  = safe_capacity_remaining
    result["summary"]["commandCenter"] = {
        "capacity": capacity,
        "nextWaveNumber": wave_number,
        "wave1Sent": wave1_sent,
        "alreadyInvited": len(existing_invites),
        "notYetInvited": pool_remaining,
        "remainingEligible": pool_remaining,
        "confirmed": confirmed_count,
        "attended": int(analytics.get("attended") or 0) + int(analytics.get("plusOneAttended") or 0),
        "plusOneRisk": plus_one_risk,
        "safeCapacityRemaining": safe_capacity_remaining,
        "recommendedNextWaveSize": effective_capacity,
        "eligibleInPreview": len(preview_members),
        "includeExisting": include_existing,
    }
    if gap_info:
        result["summary"]["gapAnalysis"] = gap_info
        result["summary"]["commandCenter"]["gapAnalysis"] = gap_info

    locked_phones = _preview_member_phones(preview_members)
    try:
        preview_session_id = _write_preview_lock(
            event_id=event_id,
            wave_number=wave_number,
            wave_size=effective_capacity,
            locked_phones=locked_phones,
            summary=result["summary"],
        )
    except Exception:
        logger.exception("handle_preview: failed to write preview lock event=%s", event_id)
        return _resp(500, {"ok": False, "error": "failed_to_lock_preview"}, origin)

    result["summary"]["previewSessionId"] = preview_session_id
    result["summary"]["lockedPhoneCount"] = len(locked_phones)
    result["summary"]["commandCenter"]["lockedPhoneCount"] = len(locked_phones)

    return _resp(200, {
        "ok":      True,
        "eventId": event_id,
        "previewSessionId": preview_session_id,
        "summary": result["summary"],
        "members": preview_members,
    }, origin)


def handle_send(body: dict, origin: str, token: str, job_id_override: str | None = None) -> dict:
    """
    Dispatch invite blast asynchronously.
    1. Write a QUEUED job record.
    2. Invoke this Lambda with InvocationType=Event (fire and forget).
    3. Return 202 immediately with jobId.
    Admin polls GET /admin/invite/status?jobId=xxx for results.
    """
    if not body.get("confirmSend"):
        return _resp(400, {"ok": False, "error": "confirmSend: true required"}, origin)

    event_id = (body.get("eventId") or "").strip()
    if job_id_override:
        existing_job = _invite_jobs_table().get_item(Key={"jobId": job_id_override}, ConsistentRead=True).get("Item") or {}
        if existing_job:
            status = (existing_job.get("status") or "").upper()
            if status in {"QUEUED", "PROCESSING", "COMPLETE"}:
                return _resp(202, {"ok": True, "jobId": job_id_override, "status": status,
                                   "recipientCount": int(existing_job.get("recipientCount") or 0), "duplicate": True}, origin)
            return _resp(409, {"ok": False, "jobId": job_id_override, "status": status,
                               "error": "automatic wave job already exists and needs review"}, origin)
    body = dict(body)
    # These fields are set only by the Lambda continuation path, never trusted
    # from an HTTP request body.
    body.pop("_serverContinuation", None)
    body.pop("_continuationProgress", None)
    confirmed_update = coerce_bool(body.get("confirmedUpdate", False))
    try:
        current_for_guard = _resolve_active_invitable_event(event_id)
        if confirmed_update:
            message = (body.get("messageOverride") or "").strip()
            if not message:
                raise ValueError("One-time update message required")
            recipients = _confirmed_update_recipients(event_id)
            if not recipients:
                raise ValueError("No confirmed members are available for this event update")
            body["phones"] = [r["phone"] for r in recipients]
            body["confirmedUpdatePhones"] = body["phones"]
            body["confirmedUpdateRecipientCount"] = len(body["phones"])
            body["manualSend"] = True
            body["autoWave"] = False
            body["lockedWave"] = True
            body["waveNumber"] = MANUAL_WAVE_NUMBER
        else:
            _validate_initial_invite_text((body.get("messageOverride") or "").strip(), current_for_guard)
    except ValueError as ve:
        status = 409 if "EVENT_MISMATCH" in str(ve) else 400
        return _resp(status, {"ok": False, "error": str(ve)}, origin)

    # Preview sends must use a persisted preview lock. This prevents wave/audience
    # drift between the admin preview and the async send job. Manual single-invite
    # sends without previewSessionId are still locked at queue time as an explicit
    # override path.
    job_id = job_id_override or str(uuid.uuid4())
    try:
        if coerce_bool(body.get("confirmedUpdate", False)):
            pass
        elif body.get("previewSessionId") or body.get("previewLockId"):
            body = _resolve_locked_send_body(body, job_id)
        else:
            try:
                submitted_wave = int(body.get("waveNumber") or 0)
            except (TypeError, ValueError):
                submitted_wave = 0
            if coerce_bool(body.get("autoWave", True)):
                next_wave = _get_next_wave_number(event_id)
                _assert_formal_wave_available(next_wave)
                body["waveNumber"] = next_wave
                body["autoWave"] = False
                body["lockedWave"] = True
            elif submitted_wave < 1:
                # Explicit manual/resend path. This is not Wave 4. Analytics groups
                # waveNumber 0 under Manual / Resend.
                body["waveNumber"] = MANUAL_WAVE_NUMBER
                body["autoWave"] = False
                body["lockedWave"] = True
                body["manualSend"] = True
            else:
                _assert_formal_wave_available(submitted_wave)
    except ValueError as ve:
        return _resp(409, {"ok": False, "error": str(ve)}, origin)

    try:
        _write_job(job_id, body, origin)
    except ClientError as exc:
        if job_id_override and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            existing_job = _invite_jobs_table().get_item(Key={"jobId": job_id}, ConsistentRead=True).get("Item") or {}
            status = (existing_job.get("status") or "QUEUED").upper()
            return _resp(202, {"ok": True, "jobId": job_id, "status": status,
                               "recipientCount": int(existing_job.get("recipientCount") or 0), "duplicate": True}, origin)
        logger.exception("handle_send: failed to write job record")
        return _resp(500, {"ok": False, "error": "failed to queue blast"}, origin)
    except Exception:
        logger.exception("handle_send: failed to write job record")
        return _resp(500, {"ok": False, "error": "failed to queue blast"}, origin)

    # Invoke this Lambda asynchronously — returns immediately
    try:
        import boto3 as _b3
        fn_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME", "rsvp-invite-handler")
        _b3.client("lambda").invoke(
            FunctionName=fn_name,
            InvocationType="Event",  # async — no response body
            Payload=json.dumps({
                "asyncBlast": True,
                "jobId": job_id,
                "token": token,
            }).encode(),
        )
    except Exception:
        logger.exception("handle_send: failed to invoke async Lambda")
        _update_job(job_id, {"status": "FAILED", "error": "Lambda invoke failed"})
        return _resp(500, {"ok": False, "error": "failed to dispatch blast"}, origin)

    return _resp(202, {"ok": True, "jobId": job_id, "status": "QUEUED", "recipientCount": len(body.get("phones") or [])}, origin)


def _auto_wave_job_id(event_id: str, wave_number: int) -> str:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:20]
    return f"AUTO-WAVE-{digest}-W{wave_number}"


def _get_current_event_strict() -> dict:
    """Read the active pointer strictly so storage errors trigger scheduler retries."""
    table = _events_table()
    pointer = table.get_item(Key={"eventId": "current"}, ConsistentRead=True).get("Item") or {}
    slug = (pointer.get("activeEventSlug") or pointer.get("eventSlug") or pointer.get("slug") or "").strip()
    if not slug:
        return {}
    event = table.get_item(Key={"eventId": slug}, ConsistentRead=True).get("Item") or {}
    return {**event, "activeEventSlug": slug} if event else {}


def _handle_auto_wave_schedule(event: dict) -> dict:
    event_id = str(event.get("eventId") or "").strip()
    try:
        previous_wave = int(event.get("previousWaveNumber") or 0)
    except (TypeError, ValueError):
        raise ValueError("invalid automatic wave schedule")
    if not event_id or previous_wave not in (1, 2):
        raise ValueError("invalid automatic wave schedule")

    active_event = _get_current_event_strict()
    active_slug = str(active_event.get("activeEventSlug") or "").strip()
    if active_slug != event_id or (active_event.get("event_status") or "").upper() not in {"LIVE", "INVITING"}:
        logger.info("auto_wave_stale_schedule event=%s", event_id)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "reason": "event is no longer active"})}
    if _get_next_wave_number(event_id) != previous_wave + 1:
        logger.info("auto_wave_stale_schedule event=%s previous_wave=%d", event_id, previous_wave)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "reason": "wave already advanced"})}

    now = datetime.now(timezone.utc)
    if _event_start_utc(active_event) - now < timedelta(hours=MIN_EVENT_LEAD_HOURS):
        logger.info("auto_wave_skipped event=%s reason=event_too_close", event_id)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "reason": "event starts within 24 hours"})}

    try:
        female_percent = int(event.get("femalePercent", 60))
    except (TypeError, ValueError):
        raise ValueError("invalid scheduled gender percentage")
    if not 0 <= female_percent <= 100:
        raise ValueError("invalid scheduled gender percentage")

    preview_body = {
        "eventId": event_id,
        "capacity": int(active_event.get("capacity") or 0),
        "femalePercent": female_percent,
        "audienceFilters": event.get("audienceFilters") if isinstance(event.get("audienceFilters"), dict) else {},
        "waveNumber": previous_wave + 1,
        "autoWave": False,
    }
    preview_response = handle_preview(preview_body, "")
    preview_status = int(preview_response.get("statusCode") or 500)
    preview_data = json.loads(preview_response.get("body") or "{}")
    if preview_status != 200 or not preview_data.get("ok"):
        raise RuntimeError("automatic wave preview failed: " + str(preview_data.get("error") or preview_status))

    summary = preview_data.get("summary") or {}
    members = preview_data.get("members") or []
    if int(summary.get("waveSize") or 0) <= 0 or not members:
        logger.info("auto_wave_not_needed event=%s wave=%d", event_id, previous_wave + 1)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "reason": "no additional invites needed"})}

    send_body = {
        **preview_body,
        "phones": [str(member.get("phone") or "") for member in members if member.get("phone")],
        "previewSessionId": preview_data.get("previewSessionId"),
        "confirmSend": True,
        "automaticWave": True,
        "autoWave": False,
    }
    response = handle_send(send_body, "", _admin_token(), job_id_override=_auto_wave_job_id(event_id, previous_wave + 1))
    status = int(response.get("statusCode") or 500)
    if status == 202:
        return {"statusCode": 200, "body": json.dumps({"ok": True, "waveNumber": previous_wave + 1,
                                                         "jobId": json.loads(response["body"]).get("jobId")})}
    response_data = json.loads(response.get("body") or "{}")
    logger.error("auto_wave_failed event=%s wave=%d reason=%s", event_id, previous_wave + 1,
                 response_data.get("error") or status)
    # handle_send's failed job record is the audit/recovery point. Do not retry an
    # ambiguous dispatch and risk texting a second audience.
    return {"statusCode": 200, "body": json.dumps({"ok": False, "error": response_data.get("error") or status})}


def _run_blast(body: dict, origin: str, token: str, job_id: str) -> None:
    """
    The actual blast logic — runs asynchronously inside a self-invoked Lambda.
    Updates the job record throughout. Never returns an HTTP response.
    """
    _update_job(job_id, {"status": "PROCESSING", "startedAt": _now_iso()})
    try:
        _execute_send(body, origin, token, job_id)
    except Exception as e:
        logger.exception("_run_blast failed job_id=%s", job_id)
        _update_job(job_id, {"status": "FAILED", "completedAt": _now_iso(), "error": str(e)[:500]})


def _execute_send(body: dict, origin: str, token: str, job_id: str):
    from invite_sender import execute_send
    return execute_send(body, origin, token, job_id, deps={
        "ACTION_INVITE_SENT": ACTION_INVITE_SENT,
        "MANUAL_WAVE_NUMBER": MANUAL_WAVE_NUMBER,
        "_RETRYABLE_STATUSES_IMPORTED": _RETRYABLE_STATUSES_IMPORTED,
        "_apply_audience_filters": _apply_audience_filters,
        "_event_promotion_geography": _event_promotion_geography,
        "_assert_formal_wave_available": _assert_formal_wave_available,
        "_build_invite_list": _build_invite_list,
        "_build_sms_message": _build_sms_message,
        "_execute_confirmed_update": _execute_confirmed_update,
        "_get_analytics": _get_analytics,
        "_get_approved_members": _get_approved_members,
        "_get_existing_invited_phones": _get_existing_invited_phones,
        "_get_next_wave_number": _get_next_wave_number,
        "_invite_message_metadata": _invite_message_metadata,
        "_invites_table": _invites_table,
        "_now_iso": _now_iso,
        "_resolve_active_invitable_event": _resolve_active_invitable_event,
        "_resolve_wave_capacity": _resolve_wave_capacity,
        "_update_job": _update_job,
        "_validate_initial_invite_text": _validate_initial_invite_text,
        "coerce_bool": coerce_bool,
        "calc_tier": calc_tier,
        "log_action": log_action,
        "members_table": members_table,
        "normalize_phone": normalize_phone,
        "send_sms": send_sms,
        "_schedule_auto_wave": schedule_next_wave,
        "logger": logger,
    })



def handler(event, context):
    # Structured observability — request ID in every log
    request_id = (context.aws_request_id if context and hasattr(context, "aws_request_id") else "local")
    logger.info("handler_start request_id=%s", request_id)
    try:
        if event.get("source") == "rsvp.auto-wave":
            return _handle_auto_wave_schedule(event)
        # ── Async blast: self-invoked by Lambda — no HTTP headers, auth via payload token
        if event.get("asyncBlast"):
            job_id      = event.get("jobId", "")
            async_token = (event.get("token") or "").strip()
            expected    = _admin_token()
            if not async_token or not hmac.compare_digest(async_token, expected):
                logger.error("async blast: invalid token for job_id=%s", job_id)
                _update_job(job_id, {"status": "FAILED", "error": "unauthorized async token"})
                return {"statusCode": 200, "body": json.dumps({"ok": False, "error": "unauthorized"})}
            blast_body = event.get("blastBody") if isinstance(event.get("blastBody"), dict) else None
            if blast_body is None:
                try:
                    job_item = _invite_jobs_table().get_item(Key={"jobId": job_id}).get("Item") or {}
                    blast_body_raw = job_item.get("requestBody", "{}")
                    blast_body = json.loads(blast_body_raw) if blast_body_raw else {}
                except Exception:
                    logger.exception("async blast: failed to load job body job_id=%s", job_id)
                    _update_job(job_id, {"status": "FAILED", "error": "Could not load send request"})
                    return {"statusCode": 200, "body": json.dumps({"ok": False})}
            if blast_body:
                blast_body["confirmSend"] = True
                if event.get("continuation"):
                    blast_body["_serverContinuation"] = True
                _run_blast(blast_body, None, async_token, job_id)
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        method  = _get_method(event).upper()
        headers = _get_headers(event)
        origin  = headers.get("origin") or headers.get("Origin")

        if method == "OPTIONS":
            return _resp(200, {"ok": True}, origin)

        token = (headers.get("x-admin-token") or headers.get("X-Admin-Token") or "").strip()
        expected = _admin_token()
        if not token or not hmac.compare_digest(token, expected):
            return _resp(401, {"ok": False, "error": "unauthorized"}, origin)

        path = event.get("path", "")
        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        body = json.loads(raw_body) if raw_body else {}

        if method == "POST" and path.endswith("/admin/invite/preview"):
            return handle_preview(body, origin)

        if method == "POST" and path.endswith("/admin/invite/send"):
            return handle_send(body, origin, token)

        if method == "GET" and path.endswith("/admin/invite/status"):
            qs = event.get("queryStringParameters") or {}
            return handle_job_status(qs, origin)

        return _resp(404, {"ok": False, "error": "not found"}, origin)

    except Exception:
        logger.exception("invite handler failed")
        if event.get("source") == "rsvp.auto-wave":
            raise
        return _resp(500, {"ok": False, "error": "internal error"}, None)
