"""Delivery-callback bookkeeping for Quo SMS webhooks."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError


def handle_delivery(raw_body: str, *, deps: dict) -> dict:
    logger = deps["logger"]
    try:
        parsed = json.loads(raw_body or "{}")
        data = parsed.get("data", {}) if isinstance(parsed.get("data"), dict) else {}
        data_obj = data.get("resource") if isinstance(data.get("resource"), dict) else data.get("object", {})
        msg_id = data_obj.get("id", "")
        to_phone = data_obj.get("to", "")

        if not msg_id:
            logger.info("sms_handler: delivery webhook with no message ID — skipping")
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        invite = deps["find_invite_by_message_id"](msg_id, to_phone)
        if invite:
            slug = invite.get("eventId")
            phone = invite.get("phone")
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            first_delivery = False
            try:
                deps["invites_table"]().update_item(
                    Key={"eventId": slug, "phone": phone},
                    UpdateExpression="SET deliveredAt = :now, deliveryStatus = :status",
                    ConditionExpression=Attr("deliveredAt").not_exists(),
                    ExpressionAttributeValues={":now": now_iso, ":status": "DELIVERED"},
                )
                first_delivery = True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    deps["invites_table"]().update_item(
                        Key={"eventId": slug, "phone": phone},
                        UpdateExpression="SET deliveryStatus = :status, lastDeliveryWebhookAt = :now",
                        ExpressionAttributeValues={":now": now_iso, ":status": "DELIVERED"},
                    )
                    logger.info("sms_handler: duplicate delivery webhook ignored for counters event=%s msg_id=%s", slug, msg_id)
                else:
                    raise

            if first_delivery:
                inv_wave = str(invite.get("waveNumber") or "")
                event_item = deps["events_table"]().get_item(Key={"eventId": slug}).get("Item") or {}
                same_last_wave = inv_wave and str(event_item.get("lastBlastWave") or "") == inv_wave
                event_expr = "SET deliveredCount = if_not_exists(deliveredCount, :zero) + :one, lastDeliveryAt = :now"
                event_vals = {":zero": 0, ":one": 1, ":now": now_iso}
                if same_last_wave:
                    event_expr += ", lastBlastDeliveredCount = if_not_exists(lastBlastDeliveredCount, :zero) + :one"
                deps["events_table"]().update_item(
                    Key={"eventId": slug},
                    UpdateExpression=event_expr,
                    ExpressionAttributeValues=event_vals,
                )
            logger.info("sms_handler: invite delivery confirmed event=%s msg_id=%s to=...%s first=%s", slug, msg_id, str(to_phone)[-4:], first_delivery)
        else:
            logger.info("sms_handler: delivery webhook for non-invite msg_id=%s — skipped", msg_id[:30])
    except Exception:
        logger.exception("sms_handler: delivery tracking update failed")
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
