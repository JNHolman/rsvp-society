import hmac
import hashlib
import json
import logging
import os
import time
import uuid
from event_policy import event_start, venue_mode, venue_available

import boto3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key as DKey
from sms_adapter import send_sms, get_secret_string
from admin_shared import coerce_bool, normalize_event_date, normalize_event_time, resolve_event_slug
from audit_log import log_action, ACTION_REMINDER_SENT

logger = logging.getLogger()
_DDB = boto3.resource("dynamodb")
# One Quo request may take 15 seconds; 15 recipients leaves about 70 seconds
# for reads, state writes, logging, and continuation dispatch in a 300s Lambda.
MAX_REMINDER_RECIPIENTS_PER_INVOCATION = 15


def _events_table():
    return _DDB.Table(os.environ["EVENTS_TABLE_NAME"])


def _invites_table():
    return _DDB.Table(os.environ["INVITES_TABLE_NAME"])


def _invite_jobs_table():
    return _DDB.Table(os.environ["INVITE_JOBS_TABLE_NAME"])


def _members_table():
    return _DDB.Table(os.environ["MEMBERS_TABLE_NAME"])


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
        logger.exception("reminder_handler: failed to resolve current event")
        return {}

def _event_timezone_name(event: dict) -> str:
    tz_name = (event.get("event_timezone") or "America/New_York").strip()
    return tz_name or "America/New_York"


def _event_zoneinfo(event: dict):
    tz_name = _event_timezone_name(event)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("reminder_handler: invalid event_timezone=%s — defaulting to America/New_York", tz_name)
        return ZoneInfo("America/New_York")


def _scheduled_target_time(event: dict, is_day_of: bool) -> tuple[int, int]:
    default_value = "11:00" if is_day_of else "18:00"
    field_name = "day_of_send_time" if is_day_of else "day_before_send_time"
    raw_value = event.get(field_name) or default_value
    try:
        hhmm = normalize_event_time(raw_value, field_name=field_name, allow_blank=False)
    except ValueError:
        logger.warning("reminder_handler: invalid %s=%s — defaulting to %s", field_name, raw_value, default_value)
        hhmm = default_value
    hour_str, minute_str = hhmm.split(":", 1)
    return int(hour_str), int(minute_str)


def _within_scheduled_window(local_now: datetime, target_hour: int, target_minute: int, *, window_minutes: int = 5) -> bool:
    current_total = local_now.hour * 60 + local_now.minute
    target_total = target_hour * 60 + target_minute
    return target_total <= current_total < (target_total + window_minutes)


def _build_reminder(member_name: str, event: dict) -> str:
    """
    Build the reminder SMS for a member.

    Reads day_of_template or day_before_template depending on _is_day_of flag.
    Falls back to legacy reminder_template for backward compat with old saved events.
    Falls back to building from event fields if no template is saved.
    Replace {name} with member first name.
    """
    name       = (member_name or "").split()[0] or ""
    is_day_of  = event.get("_is_day_of", False)

    # Try the specific template first, then legacy single template
    if is_day_of:
        template = (event.get("day_of_template") or event.get("reminder_template") or "").strip()
    else:
        template = (event.get("day_before_template") or event.get("reminder_template") or "").strip()

    if template:
        message = template.replace("{name}", name).strip()
        # A saved template must not prevent the promised venue release.
        if venue_available(event, confirmed=True):
            details = [str(event.get(k) or "").strip() for k in ("venue", "address")]
            missing = [value for value in details if value and value.casefold() not in message.casefold()]
            if missing:
                message += " " + ", ".join(missing) + "."
        return message

    # Fallback: build from event fields
    event_label = (event.get("event_label") or event.get("eventSlug") or "").strip()
    start_time  = (event.get("startTime") or "").strip()
    timing_word = "Today" if is_day_of else ("In two days" if venue_mode(event) == "48_hours" else "Tomorrow")

    parts = [f"{name}." if name else ""]
    parts.append(f"{timing_word}.")
    if event_label:
        parts.append(f"{event_label}.")
    if start_time:
        parts.append(f"Doors at {start_time}.")

    if venue_available(event, confirmed=True):
        location = ", ".join(str(event.get(k) or "").strip() for k in ("venue", "address") if event.get(k))
        if location:
            parts.append(location + ".")
    return " ".join(p for p in parts if p)


