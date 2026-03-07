"""
sms_adapter.py
──────────────
Thin layer between RSVP Society and Quo (formerly OpenPhone).

Production status:
- Secrets Manager lookup is live.
- Outbound SMS delivery is live via Quo's POST /v1/messages API.
- Callers should still treat send failures as exceptions so blasts can surface
  partial failures instead of silently dropping messages.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import boto3
logger = logging.getLogger()


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", ""}:
            return False
    return bool(value)


# Cache the resolved sending number id to avoid extra API calls per invocation.
_FROM_ID_CACHE: Optional[str] = None


# ── Secrets Manager ───────────────────────────────────────────────────────────

def get_secret_string(secret_id: str) -> str:
    """
    Fetch a plaintext secret from AWS Secrets Manager.
    Raises botocore.exceptions.ClientError on permission or not-found errors.
    """
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=secret_id)
    return resp.get("SecretString", "")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_e164(raw: str) -> str:
    """Best-effort E.164 normalizer (US default if 10 digits)."""
    if not raw:
        raise ValueError("phone is required")
    s = raw.strip()
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
        if not (10 <= len(digits) <= 15):
            raise ValueError("phone must be valid E.164 length (10–15 digits)")
        return f"+{digits}"
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if 10 <= len(digits) <= 15:
        return f"+{digits}"
    raise ValueError("phone must be valid E.164")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _http_json(url: str, *, method: str = "GET", headers: Optional[dict] = None, data: Optional[bytes] = None, timeout: int = 15) -> Any:
    merged = {"User-Agent": "rsvp-society-lambda/1.0", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=merged, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _list_phone_numbers(api_key: str) -> List[dict]:
    """
    Fetch Quo/OpenPhone phone numbers so we can map a human phone number (E.164)
    to a phone-number id (PN.../OP...) required by the send API.

    Docs: GET /v1/phone-numbers
    """
    payload = _http_json(
        "https://api.openphone.com/v1/phone-numbers",
        headers={"Authorization": api_key},
        method="GET",
        timeout=15,
    )

    # The API has used a few different top-level shapes over time; accept them all.
    for key in ("data", "phoneNumbers", "results", "items"):
        items = payload.get(key)
        if isinstance(items, list):
            return items

    # Some responses may be direct list payloads.
    if isinstance(payload, list):
        return payload

    return []


def _extract_id_and_number(item: dict) -> Tuple[Optional[str], Optional[str]]:
    """Heuristic extraction of (phoneNumberId, e164Number) from a phone-number object."""
    if not isinstance(item, dict):
        return None, None

    # Common id fields.
    for k in ("id", "phoneNumberId", "phone_number_id", "from"):
        v = item.get(k)
        if isinstance(v, str) and (v.startswith("PN") or v.startswith("OP")):
            phone_id = v
            break
    else:
        phone_id = None
        # Fallback: scan values for PN/OP-like ids.
        for v in item.values():
            if isinstance(v, str) and (v.startswith("PN") or v.startswith("OP")):
                phone_id = v
                break

    # Common number fields.
    candidates: List[str] = []
    for k in ("phoneNumber", "number", "phone", "e164", "phoneNumberE164"):
        v = item.get(k)
        if isinstance(v, str):
            candidates.append(v)
        elif isinstance(v, dict):
            for kk in ("phoneNumber", "number", "e164"):
                vv = v.get(kk)
                if isinstance(vv, str):
                    candidates.append(vv)

    # Fallback: scan any string values that look like phone numbers.
    for v in item.values():
        if isinstance(v, str) and _digits(v) and len(_digits(v)) >= 10:
            candidates.append(v)

    phone_e164 = None
    for c in candidates:
        try:
            phone_e164 = _normalize_e164(c)
            break
        except Exception:
            continue

    return phone_id, phone_e164


# ── Welcome SMS ───────────────────────────────────────────────────────────────

def maybe_send_welcome(member: dict) -> bool:
    """
    Fire a welcome text to a member.

    Behavior:
    - No-ops unless SMS_ENABLED == "true"
    - No-ops unless member is APPROVED (default); override with WELCOME_REQUIRE_APPROVED=false
    - No-ops unless smsOptIn is true and optOut is not set
    - No-ops if welcomeSentAt already exists

    Returns:
      True  → sent
      False → skipped (policy/config)
    Raises:
      Exceptions for Secrets Manager / Quo send failures (callers should wrap).
    """
    phone = (member.get("phone") or "").strip()
    phone_suffix = phone[-4:] if phone else "unknown"

    if (os.getenv("SMS_ENABLED", "false") or "").lower() != "true":
        logger.info("maybe_send_welcome: skipped reason=sms_disabled phone=...%s", phone_suffix)
        return False

    require_approved = (os.getenv("WELCOME_REQUIRE_APPROVED", "true") or "").lower() == "true"
    status = (member.get("status") or "").upper()
    if require_approved and status != "APPROVED":
        logger.info(
            "maybe_send_welcome: skipped reason=status_not_approved phone=...%s status=%s",
            phone_suffix,
            status or "UNKNOWN",
        )
        return False

    if not coerce_bool(member.get("smsOptIn", False)):
        logger.info("maybe_send_welcome: skipped reason=no_sms_opt_in phone=...%s", phone_suffix)
        return False
    if coerce_bool(member.get("optOut", False)):
        logger.info("maybe_send_welcome: skipped reason=opted_out phone=...%s", phone_suffix)
        return False
    if member.get("welcomeSentAt"):
        logger.info("maybe_send_welcome: skipped reason=already_sent phone=...%s", phone_suffix)
        return False

    if not phone:
        logger.info("maybe_send_welcome: skipped reason=no_phone")
        return False

    name = (member.get("name") or "").split()[0] or ""
    if name:
        msg = f"Hey {name}, it's Jade. Welcome to RSVP Society. Reply STOP to opt out."
    else:
        msg = "Hey, it's Jade. Welcome to RSVP Society. Reply STOP to opt out."

    logger.info("maybe_send_welcome: sending phone=...%s", phone_suffix)
    send_sms(phone, msg)
    return True


# ── Core send ─────────────────────────────────────────────────────────────────

def _resolve_api_key() -> str:
    secret_id = os.getenv("QUO_API_KEY_SECRET_ID")
    if not secret_id:
        raise RuntimeError("QUO_API_KEY_SECRET_ID not set")

    secret_raw = (get_secret_string(secret_id) or "").strip()
    try:
        parsed = json.loads(secret_raw)
        if isinstance(parsed, dict):
            for key in ("api_key", "apiKey", "token", "key", "secret"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except Exception:
        pass
    return secret_raw


def _resolve_from_phone_number_id() -> str:
    """
    Resolve the Quo/OpenPhone sending identifier for the `from` field.

    Accepts either:
    - A phone-number id (PN.../OP...) via QUO_PHONE_NUMBER_ID (preferred) or SMS_FROM_NUMBER (legacy)
    - A human phone number (E.164 or local digits) via QUO_PHONE_NUMBER_ID / SMS_FROM_NUMBER / QUO_FROM_NUMBER,
      which will be mapped to the proper id by calling GET /v1/phone-numbers.

    This prevents the common misconfig where operators set the actual phone number
    instead of the phone-number id required by the API.
    """
    global _FROM_ID_CACHE
    if _FROM_ID_CACHE:
        return _FROM_ID_CACHE

    raw = (
        os.getenv("QUO_PHONE_NUMBER_ID")
        or os.getenv("SMS_FROM_NUMBER")
        or os.getenv("QUO_FROM_NUMBER")
        or ""
    ).strip()

    if not raw:
        return ""

    # If already an id, use it directly.
    if raw.startswith("PN") or raw.startswith("OP"):
        _FROM_ID_CACHE = raw
        return raw

    # Treat as a phone number and map it to a phone-number id.
    desired = _normalize_e164(raw)
    api_key = (_resolve_api_key() or "").strip()
    if not api_key:
        raise RuntimeError("Resolved empty Quo API key")

    numbers = _list_phone_numbers(api_key)
    desired_digits = _digits(desired)

    for item in numbers:
        phone_id, phone_e164 = _extract_id_and_number(item)
        if not phone_id or not phone_e164:
            continue
        if _digits(phone_e164) == desired_digits:
            _FROM_ID_CACHE = phone_id
            return phone_id

    raise RuntimeError(
        f"Could not map sending number {desired} to a Quo phone-number id via /v1/phone-numbers"
    )


def send_sms(to_phone: str, message: str) -> None:
    """
    Send an SMS via Quo/OpenPhone.

    Required env vars:
      QUO_API_KEY_SECRET_ID  — Secrets Manager secret ID holding the API key.
      QUO_PHONE_NUMBER_ID    — Quo/OpenPhone phone-number ID (PN.../OP...) used as
                               the sender. You may also set this to the actual
                               phone number; it will be mapped via /v1/phone-numbers.
                               SMS_FROM_NUMBER and QUO_FROM_NUMBER are accepted
                               as backward-compatible fallback names.

    Optional env vars:
      QUO_USER_ID            — Quo user ID (US...) if you want to pin sender.
      QUO_SET_INBOX_STATUS   — default "done" to keep API-sent messages out of
                               the open inbox unless you choose otherwise.

    Raises on config, network, or non-2xx/202 provider responses.
    """
    to_phone = _normalize_e164((to_phone or "").strip())
    message = (message or "").strip()
    if not to_phone:
        raise ValueError("to_phone is required")
    if not message:
        raise ValueError("message is required")

    api_key = (_resolve_api_key() or "").strip()
    if not api_key:
        raise RuntimeError("Resolved empty Quo API key")

    from_id = _resolve_from_phone_number_id()
    if not from_id:
        raise RuntimeError("QUO_PHONE_NUMBER_ID / SMS_FROM_NUMBER / QUO_FROM_NUMBER not set")

    payload: Dict[str, Any] = {
        "content": message,
        "from": from_id,
        "to": [to_phone],
    }

    user_id = (os.getenv("QUO_USER_ID") or "").strip()
    if user_id:
        payload["userId"] = user_id

    set_inbox_status = (os.getenv("QUO_SET_INBOX_STATUS") or "done").strip()
    if set_inbox_status:
        payload["setInboxStatus"] = set_inbox_status

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openphone.com/v1/messages",
        data=data,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "User-Agent": "rsvp-society-lambda/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status not in (200, 201, 202):
                raise RuntimeError(
                    f"Quo send failed status={resp.status} phone=...{to_phone[-4:]} body={raw[:300]}"
                )
            logger.info("send_sms: sent to ...%s", to_phone[-4:])
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        raise RuntimeError(
            f"Quo send failed status={exc.code} phone=...{to_phone[-4:]} body={body[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Quo send network error phone=...{to_phone[-4:]}: {exc}") from exc
