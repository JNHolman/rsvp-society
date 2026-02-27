import os
import json
import boto3
import urllib.request

def get_secret_string(secret_id: str) -> str:
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=secret_id)
    return resp.get("SecretString", "")

def maybe_send_welcome(member: dict) -> None:
    if (os.getenv("SEND_WELCOME_SMS", "false") or "").lower() != "true":
        return

    phone = member.get("phone")
    name = (member.get("name") or "").split()[0] or ""

    if not phone:
        return

    if name:
        msg = f"Hey {name}, it's Jade. Welcome to RSVP Society. Reply STOP to opt out."
    else:
        msg = "Hey, it's Jade. Welcome to RSVP Society. Reply STOP to opt out."

    send_sms(phone, msg)

def send_sms(to_phone: str, message: str) -> None:
    secret_id = os.getenv("QUO_API_KEY_SECRET_ID")
    if not secret_id:
        return

    secret_raw = get_secret_string(secret_id)

    try:
        secret = json.loads(secret_raw)
        api_key = secret.get("api_key") or secret.get("token") or secret_raw
    except Exception:
        api_key = secret_raw

    # placeholder HTTP call to Quo/OpenPhone
    # do nothing for now if key empty
    if not api_key:
        return
