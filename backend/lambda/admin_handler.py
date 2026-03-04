import boto3
from boto3.dynamodb.conditions import Key as DKey
from botocore.exceptions import ClientError
import json
from datetime import datetime, timezone
from decimal import Decimal
import logging
import os

from member_store import (
    list_members_by_status, set_status, set_gender, set_tier_override,
    record_attendance, upsert_member, normalize_phone,
    search_members,
)
from audit_log import (
    log_action,
    ACTION_MEMBER_APPROVED, ACTION_MEMBER_DENIED, ACTION_MEMBER_PENDING,
    ACTION_MEMBER_DELETED, ACTION_MEMBER_GENDER_SET, ACTION_MEMBER_TIER_SET,
    ACTION_ATTENDANCE, ACTION_EVENT_UPDATED, ACTION_MEMBER_IMPORTED,
)
from sms_adapter import get_secret_string

logger = logging.getLogger()


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _get_method(event: dict) -> str:
    if event.get("httpMethod"):
        return event["httpMethod"]
    rc = event.get("requestContext", {}).get("http", {})
    return rc.get("method", "")


def _get_headers(event: dict) -> dict:
    return event.get("headers") or {}


def _get_query(event: dict) -> dict:
    return event.get("queryStringParameters") or {}


def _allowed_origins():
    csv = os.getenv("ALLOWED_ORIGINS", "")
    return [o.strip() for o in csv.split(",") if o.strip()]


def _pick_origin(headers: dict) -> str:
    origin = (headers.get("origin") or headers.get("Origin") or "").strip()
    allowed = _allowed_origins()
    if origin and origin in allowed:
        return origin
    return allowed[0] if allowed else "*"


def _cors_headers(headers: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": _pick_origin(headers),
        "Access-Control-Allow-Headers": "content-type,x-admin-token",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Vary": "Origin",
    }


