"""
admin_member_routes.py
All /admin/members/* route handlers.
Each function receives the parsed (method, path, headers, event, token) and
returns a complete API Gateway response dict.
"""
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key as DKey

from admin_shared import resp, get_query, get_body, normalize_import_source, invites_table, coerce_bool
from member_store import (
    list_members_by_status, set_status, set_gender, set_tier_override,
    record_attendance, upsert_member, normalize_phone,
    search_members, get_member, mark_welcome_sent, claim_welcome_send, clear_welcome_send_claim,
    normalize_member_record, set_sms_opt_in, write_welcome_error,
)
from audit_log import (
    log_action,
    ACTION_MEMBER_APPROVED, ACTION_MEMBER_DENIED, ACTION_MEMBER_PENDING,
    ACTION_MEMBER_DELETED, ACTION_MEMBER_GENDER_SET, ACTION_MEMBER_TIER_SET,
    ACTION_ATTENDANCE, ACTION_MEMBER_IMPORTED,
)
from sms_adapter import maybe_send_welcome

logger = logging.getLogger()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            [i for i in page.get("Items", []) if i.get("status") == "CONFIRMED"]
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
            # Retry unprocessed keys until fully exhausted (DDB throttling)
            unprocessed = batch_resp.get("UnprocessedKeys") or {}
            while unprocessed:
                retry = members_t.meta.client.batch_get_item(RequestItems=unprocessed)
                for item in retry.get("Responses", {}).get(members_table_name, []):
                    member_map[item["phone"]] = normalize_member_record(item) or {}
                unprocessed = retry.get("UnprocessedKeys") or {}
        except Exception:
            logger.exception("get_confirmed: batch_get_item failed batch i=%d", i)

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
        # Live check — is the plus one name in the full members table?
        # Uses search_members (full table scan) not just event-scoped member_map.
        plus_one_name = invite.get("plusOneName", "")
        plus_one_is_member = False
        if plus_one_name:
            plus_one_clean = plus_one_name.strip().lower()
            if plus_one_clean:
                try:
                    from member_store import search_members
                    matches = search_members(plus_one_clean, limit=5)
                    for match in matches:
                        mfirst = (match.get("name") or "").strip().lower()
                        mlast  = (match.get("lastName") or "").strip().lower()
                        mfull  = f"{mfirst} {mlast}".strip()
                        if plus_one_clean in (mfirst, mlast, mfull):
                            plus_one_is_member = True
                            break
                except Exception:
                    logger.exception("get_confirmed: plus-one member lookup failed name=%s", plus_one_name[:30])

        members_out.append({
            "phone":           phone,
            "name":            first_name,
            "lastName":        last_name,
            "fullName":        full_name,
            "gender":          (m.get("gender") or invite.get("gender") or "").strip(),
            "confirmedAt":     invite.get("confirmedAt", ""),
            "attendedAt":      invite.get("attendedAt", ""),
            "checkedIn":       bool(invite.get("attendedAt")),
            "plusOneName":     plus_one_name,
            "plusOneIsMember": plus_one_is_member,
        })

    members_out.sort(key=lambda x: (
        (x.get("lastName") or x.get("name") or "").lower(),
        (x.get("name") or "").lower(),
    ))
    return resp(headers, 200, {"ok": True, "members": members_out})


# ── GET /admin/members ────────────────────────────────────────────────────────

