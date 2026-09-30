#!/usr/bin/env python3
"""Read-only production data reconciliation for RSVP Society.

This tool scans the RSVP Society DynamoDB source-of-truth tables and reports
cross-table/data-integrity contradictions. It NEVER writes, updates, or deletes.

Examples:
  python backend/tools/production_reconcile.py --region us-east-1
  python backend/tools/production_reconcile.py --profile prod-admin --json report.json
  python backend/tools/production_reconcile.py --fail-on-error
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

import boto3

MEMBER_STATUSES = {"PENDING", "APPROVED", "DENIED", "DELETED"}
EVENT_STATUSES = {"DRAFT", "LIVE", "ARCHIVED"}
INVITE_STATUSES = {"INVITED", "CONFIRMED", "DECLINED", "ATTENDED", "NO_SHOW", "FAILED", "DELETED"}
CONFIRMED_FAMILY = {"CONFIRMED", "ATTENDED", "NO_SHOW"}
OPEN_JOB_STATUSES = {"QUEUED", "PROCESSING"}
DEFAULT_TABLES = {
    "members": "rsvp-members",
    "events": "rsvp-events",
    "invites": "rsvp-event-invites",
    "jobs": "rsvp-invite-jobs",
    "checkins": "rsvp-checkins",
    "pending_approvals": "rsvp-pending-approvals",
}


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    entity: str
    message: str
    details: dict[str, Any]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, Decimal)):
        return bool(value)
    return str(value).strip().lower() in {"true", "1", "yes", "y", "on"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _phone_ref(phone: Any) -> str:
    raw = str(phone or "").strip()
    if not raw:
        return "phone:missing"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"phone:{digest}:...{raw[-4:]}"


def _event_ref(event_id: Any) -> str:
    raw = str(event_id or "").strip()
    return f"event:{raw or 'missing'}"


def _job_ref(job_id: Any) -> str:
    raw = str(job_id or "").strip()
    return f"job:{raw[:12] or 'missing'}"


def _finding(severity: str, code: str, entity: str, message: str, **details: Any) -> Finding:
    return Finding(severity=severity, code=code, entity=entity, message=message, details=details)


def reconcile(snapshot: Mapping[str, list[dict[str, Any]]], *, now: datetime | None = None) -> dict[str, Any]:
    """Analyze an in-memory snapshot. Pure function: no AWS calls or mutations."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    members = list(snapshot.get("members") or [])
    events = list(snapshot.get("events") or [])
    invites = list(snapshot.get("invites") or [])
    jobs = list(snapshot.get("jobs") or [])
    checkins = list(snapshot.get("checkins") or [])
    approvals = list(snapshot.get("pending_approvals") or [])

    findings: list[Finding] = []
    member_by_phone = {str(x.get("phone") or ""): x for x in members if x.get("phone")}
    event_by_id = {str(x.get("eventId") or ""): x for x in events if x.get("eventId")}
    canonical_events = [x for x in events if str(x.get("eventId") or "") != "current"]
    invite_by_key = {
        (str(x.get("eventId") or ""), str(x.get("phone") or "")): x
        for x in invites if x.get("eventId") and x.get("phone")
    }
    checkin_by_key = {
        (str(x.get("eventId") or ""), str(x.get("phone") or "")): x
        for x in checkins if x.get("eventId") and x.get("phone")
    }
    job_by_id = {str(x.get("jobId") or ""): x for x in jobs if x.get("jobId")}

    # Members: consent/status invariants and suspicious duplicate identities.
    identity_groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for m in members:
        phone = str(m.get("phone") or "")
        ref = _phone_ref(phone)
        status = str(m.get("status") or "").upper()
        if status and status not in MEMBER_STATUSES:
            findings.append(_finding("ERROR", "MEMBER_INVALID_STATUS", ref,
                                     "Member has an unsupported status.", status=status))
        if _truthy(m.get("optOut")) and _truthy(m.get("smsOptIn")):
            findings.append(_finding("ERROR", "MEMBER_CONSENT_CONFLICT", ref,
                                     "Member is simultaneously opted out and SMS opted in."))
        if _truthy(m.get("optOut")) and not m.get("optOutAt"):
            findings.append(_finding("WARN", "MEMBER_OPTOUT_MISSING_TIMESTAMP", ref,
                                     "Opted-out member has no optOutAt timestamp."))
        if status == "PENDING":
            expiry = _parse_time(m.get("pendingExpiresAt"))
            if expiry and expiry <= now:
                findings.append(_finding("WARN", "MEMBER_EXPIRED_PENDING", ref,
                                         "Pending member is logically expired but still stored.",
                                         pendingExpiresAt=str(m.get("pendingExpiresAt"))))
        first = str(m.get("name") or m.get("firstName") or "").strip().casefold()
        last = str(m.get("lastName") or "").strip().casefold()
        zip_code = str(m.get("zipCode") or "").strip()
        if first and last:
            identity_groups[(first, last, zip_code)].append(phone)

    for (first, last, zip_code), phones in identity_groups.items():
        unique = sorted({p for p in phones if p})
        if len(unique) > 1:
            findings.append(_finding(
                "WARN", "MEMBER_POSSIBLE_DUPLICATE_IDENTITY", f"identity:{first[:1]}*** {last[:1]}***",
                "Multiple phone records share the same normalized first/last name and ZIP.",
                zipCode=zip_code or "Unknown", phones=[_phone_ref(p) for p in unique], count=len(unique),
            ))

    # Event topology: one canonical LIVE/active event and a coherent current pointer.
    live_events = [e for e in canonical_events if str(e.get("event_status") or "").upper() == "LIVE"]
    active_events = [e for e in canonical_events if _truthy(e.get("active"))]
    if len(live_events) > 1:
        findings.append(_finding("ERROR", "EVENT_MULTIPLE_LIVE", "events",
                                 "More than one canonical event is LIVE.",
                                 eventIds=[e.get("eventId") for e in live_events]))
    if len(active_events) > 1:
        findings.append(_finding("ERROR", "EVENT_MULTIPLE_ACTIVE", "events",
                                 "More than one canonical event has active=true.",
                                 eventIds=[e.get("eventId") for e in active_events]))
    for e in canonical_events:
        event_id = str(e.get("eventId") or "")
        status = str(e.get("event_status") or "").upper()
        if status == "INVITING":
            findings.append(_finding("ERROR", "EVENT_RETIRED_INVITING", _event_ref(event_id),
                                     "Legacy INVITING lifecycle state is still stored; current lifecycle is DRAFT/LIVE/ARCHIVED."))
        elif status and status not in EVENT_STATUSES:
            findings.append(_finding("ERROR", "EVENT_INVALID_STATUS", _event_ref(event_id),
                                     "Event has an unsupported lifecycle state.", status=status))
        if _truthy(e.get("active")) and status != "LIVE":
            findings.append(_finding("ERROR", "EVENT_ACTIVE_NOT_LIVE", _event_ref(event_id),
                                     "Event has active=true but is not LIVE.", status=status))

    pointer = event_by_id.get("current") or {}
    pointer_slug = str(pointer.get("activeEventSlug") or "").strip()
    if pointer_slug:
        target = event_by_id.get(pointer_slug)
        if not target:
            findings.append(_finding("ERROR", "EVENT_POINTER_MISSING_TARGET", "event:current",
                                     "Current-event pointer references a missing event.", activeEventSlug=pointer_slug))
        else:
            target_status = str(target.get("event_status") or "").upper()
            if target_status != "LIVE":
                findings.append(_finding("ERROR", "EVENT_POINTER_NOT_LIVE", "event:current",
                                         "Current-event pointer references a non-LIVE event.",
                                         activeEventSlug=pointer_slug, status=target_status))
            if not _truthy(target.get("active")):
                findings.append(_finding("ERROR", "EVENT_POINTER_ACTIVE_FLAG_DRIFT", _event_ref(pointer_slug),
                                         "Current-event pointer target does not have active=true."))
    elif live_events or active_events:
        findings.append(_finding("ERROR", "EVENT_POINTER_MISSING", "event:current",
                                 "A LIVE/active event exists but the current pointer has no activeEventSlug."))

    # Pending host approvals must correspond to a still-reviewable PENDING member.
    for p in approvals:
        member_phone = str(p.get("memberPhone") or "")
        ref = _phone_ref(member_phone)
        member = member_by_phone.get(member_phone)
        if not member:
            findings.append(_finding("ERROR", "APPROVAL_ORPHAN", ref,
                                     "Pending approval row points to a missing member."))
            continue
        status = str(member.get("status") or "").upper()
        expiry = _parse_time(member.get("pendingExpiresAt"))
        approval_expired = False
        try:
            approval_expired = bool(p.get("expiresAt") and int(p.get("expiresAt")) <= int(now.timestamp()))
        except (TypeError, ValueError):
            approval_expired = False
        if status != "PENDING" or (expiry and expiry <= now) or approval_expired:
            findings.append(_finding("ERROR", "APPROVAL_STALE", ref,
                                     "Pending approval exists for a member who is no longer reviewable or the approval itself has expired.",
                                     memberStatus=status, memberExpired=bool(expiry and expiry <= now),
                                     approvalExpired=approval_expired))

    # Invite/check-in/attendance consistency and finalized-event settlement.
    invite_counts_by_phone: dict[str, Counter] = defaultdict(Counter)
    confirmed_heads_by_event: Counter = Counter()
    delivered_by_event: Counter = Counter()
    invites_by_job: Counter = Counter()
    sms_sent_by_job: Counter = Counter()

    for inv in invites:
        event_id = str(inv.get("eventId") or "")
        phone = str(inv.get("phone") or "")
        status = str(inv.get("status") or "").upper()
        ref = f"{_event_ref(event_id)}/{_phone_ref(phone)}"
        if status and status not in INVITE_STATUSES:
            findings.append(_finding("ERROR", "INVITE_INVALID_STATUS", ref,
                                     "Invite has an unsupported status.", status=status))
        if status not in {"FAILED", "DELETED", ""}:
            invite_counts_by_phone[phone]["invited"] += 1
        if status in CONFIRMED_FAMILY:
            invite_counts_by_phone[phone]["confirmed"] += 1
            confirmed_heads_by_event[event_id] += 1
            if str(inv.get("plusOneName") or "").strip():
                confirmed_heads_by_event[event_id] += 1
        if status == "ATTENDED":
            invite_counts_by_phone[phone]["attended"] += 1
        if status == "NO_SHOW":
            invite_counts_by_phone[phone]["no_show"] += 1
        if inv.get("deliveredAt"):
            delivered_by_event[event_id] += 1

        member = member_by_phone.get(phone)
        if not member and status not in {"FAILED", "DELETED"}:
            findings.append(_finding("WARN", "INVITE_MEMBER_MISSING", ref,
                                     "Invite references a phone with no member record."))

        if status == "ATTENDED":
            if not inv.get("attendedAt"):
                findings.append(_finding("ERROR", "ATTENDED_MISSING_TIMESTAMP", ref,
                                         "ATTENDED invite is missing attendedAt."))
            attended_at = _parse_time(inv.get("attendedAt"))
            checkin_should_exist = not attended_at or (now - attended_at.astimezone(timezone.utc)).total_seconds() <= 91 * 86400
            if checkin_should_exist and (event_id, phone) not in checkin_by_key:
                findings.append(_finding("ERROR", "ATTENDED_MISSING_CHECKIN", ref,
                                         "Recent ATTENDED invite has no corresponding check-in row."))
            if inv.get("noShowAt"):
                findings.append(_finding("ERROR", "ATTENDED_HAS_NOSHOW_TIMESTAMP", ref,
                                         "ATTENDED invite still has noShowAt."))
        if status == "NO_SHOW":
            if not inv.get("noShowAt"):
                findings.append(_finding("ERROR", "NOSHOW_MISSING_TIMESTAMP", ref,
                                         "NO_SHOW invite is missing noShowAt."))
            if inv.get("attendedAt") or (event_id, phone) in checkin_by_key:
                findings.append(_finding("ERROR", "NOSHOW_ATTENDANCE_CONTRADICTION", ref,
                                         "NO_SHOW invite also has attendance evidence."))

        plus_name = str(inv.get("plusOneName") or "").strip()
        plus_attended = bool(inv.get("plusOneAttendedAt"))
        plus_noshow = bool(inv.get("plusOneNoShowAt"))
        if plus_attended and plus_noshow:
            findings.append(_finding("ERROR", "PLUS_ONE_ATTENDANCE_CONTRADICTION", ref,
                                     "+1 is marked both attended and no-show."))
        if (plus_attended or plus_noshow) and not plus_name:
            findings.append(_finding("ERROR", "PLUS_ONE_STATUS_WITHOUT_NAME", ref,
                                     "+1 attendance state exists without a +1 name."))
        plus_checkin_key = (event_id, f"PLUSONE#{phone}")
        plus_attended_at = _parse_time(inv.get("plusOneAttendedAt"))
        plus_checkin_should_exist = not plus_attended_at or (now - plus_attended_at.astimezone(timezone.utc)).total_seconds() <= 91 * 86400
        if plus_attended and plus_checkin_should_exist and plus_checkin_key not in checkin_by_key:
            findings.append(_finding("ERROR", "PLUS_ONE_ATTENDED_MISSING_CHECKIN", ref,
                                     "Recent +1 attendance has no +1 check-in row."))
        if plus_noshow and plus_checkin_key in checkin_by_key:
            findings.append(_finding("ERROR", "PLUS_ONE_NOSHOW_HAS_CHECKIN", ref,
                                     "+1 is no-show but a +1 check-in row exists."))

        job_id = str(inv.get("jobId") or "").strip()
        if job_id:
            invites_by_job[job_id] += 1
            if inv.get("smsSendStatus") == "SENT" or inv.get("quoMessageId"):
                sms_sent_by_job[job_id] += 1

    # Check-in rows should point back to coherent invites.
    for chk in checkins:
        event_id = str(chk.get("eventId") or "")
        phone = str(chk.get("phone") or "")
        if phone.startswith("PLUSONE#"):
            sponsor = str(chk.get("sponsorPhone") or phone.removeprefix("PLUSONE#"))
            inv = invite_by_key.get((event_id, sponsor))
            ref = f"{_event_ref(event_id)}/{_phone_ref(sponsor)}"
            if not inv:
                findings.append(_finding("ERROR", "PLUS_ONE_CHECKIN_ORPHAN", ref,
                                         "+1 check-in has no sponsor invite."))
            elif not inv.get("plusOneAttendedAt"):
                findings.append(_finding("ERROR", "PLUS_ONE_CHECKIN_FLAG_DRIFT", ref,
                                         "+1 check-in exists but sponsor invite lacks plusOneAttendedAt."))
        else:
            inv = invite_by_key.get((event_id, phone))
            ref = f"{_event_ref(event_id)}/{_phone_ref(phone)}"
            if not inv:
                findings.append(_finding("ERROR", "CHECKIN_ORPHAN", ref,
                                         "Member check-in has no matching invite."))
            elif str(inv.get("status") or "").upper() != "ATTENDED":
                findings.append(_finding("ERROR", "CHECKIN_STATUS_DRIFT", ref,
                                         "Member check-in exists but invite is not ATTENDED.",
                                         inviteStatus=str(inv.get("status") or "")))

    # Finalized events must have no unresolved confirmed attendees/+1s.
    for e in canonical_events:
        if not _truthy(e.get("attendanceFinalized")):
            continue
        event_id = str(e.get("eventId") or "")
        for inv in invites:
            if str(inv.get("eventId") or "") != event_id:
                continue
            status = str(inv.get("status") or "").upper()
            phone = str(inv.get("phone") or "")
            ref = f"{_event_ref(event_id)}/{_phone_ref(phone)}"
            if status == "CONFIRMED":
                findings.append(_finding("ERROR", "FINALIZED_UNRESOLVED_CONFIRMED", ref,
                                         "Attendance-finalized event still has a CONFIRMED invite."))
            if str(inv.get("plusOneName") or "").strip() and not inv.get("plusOneAttendedAt") and not inv.get("plusOneNoShowAt"):
                findings.append(_finding("ERROR", "FINALIZED_UNRESOLVED_PLUS_ONE", ref,
                                         "Attendance-finalized event still has an unsettled +1."))

    # Stored counters are derived data: drift is a reconciliation warning, not automatic corruption.
    for phone, expected in invite_counts_by_phone.items():
        member = member_by_phone.get(phone)
        if not member:
            continue
        for field, key in (("invitedCount", "invited"), ("confirmedCount", "confirmed"),
                           ("attendedCount", "attended"), ("noShowCount", "no_show")):
            actual = _int(member.get(field), 0)
            wanted = int(expected[key])
            if actual < wanted:
                findings.append(_finding("WARN", "MEMBER_COUNTER_UNDERCOUNT", _phone_ref(phone),
                                         "Member lifetime counter is lower than surviving invite history; hard-deleted history can explain overcounts, not undercounts.",
                                         field=field, stored=actual, minimumDerived=wanted))

    for e in canonical_events:
        event_id = str(e.get("eventId") or "")
        if "confirmedHeadcount" in e:
            actual = _int(e.get("confirmedHeadcount"), 0)
            wanted = int(confirmed_heads_by_event[event_id])
            if actual != wanted:
                findings.append(_finding("ERROR", "EVENT_HEADCOUNT_DRIFT", _event_ref(event_id),
                                         "Event confirmedHeadcount differs from confirmed members + reserved +1s.",
                                         stored=actual, derived=wanted))
        if "deliveredCount" in e:
            actual = _int(e.get("deliveredCount"), 0)
            wanted = int(delivered_by_event[event_id])
            if actual != wanted:
                findings.append(_finding("WARN", "EVENT_DELIVERY_COUNTER_DRIFT", _event_ref(event_id),
                                         "Event deliveredCount differs from invites with deliveredAt.",
                                         stored=actual, derived=wanted))

    # Operational invite jobs: flag stuck jobs conservatively after 30 minutes.
    for j in jobs:
        job_id = str(j.get("jobId") or "")
        status = str(j.get("status") or "").upper()
        if str(j.get("kind") or "") in {"PREVIEW_LOCK", "ACCESS_REPLY_LIMIT", "INBOUND_RECEIPT"}:
            continue
        updated = _parse_time(j.get("updatedAt") or j.get("submittedAt"))
        age_minutes = None
        if updated:
            age_minutes = max(0, int((now - updated.astimezone(timezone.utc)).total_seconds() // 60))
        if status in OPEN_JOB_STATUSES and age_minutes is not None and age_minutes >= 30:
            findings.append(_finding("ERROR", "INVITE_JOB_STUCK", _job_ref(job_id),
                                     "Invite job is still open after 30 minutes.", status=status, ageMinutes=age_minutes))
        if status == "COMPLETE" and not j.get("completedAt"):
            findings.append(_finding("WARN", "INVITE_JOB_COMPLETE_MISSING_TIMESTAMP", _job_ref(job_id),
                                     "Completed invite job has no completedAt timestamp."))
        # Only compare successful-row lower bounds; failed provider sends may delete rows by design.
        written = _int(j.get("invitesWritten"), -1)
        if written >= 0 and invites_by_job[job_id] > written:
            findings.append(_finding("ERROR", "INVITE_JOB_COUNTER_UNDERCOUNT", _job_ref(job_id),
                                     "Job invitesWritten is lower than surviving invite rows for the job.",
                                     stored=written, survivingRows=int(invites_by_job[job_id])))
        sent = _int(j.get("smsSent"), -1)
        if sent >= 0 and sms_sent_by_job[job_id] > sent:
            findings.append(_finding("ERROR", "INVITE_JOB_SMS_UNDERCOUNT", _job_ref(job_id),
                                     "Job smsSent is lower than invite rows carrying send evidence.",
                                     stored=sent, survivingSentRows=int(sms_sent_by_job[job_id])))

    # Recent invite rows should still have their job; old job records TTL after 30 days.
    for inv in invites:
        job_id = str(inv.get("jobId") or "").strip()
        if not job_id or job_id in job_by_id:
            continue
        created = _parse_time(inv.get("invitedAt") or inv.get("createdAt"))
        if created and (now - created.astimezone(timezone.utc)).total_seconds() <= 31 * 86400:
            findings.append(_finding("WARN", "RECENT_INVITE_JOB_MISSING", _job_ref(job_id),
                                     "Recent invite row references an invite job that is missing before normal job TTL.",
                                     eventId=str(inv.get("eventId") or ""), phone=_phone_ref(inv.get("phone"))))

    severity_order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 9), f.code, f.entity))
    counts = Counter(f.severity for f in findings)
    return {
        "generatedAt": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "readOnly": True,
        "tableCounts": {
            "members": len(members), "events": len(events), "invites": len(invites),
            "jobs": len(jobs), "checkins": len(checkins), "pendingApprovals": len(approvals),
        },
        "summary": {
            "errors": counts["ERROR"], "warnings": counts["WARN"],
            "findings": len(findings), "clean": not findings,
        },
        "findings": [asdict(f) for f in findings],
    }