def _resp(headers: dict, status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": _cors_headers(headers),
        "body": json.dumps(body, cls=DecimalEncoder) if not isinstance(body, str) else body,
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


def _events_table():
    ddb = boto3.resource("dynamodb")
    name = os.getenv("EVENTS_TABLE_NAME", "rsvp-events")
    return ddb.Table(name)


def get_current_event():
    resp = _events_table().get_item(Key={"eventId": "current"})
    return resp.get("Item")


def set_current_event(data: dict):
    raw_capacity = data.get("capacity")
    try:
        capacity = int(raw_capacity or 0)
        if capacity < 0:
            raise ValueError("negative")
    except (ValueError, TypeError):
        raise ValueError(f"capacity must be a non-negative integer, got: {raw_capacity!r}")

    # notes (internal operational notes) removed — use description for Jade's brief
    shared_reminder = (data.get("reminder_template") or "").strip()
    day_before_template = (data.get("day_before_template") or shared_reminder).strip()
    day_of_template = (data.get("day_of_template") or shared_reminder).strip()

    item = {
        "eventId":           "current",
        "updatedAt":         datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eventSlug":         (data.get("eventSlug") or "").strip(),
        "date":              (data.get("date") or "").strip(),
        "venue":             (data.get("venue") or "").strip(),
        "dresscode":         (data.get("dresscode") or "").strip(),
        "capacity":          capacity,
        "city":              (data.get("city") or "").strip(),
        "event_timezone":    (data.get("event_timezone") or "America/New_York").strip() or "America/New_York",
        "address":           (data.get("address") or "").strip(),
        "revealVenue":       bool(data.get("revealVenue", False)),
        "vibe_tag":          (data.get("vibe_tag") or "").strip(),
        "event_label":       (data.get("event_label") or "").strip(),
        "startTime":         (data.get("startTime") or "").strip(),
        "reminderTiming":    (data.get("reminderTiming") or "manual").strip(),
        "description":       (data.get("description") or "").strip(),
        "event_type":        (data.get("event_type") or "").strip(),
        "invite_template":   (data.get("invite_template") or "").strip(),
        "reminder_template": shared_reminder,
        "day_before_template": day_before_template,
        "day_of_template":     day_of_template,
    }
    _events_table().put_item(Item=item)
    return item


def handler(event, context):
    headers = {}
    try:
        method = _get_method(event).upper()
        headers = _get_headers(event)

        if method == "OPTIONS":
            return _resp(headers, 200, {"ok": True})

        path = event.get("path", "")

        # Public current-event endpoints do not require the admin token.
        if method == "GET" and (path.endswith("/event") or path.endswith("/event/current")) and not path.endswith("/admin/event"):
            ev = get_current_event()
            if ev:
                reveal = bool(ev.get("revealVenue", False))
                hidden = {"venue", "address"} if not reveal else set()
                public = {k: v for k, v in ev.items() if k not in hidden}
            else:
                public = {}
            return _resp(headers, 200, {"ok": True, "event": public})

        token = (headers.get("x-admin-token") or headers.get("X-Admin-Token") or "").strip()
        if not token or token != _admin_token():
            return _resp(headers, 401, {"ok": False, "error": "unauthorized"})

        # ── GET /admin/members/confirmed ──────────────────────────────────────
        if method == "GET" and "/admin/members/confirmed" in path:
            ddb = boto3.resource("dynamodb")
            invites_t = ddb.Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))
            qs = event.get("queryStringParameters") or {}
            event_id = (qs.get("eventId") or "current").strip() or "current"
            members_table_name = os.getenv("MEMBERS_TABLE_NAME", "rsvp-members")
            members_t = ddb.Table(members_table_name)

            # Paginate invite query to handle large events
            confirmed_invites = []
            kwargs: dict = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
            while True:
                resp = invites_t.query(**kwargs)
                confirmed_invites.extend(
                    [i for i in resp.get("Items", []) if i.get("status") == "CONFIRMED"]
                )
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last

            # BatchGetItem replaces N+1 individual reads — 200 confirmed = 2 batch calls
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
                        member_map[item["phone"]] = item
                    # Retry any unprocessed keys (DDB throttle)
                    unprocessed = batch_resp.get("UnprocessedKeys", {})
                    if unprocessed:
                        retry_resp = members_t.meta.client.batch_get_item(
                            RequestItems=unprocessed
                        )
                        for item in retry_resp.get("Responses", {}).get(members_table_name, []):
                            member_map[item["phone"]] = item
                except Exception:
                    logger.exception("confirmed: batch_get_item failed for batch starting i=%d", i)

            members_out = []
            for invite in confirmed_invites:
                phone = invite.get("phone", "")
                if not phone:
                    continue
                m = member_map.get(phone) or {}
                members_out.append({
                    "phone":       phone,
                    "name":        m.get("name", ""),
                    "lastName":    m.get("lastName", ""),
                    "gender":      m.get("gender", ""),
                    "confirmedAt": invite.get("confirmedAt", ""),
                    "attendedAt":  invite.get("attendedAt", ""),
                    "checkedIn":   bool(invite.get("attendedAt")),
                })

            members_out.sort(key=lambda x: (
                (x.get("lastName") or x.get("name") or "").lower(),
                (x.get("name") or "").lower(),
            ))
            return _resp(headers, 200, {"ok": True, "members": members_out})

        # ── GET /admin/members ────────────────────────────────────────────────
        if method == "GET" and path.endswith("/admin/members"):
            qs = _get_query(event)
            status = (qs.get("status") or "PENDING").upper()
            members = list_members_by_status(status=status)
            return _resp(headers, 200, {"ok": True, "members": members})

        # ── DELETE /admin/members ─────────────────────────────────────────────
        # Soft-delete: wipes PII, sets status=DELETED. Tombstones all invite
        # records for this member so they are excluded from future blasts and
        # analytics. Checkin records are left intact as attendance history.
        if method == "DELETE" and path.endswith("/admin/members"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = normalize_phone((data.get("phone") or "").strip())
            if not phone:
                return _resp(headers, 400, {"ok": False, "error": "phone required"})

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
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

            invites_t = ddb.Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))
            try:
                invite_resp = invites_t.query(
                    IndexName="phone-index",
                    KeyConditionExpression=DKey("phone").eq(phone),
                )
                for inv in invite_resp.get("Items", []):
                    invites_t.update_item(
                        Key={"eventId": inv["eventId"], "phone": phone},
                        UpdateExpression="SET #status = :deleted",
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={":deleted": "DELETED"},
                    )
            except Exception:
                logger.exception("delete: failed to tombstone invites for phone=...%s", phone[-4:])

            log_action(token=token, action=ACTION_MEMBER_DELETED, target_phone=phone)
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/status ────────────────────────────────────────
        if method == "POST" and path.endswith("/admin/members/status"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            status = (data.get("status") or "").strip().upper()
            if not phone or status not in ("PENDING", "APPROVED", "DENIED"):
                return _resp(headers, 400, {"ok": False, "error": "phone and valid status required"})
            set_status(phone, status)
            action = (ACTION_MEMBER_APPROVED if status == "APPROVED"
                      else ACTION_MEMBER_DENIED if status == "DENIED"
                      else ACTION_MEMBER_PENDING)
            log_action(token=token, action=action, target_phone=phone,
                       metadata={"status": status})
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/gender ────────────────────────────────────────
        if method == "POST" and path.endswith("/admin/members/gender"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            gender = (data.get("gender") or "").strip().upper()
            if not phone or gender not in ("M", "F", "O"):
                return _resp(headers, 400, {"ok": False, "error": "phone and gender (M/F/O) required"})
            set_gender(phone, gender)
            log_action(token=token, action=ACTION_MEMBER_GENDER_SET,
                       target_phone=phone, metadata={"gender": gender})
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/tier ──────────────────────────────────────────
        if method == "POST" and path.endswith("/admin/members/tier"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            tier = data.get("tier")
            if not phone or tier is None:
                return _resp(headers, 400, {"ok": False, "error": "phone and tier required"})
            set_tier_override(phone, int(tier))
            log_action(token=token, action=ACTION_MEMBER_TIER_SET,
                       target_phone=phone, metadata={"tier": int(tier)})
            return _resp(headers, 200, {"ok": True})

        # ── POST /admin/members/attendance ────────────────────────────────────
        if method == "POST" and path.endswith("/admin/members/attendance"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            phone = (data.get("phone") or "").strip()
            event_id = (data.get("eventId") or "current").strip()
            attended = bool(data.get("attended", False))
            if not phone:
                return _resp(headers, 400, {"ok": False, "error": "phone required"})
            is_new = record_attendance(phone, attended, event_id=event_id)
            log_action(token=token, action=ACTION_ATTENDANCE,
                       target_phone=phone,
                       metadata={"attended": attended, "eventId": event_id,
                                 "alreadyCheckedIn": not is_new})
            return _resp(headers, 200, {"ok": True, "alreadyCheckedIn": not is_new})

        # ── POST /admin/members/import ────────────────────────────────────────
        if method == "POST" and path.endswith("/admin/members/import"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            members_to_import = data.get("members") or []
            if not members_to_import:
                return _resp(headers, 400, {"ok": False, "error": "members array required"})

            import_status = (data.get("status") or "APPROVED").upper()
            if import_status not in ("APPROVED", "PENDING"):
                import_status = "APPROVED"

            imported = 0
            skipped = 0
            errors = []

            members_t = boto3.resource("dynamodb").Table(
                os.getenv("MEMBERS_TABLE_NAME", "rsvp-members")
            )

            for idx, row in enumerate(members_to_import):
                try:
                    raw_phone = (row.get("phone") or "").strip()
                    if not raw_phone:
                        skipped += 1
                        continue

                    phone_e164 = normalize_phone(raw_phone)
                    first_name = (row.get("name") or "").strip() or "Unknown"
                    last_name = (row.get("lastName") or "").strip() or None
                    email = (row.get("email") or "").strip() or None
                    instagram = (row.get("instagram") or "").strip() or None
                    tags = (row.get("tags") or "").strip() or None
                    # Parse smsOptIn safely — CSV values arrive as strings.
                    # bool("false") == True in Python, so we check string values explicitly.
                    raw_opt_in = row.get("smsOptIn", True)
                    if isinstance(raw_opt_in, str):
                        sms_opt_in = raw_opt_in.strip().lower() not in ("false", "0", "no", "n", "")
                    else:
                        sms_opt_in = bool(raw_opt_in)

                    upsert_member(
                        phone=phone_e164,
                        name=first_name,
                        last_name=last_name,
                        email=email,
                        source="import",
                        sms_opt_in=sms_opt_in,
                        tags=tags,
                    )

                    update_expr = "SET #status = :status"
                    expr_names = {"#status": "status"}
                    expr_vals: dict = {":status": import_status}
                    if instagram:
                        update_expr += ", instagram = :ig"
                        expr_vals[":ig"] = instagram[:80]

                    members_t.update_item(
                        Key={"phone": phone_e164},
                        UpdateExpression=update_expr,
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
            return _resp(headers, 200, {
                "ok": True,
                "imported": imported,
                "skipped": skipped,
                "status": import_status,
                "errors": errors[:10],
            })

        # ── GET /admin/members/search ─────────────────────────────────────────
        if method == "GET" and path.endswith("/admin/members/search"):
            qs = _get_query(event)
            query = (qs.get("q") or "").strip()
            if not query:
                return _resp(headers, 400, {"ok": False, "error": "q parameter required"})
            members = search_members(query, limit=50)
            return _resp(headers, 200, {"ok": True, "members": members})

        # ── GET /admin/event/analytics ────────────────────────────────────────
        # Must be checked before /admin/event so endswith("/admin/event") below
        # cannot shadow it.
        if method == "GET" and "/admin/event/analytics" in path:
            ddb = boto3.resource("dynamodb")
            invites_t = ddb.Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))

            qs = event.get("queryStringParameters") or {}
            event_id = (qs.get("eventId") or "current").strip() or "current"

            items = []
            kwargs: dict = {"KeyConditionExpression": DKey("eventId").eq(event_id)}
            while True:
                resp = invites_t.query(**kwargs)
                items.extend(resp.get("Items", []))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last

            # Fix #19: exclude DELETED records from all totals
            # Fix #20: tier-3 bucket added
            # Fix #21: no_response only counts genuinely unreplied INVITED records
            totals = {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0}
            by_gender = {
                "M": {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
                "F": {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
                "O": {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
            }
            by_tier = {
                1: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
                2: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
                3: {"invited": 0, "confirmed": 0, "declined": 0, "attended": 0},
            }
            by_wave = {
                1: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0},
                2: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0},
                3: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0},
                0: {"invited": 0, "confirmed": 0, "declined": 0, "no_response": 0, "attended": 0},
            }

            for item in items:
                status = item.get("status", "INVITED")

                # Fix #19: skip tombstoned records entirely
                if status == "DELETED":
                    continue

                gender = (item.get("gender") or "O").upper()
                if gender not in ("M", "F"):
                    gender = "O"

                tier = int(item.get("tier", 1))
                # Fix #20: tier-3 gets its own bucket instead of being absorbed into tier-1
                if tier not in (1, 2, 3):
                    tier = 1

                wave = int(item.get("waveNumber", 0) or 0)
                if wave not in (1, 2, 3):
                    wave = 0

                attended = bool(item.get("attendedAt"))

                totals["invited"] += 1
                if status == "CONFIRMED":
                    totals["confirmed"] += 1
                elif status == "DECLINED":
                    totals["declined"] += 1
                elif status == "INVITED":
                    # Fix #21: only genuine pending INVITED records count as no_response
                    totals["no_response"] += 1
                # Any other unknown status is silently excluded from rates

                if attended:
                    totals["attended"] += 1

                w = by_wave[wave]
                w["invited"] += 1
                if status == "CONFIRMED":
                    w["confirmed"] += 1
                elif status == "DECLINED":
                    w["declined"] += 1
                elif status == "INVITED":
                    w["no_response"] += 1
                if attended:
                    w["attended"] += 1

                g = by_gender[gender]
                g["invited"] += 1
                if status == "CONFIRMED":
                    g["confirmed"] += 1
                elif status == "DECLINED":
                    g["declined"] += 1
                if attended:
                    g["attended"] += 1

                t = by_tier[tier]
                t["invited"] += 1
                if status == "CONFIRMED":
                    t["confirmed"] += 1
                elif status == "DECLINED":
                    t["declined"] += 1
                if attended:
                    t["attended"] += 1

            def rate(n, d):
                return round(n / d * 100, 1) if d else 0

            summary = {
                "totals": totals,
                "rates": {
                    "confirm_rate": rate(totals["confirmed"], totals["invited"]),
                    "decline_rate": rate(totals["declined"], totals["invited"]),
                    "show_rate":    rate(totals["attended"], totals["confirmed"]),
                    "ghost_rate":   rate(totals["confirmed"] - totals["attended"], totals["confirmed"]),
                },
                "by_gender": by_gender,
                "by_tier":   by_tier,
                "by_wave":   by_wave,
                "total_records": len(items),
            }
            return _resp(headers, 200, {"ok": True, "eventId": event_id, "analytics": summary})

        # ── GET /admin/event ──────────────────────────────────────────────────
        if method == "GET" and path.endswith("/admin/event"):
            ev = get_current_event()
            return _resp(headers, 200, {"ok": True, "event": ev or {}})

        # ── POST /admin/event ─────────────────────────────────────────────────
        if method == "POST" and path.endswith("/admin/event"):
            raw_body = event.get("body") or ""
            data = json.loads(raw_body) if raw_body else {}
            try:
                ev = set_current_event(data)
            except ValueError as ve:
                return _resp(headers, 400, {"ok": False, "error": str(ve)})
            log_action(token=token, action=ACTION_EVENT_UPDATED,
                       metadata={"date": ev.get("date"), "venue": ev.get("venue"),
                                 "capacity": ev.get("capacity"), "eventSlug": ev.get("eventSlug")})
            return _resp(headers, 200, {"ok": True, "event": ev})

        return _resp(headers, 404, {"ok": False, "error": "not found"})

    except Exception:
        logger.exception("Unhandled error in admin_handler")
        return _resp(headers, 500, {"ok": False, "error": "An internal error occurred"})
