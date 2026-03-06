"""
admin_shared.py
Shared helpers used by admin_handler, admin_member_routes, and admin_event_routes.
Nothing in here has side effects — pure utilities only.
"""
import json
import logging
import os
from decimal import Decimal

import boto3
from sms_adapter import get_secret_string

logger = logging.getLogger()


# ── Bool coercion ─────────────────────────────────────────────────────────────
# Using raw bool() is unsafe for values arriving from JSON or DynamoDB where
# a string "false" would coerce to True. coerce_bool handles all cases correctly.

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


# ── JSON encoder ──────────────────────────────────────────────────────────────

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


# ── Request parsing ───────────────────────────────────────────────────────────

def get_method(event: dict) -> str:
    if event.get("httpMethod"):
        return event["httpMethod"]
    rc = event.get("requestContext", {}).get("http", {})
    return rc.get("method", "")


def get_headers(event: dict) -> dict:
    return event.get("headers") or {}


def get_query(event: dict) -> dict:
    return event.get("queryStringParameters") or {}


def get_body(event: dict) -> dict:
    raw = event.get("body") or ""
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


# ── CORS / response ───────────────────────────────────────────────────────────

def _allowed_origins() -> list:
    csv = os.getenv("ALLOWED_ORIGINS", "")
    return [o.strip() for o in csv.split(",") if o.strip()]


def _pick_origin(headers: dict) -> str:
    origin = (headers.get("origin") or headers.get("Origin") or "").strip()
    allowed = _allowed_origins()
    if origin and origin in allowed:
        return origin
    return allowed[0] if allowed else "*"


def cors_headers(headers: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": _pick_origin(headers),
        "Access-Control-Allow-Headers": "content-type,x-admin-token",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Vary": "Origin",
    }


def resp(headers: dict, status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": cors_headers(headers),
        "body": json.dumps(body, cls=DecimalEncoder) if not isinstance(body, str) else body,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_admin_token() -> str:
    sid = os.getenv("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token")
    s = get_secret_string(sid)
    try:
        j = json.loads(s)
        if isinstance(j, dict) and j.get("token"):
            return j["token"]
    except Exception:
        pass
    return s or ""


# ── DynamoDB table accessors ──────────────────────────────────────────────────

def events_table():
    return boto3.resource("dynamodb").Table(
        os.getenv("EVENTS_TABLE_NAME", "rsvp-events")
    )


def members_table():
    return boto3.resource("dynamodb").Table(
        os.getenv("MEMBERS_TABLE_NAME", "rsvp-members")
    )


def invites_table():
    return boto3.resource("dynamodb").Table(
        os.getenv("INVITES_TABLE_NAME", "rsvp-event-invites")
    )


# ── Source normalization ──────────────────────────────────────────────────────

def normalize_import_source(value: str) -> str:
    src = (value or "").strip().lower()
    if src in ("csv", "web", "import", "eventbrite", "posh", "superphone"):
        return src
    return ""
