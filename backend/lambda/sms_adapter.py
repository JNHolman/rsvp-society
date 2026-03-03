"""
sms_adapter.py
──────────────
Thin layer between RSVP Radio and the SMS provider (Quo / OpenPhone).

The `send_sms` function is currently a documented stub.
To go live, fill in the HTTP call in the section marked TODO.

All other functions are production-ready.
"""
import json
import logging
import os

import boto3

logger = logging.getLogger()


# ── Secrets Manager ───────────────────────────────────────────────────────────

def get_secret_string(secret_id: str) -> str:
    """
    Fetch a plaintext secret from AWS Secrets Manager.
    Raises botocore.exceptions.ClientError on permission or not-found errors.
    """
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=secret_id)
    return resp.get("SecretString", "")


# ── Welcome SMS ───────────────────────────────────────────────────────────────

def maybe_send_welcome(member: dict) -> None:
    """
    Fire a welcome text to a newly signed-up member.

    Safe to call unconditionally — returns immediately if SMS_ENABLED is not
    "true", or if the member record has no phone.

    Raises on Secrets Manager or send_sms failure. Callers are responsible for
    isolating this in a try/except so welcome-SMS failures never reject a
    valid sign-up (access_request.py does this correctly).
    """
    if (os.getenv("SMS_ENABLED", "false") or "").lower() != "true":
        return

    phone = member.get("phone")
    if not phone:
        return

    name = (member.get("name") or "").split()[0] or ""
    if name:
        msg = f"Hey {name}, it's Jade. Welcome to RSVP Society. Reply STOP to opt out."
    else:
        msg = "Hey, it's Jade. Welcome to RSVP Society. Reply STOP to opt out."

    send_sms(phone, msg)


# ── Core send ─────────────────────────────────────────────────────────────────

def send_sms(to_phone: str, message: str) -> None:
    """
    Send an SMS via the configured provider.

    Environment variables required:
      QUO_API_KEY_SECRET_ID  — Secrets Manager secret ID holding the API key.
                               The secret can be a plain string or a JSON object
                               with an "api_key" or "token" key.
      SMS_FROM_NUMBER        — (optional) The sender phone number / short code.

    Raises on network errors or non-2xx provider responses so callers can log
    and decide whether to treat the error as fatal or best-effort.

    TODO: replace the stub block below with the real provider HTTP call once
    the Quo/OpenPhone account is confirmed and the API contract is known.
    """
    secret_id = os.getenv("QUO_API_KEY_SECRET_ID")
    if not secret_id:
        logger.warning(
            "send_sms: QUO_API_KEY_SECRET_ID not set — SMS not sent to ...%s", to_phone[-4:]
        )
        return

    secret_raw = get_secret_string(secret_id)
    try:
        parsed = json.loads(secret_raw)
        api_key = parsed.get("api_key") or parsed.get("token") or secret_raw
    except Exception:
        api_key = secret_raw

    if not api_key:
        logger.warning(
            "send_sms: resolved empty API key — SMS not sent to ...%s", to_phone[-4:]
        )
        return

    # ── TODO: implement real HTTP call ────────────────────────────────────────
    # Example skeleton for OpenPhone:
    #
    # import urllib.request
    # from_number = os.getenv("SMS_FROM_NUMBER", "")
    # payload = json.dumps({
    #     "content": message,
    #     "from":    from_number,
    #     "to":      to_phone,
    # }).encode("utf-8")
    # req = urllib.request.Request(
    #     "https://api.openphone.com/v1/messages",
    #     data=payload,
    #     headers={
    #         "Authorization": api_key,
    #         "Content-Type":  "application/json",
    #     },
    #     method="POST",
    # )
    # with urllib.request.urlopen(req, timeout=10) as resp:
    #     if resp.status >= 300:
    #         raise RuntimeError(
    #             f"send_sms: provider returned {resp.status} for ...{to_phone[-4:]}"
    #         )
    # ── END TODO ──────────────────────────────────────────────────────────────

    logger.info("send_sms: [stub] would send to ...%s: %.60s", to_phone[-4:], message)