def _get_confirmed_invites(event_id: str) -> list:
    """
    Query confirmed invitees using eventId primary hash key.
    Returns full invite items (including dedup sentinel fields).
    """
    invites_t = _invites_table()
    items = []
    kwargs: dict = {
        "KeyConditionExpression": DKey("eventId").eq(event_id),
    }
    while True:
        resp = invites_t.query(**kwargs)
        # Include CONFIRMED and ATTENDED — ATTENDED members were confirmed and
        # may still need reminders (e.g. logistics day-of)
        items.extend([i for i in resp.get("Items", [])
                      if i.get("status") in ("CONFIRMED", "ATTENDED")])
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _batch_get_members(phones: list) -> dict:
    """
    Fix #32: BatchGetItem instead of N+1 individual reads.
    Returns {phone: member_item} map.
    """
    if not phones:
        return {}

    members_t = _members_table()
    member_map = {}

    for i in range(0, len(phones), 100):
        batch = phones[i:i + 100]
        request_items = {
            members_t.name: {
                "Keys": [{"phone": p} for p in batch],
                "ProjectionExpression": "phone, #n, smsOptIn, optOut",
                "ExpressionAttributeNames": {"#n": "name"},
            }
        }
        try:
            retries = 0
            while request_items and retries < 4:
                resp = members_t.meta.client.batch_get_item(RequestItems=request_items)
                for item in resp.get("Responses", {}).get(members_t.name, []):
                    member_map[item["phone"]] = item
                # Retry throttled keys, but never spin until Lambda timeout. Any
                # phone still missing from member_map is treated as unverifiable
                # and therefore ineligible to receive SMS in send_reminders().
                request_items = resp.get("UnprocessedKeys") or {}
                retries += 1
            if request_items:
                unresolved = len((request_items.get(members_t.name) or {}).get("Keys") or [])
                logger.error("_batch_get_members: %d unresolved phone(s) after retries", unresolved)
        except Exception:
            logger.exception("_batch_get_members: batch failed for %d phones", len(batch))

    return member_map


def _batch_get_invites(event_id: str, phones: list) -> dict:
    """Read only the locked continuation recipients, with current invite state."""
    if not phones:
        return {}
    invites_t = _invites_table()
    invite_map = {}
    for offset in range(0, len(phones), 100):
        request_items = {
            invites_t.name: {
                "Keys": [
                    {"eventId": event_id, "phone": phone}
                    for phone in phones[offset:offset + 100]
                ],
                "ConsistentRead": True,
            }
        }
        retries = 0
        while request_items and retries < 4:
            response = invites_t.meta.client.batch_get_item(RequestItems=request_items)
            for item in response.get("Responses", {}).get(invites_t.name, []):
                invite_map[item["phone"]] = item
            request_items = response.get("UnprocessedKeys") or {}
            retries += 1
        if request_items:
            unresolved = len((request_items.get(invites_t.name) or {}).get("Keys") or [])
            raise RuntimeError(f"_batch_get_invites: {unresolved} locked invite row(s) remain unread")
    return invite_map


def _queue_reminder_continuation(context: dict, phone_keys: list) -> None:
    function_name = (os.getenv("AWS_LAMBDA_FUNCTION_NAME") or "").strip()
    if not function_name:
        raise RuntimeError("reminder continuation requires AWS_LAMBDA_FUNCTION_NAME")
    payload = {**context, "continuationPhoneKeys": phone_keys}
    response = boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
    if int(response.get("StatusCode") or 0) != 202:
        raise RuntimeError("reminder continuation invocation was not accepted")


def _reminder_continuation_context(event: dict, is_day_of: bool) -> dict:
    target_hour, target_minute = _scheduled_target_time(event, is_day_of)
    return {
        "source": "reminder-continuation",
        "timing": "day_of" if is_day_of else "day_before",
        "eventSlug": resolve_event_slug(event),
        "expectedDate": normalize_event_date(str(event.get("date") or "")),
        "expectedSendTime": f"{target_hour:02d}:{target_minute:02d}",
        "expectedTimezone": _event_timezone_name(event),
        "expectedReminderTiming": str(event.get("reminderTiming") or "manual").strip().lower(),
    }


def _queue_manual_reminder_job(job_id: str, offset: int) -> None:
    function_name = (os.getenv("AWS_LAMBDA_FUNCTION_NAME") or "").strip()
    if not function_name:
        raise RuntimeError("manual reminder continuation requires AWS_LAMBDA_FUNCTION_NAME")
    response = boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({
            "source": "manual-reminder-continuation",
            "jobId": job_id,
            "offset": offset,
        }, separators=(",", ":")).encode("utf-8"),
    )
    if int(response.get("StatusCode") or 0) != 202:
        raise RuntimeError("manual reminder continuation invocation was not accepted")


