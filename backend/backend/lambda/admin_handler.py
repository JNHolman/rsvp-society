"""
admin_handler.py
Thin dispatcher — parses the request, checks auth, routes to the right module.
Business logic lives in admin_member_routes.py and admin_event_routes.py.
Shared utilities live in admin_shared.py.
"""
import hmac
import logging

from admin_shared import get_method, get_headers, get_admin_token, resp
from admin_member_routes import (
    get_confirmed,
    list_members,
    delete_member,
    set_member_status,
    set_member_gender,
    set_member_tier,
    record_member_attendance,
    import_members,
    search_members_route,
)
from admin_event_routes import (
    get_public_event,
    get_admin_event,
    save_admin_event,
    get_analytics,
)

logger = logging.getLogger()


def handler(event, context):
    headers = {}
    try:
        method  = get_method(event).upper()
        headers = get_headers(event)
        path    = event.get("path", "")

        # ── Preflight ─────────────────────────────────────────────────────────
        if method == "OPTIONS":
            return resp(headers, 200, {"ok": True})

        # ── Public current-event endpoint (no auth) ───────────────────────────
        if (method == "GET"
                and (path.endswith("/event") or path.endswith("/event/current"))
                and not path.endswith("/admin/event")):
            return get_public_event(headers)

        # ── Auth gate — everything below requires the admin token ─────────────
        token = (headers.get("x-admin-token") or headers.get("X-Admin-Token") or "").strip()
        if not token or not hmac.compare_digest(token, get_admin_token()):
            return resp(headers, 401, {"ok": False, "error": "unauthorized"})

        # ── Member routes ─────────────────────────────────────────────────────
        if method == "GET"    and "/admin/members/confirmed" in path:
            return get_confirmed(event, headers, token)

        if method == "GET"    and path.endswith("/admin/members/search"):
            return search_members_route(event, headers, token)

        if method == "GET"    and path.endswith("/admin/members"):
            return list_members(event, headers, token)

        if method == "DELETE" and path.endswith("/admin/members"):
            return delete_member(event, headers, token)

        if method == "POST"   and path.endswith("/admin/members/status"):
            return set_member_status(event, headers, token)

        if method == "POST"   and path.endswith("/admin/members/gender"):
            return set_member_gender(event, headers, token)

        if method == "POST"   and path.endswith("/admin/members/tier"):
            return set_member_tier(event, headers, token)

        if method == "POST"   and path.endswith("/admin/members/attendance"):
            return record_member_attendance(event, headers, token)

        if method == "POST"   and path.endswith("/admin/members/import"):
            return import_members(event, headers, token)

        # ── Event routes ──────────────────────────────────────────────────────
        # Analytics must be checked before /admin/event to avoid shadowing
        if method == "GET"  and "/admin/event/analytics" in path:
            return get_analytics(event, headers, token)

        if method == "GET"  and path.endswith("/admin/event"):
            return get_admin_event(event, headers, token)

        if method == "POST" and path.endswith("/admin/event"):
            return save_admin_event(event, headers, token)

        # ── 404 ───────────────────────────────────────────────────────────────
        return resp(headers, 404, {"ok": False, "error": "not found"})

    except Exception:
        logger.exception("Unhandled error in admin_handler")
        return resp(headers, 500, {"ok": False, "error": "An internal error occurred"})
