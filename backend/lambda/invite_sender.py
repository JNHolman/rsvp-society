import json
import os
import time

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeSerializer
from admin_shared import coerce_bool
from capacity_policy import expected_show_rate


def reserve_invite(invites, members, item: dict, expected_status: str | None = None) -> None:
    """Atomically reserve only while the member and event remain eligible."""
    av = TypeSerializer().serialize
    member = members.get_item(Key={"phone": item["phone"]}, ConsistentRead=True).get("Item") or {}
    if member.get("status") != "APPROVED" or coerce_bool(member.get("optOut", False)) or (member.get("smsOptIn") is not None and not coerce_bool(member["smsOptIn"])):
        raise ValueError("Member is no longer eligible for SMS")
    member_condition = "#s = :approved"
    member_values = {":approved": av("APPROVED")}
    for field in ("optOut", "smsOptIn"):
        if field in member:
            member_condition += f" AND {field} = :{field}"
            member_values[f":{field}"] = av(member[field])
        else:
            member_condition += f" AND attribute_not_exists({field})"
    put = {"TableName": invites.name, "Item": {k: av(v) for k, v in item.items()},
           "ConditionExpression": "attribute_not_exists(phone)"}
    if expected_status is not None:
        put.update(ConditionExpression="#s = :expected",
                   ExpressionAttributeNames={"#s": "status"},
                   ExpressionAttributeValues={":expected": av(expected_status)})
    try:
        boto3.client("dynamodb").transact_write_items(TransactItems=[
            {"Put": put},
            {"ConditionCheck": {
                "TableName": members.name, "Key": {"phone": av(item["phone"])},
                "ConditionExpression": member_condition,
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": member_values,
            }},
            {"ConditionCheck": {
                "TableName": os.getenv("EVENTS_TABLE_NAME", "rsvp-events"),
                "Key": {"eventId": av(item["eventId"])},
                "ConditionExpression": "event_status = :live AND (attribute_not_exists(archived) OR archived = :false) AND (attribute_not_exists(hardDeleting) OR hardDeleting = :false)",
                "ExpressionAttributeValues": {":live": av("LIVE"), ":false": av(False)},
            }},
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            reasons = exc.response.get("CancellationReasons") or []
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed" and all(r.get("Code") in (None, "None") for r in reasons[1:]):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "Invite already exists or changed"}}, "PutItem") from exc
        raise


def _get_members_for_locked_phones(phones: list[str], members_t, *, calc_tier, logger) -> list[dict]:
    """Batch-read only a locked-send continuation's remaining recipients."""
    client = members_t.meta.client
    table_name = members_t.name
    found = {}
    for start in range(0, len(phones), 100):
        pending = {table_name: {"Keys": [{"phone": phone} for phone in phones[start:start + 100]]}}
        for _attempt in range(4):
            if not pending:
                break
            try:
                response = client.batch_get_item(RequestItems=pending)
                for member in response.get("Responses", {}).get(table_name, []):
                    found[member.get("phone")] = member
                pending = response.get("UnprocessedKeys") or {}
            except Exception:
                logger.exception("invite continuation: selected-member batch read failed")
                raise
        if pending:
            raise RuntimeError("invite continuation: selected-member batch read left unprocessed keys")

    eligible = []
    for phone in phones:
        member = found.get(phone)
        if not member or (member.get("status") or "").upper() != "APPROVED":
            continue
        if coerce_bool(member.get("optOut", False)):
            continue
        if member.get("smsOptIn") is not None and not coerce_bool(member.get("smsOptIn")):
            continue
        member["_tier"] = calc_tier(member)
        eligible.append(member)
    return eligible


def execute_send(body: dict, origin: str, token: str, job_id: str, *, deps: dict):
    ACTION_INVITE_SENT = deps['ACTION_INVITE_SENT']
    MANUAL_WAVE_NUMBER = deps['MANUAL_WAVE_NUMBER']
    _RETRYABLE_STATUSES_IMPORTED = deps['_RETRYABLE_STATUSES_IMPORTED']
    _apply_audience_filters = deps['_apply_audience_filters']
    _event_promotion_geography = deps['_event_promotion_geography']
    _assert_formal_wave_available = deps['_assert_formal_wave_available']
    _build_invite_list = deps['_build_invite_list']
    _build_sms_message = deps['_build_sms_message']
    _execute_confirmed_update = deps['_execute_confirmed_update']
    _get_analytics = deps['_get_analytics']
    _get_approved_members = deps['_get_approved_members']
    _get_existing_invited_phones = deps['_get_existing_invited_phones']
    _get_next_wave_number = deps['_get_next_wave_number']
    _invite_message_metadata = deps['_invite_message_metadata']
    _invites_table = deps['_invites_table']
    _now_iso = deps['_now_iso']
    _resolve_active_invitable_event = deps['_resolve_active_invitable_event']
    _resolve_wave_capacity = deps['_resolve_wave_capacity']
    _update_job = deps['_update_job']
    _validate_initial_invite_text = deps['_validate_initial_invite_text']
    coerce_bool = deps['coerce_bool']
    log_action = deps['log_action']
    members_table = deps['members_table']
    normalize_phone = deps['normalize_phone']
    send_sms = deps['send_sms']
    logger = deps['logger']
    calc_tier = deps['calc_tier']
    """Core blast execution — extracted from handle_send for async use."""
    event_id   = (body.get("eventId") or "").strip()

    # ── Guard: active event must match submitted eventId before sending ───────
    try:
        current_ev = _resolve_active_invitable_event(event_id)
    except ValueError:
        raise
    except Exception:
        logger.exception("invite: failed to check event state before blast event=%s", event_id)
        raise ValueError("Could not verify event state — blast aborted")
    # ─────────────────────────────────────────────────────────────────────────

    if coerce_bool(body.get("confirmedUpdate", False)):
        _execute_confirmed_update(body, current_ev, token, job_id)
        return

    capacity   = int(body.get("capacity") or 0)
    wave_number = int(body.get("waveNumber") or 0)
    if wave_number < 1:
        wave_number = MANUAL_WAVE_NUMBER if coerce_bool(body.get("manualSend", False)) else _get_next_wave_number(event_id)
    if wave_number > 0:
        _assert_formal_wave_available(wave_number)
    wave_size   = int(body.get("waveSize") or 0)
    removed_phones = body.get("removedPhones") or []
    message_override = (body.get("messageOverride") or "").strip()
    _validate_initial_invite_text(message_override or (current_ev.get("invite_template") or ""), current_ev)

    # selected_phones: normalize each phone, skip malformed ones rather than crashing the send
    raw_phones = body.get("phones") or []
    selected_phones = []
    for p in raw_phones:
        if p:
            try:
                selected_phones.append(normalize_phone(p))
            except ValueError:
                logger.warning("handle_send: skipping malformed phone in selected list (length=%d)", len(str(p)))

    # Fix #29: validate these server-side — don't trust client values
    try:
        female_pct   = int(body.get("femalePercent") or 60)
    except (ValueError, TypeError):
        raise ValueError("femalePercent must be an integer")

    if not (0 <= female_pct <= 100):
        raise ValueError("femalePercent must be 0–100")
    if not event_id or capacity < 1:
        raise ValueError("eventId and capacity required")

    progress = body.get("_continuationProgress") or {}
    is_server_continuation = bool(body.get("_serverContinuation"))

    # Fix C5: wave 2+ must use live analytics to compute effective_capacity,
    # matching exactly what handle_preview showed the admin.
    # Initial send resolves the full wave once. Later invocations only batch-read
    # the locked remainder; reserve_invite still checks current consent/status and
    # atomically rejects an invite row that already exists.
    existing_invites = set() if is_server_continuation else _get_existing_invited_phones(event_id)

    if wave_number >= 2 and not wave_size:
        analytics = _get_analytics(event_id)
        confirmed_count = analytics.get("confirmedHeadcount", analytics.get("confirmed", 0) + analytics.get("plusOneRisk", 0))
        actual_confirm_rate = analytics.get("confirmRate")
        actual_show_rate = expected_show_rate(current_ev)
        responses = analytics.get("confirmed", 0) + analytics.get("declined", 0)
        effective_capacity = _resolve_wave_capacity(
            capacity, wave_number, wave_size,
            confirmed=confirmed_count,
            already_invited=len(existing_invites),
            actual_confirm_rate=actual_confirm_rate,
            actual_show_rate=actual_show_rate,
            responses=responses,
        )
    else:
        effective_capacity = _resolve_wave_capacity(capacity, wave_number, wave_size)

    if is_server_continuation and selected_phones:
        selected_members = _get_members_for_locked_phones(
            selected_phones, members_table(), calc_tier=calc_tier, logger=logger
        )
        result = {"members": selected_members, "summary": {}}
    else:
        approved_members = _get_approved_members()
        available_members = [m for m in approved_members if m.get("phone") not in existing_invites]

    if not (is_server_continuation and selected_phones) and selected_phones:
        # The preview-selected phone list is authoritative. Re-check only hard
        # safety gates (approval / STOP / explicit smsOptIn false / already
        # invited) via _get_approved_members + existing_invites. Do not re-run
        # mutable audience filters such as market, tier, gender, or search text.
        full_member_map = {}
        for member in available_members:
            raw_phone = member.get("phone")
            if not raw_phone:
                continue
            try:
                full_member_map[normalize_phone(raw_phone)] = member
            except ValueError:
                continue
        selected_members = [full_member_map[p] for p in selected_phones if p in full_member_map]
        result = {"members": selected_members, "summary": {}}
    elif not (is_server_continuation and selected_phones):
        if not _event_promotion_geography(current_ev):
            raise ValueError("Event ZIP code and promotion radius are required before building an invite audience")
        members_list = _apply_audience_filters(available_members, body.get("audienceFilters") or {}, current_ev)
        result = _build_invite_list(members_list, effective_capacity, female_pct, removed_phones, wave_number)
        selected_members = result["members"]

    # Bound one Lambda execution so pacing/retries cannot consume the full timeout.
    # Any remainder is self-invoked on the same job with cumulative progress.
    try:
        # Each invitation can make up to three provider attempts. Quo requests
        # may each take 15 seconds, plus rate-limit backoff, so a 250-recipient
        # batch can exceed the Lambda's 300-second timeout by hours. Four keeps
        # the worst provider-wait bound under four minutes with room for DDB.
        max_per_invocation = min(4, max(1, int(os.getenv("INVITE_MAX_PER_INVOCATION", "4"))))
    except (TypeError, ValueError):
        max_per_invocation = 4
    remainder_members = selected_members[max_per_invocation:]
    selected_members = selected_members[:max_per_invocation]
    remainder_phones = []
    for member in remainder_members:
        raw_phone = member.get("phone")
        if not raw_phone:
            continue
        try:
            remainder_phones.append(normalize_phone(raw_phone))
        except ValueError:
            continue

    current_event = current_ev
    invites_t = _invites_table()
    members_t = members_table()
    now = _now_iso()

    sms_enabled = (os.getenv("SMS_ENABLED", "false") or "").lower() == "true"
    message_meta = _invite_message_metadata(current_ev, message_override)
    sent            = 0
    failed          = 0
    invites_written = 0
    already_invited = 0   # initialized here, incremented when member already has real invite

    for m in selected_members:
        phone = normalize_phone(m.get("phone", ""))
        if not phone:
            continue

        try:
            # Statuses that should NOT block a new invite attempt
            _RETRYABLE_STATUSES = _RETRYABLE_STATUSES_IMPORTED  # centralized in member_store

            new_invite_item = {
                "eventId":    event_id,
                "phone":      phone,
                "status":     "INVITED",
                "gender":     m.get("gender", ""),
                "tier":       m["_tier"],
                "invitedAt":  now,
                "waveNumber": wave_number,
                "waveSentAt": now,
                "name":       m.get("name", ""),
                "lastName":   m.get("lastName", ""),
                "eventSlug":  event_id,
                "eventLabel": current_event.get("event_label") or current_event.get("label") or event_id,
                "jobId":      job_id,
                "smsSendStatus": "PENDING",
                **message_meta,
            }

            try:
                reserve_invite(invites_t, members_t, new_invite_item)
                invites_written += 1
            except ClientError as ce:
                if ce.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                # Row exists — check if it's a retryable status or a real invite
                try:
                    existing = invites_t.get_item(
                        Key={"eventId": event_id, "phone": phone},
                        ProjectionExpression="#s",
                        ExpressionAttributeNames={"#s": "status"},
                    ).get("Item", {})
                    existing_status = (existing.get("status") or "").upper()
                except Exception:
                    existing_status = "INVITED"  # assume real invite if lookup fails

                if existing_status in _RETRYABLE_STATUSES:
                    # Previous attempt never reached them — overwrite with fresh invite
                    reserve_invite(invites_t, members_t, new_invite_item, existing_status)
                    invites_written += 1
                    logger.info(
                        "invite: overwrote %s row with fresh INVITED phone=...%s",
                        existing_status, phone[-4:],
                    )
                else:
                    # Genuinely already invited — skip silently, track count
                    already_invited += 1
                    continue

            sms_succeeded = False
            if sms_enabled:
                message = _build_sms_message(m, current_event, message_override)
                msg_id = None
                for attempt in range(3):
                    try:
                        msg_id = send_sms(phone, message)
                        sent += 1
                        sms_succeeded = True
                        break
                    except RuntimeError as sms_err:
                        err_str = str(sms_err)
                        if "429" in err_str or "rate" in err_str.lower():
                            wait = (attempt + 1) * 1.5
                            logger.warning(
                                "invite send rate limited, waiting %.1fs phone=...%s attempt=%d",
                                wait, phone[-4:], attempt + 1,
                            )
                            time.sleep(wait)
                            continue
                        # BUG-1 fix: a non-rate-limit send error must NOT raise out of the loop.
                        # Raising left the invite row stuck at INVITED, after which the member was
                        # skipped on every retry and excluded from future waves (silently dropped).
                        # Stop retrying this number; the post-loop check marks it FAILED so the
                        # retryable-status path re-sends a fresh invite on the next wave/run.
                        logger.exception(
                            "invite send non-retryable error phone=...%s — will mark FAILED",
                            phone[-4:],
                        )
                        break

                # Post-loop: if no attempt succeeded (rate-limit exhausted OR non-retryable error),
                # mark the row FAILED. FAILED is in the retryable set, so the member is recoverable
                # and is never stranded at INVITED-without-SMS.
                if not sms_succeeded:
                    failed += 1
                    logger.error("invite send failed phone=...%s — marking FAILED", phone[-4:])
                    try:
                        invites_t.update_item(
                            Key={"eventId": event_id, "phone": phone},
                            UpdateExpression="SET #s = :failed, smsSendStatus = :failed",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":failed": "FAILED", ":job": job_id, ":invited": "INVITED"},
                            ConditionExpression="attribute_exists(phone) AND jobId = :job AND #s = :invited",
                        )
                    except Exception:
                        logger.exception("invite: failed to mark FAILED status phone=...%s", phone[-4:])
                if msg_id:
                    try:
                        invites_t.update_item(
                            Key={"eventId": event_id, "phone": phone},
                            UpdateExpression="SET quoMessageId = :mid, messageProvider = :provider, messageTrackedAt = :now, smsSendStatus = :sent",
                            ExpressionAttributeValues={":mid": msg_id, ":provider": "quo", ":now": _now_iso(), ":sent": "SENT", ":job": job_id, ":deleted": "DELETED"},
                            ExpressionAttributeNames={"#s": "status"},
                            ConditionExpression="attribute_exists(phone) AND jobId = :job AND #s <> :deleted",
                        )
                    except Exception:
                        logger.exception("invite send: failed to store msg_id phone=...%s", phone[-4:])
                time.sleep(0.25)
            else:
                sms_succeeded = True  # SMS disabled, invite row is still valid

            # Only increment invitedCount after the invite row is written and SMS succeeded.
            # This prevents count drift when sends fail and rows get cleaned up.
            if sms_succeeded:
                # Quo accepting the SMS is the primary operation. invitedCount is
                # secondary bookkeeping and must never erase/reopen a delivered invite.
                try:
                    members_t.update_item(
                        Key={"phone": phone},
                        UpdateExpression="SET invitedCount = if_not_exists(invitedCount, :zero) + :one, lastSeenAt = :now",
                        ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now, ":approved": "APPROVED"},
                        ExpressionAttributeNames={"#s": "status"},
                        ConditionExpression="attribute_exists(phone) AND #s = :approved",
                    )
                except Exception:
                    logger.exception(
                        "invite send: SMS succeeded but invitedCount update failed phone=...%s",
                        phone[-4:],
                    )

        except Exception:
            failed += 1
            logger.exception("invite send failed event=%s phone=...%s", event_id, phone[-4:])
            # Delete the invite row so a re-run of the blast can retry this member.
            # invitedCount has NOT been incremented yet (it only increments after
            # successful send), so cleanup is clean — no counter drift.
            try:
                invites_t.delete_item(
                    Key={"eventId": event_id, "phone": phone},
                    ConditionExpression="jobId = :job AND #s = :invited AND smsSendStatus = :pending",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":job": job_id, ":invited": "INVITED", ":pending": "PENDING"},
                )
            except Exception:
                logger.exception("invite send: failed to clean up invite row phone=...%s", phone[-4:])
            continue

    result["summary"]["waveNumber"] = wave_number
    result["summary"]["waveSize"]   = effective_capacity

    cumulative = {
        "queued": int(progress.get("queued") or 0) + len(selected_members),
        "invitesWritten": int(progress.get("invitesWritten") or 0) + invites_written,
        "smsSent": int(progress.get("smsSent") or 0) + sent,
        "failed": int(progress.get("failed") or 0) + failed,
        "alreadyInvited": int(progress.get("alreadyInvited") or 0) + already_invited,
    }

    if remainder_phones:
        continuation_body = dict(body)
        continuation_body["phones"] = remainder_phones
        continuation_body["autoWave"] = False
        continuation_body["lockedWave"] = True
        continuation_body["_continuationProgress"] = cumulative
        continuation_body["_serverContinuation"] = True
        continuation_body["waveSize"] = effective_capacity
        _update_job(job_id, {
            "status": "PROCESSING",
            "invitesWritten": cumulative["invitesWritten"],
            "smsSent": cumulative["smsSent"],
            "failed": cumulative["failed"],
            "eventId": event_id,
            "waveNumber": wave_number,
            "breakdown": json.dumps(cumulative),
            "message": f"Continuing invite send: {len(remainder_phones)} recipients remaining",
        })
        try:
            fn_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME", "rsvp-invite-handler")
            boto3.client("lambda").invoke(
                FunctionName=fn_name,
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
            logger.exception("invite continuation invoke failed job_id=%s", job_id)
            _update_job(job_id, {
                "status": "FAILED",
                "completedAt": _now_iso(),
                "error": f"continuation invoke failed: {str(exc)[:300]}",
            })
            raise
        return

    # Final chunk: write cumulative blast stats to the exact event record used for analytics.
    try:
        events_t = boto3.resource("dynamodb").Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))
        stat_values = {
            ":now": _now_iso(),
            ":sent": cumulative["smsSent"],
            ":failed": cumulative["failed"],
            ":wave": wave_number,
            ":zero": 0,
        }
        stat_expr = (
            "SET lastBlastAt = :now, "
            "lastBlastSmsSent = :sent, "
            "lastBlastFailed = :failed, "
            "lastBlastWave = :wave, "
            "lastBlastDeliveredCount = :zero"
        )
        events_t.update_item(Key={"eventId": event_id}, UpdateExpression=stat_expr, ExpressionAttributeValues=stat_values, ConditionExpression="attribute_exists(eventId) AND attribute_not_exists(hardDeleting)")
    except Exception:
        logger.exception("invite send: failed to write blast stats to event record")

    log_action(
        token=token,
        action=ACTION_INVITE_SENT,
        metadata={
            "eventId":        event_id,
            "waveNumber":     wave_number,
            "waveSize":       effective_capacity,
            "invitesWritten": cumulative["invitesWritten"],
            "smsSent":        cumulative["smsSent"],
            "failed":         cumulative["failed"],
            "lockedWave":     coerce_bool(body.get("lockedWave", False)),
            "manualOverride": bool(message_override),
        },
    )

    _update_job(job_id, {
        "status":         "COMPLETE",
        "completedAt":    _now_iso(),
        "invitesWritten": cumulative["invitesWritten"],
        "smsSent":        cumulative["smsSent"],
        "failed":         cumulative["failed"],
        "eventId":        event_id,
        "waveNumber":     wave_number,
        "breakdown": json.dumps(cumulative),
        "message": "Invite send complete",
    })

    schedule_auto_wave = deps.get("_schedule_auto_wave")
    if (callable(schedule_auto_wave) and wave_number in (1, 2) and cumulative["smsSent"] > 0
            and not coerce_bool(body.get("manualSend", False))):
        try:
            schedule_result = schedule_auto_wave(
                current_event,
                wave_number,
                female_percent=int(body.get("femalePercent") or 60),
                audience_filters=body.get("audienceFilters") or {},
            )
            _update_job(job_id, {
                "autoWaveStatus": "SCHEDULED" if schedule_result.get("scheduled") else "SKIPPED",
                "autoWaveNumber": wave_number + 1,
                "autoWaveAt": schedule_result.get("responseWindowHours"),
            })
        except Exception as exc:
            logger.exception("auto_wave_failed stage=schedule event=%s wave=%d", event_id, wave_number)
            _update_job(job_id, {"autoWaveStatus": "FAILED", "autoWaveError": str(exc)[:300]})
