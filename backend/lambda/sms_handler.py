import json
import os
import urllib.request

import boto3

from member_store import get_member, normalize_phone
from sms_adapter import get_secret_string, send_sms

JADE_SYSTEM_PROMPT = (
    "You are Jade, the RSVP Society concierge. "
    "RSVP Society is an exclusive, invite-only R&B event experience. "
    "Be warm but selective. Keep replies short — this is SMS, not email. "
    "Never reveal the venue, guest list, or invite status to anyone. "
    "If someone asks about the next event, tell them details will be "
    "sent directly to approved members. "
    "If someone asks to join, direct them to the website to request access. "
    "Never impersonate staff or make promises about approval."
)

OPT_OUT_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}

_DDB = boto3.resource("dynamodb")


def _members_table():
    name = os.getenv("MEMBERS_TABLE_NAME")
    if not name:
        raise RuntimeError("MEMBERS_TABLE_NAME env var not set")
    return _DDB.Table(name)


def _set_opt_out(phone: str) -> None:
    """Write optOut: true to the member record."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _members_table().update_item(
        Key={"phone": phone},
        UpdateExpression="SET optOut = :t, optOutAt = :now, smsOptIn = :f, lastSeenAt = :now",
        ExpressionAttributeValues={":t": True, ":f": False, ":now": now},
        ExpressionAttributeValues={":t": True, ":now": now},
    )


def _claude(message: str) -> str:
    api_key = get_secret_string("rsvp/claude-api-key")
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 200,
        "system": [
            {
                "type": "text",
                "text": JADE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": message}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        out = json.loads(resp.read())
    return out["content"][0]["text"]


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        from_phone = normalize_phone(body.get("from") or "")
        text = (body.get("text") or "").strip()

        # Handle STOP/opt-out — write to DynamoDB, carrier handles the actual block
        if text.upper() in OPT_OUT_KEYWORDS:
            if from_phone:
                try:
                    _set_opt_out(from_phone)
                except Exception:
                    pass
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        member = get_member(from_phone)

        # Don't respond if not approved or opted out
        if not member or member.get("status") != "APPROVED":
            return {"statusCode": 200, "body": json.dumps({"ok": True})}
        if member.get("optOut"):
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        # ── YES / NO RSVP replies ──
        normalized = text.upper().strip()
        sms_enabled = (os.getenv("SEND_WELCOME_SMS", "false") or "").lower() == "true"

        if normalized in ("YES", "Y", "YEP", "YUP", "IN", "CONFIRMED"):
            try:
                ddb = boto3.resource("dynamodb")
                invites = ddb.Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))
                from boto3.dynamodb.conditions import Key as DKey
                resp = invites.query(
                    IndexName="phone-index",
                    KeyConditionExpression=DKey("phone").eq(from_phone),
                    ScanIndexForward=False,
                    Limit=1,
                )
                items = resp.get("Items", [])
                if items and items[0].get("status") == "INVITED":
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    invites.update_item(
                        Key={"eventId": items[0]["eventId"], "phone": from_phone},
                        UpdateExpression="SET #s = :c, confirmedAt = :now",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":c": "CONFIRMED", ":now": now},
                    )
                    if sms_enabled:
                        send_sms(from_phone, "You're confirmed. Details coming soon. See you there.")
                    return {"statusCode": 200, "body": json.dumps({"ok": True})}
            except Exception:
                pass

        if normalized in ("NO", "N", "NOPE", "CANT", "CAN'T", "PASS", "DECLINE"):
            try:
                ddb = boto3.resource("dynamodb")
                invites = ddb.Table(os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites"))
                from boto3.dynamodb.conditions import Key as DKey
                resp = invites.query(
                    IndexName="phone-index",
                    KeyConditionExpression=DKey("phone").eq(from_phone),
                    ScanIndexForward=False,
                    Limit=1,
                )
                items = resp.get("Items", [])
                if items and items[0].get("status") == "INVITED":
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    invites.update_item(
                        Key={"eventId": items[0]["eventId"], "phone": from_phone},
                        UpdateExpression="SET #s = :d, declinedAt = :now",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":d": "DECLINED", ":now": now},
                    )
                    if sms_enabled:
                        send_sms(from_phone, "No worries — we'll catch you next time.")
                    return {"statusCode": 200, "body": json.dumps({"ok": True})}
            except Exception:
                pass

        reply = _claude(text)
        if sms_enabled and reply:
            send_sms(from_phone, reply)

        return {"statusCode": 200, "body": json.dumps({"ok": True})}

    except Exception:
        return {"statusCode": 200, "body": json.dumps({"ok": True})}