def _start_manual_reminder_job(event: dict, is_day_of: bool, custom_message: str, token: str) -> dict:
    event_id = resolve_event_slug(event)
    if not event_id:
        raise ValueError("eventSlug is required to send a manual reminder")
    phone_keys = list(dict.fromkeys(
        invite.get("phone")
        for invite in _get_confirmed_invites(event_id)
        if invite.get("phone")
    ))
    if not phone_keys:
        return {"sent": 0, "failed": 0, "skippedAlreadySent": 0, "skippedOptOut": 0}

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    table = _invite_jobs_table()
    table.put_item(
        Item={
            "jobId": job_id,
            "kind": "MANUAL_REMINDER_JOB",
            "eventId": event_id,
            "status": "QUEUED",
            "submittedAt": now_iso,
            "updatedAt": now_iso,
            "isDayOf": bool(is_day_of),
            "customMessage": custom_message,
            "phoneKeys": json.dumps(phone_keys, separators=(",", ":")),
            "recipientCount": len(phone_keys),
            "nextOffset": 0,
            "smsSent": 0,
            "failed": 0,
            "skippedOptOut": 0,
            "ttl": int((now + timedelta(days=30)).timestamp()),
        },
        ConditionExpression="attribute_not_exists(jobId)",
    )
    _queue_manual_reminder_job(job_id, 0)
    log_action(
        token=token,
        action=ACTION_REMINDER_SENT,
        metadata={
            "eventId": event_id,
            "customMessage": True,
            "queued": True,
            "jobId": job_id,
            "recipientCount": len(phone_keys),
            "smsEnabled": (os.getenv("SMS_ENABLED", "false") or "").lower() == "true",
        },
    )
    return {
        "sent": 0,
        "failed": 0,
        "skippedAlreadySent": 0,
        "skippedOptOut": 0,
        "queued": True,
        "jobId": job_id,
        "recipientCount": len(phone_keys),
    }


