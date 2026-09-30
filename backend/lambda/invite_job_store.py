"""Invite preview-lock and asynchronous job persistence helpers.

This module is deliberately behavior-preserving. The public handler keeps thin
wrappers so existing tests and monkeypatches can continue targeting
``invite_handler`` while persistence details live here.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from boto3.dynamodb.conditions import Key as DKey
from botocore.exceptions import ClientError


def write_job(job_id: str, body: dict, origin: str, *, deps: dict) -> None:
    table = deps["invite_jobs_table"]
    now_iso = deps["now_iso"]
    json_default = deps["json_default"]
    table().put_item(Item={
        "jobId": job_id,
        "eventId": (body.get("eventId") or "").strip(),
        "status": "QUEUED",
        "submittedAt": now_iso(),
        "updatedAt": now_iso(),
        "origin": origin or "",
        "requestBody": json.dumps(body, default=json_default),
        "recipientCount": len(body.get("phones") or []),
        "waveNumber": int(body.get("waveNumber") or 0),
        "previewSessionId": (body.get("previewSessionId") or ""),
        "lockedWave": bool(body.get("lockedWave")),
        "invitesWritten": 0,
        "smsSent": 0,
        "failed": 0,
        "ttl": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
    })


def update_job(job_id: str, updates: dict, *, deps: dict) -> None:
    if not updates:
        return
    now_iso = deps["now_iso"]
    table = deps["invite_jobs_table"]
    logger = deps["logger"]
    if updates.get("status") == "FAILED":
        logger.error("invite_job_failed job_id=%s error=%s", job_id, updates.get("error", "unknown"))
    updates = {**updates, "updatedAt": now_iso()}
    expr_parts = []
    names = {}
    vals = {}
    for i, (key, value) in enumerate(updates.items()):
        value_ph = f":v{i}"
        name_ph = f"#k{i}"
        expr_parts.append(f"{name_ph} = {value_ph}")
        names[name_ph] = key
        vals[value_ph] = value
    try:
        table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
        )
    except Exception:
        logger.exception("_update_job failed job_id=%s", job_id)


def preview_lock_expires_at(minutes: int = 30) -> tuple[str, int]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return expires.isoformat(timespec="seconds"), int(expires.timestamp())


def preview_member_phones(preview_members: List[Dict[str, Any]], *, deps: dict) -> List[str]:
    normalize_phone = deps["normalize_phone"]
    phones: List[str] = []
    for row in preview_members:
        phone = normalize_phone(row.get("phone", "")) if row.get("phone") else ""
        status = (row.get("currentEventInviteStatus") or row.get("inviteStatus") or "NOT_INVITED").upper()
        if phone and status == "NOT_INVITED":
            phones.append(phone)
    return phones


def write_preview_lock(*, event_id: str, wave_number: int, wave_size: int,
                       locked_phones: List[str], summary: Dict[str, Any], deps: dict) -> str:
    table = deps["invite_jobs_table"]
    now_iso = deps["now_iso"]
    json_default = deps["json_default"]
    lock_id = str(uuid.uuid4())
    expires_iso, expires_epoch = preview_lock_expires_at()
    table().put_item(Item={
        "jobId": lock_id,
        "kind": "PREVIEW_LOCK",
        "status": "PREVIEW_LOCKED",
        "eventId": event_id,
        "waveNumber": int(wave_number),
        "waveSize": int(wave_size or 0),
        "lockedPhones": json.dumps(locked_phones, separators=(",", ":")),
        "lockedPhoneCount": len(locked_phones),
        "summary": json.dumps(summary or {}, default=json_default, separators=(",", ":")),
        "submittedAt": now_iso(),
        "updatedAt": now_iso(),
        "expiresAt": expires_iso,
        "ttl": expires_epoch,
    })
    return lock_id


def read_preview_lock(lock_id: str, *, deps: dict) -> Dict[str, Any]:
    if not lock_id:
        raise ValueError("previewSessionId required. Run Preview Next Wave before sending.")
    item = deps["invite_jobs_table"]().get_item(Key={"jobId": lock_id}).get("Item") or {}
    if not item or item.get("kind") != "PREVIEW_LOCK":
        raise ValueError("Preview lock not found. Run Preview Next Wave again.")
    if (item.get("status") or "").upper() not in {"PREVIEW_LOCKED", "PREVIEW_QUEUED"}:
        raise ValueError("Preview lock is no longer usable. Run Preview Next Wave again.")
    try:
        expires_at = item.get("ttl")
        if expires_at and int(expires_at) < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("Preview lock expired. Run Preview Next Wave again.")
    except ValueError:
        raise
    except Exception:
        pass
    try:
        item["lockedPhonesList"] = json.loads(item.get("lockedPhones") or "[]")
    except Exception:
        item["lockedPhonesList"] = []
    return item


def claim_preview_lock(lock_id: str, job_id: str, *, deps: dict) -> Dict[str, Any]:
    lock = read_preview_lock(lock_id, deps=deps)
    try:
        deps["invite_jobs_table"]().update_item(
            Key={"jobId": lock_id},
            UpdateExpression="SET #s = :queued, queuedJobId = :job, updatedAt = :now",
            ConditionExpression="#s = :locked",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":queued": "PREVIEW_QUEUED",
                ":locked": "PREVIEW_LOCKED",
                ":job": job_id,
                ":now": deps["now_iso"](),
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ValueError("Preview lock was already used. Run Preview Next Wave again.")
        raise
    return lock


def normalize_phone_subset(raw_phones: List[Any], *, deps: dict) -> List[str]:
    normalize_phone = deps["normalize_phone"]
    logger = deps["logger"]
    out: List[str] = []
    seen = set()
    for raw in raw_phones or []:
        if not raw:
            continue
        try:
            phone = normalize_phone(str(raw))
        except ValueError:
            logger.warning("invite: skipping malformed locked-preview phone (length=%d)", len(str(raw)))
            continue
        if phone not in seen:
            seen.add(phone)
            out.append(phone)
    return out


def resolve_locked_send_body(body: dict, job_id: str, *, deps: dict) -> dict:
    lock_id = (body.get("previewSessionId") or body.get("previewLockId") or "").strip()
    if not lock_id:
        return body
    read_lock = deps.get("read_preview_lock")
    claim_lock = deps.get("claim_preview_lock")
    lock = read_lock(lock_id) if read_lock else read_preview_lock(lock_id, deps=deps)
    if (lock.get("eventId") or "") != (body.get("eventId") or ""):
        raise ValueError("Preview lock event does not match this send request.")
    lock = claim_lock(lock_id, job_id) if claim_lock else claim_preview_lock(lock_id, job_id, deps=deps)

    locked_list = normalize_phone_subset(lock.get("lockedPhonesList") or [], deps=deps)
    locked = set(locked_list)
    submitted = normalize_phone_subset(body.get("phones") or [], deps=deps)
    if submitted:
        invalid = [phone for phone in submitted if phone not in locked]
        if invalid:
            raise ValueError("Send contains phones outside the locked preview. Run Preview Next Wave again.")
        phones = submitted
    else:
        phones = locked_list

    next_body = dict(body)
    next_body.update({
        "phones": phones,
        "waveNumber": int(lock.get("waveNumber") or 1),
        "waveSize": int(lock.get("waveSize") or 0),
        "autoWave": False,
        "lockedWave": True,
        "previewSessionId": lock_id,
    })
    return next_body


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def fallback_job_summary(job_id: str, *, deps: dict) -> dict:
    summary = {"recipientCount": 0, "invitesWritten": 0, "smsSent": 0, "failed": 0}
    try:
        table = deps["invites_table"]()
        kwargs = {
            "IndexName": "jobId-index",
            "KeyConditionExpression": DKey("jobId").eq(job_id),
        }
        while True:
            page = table.query(**kwargs)
            for item in page.get("Items", []):
                summary["recipientCount"] += 1
                status = (item.get("status") or "").upper()
                if status and status != "FAILED":
                    summary["invitesWritten"] += 1
                if item.get("smsSendStatus") == "SENT" or item.get("quoMessageId"):
                    summary["smsSent"] += 1
                if status == "FAILED" or item.get("smsSendStatus") == "FAILED":
                    summary["failed"] += 1
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except Exception:
        deps["logger"].exception("_fallback_job_summary failed job_id=%s", job_id)
    return summary


def handle_job_status(qs: dict, origin: str, *, deps: dict) -> dict:
    job_id = (qs.get("jobId") or "").strip()
    resp = deps["resp"]
    if not job_id:
        return resp(400, {"ok": False, "error": "jobId required"}, origin)

    fallback = {"recipientCount": 0, "invitesWritten": 0, "smsSent": 0, "failed": 0}
    try:
        item = deps["invite_jobs_table"]().get_item(Key={"jobId": job_id}).get("Item") or {}
        breakdown = {}
        if item.get("breakdown"):
            try:
                parsed = json.loads(item.get("breakdown") or "{}")
                if isinstance(parsed, dict):
                    breakdown = parsed
            except Exception:
                breakdown = {}

        if not item:
            fallback = fallback_job_summary(job_id, deps=deps)
            return resp(200, {
                "ok": True,
                "jobId": job_id,
                "status": "UNKNOWN",
                "message": "Job details are unavailable, but the send request may have completed.",
                **fallback,
                "breakdown": fallback,
            }, origin)

        recipient_count = safe_int(item.get("recipientCount"), fallback.get("recipientCount", 0) or safe_int(breakdown.get("queued")))
        invites_written = safe_int(item.get("invitesWritten"), fallback.get("invitesWritten", 0))
        sms_sent = safe_int(item.get("smsSent"), fallback.get("smsSent", 0))
        failed = safe_int(item.get("failed"), fallback.get("failed", 0))

        return resp(200, {
            "ok": True,
            "jobId": item.get("jobId") or job_id,
            "eventId": item.get("eventId"),
            "status": item.get("status") or "UNKNOWN",
            "submittedAt": item.get("submittedAt"),
            "startedAt": item.get("startedAt"),
            "completedAt": item.get("completedAt"),
            "waveNumber": item.get("waveNumber"),
            "recipientCount": recipient_count,
            "invitesWritten": invites_written,
            "smsSent": sms_sent,
            "failed": failed,
            "error": item.get("error"),
            "message": item.get("message"),
            "breakdown": breakdown or fallback,
        }, origin)
    except Exception as exc:
        deps["logger"].exception("handle_job_status degraded job_id=%s", job_id)
        fallback = fallback_job_summary(job_id, deps=deps)
        return resp(200, {
            "ok": True,
            "jobId": job_id,
            "status": "UNKNOWN",
            "message": "Job status is unavailable, but the send request may have completed.",
            "error": str(exc)[:160],
            **fallback,
            "breakdown": fallback,
        }, origin)