def _scan_all(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"ConsistentRead": True}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items") or [])
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def load_snapshot(*, region: str, profile: str | None, table_names: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    ddb = session.resource("dynamodb")
    return {key: _scan_all(ddb.Table(name)) for key, name in table_names.items()}


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def _print_human(report: Mapping[str, Any]) -> None:
    counts = report["tableCounts"]
    summary = report["summary"]
    print("RSVP Society production reconciliation — READ ONLY")
    print("Tables: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"Findings: errors={summary['errors']} warnings={summary['warnings']} total={summary['findings']}")
    for f in report["findings"]:
        details = f.get("details") or {}
        suffix = f" | {json.dumps(details, default=_json_default, sort_keys=True)}" if details else ""
        print(f"[{f['severity']}] {f['code']} {f['entity']}: {f['message']}{suffix}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only RSVP Society production data reconciliation")
    parser.add_argument("--region", default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1")
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE") or None)
    parser.add_argument("--json", dest="json_path", help="Also write the redacted report to this JSON file")
    parser.add_argument("--fail-on-error", action="store_true", help="Exit 2 when reconciliation errors are found")
    for key, default in DEFAULT_TABLES.items():
        env_name = {
            "members": "MEMBERS_TABLE_NAME", "events": "EVENTS_TABLE_NAME", "invites": "INVITES_TABLE_NAME",
            "jobs": "INVITE_JOBS_TABLE_NAME", "checkins": "CHECKINS_TABLE_NAME",
            "pending_approvals": "PENDING_APPROVALS_TABLE_NAME",
        }[key]
        parser.add_argument(f"--{key.replace('_', '-')}-table", default=os.getenv(env_name, default))
    args = parser.parse_args(list(argv) if argv is not None else None)
    table_names = {key: getattr(args, f"{key}_table") for key in DEFAULT_TABLES}

    snapshot = load_snapshot(region=args.region, profile=args.profile, table_names=table_names)
    report = reconcile(snapshot)
    _print_human(report)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2, default=_json_default) + "\n", encoding="utf-8")
    if args.fail_on_error and report["summary"]["errors"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