def _process_manual_reminder_job(event: dict, current_event: dict) -> dict:
    job_id = str(event.get("jobId") or "").strip()
    try:
        offset = max(0, int(event.get("offset") or 0))
    except (ValueError, TypeError):
        return {"ok": True, "skipped": True, "reason": "invalid continuation offset"}
    if not job_id:
        return {"ok": True, "skipped": True, "reason": "missing continuation job"}

    jobs = _invite_jobs_table()
    job = jobs.get_item(Key={"jobId": job_id}, ConsistentRead=True).get("Item") or {}
    if job.get("kind") != "MANUAL_REMINDER_JOB":
        return {"ok": True, "skipped": True, "reason": "manual reminder job not found"}
    if job.get("eventId") != resolve_event_slug(current_event):
        return {"ok": True, "skipped": True, "reason": "event changed"}
    if (job.get("status") or "").upper() == "COMPLETE":
        return {"ok": True, "complete": True, "jobId": job_id}
    try:
        phone_keys = json.loads(job.get("phoneKeys") or "[]")
        next_offset = int(job.get("nextOffset") or 0)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise RuntimeError("manual reminder job state is invalid")
    if not isinstance(phone_keys, list):
        raise RuntimeError("manual reminder job phone list is invalid")
    if offset < next_offset:
        if next_offset < len(phone_keys):
            _queue_manual_reminder_job(job_id, next_offset)
        return {"ok": True, "staleContinuation": True, "jobId": job_id}
    if offset > next_offset or offset >= len(phone_keys):
        return {"ok": True, "skipped": True, "reason": "continuation is outside the current job position"}

    end_offset = min(len(phone_keys), offset + MAX_REMINDER_RECIPIENTS_PER_INVOCATION)
    batch_phones = phone_keys[offset:end_offset]
    invite_map = _batch_get_invites(job["eventId"], batch_phones)
    active_invites = [
        invite_map[phone]
        for phone in batch_phones
        if phone in invite_map and invite_map[phone].get("status") in ("CONFIRMED", "ATTENDED")
    ]
    members = _batch_get_members([invite.get("phone") for invite in active_invites])
    jobs_table = jobs
    sent = failed = skipped_opt_out = skipped_already_sent = 0
    now = datetime.now(timezone.utc)
    ttl = int((now + timedelta(days=30)).timestamp())
    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"

    for invite in active_invites:
        phone = invite["phone"]
        member = members.get(phone)
        if member is None:
            failed += 1
            continue
        if member.get("optOut") or (
            member.get("smsOptIn") is not None and not coerce_bool(member.get("smsOptIn"))
        ):
            skipped_opt_out += 1
            continue

        marker_id = f"{job_id}:recipient:{hashlib.sha256(phone.encode('utf-8')).hexdigest()}"
        try:
            jobs_table.put_item(
                Item={
                    "jobId": marker_id,
                    "kind": "MANUAL_REMINDER_RECIPIENT",
                    "status": "CLAIMED",
                    "claimedAt": now.isoformat(timespec="seconds"),
                    "ttl": ttl,
                },
                ConditionExpression="attribute_not_exists(jobId)",
            )
        except ClientError as exc:
            if (exc.response.get("Error") or {}).get("Code") == "ConditionalCheckFailedException":
                existing_marker = jobs_table.get_item(
                    Key={"jobId": marker_id}, ConsistentRead=True
                ).get("Item") or {}
                marker_status = (existing_marker.get("status") or "").upper()
                if marker_status == "SENT":
                    skipped_already_sent += 1
                else:
                    # CLAIMED means the prior invocation stopped at an uncertain
                    # point around provider delivery. Surface it as failed for
                    # operator review; never risk a duplicate paid text.
                    failed += 1
                    logger.warning("manual reminder delivery is unresolved job=%s", job_id)
                continue
            raise

        try:
            first_name = ((invite.get("name") or "").split() or [""])[0]
            message = str(job.get("customMessage") or "").replace("{name}", first_name)
            if sms_enabled:
                send_sms(phone, message)
                time.sleep(0.25)
            jobs_table.update_item(
                Key={"jobId": marker_id},
                UpdateExpression="SET #s = :sent, sentAt = :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":sent": "SENT", ":now": now.isoformat(timespec="seconds")},
            )
            sent += 1
        except Exception:
            logger.exception("manual reminder job failed for phone=...%s", phone[-4:])
            failed += 1
            jobs_table.update_item(
                Key={"jobId": marker_id},
                UpdateExpression="SET #s = :failed, failedAt = :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":failed": "FAILED", ":now": now.isoformat(timespec="seconds")},
            )

    complete = end_offset >= len(phone_keys)
    try:
        jobs_table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET nextOffset = :end, #s = :status, updatedAt = :now, "
                "smsSent = if_not_exists(smsSent, :zero) + :sent, "
                "failed = if_not_exists(failed, :zero) + :failed, "
                "skippedOptOut = if_not_exists(skippedOptOut, :zero) + :optout, "
                "skippedAlreadySent = if_not_exists(skippedAlreadySent, :zero) + :already"
            ),
            ConditionExpression="nextOffset = :offset",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":end": end_offset,
                ":offset": offset,
                ":status": "COMPLETE" if complete else "QUEUED",
                ":now": now.isoformat(timespec="seconds"),
                ":zero": 0,
                ":sent": sent,
                ":failed": failed,
                ":optout": skipped_opt_out,
                ":already": skipped_already_sent,
            },
        )
    except ClientError as exc:
        if (exc.response.get("Error") or {}).get("Code") != "ConditionalCheckFailedException":
            raise
        logger.info("manual reminder continuation already advanced job=%s offset=%s", job_id, offset)
        return {"ok": True, "staleContinuation": True, "jobId": job_id}

    if not complete:
        _queue_manual_reminder_job(job_id, end_offset)
    log_action(
        token="",
        action=ACTION_REMINDER_SENT,
        metadata={
            "eventId": job["eventId"], "jobId": job_id, "customMessage": True,
            "sent": sent, "failed": failed, "skippedAlreadySent": skipped_already_sent,
            "skippedOptOut": skipped_opt_out, "queued": not complete,
        },
    )
    return {
        "ok": True,
        "jobId": job_id,
        "sent": sent,
        "failed": failed,
        "skippedAlreadySent": skipped_already_sent,
        "skippedOptOut": skipped_opt_out,
        "complete": complete,
        "remainingRecipients": max(0, len(phone_keys) - end_offset),
    }


