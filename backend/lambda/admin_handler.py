import logging

from admin_shared import get_method, get_headers, get_admin_token, resp
from admin_event_routes import get_public_event, get_admin_event, save_admin_event, get_analytics, get_events
from admin_member_routes import (
    get_confirmed, list_members, delete_member, set_member_status, set_member_gender,
    set_member_tier, record_member_attendance, import_members, search_members_route,
)

logger = logging.getLogger()

ROUTES = {
    ("GET", "/admin/members/confirmed"): get_confirmed,
    ("GET", "/admin/members/search"): search_members_route,
    ("GET", "/admin/members"): list_members,
    ("DELETE", "/admin/members"): delete_member,
    ("POST", "/admin/members/status"): set_member_status,
    ("POST", "/admin/members/gender"): set_member_gender,
    ("POST", "/admin/members/tier"): set_member_tier,
    ("POST", "/admin/members/attendance"): record_member_attendance,
    ("POST", "/admin/members/import"): import_members,
    ("GET", "/admin/event/analytics"): get_analytics,
    ("GET", "/admin/events"): get_events,
    ("GET", "/admin/event"): get_admin_event,
    ("POST", "/admin/event"): save_admin_event,
}
PUBLIC_ROUTES = {
    ("GET", "/event"): get_public_event,
    ("GET", "/event/current"): get_public_event,
}


def _match_route(method: str, path: str):
    for (m, suffix), fn in PUBLIC_ROUTES.items():
        if method == m and path.endswith(suffix):
            return fn, False
    for (m, suffix), fn in ROUTES.items():
        if method == m and path.endswith(suffix):
            return fn, True
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
        if not token or token != get_admin_token():
            return resp(headers, 401, {"ok": False, "error": "unauthorized"})

        return route_fn(event, headers, token)
    except Exception:
        logger.exception("admin_handler failed method=%s path=%s", method, path)
        return resp(headers, 500, {"ok": False, "error": "server_error"})
