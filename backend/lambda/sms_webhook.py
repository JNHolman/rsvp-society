import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

from sms_adapter import get_secret_string

logger = logging.getLogger()

# ── Webhook signature verification (#33) ─────────────────────────────────────

def _verify_webhook_signature(event: dict) -> bool:
    """Verify current Standard-Webhooks or legacy OpenPhone-era Quo signatures."""
    secret_id = os.getenv("WEBHOOK_SECRET_ID")
    secret_id_2 = os.getenv("WEBHOOK_SECRET_ID_2", "")
    if not secret_id:
        if not os.getenv("AWS_LAMBDA_FUNCTION_NAME") and not os.getenv("AWS_EXECUTION_ENV", "").startswith("AWS_Lambda") and (os.getenv("ALLOW_UNSIGNED_WEBHOOK_DEV", "false") or "").lower() == "true":
            logger.warning("sms_handler: unsigned webhook accepted because ALLOW_UNSIGNED_WEBHOOK_DEV=true")
            return True
        logger.error("sms_handler: WEBHOOK_SECRET_ID not set — rejecting unsigned webhook")
        return False

    secret_ids = [secret_id]
    if secret_id_2:
        secret_ids.append(secret_id_2)

    def _debug_write(reason: str, extra: dict = None):
        detail = f"webhook_verify: {reason}"
        if extra:
            detail += f" | {' '.join(f'{k}={str(v)[:200]}' for k, v in extra.items())}"
        logger.warning(detail)

    def _secret_text(raw: str) -> str:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return str(
                    parsed.get("signing_secret")
                    or parsed.get("secret")
                    or parsed.get("token")
                    or parsed.get("key")
                    or raw
                ).strip()
        except Exception:
            pass
        return str(raw or "").strip()

    def _decode_key(secret: str, *, standard: bool) -> bytes:
        value = secret[6:] if standard and secret.startswith("whsec_") else secret
        value += "=" * (-len(value) % 4)
        return base64.b64decode(value)

    try:
        headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items() if v is not None}
        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        now = datetime.now(timezone.utc)

        # Current Quo generation: Standard Webhooks style headers and signing input.
        webhook_id = headers.get("webhook-id", "").strip()
        webhook_ts = headers.get("webhook-timestamp", "").strip()
        webhook_sig = headers.get("webhook-signature", "").strip()
        if webhook_id or webhook_ts or webhook_sig:
            if not (webhook_id and webhook_ts and webhook_sig):
                _debug_write("incomplete_standard_headers")
                return False
            try:
                ts = datetime.fromtimestamp(int(webhook_ts), tz=timezone.utc)
            except Exception:
                _debug_write("invalid_standard_timestamp")
                return False
            if abs((now - ts).total_seconds()) > 300:
                _debug_write("expired_standard_timestamp")
                return False

            provided = [
                token[3:]
                for token in webhook_sig.split()
                if token.startswith("v1,") and len(token) > 3
            ]
            if not provided:
                _debug_write("invalid_standard_signature_format")
                return False

            signed = f"{webhook_id}.{webhook_ts}.{raw_body}".encode("utf-8")
            for sid in secret_ids:
                try:
                    key = _decode_key(_secret_text(get_secret_string(sid)), standard=True)
                    computed = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
                    if any(hmac.compare_digest(candidate, computed) for candidate in provided):
                        return True
                except Exception:
                    continue
            logger.warning("sms_handler: standard webhook signature mismatch — no secret matched")
            return False

        # Legacy Quo/OpenPhone generation.
        signature_header = headers.get("openphone-signature", "").strip() or headers.get("x-openphone-signature", "").strip()
        if not signature_header:
            _debug_write("no_signature_header", {"header_keys": str(list(headers.keys()))[:400]})
            return False

        for sid in secret_ids:
            try:
                signing_key = _decode_key(_secret_text(get_secret_string(sid)), standard=False)
                for candidate in [c.strip() for c in signature_header.split(",") if c.strip()]:
                    parts = candidate.split(";")
                    if len(parts) != 4:
                        continue
                    scheme, version, ts_raw, provided_digest = parts
                    if scheme.lower() != "hmac" or version != "1":
                        continue
                    try:
                        ts_int = int(ts_raw)
                        if ts_int > 1_000_000_000_000:
                            ts_int //= 1000
                        ts = datetime.fromtimestamp(ts_int, tz=timezone.utc)
                    except Exception:
                        continue
                    if abs((now - ts).total_seconds()) > 300:
                        continue
                    signed = f"{ts_raw}.{raw_body}".encode("utf-8")
                    computed = base64.b64encode(hmac.new(signing_key, signed, hashlib.sha256).digest()).decode("ascii")
                    if hmac.compare_digest(provided_digest, computed):
                        return True
            except Exception:
                continue

        logger.warning("sms_handler: legacy webhook signature mismatch — no secret matched")
        return False
    except Exception as exc:
        logger.exception("sms_handler: signature verification error — rejecting request")
        _debug_write("exception", {"error": str(exc)})
        return False