def list_members(event: dict, headers: dict, token: str) -> dict:
    qs = get_query(event)
    status = (qs.get("status") or "PENDING").upper()
    members = list_members_by_status(status=status)
    return resp(headers, 200, {"ok": True, "members": members, "total": len(members)})


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

    now = _now()
    ddb = boto3.resource("dynamodb")
    members_t = ddb.Table(os.getenv("MEMBERS_TABLE_NAME", "rsvp-members"))

    members_t.update_item(
        Key={"phone": phone},
        UpdateExpression=(
            "SET #status = :deleted, deletedAt = :now "
            "REMOVE #name, lastName, email, instagram, tags, smsOptIn"
        ),
        ExpressionAttributeNames={"#status": "status", "#name": "name"},
        ExpressionAttributeValues={":deleted": "DELETED", ":now": now},
    )

    # Tombstone all invite rows for this member, paginating through them all
    inv_t = invites_table()
    try:
        inv_kwargs: dict = {
            "IndexName": "phone-index",
            "KeyConditionExpression": DKey("phone").eq(phone),
        }
        while True:
            page = inv_t.query(**inv_kwargs)
            for inv in page.get("Items", []):
                inv_t.update_item(
                    Key={"eventId": inv["eventId"], "phone": phone},
                    UpdateExpression="SET #status = :deleted",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":deleted": "DELETED"},
                )
            last = page.get("LastEvaluatedKey")
            if not last:
                break
            inv_kwargs["ExclusiveStartKey"] = last
    except Exception:
        logger.exception("delete_member: failed to tombstone invites phone=...%s", phone[-4:])

    log_action(token=token, action=ACTION_MEMBER_DELETED, target_phone=phone)
    return resp(headers, 200, {"ok": True})


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

    set_status(phone, status)

    action = (ACTION_MEMBER_APPROVED if status == "APPROVED"
              else ACTION_MEMBER_DENIED if status == "DENIED"
              else ACTION_MEMBER_PENDING)
    log_action(token=token, action=action, target_phone=phone, metadata={"status": status})

    # On APPROVED transition: send Jade welcome (best-effort) only on the
    # *first* approval — guard on both prev_status and welcomeSentAt so a
    # re-approval of an already-approved member never re-sends.
    if status == "APPROVED" and prev_status != "APPROVED":
        try:
            # Signing up is the consent act — set smsOptIn=True on first approval
            # so maybe_send_welcome() doesn't skip on no_sms_opt_in.
            # This is a best-effort write; if it fails the welcome send will skip
            # (no_sms_opt_in) but that's preferable to silently sending without consent.
            set_sms_opt_in(phone, True)
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