def send_reminders(
    event: dict,
    is_day_of: bool,
    token: str = "",
    custom_message: str = None,
    phone_keys: list | None = None,
    continuation_context: dict | None = None,
) -> dict:
    """Send reminder SMS to all confirmed members who haven't been reminded yet."""
    event = {**event, "_is_day_of": is_day_of}
    # Canonical event identity — always the slug, never "current".
    event_id = resolve_event_slug(event)
    if not event_id:
        logger.warning("send_reminders: no eventSlug on current event — cannot query invites")
        return {"sent": 0, "failed": 0, "skippedAlreadySent": 0, "skippedOptOut": 0}
    if custom_message:
        return _start_manual_reminder_job(event, is_day_of, custom_message, token)
    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"

    reminder_field = "dayOfReminderSentAt" if is_day_of else "dayBeforeReminderSentAt"
    claim_field = "dayOfReminderClaimedAt" if is_day_of else "dayBeforeReminderClaimedAt"

    if phone_keys is None:
        confirmed = _get_confirmed_invites(event_id)
        eligible = [
            invite for invite in confirmed
            if invite.get("phone")
            and not invite.get(reminder_field)
            and not invite.get(claim_field)
        ]
        phone_keys = [invite["phone"] for invite in eligible]
        invite_map = {invite["phone"]: invite for invite in eligible}
    else:
        # Retries/continuations re-read only the locked keys and recheck invite
        # status and sent/claimed markers before doing any work.
        invite_map = _batch_get_invites(event_id, phone_keys)

    max_per_invocation = MAX_REMINDER_RECIPIENTS_PER_INVOCATION
    batch_phones = phone_keys[:max_per_invocation]
    remaining_phones = phone_keys[max_per_invocation:]
    confirmed = [
        invite_map[phone]
        for phone in batch_phones
        if phone in invite_map
        and invite_map[phone].get("status") in ("CONFIRMED", "ATTENDED")
        and not invite_map[phone].get(reminder_field)
        and not invite_map[phone].get(claim_field)
    ]
    invites_t = _invites_table()
    sent = 0
    failed = 0
    skipped_already_sent = 0
    skipped_opt_out = 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Batch-fetch member records so we can enforce opt-out without N+1 reads.
    all_phones = [inv.get("phone", "") for inv in confirmed if inv.get("phone")]
    member_map = _batch_get_members(all_phones)

    for invite in confirmed:
        phone = invite.get("phone", "")
        if not phone:
            continue

        # Opt-out and consent enforcement — never text someone who sent STOP
        # or who hasn't opted in, regardless of invite status.
        member = member_map.get(phone)
        if member is None:
            # Consent cannot be verified. Fail closed rather than treating a
            # DynamoDB read failure/missing row as permission to text.
            failed += 1
            logger.warning("send_reminders: member consent unavailable phone=...%s", phone[-4:])
            continue
        if member.get("optOut"):
            skipped_opt_out += 1
            continue
        if member.get("smsOptIn") is not None and not coerce_bool(member.get("smsOptIn")):
            skipped_opt_out += 1
            continue

        if invite.get(reminder_field):
            skipped_already_sent += 1
            continue

        scheduled_send_accepted = False
        try:
            name = invite.get("name", "")
            try:
                invites_t.update_item(
                    Key={"eventId": event_id, "phone": phone},
                    UpdateExpression=f"SET {claim_field} = :now",
                    ConditionExpression=f"attribute_not_exists({reminder_field}) AND attribute_not_exists({claim_field})",
                    ExpressionAttributeValues={":now": now},
                )
            except ClientError as exc:
                error_code = (exc.response.get("Error") or {}).get("Code")
                if error_code == "ConditionalCheckFailedException":
                    skipped_already_sent += 1
                    continue
                raise

            message = _build_reminder(name, event)
            if sms_enabled:
                send_sms(phone, message)
                time.sleep(0.25)
            scheduled_send_accepted = True
            sent += 1

            # Keep the claim if provider acceptance succeeds but this stamp fails.
            invites_t.update_item(
                Key={"eventId": event_id, "phone": phone},
                UpdateExpression=f"SET {reminder_field} = :now REMOVE {claim_field}",
                ExpressionAttributeValues={":now": now},
            )

        except Exception:
            logger.exception(
                "reminder_handler: failed to process phone=...%s", phone[-4:]
            )
            failed += 1
            if not scheduled_send_accepted:
                try:
                    invites_t.update_item(
                        Key={"eventId": event_id, "phone": phone},
                        UpdateExpression=f"REMOVE {claim_field}",
                    )
                except Exception:
                    logger.exception(
                        "reminder_handler: failed to clear %s for phone=...%s", claim_field, phone[-4:]
                    )

    continuation_queued = False
    if remaining_phones and continuation_context:
        _queue_reminder_continuation(continuation_context, remaining_phones)
        continuation_queued = True

    log_action(
        token=token,
        action=ACTION_REMINDER_SENT,
        metadata={
            "eventId":            event_id,
            "isDayOf":            is_day_of,
            "sent":               sent,
            "failed":             failed,
            "skippedAlreadySent": skipped_already_sent,
            "skippedOptOut":      skipped_opt_out,
            "trigger":            "manual" if token else "scheduled",
            "customMessage":      bool(custom_message),
            "smsEnabled":         sms_enabled,
            "continuationQueued": continuation_queued,
            "remainingRecipients": len(remaining_phones) if continuation_queued else 0,
        },
    )

    return {
        "sent":               sent,
        "failed":             failed,
        "skippedAlreadySent": skipped_already_sent,
        "skippedOptOut":      skipped_opt_out,
        "continuationQueued": continuation_queued,
        "remainingRecipients": len(remaining_phones) if continuation_queued else 0,
    }


