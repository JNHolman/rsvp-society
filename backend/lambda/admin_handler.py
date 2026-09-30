import hmac
import logging
import os

import boto3

from admin_shared import get_method, get_headers, get_admin_token, resp
from admin_event_routes import (
    get_public_event, get_admin_event, save_admin_event, delete_admin_event, get_analytics,
    list_admin_events, create_or_update_admin_event, set_active_admin_event,
    archive_admin_event, duplicate_admin_event, finalize_admin_event_attendance,
    draft_admin_event_message,
)
from admin_member_routes import (
    get_confirmed, list_members, get_member_history, delete_member, set_member_status, set_member_gender,
    set_member_tier, record_member_attendance, import_members, search_members_route,
)

logger = logging.getLogger()


def _no_store(response: dict) -> dict:
    response = dict(response or {})
    headers = dict(response.get("headers") or {})
    headers["Cache-Control"] = "no-store"
    response["headers"] = headers
    return response


def _health_check(headers: dict) -> dict:
    """Lightweight liveness check — confirms Lambda runs and DDB is reachable."""
    checks = {}
    try:
        t = boto3.resource("dynamodb").Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))
        t.get_item(Key={"eventId": "current"})
        checks["dynamodb"] = "ok"
    except Exception as e:
        checks["dynamodb"] = f"error: {type(e).__name__}"

    all_ok = all(v == "ok" for v in checks.values())
    status = 200 if all_ok else 503
    return resp(headers, status, {"ok": all_ok, "checks": checks})

ROUTES = {
    ("GET", "/admin/members/confirmed"): get_confirmed,
    ("GET", "/admin/members/search"): search_members_route,
    ("GET", "/admin/members/history"): get_member_history,
    ("GET", "/admin/members"): list_members,
    ("DELETE", "/admin/members"): delete_member,
    ("POST", "/admin/members/status"): set_member_status,
    ("POST", "/admin/members/gender"): set_member_gender,
    ("POST", "/admin/members/tier"): set_member_tier,
    ("POST", "/admin/members/attendance"): record_member_attendance,
    ("POST", "/admin/members/import"): import_members,
    ("GET", "/admin/event/analytics"): get_analytics,
    ("POST", "/admin/events/set-active"): set_active_admin_event,
    ("POST", "/admin/events/archive"): archive_admin_event,
    ("POST", "/admin/events/finalize-attendance"): finalize_admin_event_attendance,
    ("POST", "/admin/event/draft-message"): draft_admin_event_message,
    ("POST", "/admin/events/duplicate"): duplicate_admin_event,
    ("GET", "/admin/events"): list_admin_events,
    ("POST", "/admin/events"): create_or_update_admin_event,
    ("DELETE", "/admin/events"): delete_admin_event,
    ("GET", "/admin/event"): get_admin_event,
    ("POST", "/admin/event"): save_admin_event,
    ("DELETE", "/admin/event"): delete_admin_event,
}
PUBLIC_ROUTES = {
    ("GET", "/event"): get_public_event,
    ("GET", "/event/current"): get_public_event,
    ("GET", "/health"): _health_check,
}


def _match_route(method: str, path: str):
    # Check auth routes first — /admin/event must not match the public /event suffix
    for (m, suffix), fn in ROUTES.items():
        if method == m and path.endswith(suffix):
            return fn, True
    for (m, suffix), fn in PUBLIC_ROUTES.items():
        if method == m and path.endswith(suffix):
            return fn, False
    return None, False


def handler(event, context):
    headers = get_headers(event)
    method = get_method(event).upper()
    path = event.get("path", "")

    if method == "OPTIONS":
        return resp(headers, 200, {"ok": True})

    try:
        route_fn, requires_auth = _match_route(method, path)
        if not route_fn:
            return resp(headers, 404, {"ok": False, "error": "not_found"})

        if not requires_auth:
            return route_fn(headers)

        token = (headers.get("x-admin-token") or headers.get("X-Admin-Token") or "").strip()
        if not token or not hmac.compare_digest(token, get_admin_token()):
            return _no_store(resp(headers, 401, {"ok": False, "error": "unauthorized"}))

        return _no_store(route_fn(event, headers, token))
    except Exception:
        logger.exception("admin_handler failed method=%s path=%s", method, path)
        return resp(headers, 500, {"ok": False, "error": "server_error"})