def record_member_attendance(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    phone = (data.get("phone") or "").strip()
    event_id = (data.get("eventId") or "current").strip()
    attended = coerce_bool(data.get("attended", False))
    if not phone:
        return resp(headers, 400, {"ok": False, "error": "phone required"})

    # Guard: member must exist — prevent ghost records from typos or stale data
    existing = get_member(phone)
    if not existing:
        return resp(headers, 400, {"ok": False, "error": "member not found"})

    is_new = record_attendance(phone, attended, event_id=event_id)
    log_action(token=token, action=ACTION_ATTENDANCE,
               target_phone=phone,
               metadata={"attended": attended, "eventId": event_id,
                         "alreadyCheckedIn": not is_new})
    return resp(headers, 200, {"ok": True, "alreadyCheckedIn": not is_new})


# ── POST /admin/members/import ────────────────────────────────────────────────

def _batch_get_existing(members_t, phones: list) -> dict:
    """Fetch up to N existing member records in batch (25 per call). Returns {phone: item}."""
    table_name = members_t.name
    existing = {}
    for i in range(0, len(phones), 25):
        chunk = phones[i:i + 25]
        unprocessed = {table_name: {"Keys": [{"phone": p} for p in chunk]}}
        retries = 0
        while unprocessed and retries < 4:
            batch_resp = members_t.meta.client.batch_get_item(RequestItems=unprocessed)
            for item in batch_resp.get("Responses", {}).get(table_name, []):
                existing[item["phone"]] = item
            unprocessed = batch_resp.get("UnprocessedKeys") or {}
            retries += 1
    return existing


def import_members(event: dict, headers: dict, token: str) -> dict:
    data = get_body(event)
    members_to_import = data.get("members") or []
    if not members_to_import:
        return resp(headers, 400, {"ok": False, "error": "members array required"})

    import_status = (data.get("status") or "APPROVED").upper()
    if import_status not in ("APPROVED", "PENDING"):
        import_status = "APPROVED"

    imported = 0
    skipped = 0
    errors = []
    request_source = normalize_import_source(data.get("source") or "csv") or "csv"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    members_t = boto3.resource("dynamodb").Table(
        os.getenv("MEMBERS_TABLE_NAME", "rsvp-members")
    )

    # ── Step 1: normalize phones and collect valid rows ──────────────────────
    valid_rows = []
    for idx, row in enumerate(members_to_import):
        raw_phone = (row.get("phone") or "").strip()
        if not raw_phone:
            skipped += 1
            continue
        try:
            phone_e164 = normalize_phone(raw_phone)
            valid_rows.append((idx, phone_e164, row))
        except Exception as e:
            errors.append(f"row {idx}: bad phone — {e}")
            skipped += 1

    # ── Step 2: batch-fetch all existing records in one shot ─────────────────
    all_phones = [phone for _, phone, _ in valid_rows]
    existing_map = {}
    try:
        existing_map = _batch_get_existing(members_t, all_phones)
    except Exception as e:
        logger.warning("batch_get_existing failed, falling back to empty map: %s", e)

    # ── Step 3: write each member with a single update_item call ─────────────
    for idx, phone_e164, row in valid_rows:
        try:
            existing = existing_map.get(phone_e164) or {}

            first_name = (row.get("firstName") or row.get("name") or "").strip() or "Unknown"
            last_name  = (row.get("lastName") or "").strip() or None
            email      = (row.get("email") or "").strip() or None
            instagram  = (row.get("instagram") or "").strip() or None
            tags       = (row.get("tags") or "").strip() or None

            # smsOptIn: explicit row value > existing value > default True for CSV imports
            if "smsOptIn" in row and row.get("smsOptIn") is not None:
                raw_opt_in = row.get("smsOptIn")
                if isinstance(raw_opt_in, str):
                    sms_opt_in = raw_opt_in.strip().lower() not in ("false", "0", "no", "n", "")
                else:
                    sms_opt_in = bool(raw_opt_in)
            elif "smsOptIn" in existing:
                sms_opt_in = coerce_bool(existing.get("smsOptIn"))
            else:
                sms_opt_in = True

            existing_source = normalize_import_source(existing.get("source") or "")
            row_source      = normalize_import_source(row.get("source") or "")
            requested_source = row_source or request_source
            if existing_source and existing_source != "csv":
                source_val = existing_source
            else:
                source_val = requested_source or existing_source or "csv"

            # Single update_item covers everything upsert_member + status write used to do
            expr_names = {"#n": "name", "#s": "status", "#src": "source"}
            expr_vals: dict = {
                ":n":       first_name[:120],
                ":src":     (source_val or "csv")[:40],
                ":ls":      now,
                ":ca":      now,
                ":pending": "PENDING",
                ":status":  import_status,
                ":soi":     sms_opt_in,
            }
            set_parts = [
                "#n = :n",
                "#src = :src",
                "lastSeenAt = :ls",
                "submittedAt = :ls",
                "createdAt = if_not_exists(createdAt, :ca)",
                # Use import_status for new members; keep existing status for returning members
                # unless import_status is APPROVED (always upgrade on import)
                "#s = :status" if import_status == "APPROVED" else "#s = if_not_exists(#s, :pending)",
                "smsOptIn = :soi",
            ]

            if sms_opt_in:
                expr_vals[":oiat"] = now
                set_parts.append("smsOptInAt = if_not_exists(smsOptInAt, :oiat)")
            if last_name:
                expr_vals[":ln"] = last_name[:120]
                set_parts.append("lastName = :ln")
            if email:
                expr_vals[":e"] = email[:200]
                set_parts.append("email = :e")
            if tags:
                expr_vals[":tg"] = tags[:200]
                set_parts.append("tags = :tg")
            if instagram:
                expr_vals[":ig"] = instagram[:80]
                set_parts.append("instagram = :ig")

            members_t.update_item(
                Key={"phone": phone_e164},
                UpdateExpression="SET " + ", ".join(set_parts),
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_vals,
            )
            imported += 1

        except Exception as row_err:
            logger.error("import row %d phone=%s: %s", idx, row.get("phone"), row_err)
            errors.append(f"row {idx}: {type(row_err).__name__}")
            skipped += 1
            continue

    log_action(token=token, action=ACTION_MEMBER_IMPORTED,
               metadata={"imported": imported, "skipped": skipped,
                         "status": import_status, "errorCount": len(errors)})
    return resp(headers, 200, {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "status": import_status,
        "errors": errors[:10],
    })


# ── GET /admin/members/search ─────────────────────────────────────────────────

def search_members_route(event: dict, headers: dict, token: str) -> dict:
    qs = get_query(event)
    query = (qs.get("q") or "").strip()
    if not query:
        return resp(headers, 400, {"ok": False, "error": "q parameter required"})
    members = search_members(query, limit=50)
    return resp(headers, 200, {"ok": True, "members": members})
