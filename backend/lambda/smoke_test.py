#!/usr/bin/env python3
"""
smoke_test.py — Post-deploy AWS smoke tests for RSVP Society
Run after every Terraform apply to verify the system is alive.

Usage:
    python3 smoke_test.py --api https://api.rsvpsociety.com --token YOUR_ADMIN_TOKEN
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

PASS = "✓"
FAIL = "✗"
WARN = "⚠"

results = []

def check(label, ok, detail=""):
    results.append((ok, label, detail))
    status = PASS if ok else FAIL
    line = f"  {status} {label}"
    if detail and not ok:
        line += f" — {detail}"
    print(line)
    return ok

def get(url, token=None, expect_status=200):
    try:
        req = urllib.request.Request(url)
        if token:
            req.add_header("x-admin-token", token)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return resp.status, data
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        return e.code, body
    except Exception as exc:
        return 0, {"error": str(exc)}

def post(url, body, token=None):
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("x-admin-token", token)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass
        return e.code, body
    except Exception as exc:
        return 0, {"error": str(exc)}


def run_event_flow_tests(api_base, token):
    """
    Deeper smoke tests that prove event system flows work end-to-end.
    Uses safe read-only checks against real data — no writes.
    """
    print("\n7. Event Lifecycle State")
    status, data = get(f"{api_base}/admin/event", token=token)
    if status == 200 and data.get("event"):
        ev = data["event"]
        ev_status = ev.get("event_status", "UNKNOWN")
        check("Event has lifecycle state", ev_status in (
            "DRAFT","LIVE","ARCHIVED"
        ), f"event_status={ev_status}")

        # Private fields must never appear in admin event response for public callers
        public_status, public_data = get(f"{api_base}/event")
        if public_status == 200 and public_data.get("event"):
            pub_ev = public_data["event"]
            check("ticketUrl not in public event",     "ticketUrl"   not in pub_ev, str(pub_ev.get("ticketUrl",""))[:30])
            check("sectionInfo not in public event",   "sectionInfo" not in pub_ev, str(pub_ev.get("sectionInfo",""))[:30])
            check("description not in public event",   "description" not in pub_ev, str(pub_ev.get("description",""))[:30])

    print("\n8. Analytics Endpoint")
    analytics_event_id = None
    event_status, event_data = get(f"{api_base}/admin/event", token=token)
    if event_status == 200 and event_data.get("event"):
        ev = event_data["event"]
        analytics_event_id = ev.get("eventSlug") or ev.get("eventId")
    if analytics_event_id:
        status, data = get(f"{api_base}/admin/event/analytics?eventId={analytics_event_id}", token=token)
    else:
        status, data = 404, {"error": "no active event"}
    check("Analytics endpoint responds", status in [200, 400, 404], f"status={status}")
    if status == 200 and data.get("analytics"):
        totals = data["analytics"].get("totals", {})
        check("Analytics has no_show field",  "no_show"  in totals, str(totals))
        check("Analytics has attended field", "attended" in totals, str(totals))
        check("Analytics has failed field",   "failed"   in totals, str(totals))

    print("\n9. Attendance Endpoint — structured error")
    # Try to check in a clearly fake phone — should get NOT_CONFIRMED or MEMBER_NOT_FOUND
    status, data = post(f"{api_base}/admin/members/attendance",
        {"phone": "+10000000000", "attended": True, "eventId": "smoke-test"},
        token=token)
    check("Attendance returns structured error for unknown member",
          status in [400, 404] and ("result" in data or "error" in data),
          f"status={status} data={str(data)[:60]}")

    print("\n10. Job Status Endpoint")
    status, data = get(f"{api_base}/admin/invite/status?jobId=smoke-fake-job-id", token=token)
    check("Job status 404 for unknown job", status in [404, 400], f"status={status}")

def run_smoke_tests(api_base, token):
    print("\nRSVP Society Smoke Tests")
    print(f"API: {api_base}")
    print(f"{'='*50}\n")

    # ── 1. API health ────────────────────────────────────────────────────────
    print("1. API Health")
    status, data = get(f"{api_base}/health")
    check("Health endpoint responds", status == 200, f"status={status}")
    check("DynamoDB reachable", data.get("checks", {}).get("dynamodb") == "ok",
          str(data.get("checks", {})))

    # ── 2. Public event endpoint ─────────────────────────────────────────────
    print("\n2. Public Event")
    status, data = get(f"{api_base}/event")
    check("Public /event responds", status in [200, 404], f"status={status}")
    if status == 200 and data.get("event"):
        ev = data["event"]
        check("ticketUrl not in public event", "ticketUrl" not in ev,
              f"found ticketUrl: {ev.get('ticketUrl', '')[:30]}")
        check("sectionInfo not in public event", "sectionInfo" not in ev,
              f"found sectionInfo: {ev.get('sectionInfo', '')[:30]}")
        check("description not in public event", "description" not in ev,
              f"found description: {ev.get('description', '')[:30]}")

    # ── 3. Admin auth ────────────────────────────────────────────────────────
    print("\n3. Admin Auth")
    status, data = get(f"{api_base}/admin/members?status=PENDING")
    check("Unauthenticated request rejected", status == 401, f"status={status}")

    if token:
        status, data = get(f"{api_base}/admin/members?status=PENDING", token=token)
        check("Authenticated request accepted", status == 200, f"status={status}")

        # ── 4. Admin event ───────────────────────────────────────────────────
        print("\n4. Admin Event")
        status, data = get(f"{api_base}/admin/event", token=token)
        check("Admin event endpoint responds", status in [200, 404], f"status={status}")

        # ── 5. Invite preview ────────────────────────────────────────────────
        print("\n5. Invite Preview")
        status, data = post(f"{api_base}/admin/invite/preview",
            {"eventId": "smoke-test", "capacity": 10, "femalePercent": 60,
             "waveNumber": 1},
            token=token)
        check("Invite preview responds", status in [200, 400, 404], f"status={status}")

        # ── 6. Job status endpoint ───────────────────────────────────────────
        print("\n6. Job Status Endpoint")
        status, data = get(f"{api_base}/admin/invite/status?jobId=smoke-test-fake", token=token)
        check("Job status endpoint responds", status in [404, 400], f"status={status}")

    if token:
        run_event_flow_tests(api_base, token)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    passed = sum(1 for ok, _, _ in results if ok)
    total  = len(results)
    print(f"PASSED {passed}/{total}")
    failed = [(label, detail) for ok, label, detail in results if not ok]
    if failed:
        print("\nFAILED:")
        for label, detail in failed:
            print(f"  {FAIL} {label}" + (f" — {detail}" if detail else ""))
        sys.exit(1)
    else:
        print("\n  All smoke tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RSVP Society smoke tests")
    parser.add_argument("--api",   default="https://api.rsvpsociety.com", help="API base URL")
    parser.add_argument("--token", default="",  help="Admin token")
    args = parser.parse_args()
    run_smoke_tests(args.api.rstrip("/"), args.token)