def _cors_headers(origin=None):
    allowed_raw = os.getenv("ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in allowed_raw.split(",") if o.strip()]
    # Fail closed: never fall back to wildcard — that opens the endpoint to any domain.
    allow_origin = origin if origin in origins else (origins[0] if origins else "")
    return {
        "content-type": "application/json",
        "access-control-allow-origin": allow_origin,
        "access-control-allow-headers": "content-type,x-admin-token",
        "access-control-allow-methods": "POST,OPTIONS",
    }


def handler(event, context):
    """
    Triggered by:
    1. EventBridge Scheduler one-time event (automatic)
    2. API Gateway POST /admin/invite/reminder (manual blast)
    """
    try:
        if event.get("source") == "rsvp.followup" and not event.get("httpMethod") and not event.get("requestContext"):
            from followups import handle
            return handle(event, send_sms=send_sms)
        if event.get("httpMethod") or event.get("requestContext"):
            headers = event.get("headers") or {}
            origin  = headers.get("origin") or headers.get("Origin") or ""
            method  = (
                event.get("httpMethod") or
                event.get("requestContext", {}).get("http", {}).get("method", "")
            ).upper()

            if method == "OPTIONS":
                return {"statusCode": 200, "headers": _cors_headers(origin), "body": "{}"}

            body = json.loads(event.get("body") or "{}")

            token    = headers.get("x-admin-token") or headers.get("X-Admin-Token") or ""
            expected = get_secret_string(os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token"))
            try:
                j = json.loads(expected)
                if isinstance(j, dict):
                    expected = j.get("token", expected)
            except Exception:
                pass

            if not token or not hmac.compare_digest(token, expected):
                return {
                    "statusCode": 401,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "unauthorized"}),
                }

            current_event = _get_current_event()
            if not current_event:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "No current event"}),
                }

            # Lifecycle guard — reminders only send for active events
            ev_state = (current_event.get("event_status") or "DRAFT").upper()
            REMINDER_ALLOWED_STATES = {"LIVE"}
            if ev_state not in REMINDER_ALLOWED_STATES:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({
                        "ok": False,
                        "error": f"Cannot send reminders — event is {ev_state}. "
                                 f"Event must be Live to send reminders."
                    }),
                }

            manual_timing = str(body.get("timing") or "").strip().lower()
            if manual_timing in {"day_before", "day_of"}:
                is_day_of = manual_timing == "day_of"
            elif "is_day_of" in body:
                is_day_of = coerce_bool(body["is_day_of"])
            else:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "timing (day_before/day_of) or is_day_of (true/false) required"}),
                }

            custom_message = str(body.get("custom_message") or "").strip() or None
            if custom_message:
                result = _start_manual_reminder_job(
                    current_event,
                    is_day_of=is_day_of,
                    custom_message=custom_message,
                    token=token,
                )
                return {
                    "statusCode": 202 if result.get("queued") else 200,
                    "headers": _cors_headers(origin),
                    "body": json.dumps({"ok": True, **result}),
                }
            result = send_reminders(
                current_event,
                is_day_of=is_day_of,
                token=token,
                continuation_context=_reminder_continuation_context(current_event, is_day_of),
            )
            return {
                "statusCode": 200,
                "headers": _cors_headers(origin),
                "body": json.dumps({"ok": True, **result}),
            }

        # ── Scheduled trigger ─────────────────────────────────────────────────
        current_event = _get_current_event()
        if not current_event:
            logger.info("reminder_handler: no current event — skipping")
            return {"ok": True, "skipped": True}

        # Lifecycle guard — never fire scheduled reminders for inactive events
        sched_ev_state = (current_event.get("event_status") or "DRAFT").upper()
        REMINDER_ALLOWED_STATES = {"LIVE"}
        if sched_ev_state not in REMINDER_ALLOWED_STATES:
            logger.info("reminder_handler: skipping scheduled reminder — event is %s", sched_ev_state)
            return {"ok": True, "skipped": True, "reason": f"event_status={sched_ev_state}"}

        if str(event.get("source") or "").strip().lower() == "manual-reminder-continuation":
            return _process_manual_reminder_job(event, current_event)

        if str(event.get("source") or "").strip().lower() == "reminder-continuation":
            timing = str(event.get("timing") or "").strip().lower()
            if timing not in {"day_before", "day_of"}:
                return {"ok": True, "skipped": True, "reason": "invalid continuation timing"}
            is_day_of = timing == "day_of"
            expected_slug = str(event.get("eventSlug") or "").strip()
            target_hour, target_minute = _scheduled_target_time(current_event, is_day_of)
            valid = (
                expected_slug == resolve_event_slug(current_event)
                and str(event.get("expectedDate") or "") == normalize_event_date(str(current_event.get("date") or ""))
                and str(event.get("expectedSendTime") or "") == f"{target_hour:02d}:{target_minute:02d}"
                and str(event.get("expectedTimezone") or "") == _event_timezone_name(current_event)
                and str(event.get("expectedReminderTiming") or "manual").strip().lower()
                    == str(current_event.get("reminderTiming") or "manual").strip().lower()
                and isinstance(event.get("continuationPhoneKeys"), list)
            )
            if not valid:
                logger.info("reminder_handler: stale continuation for %s — skipping", expected_slug or "<missing>")
                return {"ok": True, "skipped": True, "reason": "stale continuation"}
            result = send_reminders(
                current_event,
                is_day_of=is_day_of,
                phone_keys=event["continuationPhoneKeys"],
                continuation_context=_reminder_continuation_context(current_event, is_day_of),
            )
            return {"ok": True, **result}

        reminder_timing = current_event.get("reminderTiming", "manual")
        if reminder_timing == "manual":
            logger.info("reminder_handler: timing=manual — skipping scheduled trigger")
            return {"ok": True, "skipped": True}

        event_date_str = (current_event.get("date") or "").strip()
        if not event_date_str:
            logger.warning("reminder_handler: no event date set — skipping")
            return {"ok": True, "skipped": True, "reason": "no event date"}

        try:
            event_date = datetime.strptime(normalize_event_date(event_date_str), "%Y-%m-%d").date()
        except ValueError:
            logger.warning("reminder_handler: unparseable date '%s' — skipping", event_date_str)
            return {"ok": True, "skipped": True, "reason": "unparseable date"}

        trigger_timing = str(event.get("timing") or "").strip().lower()

        if reminder_timing == "both":
            if trigger_timing in {"day_before", "day_of"}:
                is_day_of = trigger_timing == "day_of"
            else:
                zone = _event_zoneinfo(current_event)
                local_now = datetime.now(zone)
                local_today = local_now.date()
                before_hour, before_minute = _scheduled_target_time(current_event, False)
                dayof_hour, dayof_minute = _scheduled_target_time(current_event, True)
                if local_today == (event_date - timedelta(days=1)) and _within_scheduled_window(local_now, before_hour, before_minute):
                    is_day_of = False
                    logger.info("reminder_handler: inferred day_before trigger for timing=both")
                elif local_today == event_date and _within_scheduled_window(local_now, dayof_hour, dayof_minute):
                    is_day_of = True
                    logger.info("reminder_handler: inferred day_of trigger for timing=both")
                else:
                    logger.info("reminder_handler: timing=both and trigger timing missing outside eligible windows — skipping")
                    return {"ok": True, "skipped": True, "reason": "missing trigger timing"}
        elif reminder_timing == "day_before":
            if trigger_timing and trigger_timing != "day_before":
                logger.info("reminder_handler: trigger=%s but event timing=day_before — skipping", trigger_timing)
                return {"ok": True, "skipped": True, "reason": "wrong trigger timing"}
            is_day_of = False
        elif reminder_timing == "day_of":
            if trigger_timing and trigger_timing != "day_of":
                logger.info("reminder_handler: trigger=%s but event timing=day_of — skipping", trigger_timing)
                return {"ok": True, "skipped": True, "reason": "wrong trigger timing"}
            is_day_of = True
        else:
            logger.info("reminder_handler: unsupported timing=%s — skipping", reminder_timing)
            return {"ok": True, "skipped": True, "reason": "unsupported timing"}

        # One-time EventBridge Scheduler invocations already represent the exact
        # saved local reminder time. Validate that the event has not changed
        # since the schedule was created, then send without a narrow wall-clock
        # window so Scheduler retries are still useful.
        if str(event.get("source") or "").strip().lower() == "scheduler":
            expected_slug = str(event.get("eventSlug") or "").strip()
            active_slug = resolve_event_slug(current_event)
            expected_date = str(event.get("expectedDate") or "").strip()
            expected_time = str(event.get("expectedSendTime") or "").strip()
            expected_tz = str(event.get("expectedTimezone") or "").strip()
            target_hour, target_minute = _scheduled_target_time(current_event, is_day_of)
            current_time = f"{target_hour:02d}:{target_minute:02d}"
            if not is_day_of and venue_mode(current_event) == "48_hours":
                current_time = (event_start(current_event).astimezone(timezone.utc) - timedelta(hours=48)).astimezone(ZoneInfo(_event_timezone_name(current_event))).strftime("%H:%M")
            current_date = normalize_event_date(event_date_str)
            current_tz = _event_timezone_name(current_event)
            if (
                not expected_slug
                or expected_slug != active_slug
                or (expected_date and expected_date != current_date)
                or (expected_time and expected_time != current_time)
                or (expected_tz and expected_tz != current_tz)
                or (event.get("expectedVenueReleaseMode") and event["expectedVenueReleaseMode"] != venue_mode(current_event))
                or (event.get("expectedStartTime") and event["expectedStartTime"] != current_event.get("startTime"))
            ):
                logger.info("reminder_handler: stale scheduler invocation for %s — skipping", expected_slug or "<missing>")
                return {"ok": True, "skipped": True, "reason": "stale schedule"}
            result = send_reminders(
                current_event,
                is_day_of=is_day_of,
                token="",
                phone_keys=event.get("continuationPhoneKeys"),
                continuation_context=_reminder_continuation_context(current_event, is_day_of),
            )
            logger.info(
                "reminder_handler: scheduled blast sent=%s failed=%s skippedAlreadySent=%s",
                result["sent"], result["failed"], result["skippedAlreadySent"],
            )
            return {"ok": True, **result}

        zone = _event_zoneinfo(current_event)
        local_now = datetime.now(zone)
        local_today = local_now.date()
        target_hour, target_minute = _scheduled_target_time(current_event, is_day_of)
        expected_fire_date = event_date if is_day_of else event_date - timedelta(days=1)

        if not is_day_of and venue_mode(current_event) == "48_hours":
            release = (event_start(current_event).astimezone(timezone.utc) - timedelta(hours=48)).astimezone(zone)
            expected_fire_date = release.date()
            target_hour, target_minute = release.hour, release.minute

        if local_today != expected_fire_date:
            logger.info(
                "reminder_handler: local_today=%s expected_fire=%s tz=%s — skipping",
                local_today.isoformat(), expected_fire_date.isoformat(), _event_timezone_name(current_event),
            )
            return {"ok": True, "skipped": True, "reason": "not the right day"}

        if not _within_scheduled_window(local_now, target_hour, target_minute, window_minutes=5):
            logger.info(
                "reminder_handler: local_time=%s target_time=%02d:%02d tz=%s trigger=%s — skipping",
                local_now.strftime("%H:%M"), target_hour, target_minute, _event_timezone_name(current_event), trigger_timing,
            )
            return {"ok": True, "skipped": True, "reason": "not the right minute"}

        result = send_reminders(
            current_event,
            is_day_of=is_day_of,
            token="",
            continuation_context=_reminder_continuation_context(current_event, is_day_of),
        )
        logger.info(
            "reminder_handler: scheduled blast sent=%s failed=%s skippedAlreadySent=%s",
            result["sent"], result["failed"], result["skippedAlreadySent"],
        )
        return {"ok": True, **result}

    except Exception:
        logger.exception("Unhandled error in reminder_handler")
        if str((event or {}).get("source") or "").strip().lower() in {
            "scheduler", "reminder-continuation", "manual-reminder-continuation",
        }:
            raise
        return {"ok": False, "error": "An internal error occurred"}
