#!/usr/bin/env python3
"""
integration_tests.py

Local integration tests for the RSVP Society backend.
Tests run against real Lambda handlers in-process via moto — no deployed
stack required.

Requirements:
    pip install 'moto[dynamodb,secretsmanager]' boto3 pytest

Run:
    pytest integration_tests.py -v
    python integration_tests.py
"""
from __future__ import annotations

import importlib
from datetime import datetime, timezone, timedelta
import json
from decimal import Decimal
import os
import sys
import unittest
from typing import Any, Dict
from unittest.mock import MagicMock, patch

# ── Region must be set before any boto3 import hits a live endpoint ───────────
os.environ["AWS_DEFAULT_REGION"]    = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"]     = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"

try:
    from moto import mock_aws
except ImportError:
    sys.exit("Install moto: pip install 'moto[dynamodb,secretsmanager]'")

import boto3

# ── Lambda env vars ───────────────────────────────────────────────────────────
os.environ.setdefault("MEMBERS_TABLE_NAME",      "rsvp-members-test")
os.environ.setdefault("INVITES_TABLE_NAME",      "rsvp-event-invites-test")
os.environ.setdefault("EVENTS_TABLE_NAME",       "rsvp-events-test")
os.environ.setdefault("INVITE_JOBS_TABLE_NAME",  "rsvp-invite-jobs-test")
os.environ.setdefault("CHECKINS_TABLE_NAME",     "rsvp-checkins-test")
os.environ.setdefault("EVENT_HISTORY_TABLE_NAME", "rsvp-event-history-test")
os.environ.setdefault("AUDIT_LOG_TABLE_NAME",    "rsvp-audit-log-test")
os.environ.setdefault("PENDING_APPROVALS_TABLE_NAME", "rsvp-pending-approvals-test")
os.environ.setdefault("RSVP_TEST_DISABLE_DDB_TRANSACTIONS", "true")
os.environ.setdefault("ALLOWED_ORIGINS",         "https://admin.rsvpsociety.com")
os.environ.setdefault("SMS_ENABLED",             "false")
os.environ.setdefault("ADMIN_TOKEN_SECRET_ID",   "rsvp/admin-token-test")

ADMIN_TOKEN = "test-admin-token-abc123"

# ── Module names that carry module-level boto3 resources ─────────────────────
# These must be reloaded inside each mock_aws context so they get fresh
# moto-intercepted clients rather than the real ones created at import time.
_RELOAD_MODULES = [
    "member_store",
    "audit_log",
    "admin_shared",
    "admin_member_routes",
    "admin_event_routes",
    "admin_handler",
    "access_request",
    "sms_adapter",
    "invite_capacity",
    "sms_handler",
    "invite_handler",
    "reminder_handler",
]


def _reload_lambda_modules():
    """Force-reload all Lambda modules so boto3 clients are created inside moto."""
    for name in _RELOAD_MODULES:
        if name in sys.modules:
            del sys.modules[name]
    # Now import them fresh — boto3 calls happen inside the active mock_aws context
    import member_store      # noqa: F401
    import audit_log         # noqa: F401
    import admin_shared      # noqa: F401
    import admin_member_routes  # noqa: F401
    import admin_event_routes   # noqa: F401
    import admin_handler     # noqa: F401
    import sms_adapter       # noqa: F401
    import invite_handler    # noqa: F401
    import reminder_handler  # noqa: F401


# ── Table provisioning ────────────────────────────────────────────────────────

def _create_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")

    ddb.create_table(
        TableName="rsvp-members-test",
        KeySchema=[{"AttributeName": "phone", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "phone",  "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "status-index",
            "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="rsvp-event-invites-test",
        KeySchema=[
            {"AttributeName": "eventId", "KeyType": "HASH"},
            {"AttributeName": "phone",   "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "eventId", "AttributeType": "S"},
            {"AttributeName": "phone",   "AttributeType": "S"},
            {"AttributeName": "quoMessageId", "AttributeType": "S"},
            {"AttributeName": "jobId", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "phone-index",
                "KeySchema": [{"AttributeName": "phone", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "quo-message-index",
                "KeySchema": [{"AttributeName": "quoMessageId", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "jobId-index",
                "KeySchema": [{"AttributeName": "jobId", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    ddb.create_table(
        TableName="rsvp-invite-jobs-test",
        KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "jobId", "AttributeType": "S"},
            {"AttributeName": "eventId", "AttributeType": "S"},
            {"AttributeName": "submittedAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "eventId-submittedAt-index",
            "KeySchema": [
                {"AttributeName": "eventId", "KeyType": "HASH"},
                {"AttributeName": "submittedAt", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="rsvp-events-test",
        KeySchema=[{"AttributeName": "eventId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "eventId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="rsvp-event-history-test",
        KeySchema=[{"AttributeName": "historyPk", "KeyType": "HASH"}, {"AttributeName": "eventKey", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "historyPk", "AttributeType": "S"}, {"AttributeName": "eventKey", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="rsvp-checkins-test",
        KeySchema=[
            {"AttributeName": "eventId", "KeyType": "HASH"},
            {"AttributeName": "phone",   "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "eventId", "AttributeType": "S"},
            {"AttributeName": "phone",   "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="rsvp-audit-log-test",
        KeySchema=[
            {"AttributeName": "actionId", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "actionId", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="rsvp-pending-approvals-test",
        KeySchema=[
            {"AttributeName": "hostPhone", "KeyType": "HASH"},
            {"AttributeName": "memberPhone", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "hostPhone", "AttributeType": "S"},
            {"AttributeName": "memberPhone", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _stub_secret(token: str = ADMIN_TOKEN):
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    sm.create_secret(
        Name="rsvp/admin-token-test",
        SecretString=json.dumps({"token": token}),
    )


def _seed_active_event(event_id: str = "test-event", **overrides) -> dict:
    """Seed canonical event + pure current pointer for local tests."""
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "eventId": event_id,
        "eventSlug": event_id,
        "slug": event_id,
        "event_label": overrides.pop("event_label", "Test Event"),
        "date": overrides.pop("date", "2026-04-18"),
        "startTime": overrides.pop("startTime", "21:00"),
        "city": overrides.pop("city", "Louisville"),
        "capacity": overrides.pop("capacity", 100),
        "event_timezone": overrides.pop("event_timezone", "America/New_York"),
        "event_status": overrides.pop("event_status", "LIVE"),
        "active": True,
        "createdAt": now,
        "updatedAt": now,
    }
    event.update(overrides)
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(os.environ["EVENTS_TABLE_NAME"])
    table.put_item(Item=event)
    table.put_item(Item={
        "eventId": "current",
        "activeEventSlug": event_id,
        "active": True,
        "pointerVersion": 2,
        "updatedAt": now,
    })
    return event



# ── Request factory ───────────────────────────────────────────────────────────

def _event(
    method: str,
    path: str,
    body: Any = None,
    qs: Dict[str, str] | None = None,
    token: str = ADMIN_TOKEN,
) -> dict:
    return {
        "httpMethod": method,
        "path": path,
        "headers": {
            "origin":         "https://admin.rsvpsociety.com",
            "x-admin-token":  token,
        },
        "queryStringParameters": qs or {},
        "body": json.dumps(body) if body is not None else "",
    }


# ── Test cases ────────────────────────────────────────────────────────────────

class TestFinalizeAttendance(unittest.TestCase):
    def test_close_event_marks_unattended_as_no_show_and_locks(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler

            members_t = boto3.resource("dynamodb", region_name="us-east-1").Table(os.environ["MEMBERS_TABLE_NAME"])
            invites_t = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-event-invites-test")

            _seed_active_event("swim-test", date="2026-04-18", endTime="22:00")

            # Member A: confirmed, never checked in -> should become NO_SHOW.
            members_t.put_item(Item={"phone": "+15025550010", "name": "Ghost One", "status": "APPROVED"})
            invites_t.put_item(Item={"eventId": "swim-test", "phone": "+15025550010", "status": "CONFIRMED",
                                     "plusOneName": "Plus Ghost"})  # +1 confirmed, never checked in
            # Member B: confirmed AND attended -> should stay attended, not flipped.
            members_t.put_item(Item={"phone": "+15025550011", "name": "Real Attendee", "status": "APPROVED"})
            invites_t.put_item(Item={"eventId": "swim-test", "phone": "+15025550011", "status": "ATTENDED",
                                     "attendedAt": "2026-04-18T22:30:00Z"})

            # Close the event.
            r = handler(_event("POST", "/admin/events/finalize-attendance", body={"eventSlug": "swim-test"}), None)
            b = json.loads(r["body"])
            self.assertEqual(r["statusCode"], 200, b)
            self.assertTrue(b.get("ok"), b)
            self.assertEqual(b.get("memberNoShows"), 1, f"one member should be no-show: {b}")
            self.assertEqual(b.get("plusOneNoShows"), 1, f"one +1 should be no-show: {b}")

            # Member A invite row is now NO_SHOW; the attended member is untouched.
            row_a = invites_t.get_item(Key={"eventId": "swim-test", "phone": "+15025550010"}).get("Item")
            self.assertEqual(row_a.get("status"), "NO_SHOW")
            self.assertTrue(row_a.get("plusOneNoShowAt"), "plus-one no-show should be stamped")
            row_b = invites_t.get_item(Key={"eventId": "swim-test", "phone": "+15025550011"}).get("Item")
            self.assertEqual(row_b.get("status"), "ATTENDED", "attended guest must not be flipped to no-show")

            # noShowCount incremented so tiers self-correct on next read.
            mem_a = members_t.get_item(Key={"phone": "+15025550010"}).get("Item")
            self.assertEqual(int(mem_a.get("noShowCount") or 0), 1)

            # Locked: re-running returns alreadyFinalized and does NOT double-count.
            r2 = handler(_event("POST", "/admin/events/finalize-attendance", body={"eventSlug": "swim-test"}), None)
            b2 = json.loads(r2["body"])
            self.assertTrue(b2.get("ok"))
            self.assertTrue(b2.get("alreadyFinalized"), f"second close should be a no-op: {b2}")
            mem_a_again = members_t.get_item(Key={"phone": "+15025550010"}).get("Item")
            self.assertEqual(int(mem_a_again.get("noShowCount") or 0), 1, "no double-count on re-close")


class TestPrivacyGateTiers(unittest.TestCase):
    """The leak: non-members (uninvited / declined / +1 with no row) must get NO event
    details — not venue, not address, and not date/time/label either."""
    def _ctx_for_status(self, status):
        import boto3, sms_handler
        members_t = boto3.resource("dynamodb", region_name="us-east-1").Table(os.environ["MEMBERS_TABLE_NAME"])
        invites_t = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-event-invites-test")
        # Unique phone per status so a prior call's invite row never leaks into a
        # later "no invite" (Tier 0) lookup within the same mock session.
        _digit = {"INVITED": "1", "CONFIRMED": "2", "ATTENDED": "3", "DECLINED": "4", None: "0"}
        phone = "+150255590" + _digit.get(status, "8") + "0"
        members_t.put_item(Item={"phone": phone, "name": "Gate Test", "status": "APPROVED"})
        if status is not None:
            invites_t.put_item(Item={"eventId": "swim-test", "phone": phone, "status": status})
        return sms_handler._build_event_context(member={"phone": phone, "name": "Gate Test"})

    def test_gate_tiers(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            _seed_active_event("swim-test", date="2026-06-01", startTime="16:00",
                               endTime="22:00", venue="Myraid Pool", address="100 Charlestown Ct",
                               dresscode="Swim suits", description="Food from Las Mamas. Sections $100.",
                               event_status="LIVE")

            # Tier 0 — no invite row at all (uninvited / plus-one with no own row).
            ctx = self._ctx_for_status(None)
            for leak in ("Myraid Pool", "100 Charlestown", "Las Mamas", "June", "4:00", "16:00", "Swim suits"):
                self.assertNotIn(leak, ctx, f"Tier 0 leaked {leak!r}:\n{ctx}")

            # Tier 0 — DECLINED is also a non-member; same gate.
            import boto3
            boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-event-invites-test").put_item(
                Item={"eventId": "swim-test", "phone": "+15025559002", "status": "DECLINED"})
            import sms_handler
            ctx_dec = sms_handler._build_event_context(member={"phone": "+15025559002", "name": "Declined"})
            for leak in ("Myraid Pool", "100 Charlestown", "June", "4:00", "16:00"):
                self.assertNotIn(leak, ctx_dec, f"declined leaked {leak!r}")

    def test_invited_unconfirmed_gets_teaser_not_venue(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            _seed_active_event("swim-test", date="2026-06-01", startTime="16:00", endTime="22:00",
                               venue="Myraid Pool", address="100 Charlestown Ct",
                               description="Food from Las Mamas.", event_status="LIVE")
            ctx = self._ctx_for_status("INVITED")
            # Teaser allowed: date/time visible.
            self.assertTrue(("June" in ctx) or ("4" in ctx), f"invited should see teaser date/time:\n{ctx}")
            # But venue/address/description still hidden until confirmed.
            for leak in ("Myraid Pool", "100 Charlestown", "Las Mamas"):
                self.assertNotIn(leak, ctx, f"invited-unconfirmed leaked {leak!r}")

    def test_confirmed_gets_full(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            _seed_active_event("swim-test", date="2026-06-01", startTime="16:00", endTime="22:00",
                               venue="Myraid Pool", address="100 Charlestown Ct",
                               sectionInfo="$100 for 4 people + 1 bottle.",
                               description="Food from Las Mamas.", event_status="LIVE")
            ctx = self._ctx_for_status("CONFIRMED")
            self.assertIn("Myraid Pool", ctx, f"confirmed should see venue:\n{ctx}")

    def test_sections_visible_to_invited_not_tier0(self):
        """Section pricing is a teaser (upsell), not confidential. Invited-unconfirmed
        members must see it; non-members (Tier 0) must not."""
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            _seed_active_event("swim-test", date="2026-06-01", startTime="16:00",
                               venue="Myraid Pool", address="100 Charlestown Ct",
                               sectionInfo="$100 for 4 people plus 1 bottle.",
                               event_status="LIVE")
            # Invited, unconfirmed: should SEE section pricing (decision-making upsell).
            ctx_inv = self._ctx_for_status("INVITED")
            self.assertIn("$100", ctx_inv, f"invited should see section pricing:\n{ctx_inv}")
            # But still not the venue/address.
            self.assertNotIn("Myraid Pool", ctx_inv, "invited must still not see venue")
            # Tier 0 (no invite): must NOT see sections.
            ctx_t0 = self._ctx_for_status(None)
            self.assertNotIn("$100", ctx_t0, f"Tier 0 must not see section pricing:\n{ctx_t0}")


class TestDeclineReconfirm(unittest.TestCase):
    """Sticky decline: a declined member who says yes must flip DECLINED -> CONFIRMED."""
    def test_declined_can_reconfirm(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            from admin_handler import handler  # noqa
            import boto3, sms_handler
            sms_handler = importlib.reload(sms_handler)
            _seed_active_event("swim-test", date="2026-06-01", startTime="16:00",
                               capacity=100, event_status="LIVE")
            phone = "+15025559010"
            members_t = boto3.resource("dynamodb", region_name="us-east-1").Table(os.environ["MEMBERS_TABLE_NAME"])
            invites_t = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-event-invites-test")
            members_t.put_item(Item={"phone": phone, "name": "Changed Mind", "status": "APPROVED"})
            invites_t.put_item(Item={"eventId": "swim-test", "phone": phone, "status": "DECLINED"})

            # The reconfirmable lookup must find the DECLINED row.
            found = sms_handler._get_reconfirmable_invite(phone)
            self.assertIsNotNone(found, "declined invite should be reconfirmable")
            self.assertEqual(found.get("status"), "DECLINED")

            # _get_pending_invite (INVITED-only) must NOT find it — proves the bug was real.
            self.assertIsNone(sms_handler._get_pending_invite(phone),
                              "pending lookup should miss a declined row (the original bug)")

    def test_stop_before_yes_transaction_prevents_confirmation_and_capacity_claim(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            import boto3, sms_handler
            sms_handler = importlib.reload(sms_handler)
            event_id = "stop-race-test"
            phone = "+15025559011"
            _seed_active_event(event_id, capacity=10, confirmedHeadcount=0, event_status="LIVE")
            resource = boto3.resource("dynamodb", region_name="us-east-1")
            resource.Table(os.environ["MEMBERS_TABLE_NAME"]).put_item(Item={
                "phone": phone, "status": "APPROVED", "smsOptIn": True, "optOut": True,
            })
            resource.Table(os.environ["INVITES_TABLE_NAME"]).put_item(Item={
                "eventId": event_id, "phone": phone, "status": "INVITED",
            })

            result = sms_handler._confirm_invite_with_capacity(event_id, phone, 10)
            self.assertEqual(result, "NOOP")
            invite = resource.Table(os.environ["INVITES_TABLE_NAME"]).get_item(
                Key={"eventId": event_id, "phone": phone}
            )["Item"]
            event = resource.Table(os.environ["EVENTS_TABLE_NAME"]).get_item(
                Key={"eventId": event_id}
            )["Item"]
            self.assertEqual(invite["status"], "INVITED")
            self.assertEqual(int(event["confirmedHeadcount"]), 0)


class TestVenueReveal(unittest.TestCase):
    """
    Public /event endpoint must always hide venue+address.
    revealVenue only applies to confirmed/private flows, never public discovery.
    """

    def _run(self, reveal: bool) -> dict:
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            _seed_active_event(
                "swim-test",
                event_label="Swim Test",
                date="2026-04-18",
                venue="Tribe",
                address="100 Charlestown Court",
                revealVenue=reveal,
            )

            from admin_handler import handler
            resp = handler(_event("GET", "/event", token=""), None)
            body = json.loads(resp["body"])
            self.assertEqual(resp["statusCode"], 200)
            self.assertTrue(body["ok"])
            return body["event"]

    def test_venue_hidden_when_reveal_false(self):
        event = self._run(reveal=False)
        self.assertNotIn("venue",   event, "venue must be hidden when revealVenue=False")
        self.assertNotIn("address", event, "address must be hidden when revealVenue=False")

    def test_venue_hidden_even_when_reveal_true(self):
        event = self._run(reveal=True)
        self.assertNotIn("venue",   event, "public /event must not expose venue even when revealVenue=True")
        self.assertNotIn("address", event, "public /event must not expose address even when revealVenue=True")
        self.assertNotIn("revealVenue", event, "public /event must not expose revealVenue state")


class TestUnauthorizedRejection(unittest.TestCase):
    """
    All /admin/* endpoints must return 401 for missing or wrong token.
    The public /event endpoint must NOT require a token.
    OPTIONS preflight must always return 200.
    """

    def _call(self, method, path, token):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler
            return handler(_event(method, path, token=token), None)

    def test_missing_token_returns_401(self):
        resp = self._call("GET", "/admin/members", token="")
        self.assertEqual(resp["statusCode"], 401)
        self.assertFalse(json.loads(resp["body"])["ok"])

    def test_wrong_token_returns_401(self):
        resp = self._call("GET", "/admin/members", token="definitely-wrong")
        self.assertEqual(resp["statusCode"], 401)

    def test_public_event_needs_no_token(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            _seed_active_event("swim-test", event_label="Swim Test", date="2026-04-18", revealVenue=False)
            from admin_handler import handler
            resp = handler(_event("GET", "/event", token=""), None)
            self.assertEqual(resp["statusCode"], 200)

    def test_options_preflight_always_200(self):
        resp = self._call("OPTIONS", "/admin/members", token="")
        self.assertEqual(resp["statusCode"], 200)


class TestFirstApprovalWelcome(unittest.TestCase):
    """
    Approving a member for the first time triggers maybe_send_welcome which
    fires send_sms and stamps welcomeSentAt. Approving again does NOT re-send.

    We enable SMS_ENABLED=true to exercise the real gate logic, but stub
    send_sms at the transport layer so no real HTTP calls are made.
    """

    def _approve(self, phone: str, welcome_already_sent: bool = False) -> list:
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            item: dict = {
                "phone": phone, "name": "Test Member",
                "status": "PENDING", "smsOptIn": True,
            }
            if welcome_already_sent:
                item["welcomeSentAt"] = "2026-01-01T00:00:00+00:00"
            boto3.resource("dynamodb").Table("rsvp-members-test").put_item(Item=item)

            sms_calls = []

            def fake_send_sms(to_phone, message):
                sms_calls.append(to_phone)

            import sms_adapter
            with patch.dict(os.environ, {"SMS_ENABLED": "true"}):
                with patch.object(sms_adapter, "send_sms", side_effect=fake_send_sms):
                    from admin_handler import handler
                    resp = handler(
                        _event("POST", "/admin/members/status",
                               body={"phone": phone, "status": "APPROVED"}),
                        None,
                    )

            self.assertEqual(resp["statusCode"], 200)
            return sms_calls

    def test_first_approval_sends_welcome(self):
        calls = self._approve("+15025550001", welcome_already_sent=False)
        self.assertEqual(len(calls), 1, "send_sms should fire exactly once on first approval")

    def test_repeat_approval_skips_welcome(self):
        calls = self._approve("+15025550002", welcome_already_sent=True)
        self.assertEqual(len(calls), 0, "send_sms must not fire when welcomeSentAt already set")


class TestDeleteTombstoning(unittest.TestCase):
    """
    Deleting a member must wipe PII from the member row, set status=DELETED,
    and tombstone ALL invite rows for that phone across all events.
    """

    def test_delete_wipes_pii_and_tombstones_invites(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            phone     = "+15025550003"
            from sms_plus_one import plus_one_reservation_key
            guest_key = plus_one_reservation_key("Taylor Guest", deps={})
            members_t = boto3.resource("dynamodb").Table("rsvp-members-test")
            invites_t = boto3.resource("dynamodb").Table("rsvp-event-invites-test")
            events_t = boto3.resource("dynamodb").Table("rsvp-events-test")

            members_t.put_item(Item={
                "phone": phone, "name": "Delete Me", "email": "del@test.com",
                "status": "APPROVED", "smsOptIn": True, "zipCode": "40205",
                "city": "Louisville", "state": "KY", "latitude": Decimal("38.22"),
                "longitude": Decimal("-85.68"), "locationSource": "zip",
            })
            invites_t.put_item(Item={"eventId": "swim-test", "phone": phone, "status": "CONFIRMED", "plusOneName": "Taylor Guest"})
            invites_t.put_item(Item={"eventId": "event-2", "phone": phone, "status": "INVITED"})
            events_t.put_item(Item={"eventId": "swim-test", "plusOneReservations": {guest_key: phone}})

            from admin_handler import handler
            resp = handler(_event("DELETE", "/admin/members", body={"phone": phone, "confirmPhone": phone}), None)
            self.assertEqual(resp["statusCode"], 200)

            member = members_t.get_item(Key={"phone": phone})["Item"]
            self.assertEqual(member["status"], "DELETED")
            self.assertNotIn("name",     member, "name must be wiped")
            self.assertNotIn("email",    member, "email must be wiped")
            self.assertNotIn("smsOptIn", member, "smsOptIn must be wiped")
            for location_field in ("zipCode", "city", "state", "latitude", "longitude", "locationSource"):
                self.assertNotIn(location_field, member, f"{location_field} must be wiped")

            for event_id in ("swim-test", "event-2"):
                invite = invites_t.get_item(Key={"eventId": event_id, "phone": phone})["Item"]
                self.assertEqual(invite["status"], "DELETED",
                                 f"invite for {event_id} must be tombstoned")
            event = events_t.get_item(Key={"eventId": "swim-test"})["Item"]
            self.assertNotIn(guest_key, event.get("plusOneReservations", {}))
            self.assertEqual(int(event.get("confirmedHeadcount") or 0), 0)
            self.assertNotIn("plusOneName", invites_t.get_item(Key={"eventId": "swim-test", "phone": phone})["Item"])

    def test_confirmed_member_cancel_releases_member_and_plus_one_slots_atomically(self):
        with mock_aws():
            _create_tables()
            _reload_lambda_modules()
            from sms_handler import _cancel_confirmed_invite
            from sms_plus_one import plus_one_reservation_key

            phone = "+15025550009"
            guest_name = "Taylor Guest"
            guest_key = plus_one_reservation_key(guest_name, deps={})
            events = boto3.resource("dynamodb").Table("rsvp-events-test")
            invites = boto3.resource("dynamodb").Table("rsvp-event-invites-test")
            events.put_item(Item={
                "eventId": "cancel-event", "event_status": "LIVE", "confirmedHeadcount": 2,
                "plusOneReservations": {guest_key: phone},
            })
            invites.put_item(Item={
                "eventId": "cancel-event", "phone": phone, "status": "CONFIRMED",
                "plusOneName": guest_name,
            })

            self.assertTrue(_cancel_confirmed_invite("cancel-event", phone))
            invite = invites.get_item(Key={"eventId": "cancel-event", "phone": phone})["Item"]
            event = events.get_item(Key={"eventId": "cancel-event"})["Item"]
            self.assertEqual(invite["status"], "DECLINED")
            self.assertNotIn("plusOneName", invite)
            self.assertEqual(int(event["confirmedHeadcount"]), 0)
            self.assertNotIn(guest_key, event.get("plusOneReservations", {}))
            self.assertFalse(_cancel_confirmed_invite("cancel-event", phone))

    def test_plus_one_reservation_from_a_previously_deleted_member_is_reclaimed(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            import boto3, sms_handler
            sms_handler = importlib.reload(sms_handler)
            event_id = "legacy-stale-plus-one"
            owner_phone = "+15025550004"
            new_host_phone = "+15025550005"
            guest_name = "Taylor Guest"
            from sms_plus_one import plus_one_reservation_key
            guest_key = plus_one_reservation_key(guest_name, deps={})
            resource = boto3.resource("dynamodb", region_name="us-east-1")
            resource.Table(os.environ["EVENTS_TABLE_NAME"]).put_item(Item={
                "eventId": event_id,
                "event_status": "LIVE",
                "capacity": 100,
                "confirmedHeadcount": 1,
                "plusOneReservations": {guest_key: owner_phone},
            })
            members = resource.Table(os.environ["MEMBERS_TABLE_NAME"])
            members.put_item(Item={"phone": owner_phone, "status": "DELETED"})
            members.put_item(Item={"phone": new_host_phone, "status": "APPROVED", "smsOptIn": True})
            invites = resource.Table(os.environ["INVITES_TABLE_NAME"])
            invites.put_item(Item={
                "eventId": event_id, "phone": owner_phone,
                "status": "DELETED", "plusOneName": guest_name,
            })
            invites.put_item(Item={"eventId": event_id, "phone": new_host_phone, "status": "CONFIRMED"})

            result = sms_handler._set_plus_one(event_id, new_host_phone, guest_name, False)
            self.assertEqual(result, "SAVED")
            event = resource.Table(os.environ["EVENTS_TABLE_NAME"]).get_item(
                Key={"eventId": event_id}
            )["Item"]
            self.assertEqual(event["plusOneReservations"][guest_key], new_host_phone)


class TestDuplicateAttendance(unittest.TestCase):
    """
    Tapping the same phone twice must not double-increment attendance.
    Second tap returns alreadyCheckedIn=True.
    """

    def test_duplicate_checkin_flagged(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            phone     = "+15025550004"
            members_t = boto3.resource("dynamodb").Table("rsvp-members-test")
            invites_t = boto3.resource("dynamodb").Table("rsvp-event-invites-test")

            members_t.put_item(Item={"phone": phone, "name": "Double Tap", "status": "APPROVED"})
            invites_t.put_item(Item={"eventId": "swim-test", "phone": phone, "status": "CONFIRMED"})

            body = {"phone": phone, "attended": True, "eventId": "swim-test"}

            from admin_handler import handler

            r1 = handler(_event("POST", "/admin/members/attendance", body=body), None)
            b1 = json.loads(r1["body"])
            self.assertEqual(r1["statusCode"], 200)
            self.assertTrue(b1.get("ok"), f"first tap should succeed: {b1}")

            r2 = handler(_event("POST", "/admin/members/attendance", body=body), None)
            b2 = json.loads(r2["body"])
            self.assertEqual(r2["statusCode"], 409)
            self.assertFalse(b2.get("ok"), f"second tap should fail: {b2}")
            self.assertEqual(b2.get("result"), "ALREADY_CHECKED_IN")

    def test_attendance_string_false_does_not_check_in(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler

            phone = "+15025550123"
            # Create the member so the existence guard passes
            boto3.resource("dynamodb", region_name="us-east-1").Table(
                os.environ["MEMBERS_TABLE_NAME"]
            ).put_item(Item={"phone": phone, "name": "Test", "status": "APPROVED"})

            _seed_active_event("swim-test", date="2026-04-18")

            resp = handler(_event("POST", "/admin/members/attendance", body={"phone": phone, "attended": "false"}), None)
            self.assertEqual(resp["statusCode"], 400)
            body = json.loads(resp["body"])
            self.assertFalse(body.get("ok"))
            self.assertIn(body.get("result"), {"INVITE_NOT_FOUND", "NOT_CONFIRMED"})



class TestEventSaveValidation(unittest.TestCase):
    """
    POST /admin/event rejects invalid capacity; accepts valid payloads;
    revealVenue is coerced to bool.
    """

    def _save(self, body: dict) -> dict:
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler
            return handler(_event("POST", "/admin/event", body=body), None)

    def test_negative_capacity_rejected(self):
        resp = self._save({"date": "2026-04-18", "eventSlug": "neg-cap", "capacity": -1})
        self.assertEqual(resp["statusCode"], 400)
        body = json.loads(resp["body"])
        self.assertFalse(body["ok"])
        self.assertIn("capacity", body["error"])

    def test_non_numeric_capacity_rejected(self):
        resp = self._save({"date": "2026-04-18", "capacity": "lots"})
        self.assertEqual(resp["statusCode"], 400)

    def test_valid_event_saves(self):
        resp = self._save({
            "date":       "2026-04-18",
            "eventSlug":  "derby-night",
            "capacity":   120,
            "venue":      "The Venue",
            "revealVenue": False,
        })
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["event"]["eventSlug"], "derby-night")
        self.assertEqual(body["event"]["capacity"],  120)
        self.assertIs(body["event"]["revealVenue"],  False)

    def test_string_false_reveal_venue_is_false(self):
        resp = self._save({
            "date": "2026-04-18",
            "eventSlug": "test-reveal",
            "capacity": 120,
            "revealVenue": "false",
        })
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIs(body["event"]["revealVenue"], False)

    def test_zero_capacity_accepted(self):
        resp = self._save({"date": "2026-04-18", "eventSlug": "test-zero", "capacity": 0})
        self.assertEqual(resp["statusCode"], 200)

    def test_event_save_overwrites_in_place_same_slug(self):
        # ACTUAL CONTRACT: a normal save overwrites the event record in place for the
        # same slug. Snapshot-on-every-save is NOT a built feature (history is written
        # only on explicit archive). This test pins current behavior; if snapshot-on-save
        # is later desired, that is a deliberate product change (write-amplification),
        # not a bug. See fix-batch spec, PART 7 (open product decision).
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler

            for venue in ("Venue A", "Venue B", "Venue C"):
                resp = handler(_event("POST", "/admin/event", body={
                    "date": "2026-04-18",
                    "eventSlug": "derby-night",
                    "capacity": 120,
                    "venue": venue,
                }), None)
                self.assertEqual(resp["statusCode"], 200)

            events = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-events-test")
            row = events.get_item(Key={"eventId": "derby-night"}).get("Item")
            self.assertIsNotNone(row)
            self.assertEqual(row["venue"], "Venue C")  # last write wins, in place

    def test_event_history_archives_previous_current(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler

            first = handler(_event("POST", "/admin/event", body={
                "date": "2026-04-18",
                "eventSlug": "derby-night",
                "capacity": 120,
                "venue": "The Venue",
            }), None)
            self.assertEqual(first["statusCode"], 200)

            second = handler(_event("POST", "/admin/event", body={
                "date": "2026-05-01",
                "eventSlug": "rooftop-fridays",
                "capacity": 140,
                "venue": "Skyline",
            }), None)
            self.assertEqual(second["statusCode"], 200)

            listed = handler(_event("GET", "/admin/events"), None)
            self.assertEqual(listed["statusCode"], 200)
            payload = json.loads(listed["body"])
            self.assertTrue(payload["ok"])
            slugs = {event.get("slug") for event in payload.get("events", [])}
            self.assertIn("derby-night", slugs)
            self.assertIn("rooftop-fridays", slugs)


class TestEventSchedulingNormalization(unittest.TestCase):
    def test_event_save_normalizes_iso_date_and_send_times(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler

            resp = handler(_event("POST", "/admin/event", body={
                "eventSlug": "rooftop-may-2026",
                "event_label": "Rooftop Night",
                "date": "Saturday, May 15, 2026",
                "startTime": "9:00 PM",
                "city": "Louisville, KY",
                "capacity": 150,
                "event_timezone": "America/New_York",
                "venue": "Skybar",
                "address": "123 Main",
                "dresscode": "All Black",
                "revealVenue": True,
                "reminderTiming": "both",
                "day_before_send_time": "5:30 PM",
                "day_of_send_time": "10:15 AM",
            }), None)
            self.assertEqual(resp["statusCode"], 200)
            body = json.loads(resp["body"])
            self.assertEqual(body["event"]["date"], "2026-05-15")
            self.assertEqual(body["event"]["startTime"], "21:00")
            self.assertEqual(body["event"]["day_before_send_time"], "17:30")
            self.assertEqual(body["event"]["day_of_send_time"], "10:15")

    def test_event_save_rejects_bad_send_time(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler

            resp = handler(_event("POST", "/admin/event", body={
                "eventSlug": "bad-time",
                "event_label": "Bad Time",
                "date": "2026-05-15",
                "startTime": "21:00",
                "city": "Louisville, KY",
                "capacity": 20,
                "event_timezone": "America/New_York",
                "day_before_send_time": "banana",
            }), None)
            self.assertEqual(resp["statusCode"], 400)
            self.assertIn("day_before_send_time", json.loads(resp["body"])["error"])

    def test_event_save_rejects_non_aligned_send_time(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler

            resp = handler(_event("POST", "/admin/event", body={
                "eventSlug": "bad-minute",
                "event_label": "Bad Minute",
                "date": "2026-05-15",
                "startTime": "21:00",
                "city": "Louisville, KY",
                "capacity": 20,
                "event_timezone": "America/New_York",
                "day_before_send_time": "17:32",
            }), None)
            self.assertEqual(resp["statusCode"], 400)
            self.assertIn("5-minute intervals", json.loads(resp["body"])["error"])


class TestReminderSchedulePrecision(unittest.TestCase):
    def test_scheduled_reminder_waits_for_exact_local_minute(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            _seed_active_event(
                "precision-night",
                date="2026-05-15",
                event_timezone="America/New_York",
                reminderTiming="day_before",
                day_before_send_time="17:30",
            )

            import reminder_handler

            class FakeDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    base = datetime(2026, 5, 14, 17, 25, tzinfo=timezone.utc)
                    return base.astimezone(tz) if tz else base

            with patch.object(reminder_handler, 'datetime', FakeDateTime):
                result = reminder_handler.handler({"source": "eventbridge", "timing": "day_before"}, None)
            self.assertTrue(result["skipped"])
            self.assertEqual(result["reason"], "not the right minute")

# ── Runner ────────────────────────────────────────────────────────────────────

class TestAccessRequestNameCapture(unittest.TestCase):
    def test_access_request_stores_last_name_from_structured_fields(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            lookup = patch("access_request.resolve_us_zip", return_value={"zipCode": "40205", "city": "Louisville", "state": "KY", "latitude": 38.22, "longitude": -85.68, "locationSource": "zip"})
            lookup.start()
            self.addCleanup(lookup.stop)

            from access_request import handler
            resp = handler({
                "httpMethod": "POST",
                "path": "/access",
                "headers": {"origin": "https://admin.rsvpsociety.com"},
                "body": json.dumps({
                    "firstName": "Josh",
                    "lastName": "Holman",
                    "phone": "5550100001",
                    "source": "web",
                    "zipCode": "40205", "smsOptIn": True,
                }),
            }, None)

            self.assertEqual(resp["statusCode"], 200)
            item = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-members-test").get_item(
                Key={"phone": "+15550100001"}
            )["Item"]
            self.assertEqual(item["name"], "Josh")
            self.assertEqual(item["lastName"], "Holman")

    def test_access_request_splits_legacy_name_as_fallback(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            lookup = patch("access_request.resolve_us_zip", return_value={"zipCode": "40205", "city": "Louisville", "state": "KY", "latitude": 38.22, "longitude": -85.68, "locationSource": "zip"})
            lookup.start()
            self.addCleanup(lookup.stop)

            from access_request import handler
            resp = handler({
                "httpMethod": "POST",
                "path": "/access",
                "headers": {"origin": "https://admin.rsvpsociety.com"},
                "body": json.dumps({
                    "name": "Josh Holman",
                    "phone": "5550100001",
                    "source": "web",
                    "zipCode": "40205", "smsOptIn": True,
                }),
            }, None)

            self.assertEqual(resp["statusCode"], 200)
            item = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-members-test").get_item(
                Key={"phone": "+15550100001"}
            )["Item"]
            self.assertEqual(item["name"], "Josh")
            self.assertEqual(item["lastName"], "Holman")


class TestLegacyNameNormalization(unittest.TestCase):
    def test_list_members_normalizes_legacy_full_name(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-members-test").put_item(Item={
                "phone": "+15550100001",
                "name": "Josh Holman",
                "status": "APPROVED",
                "createdAt": "2026-03-06T12:00:00+00:00",
            })

            from admin_handler import handler
            resp = handler(_event("GET", "/admin/members", qs={"status": "APPROVED"}), None)
            body = json.loads(resp["body"])
            self.assertEqual(resp["statusCode"], 200)
            self.assertEqual(body["members"][0]["name"], "Josh")
            self.assertEqual(body["members"][0]["lastName"], "Holman")

    def test_search_members_matches_last_name_from_legacy_full_name(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-members-test").put_item(Item={
                "phone": "+15550100001",
                "name": "Josh Holman",
                "status": "APPROVED",
                "createdAt": "2026-03-06T12:00:00+00:00",
            })

            import member_store
            matches = member_store.search_members("holman")
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["name"], "Josh")
            self.assertEqual(matches[0]["lastName"], "Holman")

    def test_get_confirmed_normalizes_legacy_member_name(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.Table("rsvp-members-test").put_item(Item={
                "phone": "+15550100001",
                "name": "Josh Holman",
                "status": "APPROVED",
                "createdAt": "2026-03-06T12:00:00+00:00",
            })
            ddb.Table("rsvp-event-invites-test").put_item(Item={
                "eventId": "Swim Test",
                "phone": "+15550100001",
                "status": "CONFIRMED",
                "confirmedAt": "2026-03-06T12:05:00+00:00",
            })

            from admin_handler import handler
            resp = handler(_event("GET", "/admin/members/confirmed", qs={"eventId": "Swim Test"}), None)
            body = json.loads(resp["body"])
            self.assertEqual(resp["statusCode"], 200)
            self.assertEqual(body["members"][0]["name"], "Josh")
            self.assertEqual(body["members"][0]["lastName"], "Holman")
            self.assertEqual(body["members"][0]["fullName"], "Josh Holman")


class TestApprovalQueueMultiSlot(unittest.TestCase):
    """Burst signups each create their own member record with unique queue keys — no overwrite."""

    def test_two_signups_create_separate_member_records(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            lookup = patch("access_request.resolve_us_zip", return_value={"zipCode": "40205", "city": "Louisville", "state": "KY", "latitude": 38.22, "longitude": -85.68, "locationSource": "zip"})
            lookup.start()
            self.addCleanup(lookup.stop)

            import access_request
            with patch("access_request.send_sms", return_value=True):
                r1 = access_request.handler(_event("POST", "/access", {
                    "phone": "+15025550001", "name": "Alice", "lastName": "A", "zipCode": "40205", "smsOptIn": True
                }, token=""), None)
                r2 = access_request.handler(_event("POST", "/access", {
                    "phone": "+15025550002", "name": "Bob", "lastName": "B", "zipCode": "40205", "smsOptIn": True
                }, token=""), None)

            self.assertEqual(r1["statusCode"], 200)
            self.assertEqual(r2["statusCode"], 200)

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            t = ddb.Table("rsvp-members-test")
            alice = t.get_item(Key={"phone": "+15025550001"}).get("Item")
            bob   = t.get_item(Key={"phone": "+15025550002"}).get("Item")
            self.assertIsNotNone(alice)
            self.assertIsNotNone(bob)


class TestDualHostFinalization(unittest.TestCase):
    """Once one host approves/denies, the second host's pending record is cleared."""

    def _setup_pending(self, sms_handler, host1, member_phone):
        """Write a pending_approval record for both hosts using new queue key format."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pending_t = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-pending-approvals-test")
        # Dedicated pending approvals table: pk=hostPhone, sk=memberPhone
        for host in [host1, "+15559990002"]:
            pending_t.put_item(Item={
                "hostPhone": host,
                "memberPhone": member_phone,
                "memberName": "Test User",
                "storedAt": now,
                "status": "PENDING",
                "approvalCode": "123456",  # fixed code for test predictability
            })

    def test_second_host_cannot_reverse_after_first_approves(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            os.environ["HOST_PHONE_1"] = "+15559990001"
            os.environ["HOST_PHONE_2"] = "+15559990002"
            os.environ["ALLOW_UNSIGNED_WEBHOOK_DEV"] = "true"
            os.environ["SMS_ENABLED"] = "true"
            os.environ.pop("WEBHOOK_SECRET_ID", None)

            member_phone = "+15025550099"
            boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-members-test").put_item(Item={
                "phone": member_phone, "name": "Test", "status": "PENDING",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })

            import sms_handler
            self._setup_pending(sms_handler, "+15559990001", member_phone)

            with patch("sms_handler.send_sms", return_value=True):
                # Host 1 approves
                sms_handler.handler(_event("POST", "/sms/inbound", {
                    "from": "+15559990001", "body": "Y 123456"
                }, token=""), None)

                # Host 2 tries to deny — should be ignored (record finalized)
                sms_handler.handler(_event("POST", "/sms/inbound", {
                    "from": "+15559990002", "body": "N 123456"
                }, token=""), None)

            import member_store
            member = member_store.get_member(member_phone)
            # Status should remain APPROVED — Host 2's N was ignored
            self.assertEqual(member["status"], "APPROVED")


class TestOptOutEnforcedInReminders(unittest.TestCase):
    """Members with optOut=True are skipped during reminder sends."""

    def test_opted_out_member_receives_no_reminder(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            # Opted-out member with a CONFIRMED invite
            ddb.Table("rsvp-members-test").put_item(Item={
                "phone": "+15025550010", "name": "Opted", "status": "APPROVED",
                "optOut": True,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            ddb.Table("rsvp-event-invites-test").put_item(Item={
                "eventId": "test-event", "phone": "+15025550010",
                "status": "CONFIRMED",
            })
            _seed_active_event("test-event", date="2026-04-05", startTime="21:00", venue="The Venue", reminderTiming="day_before")

            sent_to = []
            import reminder_handler
            event_record = {
                "eventId": "test-event",
                "eventSlug": "test-event",
                "date": "2026-04-05",
                "startTime": "21:00",
                "venue": "The Venue",
                "reminderTiming": "day_before",
            }
            with patch.object(reminder_handler, "send_sms", side_effect=lambda phone, msg: sent_to.append(phone) or True):
                reminder_handler.send_reminders(event_record, is_day_of=False)

            self.assertNotIn("+15025550010", sent_to)


class TestManualReminderContinuation(unittest.TestCase):
    def test_manual_custom_reminder_job_is_chunked_and_retry_safe(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            phone = "+15025550031"
            ddb.Table("rsvp-members-test").put_item(Item={
                "phone": phone, "name": "Jordan Smith", "status": "APPROVED",
                "smsOptIn": True, "optOut": False,
            })
            ddb.Table("rsvp-event-invites-test").put_item(Item={
                "eventId": "manual-reminder-event", "phone": phone,
                "name": "Jordan Smith", "status": "CONFIRMED",
            })
            _seed_active_event(
                "manual-reminder-event", date="2026-09-29", reminderTiming="manual",
            )

            import reminder_handler
            lambda_client = MagicMock()
            lambda_client.invoke.return_value = {"StatusCode": 202}
            event_record = {
                "eventId": "manual-reminder-event", "eventSlug": "manual-reminder-event",
                "event_status": "LIVE",
            }
            with patch.dict(os.environ, {"SMS_ENABLED": "true", "AWS_LAMBDA_FUNCTION_NAME": "rsvp-reminder-handler"}), \
                 patch.object(reminder_handler.boto3, "client", return_value=lambda_client), \
                 patch.object(reminder_handler, "send_sms") as send_sms:
                queued = reminder_handler._start_manual_reminder_job(
                    event_record, True, "Hi {name}, event update.", "test-admin-token-abc123"
                )
                payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
                complete = reminder_handler.handler(payload, None)
                retried = reminder_handler.handler(payload, None)

            self.assertTrue(queued["queued"])
            self.assertTrue(complete["complete"])
            self.assertTrue(retried["complete"])
            send_sms.assert_called_once_with(phone, "Hi Jordan, event update.")
            jobs = ddb.Table("rsvp-invite-jobs-test")
            master = jobs.get_item(Key={"jobId": queued["jobId"]}, ConsistentRead=True)["Item"]
            self.assertEqual(master["status"], "COMPLETE")
            self.assertEqual(master["smsSent"], 1)
            marker_id = f"{queued['jobId']}:recipient:{reminder_handler.hashlib.sha256(phone.encode()).hexdigest()}"
            marker = jobs.get_item(Key={"jobId": marker_id}, ConsistentRead=True)["Item"]
            self.assertEqual(marker["status"], "SENT")


class TestInviteWriteWithoutSend(unittest.TestCase):
    """Opted-out/non-opted members stay out of the active invite pool."""

    def test_non_opted_member_is_not_invited_or_texted(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.Table("rsvp-members-test").put_item(Item={
                "phone": "+15025550020", "name": "Fail", "lastName": "Test",
                "status": "APPROVED", "smsOptIn": False,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            _seed_active_event("test-event", date="2026-04-05", startTime="21:00", venue="The Venue")

            old_sms_enabled = os.environ.get("SMS_ENABLED")
            os.environ["SMS_ENABLED"] = "true"
            import invite_handler
            sent_to = []
            with patch.object(invite_handler, "send_sms", side_effect=lambda p, m: sent_to.append(p) or True):
                invite_handler._write_job("test-job", {
                    "confirmSend": True,
                    "eventId": "test-event",
                    "capacity": 10,
                    "waveNumber": 1,
                    "waveSize": 1,
                    "femalePercent": 60,
                    "phones": ["+15025550020"],
                    "removedPhones": [],
                }, "https://admin.rsvpsociety.com")
                invite_handler._execute_send(
                    body={
                        "confirmSend": True,
                        "eventId": "test-event",
                        "capacity": 10,
                        "waveNumber": 1,
                        "waveSize": 1,
                        "femalePercent": 60,
                            "phones": ["+15025550020"],
                        "removedPhones": [],
                    },
                    origin="https://admin.rsvpsociety.com",
                    token="test-token",
                    job_id="test-job",
                )
            if old_sms_enabled is None:
                os.environ.pop("SMS_ENABLED", None)
            else:
                os.environ["SMS_ENABLED"] = old_sms_enabled

            invite = ddb.Table("rsvp-event-invites-test").get_item(
                Key={"eventId": "test-event", "phone": "+15025550020"}
            ).get("Item")
            self.assertIsNone(invite, "Inactive/non-opted members should not receive invite rows")
            self.assertNotIn("+15025550020", sent_to, "No SMS should be sent")


class TestCheckinNoDoubleConfirmedCount(unittest.TestCase):
    """Physical check-in increments attendedCount only — not confirmedCount."""

    def test_checkin_does_not_increment_confirmed_count(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.Table("rsvp-members-test").put_item(Item={
                "phone": "+15025550030", "name": "Check", "status": "APPROVED",
                "confirmedCount": 1,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            # Must have CONFIRMED invite row — check-in now requires it
            ddb.Table("rsvp-event-invites-test").put_item(Item={
                "eventId": "test-event",
                "phone":   "+15025550030",
                "status":  "CONFIRMED",
            })

            import member_store
            result = member_store.record_attendance("+15025550030", attended=True, event_id="test-event")
            self.assertTrue(result["ok"], f"Check-in failed: {result}")
            self.assertEqual(result["result"], member_store.ATTENDANCE_OK)

            member = member_store.get_member("+15025550030")
            self.assertEqual(int(member["attendedCount"]), 1)
            self.assertEqual(int(member["confirmedCount"]), 1)

    def test_duplicate_checkin_returns_false(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.Table("rsvp-members-test").put_item(Item={
                "phone": "+15025550031", "name": "Dup", "status": "APPROVED",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            # Must have CONFIRMED invite row
            ddb.Table("rsvp-event-invites-test").put_item(Item={
                "eventId": "test-event",
                "phone":   "+15025550031",
                "status":  "CONFIRMED",
            })

            import member_store
            first  = member_store.record_attendance("+15025550031", attended=True, event_id="test-event")
            second = member_store.record_attendance("+15025550031", attended=True, event_id="test-event")
            self.assertTrue(first["ok"], f"First check-in failed: {first}")
            self.assertFalse(second["ok"], f"Duplicate should fail: {second}")
            self.assertEqual(second["result"], member_store.ATTENDANCE_ALREADY,
                             f"Duplicate check-in must return ALREADY_CHECKED_IN, got: {second['result']}")

            member = member_store.get_member("+15025550031")
            self.assertEqual(int(member["attendedCount"]), 1)


class TestDateNormalization(unittest.TestCase):
    """normalize_event_date handles all real-world input formats including no-comma weekday."""

    def test_all_date_formats_parse(self):
        from admin_shared import normalize_event_date
        cases = [
            ("2026-03-07",              "2026-03-07"),
            ("03/07/2026",              "2026-03-07"),
            ("Saturday, March 7, 2026", "2026-03-07"),
            ("Saturday March 7, 2026",  "2026-03-07"),  # the bug format
            ("Sat, March 7, 2026",      "2026-03-07"),
            ("Sat March 7, 2026",       "2026-03-07"),
            ("March 7, 2026",           "2026-03-07"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_event_date(raw), expected)

    def test_invalid_date_raises(self):
        from admin_shared import normalize_event_date
        with self.assertRaises(ValueError):
            normalize_event_date("not a date")

class TestJadeTicketParkingGate(unittest.TestCase):
    """Ticket URL and parking info must not reach Jade before CONFIRMED/ATTENDED."""

    def test_ticket_url_blocked_for_invited_not_confirmed(self):
        """INVITED member gets no ticket URL — must confirm first."""
        import sms_handler
        fake_ev = {
            "eventId": "test-event", "eventSlug": "test-event", "event_label": "Test",
            "date": "2026-06-01", "ticketUrl": "https://posh.vip/test",
            "revealVenue": True, "event_status": "LIVE",
        }
        fake_member = {"phone": "+15550001", "status": "INVITED"}
        orig = sms_handler._events_table
        def fake_event_get_item(self, **kw):
            key = (kw.get("Key") or {}).get("eventId")
            if key == "current":
                return {"Item": {"eventId": "current", "activeEventSlug": "test-event", "active": True, "pointerVersion": 2}}
            return {"Item": fake_ev} if key == "test-event" else {}
        sms_handler._events_table = lambda: type("T", (), {"get_item": fake_event_get_item})()
        orig_inv = sms_handler._invites_table
        sms_handler._invites_table = lambda: type("T", (), {
            "query": lambda self, **kw: {"Items": [{"eventId": "test-event", "phone": "+15550001", "status": "INVITED"}]},
            "get_item": lambda self, **kw: {"Item": {"status": "INVITED"}}
        })()
        try:
            context = sms_handler._build_event_context(member=fake_member)
        finally:
            sms_handler._events_table  = orig
            sms_handler._invites_table = orig_inv
        self.assertNotIn("ticket_url", context,
                         "ticket_url must not appear in Jade context for INVITED member")

    def test_parking_info_blocked_before_confirmation(self):
        """Parking info must not appear in Jade context before CONFIRMED."""
        import sms_handler
        fake_ev = {
            "eventId": "test-event", "eventSlug": "test-event", "event_label": "Test",
            "date": "2026-06-01", "parkingInfo": "Parking behind The Tribe.",
            "revealVenue": True, "event_status": "LIVE",
        }
        fake_member = {"phone": "+15550001", "status": "INVITED"}
        orig = sms_handler._events_table
        def fake_event_get_item(self, **kw):
            key = (kw.get("Key") or {}).get("eventId")
            if key == "current":
                return {"Item": {"eventId": "current", "activeEventSlug": "test-event", "active": True, "pointerVersion": 2}}
            return {"Item": fake_ev} if key == "test-event" else {}
        sms_handler._events_table = lambda: type("T", (), {"get_item": fake_event_get_item})()
        orig_inv = sms_handler._invites_table
        sms_handler._invites_table = lambda: type("T", (), {
            "query": lambda self, **kw: {"Items": [{"eventId": "test-event", "phone": "+15550001", "status": "INVITED"}]},
            "get_item": lambda self, **kw: {"Item": {"status": "INVITED"}}
        })()
        try:
            context = sms_handler._build_event_context(member=fake_member)
        finally:
            sms_handler._events_table  = orig
            sms_handler._invites_table = orig_inv
        self.assertNotIn("parking_info", context,
                         "parking_info must not appear before confirmation — can reveal venue")

    def test_ticket_url_visible_after_confirmation(self):
        """CONFIRMED member can see ticket URL."""
        import sms_handler
        fake_ev = {
            "eventId": "test-event", "eventSlug": "test-event", "event_label": "Test",
            "date": "2026-06-01", "ticketUrl": "https://posh.vip/test",
            "revealVenue": True, "event_status": "LIVE",
        }
        fake_member = {"phone": "+15550001", "status": "CONFIRMED"}
        orig = sms_handler._events_table
        def fake_event_get_item(self, **kw):
            key = (kw.get("Key") or {}).get("eventId")
            if key == "current":
                return {"Item": {"eventId": "current", "activeEventSlug": "test-event", "active": True, "pointerVersion": 2}}
            return {"Item": fake_ev} if key == "test-event" else {}
        sms_handler._events_table = lambda: type("T", (), {"get_item": fake_event_get_item})()
        orig_inv = sms_handler._invites_table
        sms_handler._invites_table = lambda: type("T", (), {
            "query": lambda self, **kw: {"Items": []},
            "get_item": lambda self, **kw: {"Item": {"status": "CONFIRMED"}}
        })()
        try:
            context = sms_handler._build_event_context(member=fake_member)
        finally:
            sms_handler._events_table  = orig
            sms_handler._invites_table = orig_inv
        self.assertIn("ticket_url", context,
                      "ticket_url must be visible to CONFIRMED member")


class TestTransactionalAttendanceState(unittest.TestCase):
    """Attendance/no-show state transitions must be strict and side-effect safe."""

    def _seed_member(self, phone: str):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.Table("rsvp-members-test").put_item(Item={
            "phone": phone,
            "name": "State",
            "status": "APPROVED",
            "createdAt": datetime.now(timezone.utc).isoformat(),
        })

    def _seed_invite(self, phone: str, status: str):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.Table("rsvp-event-invites-test").put_item(Item={
            "eventId": "test-event",
            "phone": phone,
            "status": status,
        })

    def test_invited_cannot_check_in_and_no_side_effects(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            phone = "+15025550080"
            self._seed_member(phone)
            self._seed_invite(phone, "INVITED")

            import member_store
            result = member_store.record_attendance(phone, attended=True, event_id="test-event")
            self.assertFalse(result["ok"])
            self.assertIn(result["result"], {
                member_store.ATTENDANCE_NOT_CONFIRMED,
                member_store.ATTENDANCE_INVALID_STATUS,
            })

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            invite = ddb.Table("rsvp-event-invites-test").get_item(
                Key={"eventId": "test-event", "phone": phone}
            )["Item"]
            self.assertEqual(invite["status"], "INVITED")
            checkin = ddb.Table("rsvp-checkins-test").get_item(
                Key={"eventId": "test-event", "phone": phone}
            ).get("Item")
            self.assertIsNone(checkin)
            member = member_store.get_member(phone)
            self.assertNotIn("attendedCount", member)

    def test_confirmed_no_show_updates_only_valid_invite(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            phone = "+15025550081"
            self._seed_member(phone)
            self._seed_invite(phone, "CONFIRMED")

            import member_store
            result = member_store.record_attendance(phone, attended=False, event_id="test-event")
            self.assertTrue(result["ok"], f"No-show failed: {result}")

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            invite = ddb.Table("rsvp-event-invites-test").get_item(
                Key={"eventId": "test-event", "phone": phone}
            )["Item"]
            self.assertEqual(invite["status"], "NO_SHOW")
            member = member_store.get_member(phone)
            self.assertEqual(int(member["noShowCount"]), 1)

    def test_invited_cannot_be_marked_no_show(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            phone = "+15025550082"
            self._seed_member(phone)
            self._seed_invite(phone, "INVITED")

            import member_store
            result = member_store.record_attendance(phone, attended=False, event_id="test-event")
            self.assertFalse(result["ok"])

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            invite = ddb.Table("rsvp-event-invites-test").get_item(
                Key={"eventId": "test-event", "phone": phone}
            )["Item"]
            self.assertEqual(invite["status"], "INVITED")
            member = member_store.get_member(phone)
            self.assertNotIn("noShowCount", member)




class TestContinuationMutationGuards(unittest.TestCase):
    """Exercise DynamoDB conditions, including a mutation between read and write."""

    def test_deleted_or_missing_member_cannot_be_mutated(self):
        from botocore.exceptions import ClientError
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            import member_store
            table = boto3.resource("dynamodb").Table("rsvp-members-test")
            phone = "+15025551212"
            for initial in ({"phone": phone, "status": "DELETED"}, None):
                if initial:
                    table.put_item(Item=initial)
                else:
                    table.delete_item(Key={"phone": phone})
                for operation in (
                    lambda: member_store.set_status(phone, "APPROVED"),
                    lambda: member_store.set_status(phone, "PENDING"),
                    lambda: member_store.set_gender(phone, "F"),
                    lambda: member_store.set_tier_override(phone, 1),
                    lambda: member_store.set_sms_opt_in(phone, True),
                ):
                    with self.assertRaises(ClientError):
                        operation()
                self.assertFalse(member_store.claim_welcome_send(phone))
                self.assertFalse(member_store.mark_welcome_sent(phone))
                self.assertEqual(table.get_item(Key={"phone": phone}).get("Item"), initial)

    def test_fresh_signup_can_restore_deleted_member_to_pending(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            import member_store
            table = boto3.resource("dynamodb").Table("rsvp-members-test")
            phone = "+15025551212"
            table.put_item(Item={"phone": phone, "status": "DELETED"})
            member = member_store.upsert_member(phone=phone, name="Jordan", last_name="Smith", sms_opt_in=True)
            self.assertEqual(member["status"], "PENDING")

    def test_guest_rename_during_checkin_cannot_check_in_old_name(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            import admin_member_routes
            table = boto3.resource("dynamodb").Table("rsvp-event-invites-test")
            key = {"eventId": "race-event", "phone": "+15025551212"}
            table.put_item(Item={**key, "status": "CONFIRMED", "plusOneName": "Old Guest"})
            client = boto3.client("dynamodb")
            real_write = client.transact_write_items
            def rename_then_write(**kwargs):
                table.update_item(Key=key, UpdateExpression="SET plusOneName = :n", ExpressionAttributeValues={":n": "New Guest"})
                return real_write(**kwargs)
            client.transact_write_items = rename_then_write
            with patch.object(admin_member_routes.boto3, "client", return_value=client):
                result = admin_member_routes._record_plus_one_attendance(key["eventId"], key["phone"], True)
            self.assertEqual(result["result"], "ATTENDANCE_CONFLICT")
            self.assertNotIn("plusOneAttendedAt", table.get_item(Key=key)["Item"])
            checkins = boto3.resource("dynamodb").Table("rsvp-checkins-test")
            self.assertEqual(checkins.scan()["Items"], [])

    def test_successful_plus_one_checkin_records_current_guest(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            import admin_member_routes
            table = boto3.resource("dynamodb").Table("rsvp-event-invites-test")
            key = {"eventId": "race-event", "phone": "+15025551212"}
            table.put_item(Item={**key, "status": "CONFIRMED", "plusOneName": "Current Guest"})
            result = admin_member_routes._record_plus_one_attendance(key["eventId"], key["phone"], True)
            self.assertTrue(result["ok"])
            row = boto3.resource("dynamodb").Table("rsvp-checkins-test").scan()["Items"][0]
            self.assertEqual(row["guestName"], "Current Guest")

    def test_old_active_event_archived_during_switch_is_not_reopened(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            import admin_event_routes
            table = boto3.resource("dynamodb").Table("rsvp-events-test")
            table.put_item(Item={"eventId": "current", "activeEventSlug": "old"})
            table.put_item(Item={"eventId": "old", "event_status": "ARCHIVED", "archived": True})
            new = {"eventId": "new", "eventSlug": "new", "event_status": "LIVE", "eventZipCode": "40205", "latitude": Decimal("38.22"), "longitude": Decimal("-85.68"), "promotionRadiusMiles": 60}
            table.put_item(Item=new)
            admin_event_routes.set_active_event_by_slug("new")
            self.assertEqual(table.get_item(Key={"eventId": "old"})["Item"]["event_status"], "ARCHIVED")
            self.assertEqual(table.get_item(Key={"eventId": "current"})["Item"]["activeEventSlug"], "new")


    def test_delayed_sms_result_does_not_recreate_deleted_rows(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            import invite_handler
            ddb = boto3.resource("dynamodb")
            members = ddb.Table("rsvp-members-test")
            invites = ddb.Table("rsvp-event-invites-test")
            events = ddb.Table("rsvp-events-test")
            phone = "+15025551212"
            member = {"phone": phone, "status": "APPROVED", "_tier": 1, "name": "Jordan"}
            event = {"eventId": "race-event", "eventSlug": "race-event", "event_status": "LIVE", "invite_template": "RSVP Society."}
            members.put_item(Item=member)
            events.put_item(Item=event)
            def delete_while_provider_accepts(*args):
                members.delete_item(Key={"phone": phone})
                invites.delete_item(Key={"eventId": "race-event", "phone": phone})
                events.delete_item(Key={"eventId": "race-event"})
                return "provider-message-id"
            with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
                 patch.object(invite_handler, "_resolve_active_invitable_event", return_value=event), \
                 patch.object(invite_handler, "_get_approved_members", return_value=[member]), \
                 patch.object(invite_handler, "_get_existing_invited_phones", return_value=set()), \
                 patch.object(invite_handler, "_assert_formal_wave_available"), \
                 patch.object(invite_handler, "send_sms", side_effect=delete_while_provider_accepts), \
                 patch.object(invite_handler, "schedule_next_wave", return_value={"scheduled": False}), \
                 patch.object(invite_handler, "_update_job"), \
                 patch.object(invite_handler, "log_action"), \
                 patch("invite_sender.time.sleep"):
                invite_handler._execute_send({"eventId": "race-event", "capacity": 10, "waveNumber": 1, "phones": [phone]}, "", "token", "race-job")
            self.assertEqual(members.scan()["Items"], [])
            self.assertEqual(invites.scan()["Items"], [])
            self.assertEqual(events.scan()["Items"], [])



    def test_event_commit_rejects_stale_pointer_without_overwriting_event(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            import admin_event_routes as routes
            table = boto3.resource("dynamodb").Table("rsvp-events-test")
            previous = {"eventId": "old", "event_status": "LIVE", "revision": "one"}
            table.put_item(Item=previous)
            table.put_item(Item={"eventId": "current", "activeEventSlug": "new", "revision": "new-pointer"})
            stale_pointer = {"eventId": "current", "activeEventSlug": "old", "revision": "old-pointer"}
            with self.assertRaises(ValueError):
                routes._commit_event_snapshot({**previous, "revision": "two"}, previous, stale_pointer)
            self.assertEqual(table.get_item(Key={"eventId": "old"})["Item"], previous)
            self.assertEqual(table.get_item(Key={"eventId": "current"})["Item"]["activeEventSlug"], "new")

    def test_event_commit_cannot_restore_hard_deleted_event(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            import admin_event_routes as routes
            table = boto3.resource("dynamodb").Table("rsvp-events-test")
            previous = {"eventId": "deleted", "event_status": "LIVE", "revision": "one"}
            with self.assertRaises(ValueError):
                routes._commit_event_snapshot({**previous, "revision": "two"}, previous, {})
            self.assertEqual(table.scan()["Items"], [])

    def test_hard_delete_freezes_event_before_child_cleanup(self):
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            import admin_event_routes as routes
            table = boto3.resource("dynamodb").Table("rsvp-events-test")
            table.put_item(Item={"eventId": "delete-me", "event_status": "LIVE", "active": True})
            table.put_item(Item={"eventId": "current", "activeEventSlug": "delete-me"})
            def cleanup(*args):
                row = table.get_item(Key={"eventId": "delete-me"})["Item"]
                self.assertTrue(row["hardDeleting"])
                self.assertEqual(row["event_status"], "ARCHIVED")
                self.assertNotIn("Item", table.get_item(Key={"eventId": "current"}))
                return 0
            with patch.object(routes, "_delete_items_for_event", side_effect=cleanup), \
                 patch.object(routes, "cancel_event_reminder_schedules"), patch.object(routes, "log_action"):
                response = routes.hard_delete_admin_event({"body": json.dumps({"eventSlug": "delete-me", "confirmSlug": "delete-me"})}, {}, "token")
            self.assertEqual(response["statusCode"], 200)
            self.assertEqual(table.scan()["Items"], [])

    def test_invite_reservation_rejects_archived_or_deleted_event(self):
        from botocore.exceptions import ClientError
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            from invite_sender import reserve_invite
            ddb = boto3.resource("dynamodb")
            members = ddb.Table("rsvp-members-test")
            invites = ddb.Table("rsvp-event-invites-test")
            events = ddb.Table("rsvp-events-test")
            phone = "+15025551212"
            members.put_item(Item={"phone": phone, "status": "APPROVED"})
            for event in (None, {"eventId": "closed", "event_status": "ARCHIVED", "hardDeleting": True}):
                if event:
                    events.put_item(Item=event)
                with self.assertRaises(ClientError):
                    reserve_invite(invites, members, {"eventId": "closed", "phone": phone, "status": "INVITED"})
                self.assertEqual(invites.scan()["Items"], [])

    def test_invite_reservation_honors_legacy_consent_and_detects_stop_race(self):
        from botocore.exceptions import ClientError
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            import invite_sender
            ddb = boto3.resource("dynamodb")
            members = ddb.Table("rsvp-members-test")
            invites = ddb.Table("rsvp-event-invites-test")
            events = ddb.Table("rsvp-events-test")
            phone = "+15025551212"
            members.put_item(Item={"phone": phone, "status": "APPROVED", "smsOptIn": "true", "optOut": "false"})
            events.put_item(Item={"eventId": "open", "event_status": "LIVE"})
            item = {"eventId": "open", "phone": phone, "status": "INVITED"}
            invite_sender.reserve_invite(invites, members, item)
            self.assertEqual(invites.get_item(Key={"eventId": "open", "phone": phone})["Item"], item)
            invites.delete_item(Key={"eventId": "open", "phone": phone})
            client = boto3.client("dynamodb")
            real_write = client.transact_write_items
            def stop_then_write(**kwargs):
                members.update_item(Key={"phone": phone}, UpdateExpression="SET optOut = :yes", ExpressionAttributeValues={":yes": True})
                return real_write(**kwargs)
            client.transact_write_items = stop_then_write
            with patch.object(invite_sender.boto3, "client", return_value=client), self.assertRaises(ClientError):
                invite_sender.reserve_invite(invites, members, item)
            self.assertEqual(invites.scan()["Items"], [])

    def test_overnight_event_settles_after_next_day_end_and_grace(self):
        import member_store
        event = {"date": "2026-09-28", "startTime": "22:00", "endTime": "02:00", "event_timezone": "America/New_York"}
        self.assertEqual(member_store._event_end_dt(event), datetime(2026, 9, 29, 6, tzinfo=timezone.utc))
        self.assertFalse(member_store.attendance_is_settled(event, datetime(2026, 9, 29, 9, 59, tzinfo=timezone.utc)))
        self.assertTrue(member_store.attendance_is_settled(event, datetime(2026, 9, 29, 10, tzinfo=timezone.utc)))

    def test_stale_host_decision_cannot_reverse_admin_decision(self):
        from botocore.exceptions import ClientError
        with mock_aws():
            _create_tables(); _stub_secret(); _reload_lambda_modules()
            import member_store
            table = boto3.resource("dynamodb").Table("rsvp-members-test")
            phone = "+15025551212"
            table.put_item(Item={"phone": phone, "status": "DENIED"})
            with self.assertRaises(ClientError):
                member_store.set_status(phone, "APPROVED", expected_status="PENDING")
            self.assertEqual(table.get_item(Key={"phone": phone})["Item"]["status"], "DENIED")


class TestDeepAuditRegressions(unittest.TestCase):
    def test_expired_checkin_record_cannot_count_attendance_twice(self):
        with mock_aws(), patch.dict(os.environ, {"RSVP_TEST_DISABLE_DDB_TRANSACTIONS": "false"}):
            _create_tables(); _reload_lambda_modules()
            import attendance_store
            ddb = boto3.resource("dynamodb")
            phone = "+15025551212"
            members = ddb.Table("rsvp-members-test")
            members.put_item(Item={"phone": phone, "status": "APPROVED", "attendedCount": 5})
            ddb.Table("rsvp-event-invites-test").put_item(Item={
                "eventId": "old-event", "phone": phone, "status": "ATTENDED", "attendedAt": "original",
            })
            result = attendance_store.record_attendance(phone, True, "old-event")
            self.assertFalse(result["ok"])
            self.assertEqual(result["result"], "ALREADY_CHECKED_IN")
            self.assertEqual(members.get_item(Key={"phone": phone})["Item"]["attendedCount"], 5)

    def _import(self, routes, **options):
        body = {"members": [{"phone": "5025551212", "name": "Jordan", "zipCode": "40205"}], **options}
        location = {"zipCode": "40205", "city": "Louisville", "state": "KY", "latitude": 38.22, "longitude": -85.68}
        with patch.object(routes, "resolve_us_zip", return_value=location), patch.object(routes, "log_action"):
            return json.loads(routes.import_members({"body": json.dumps(body)}, {}, "token")["body"])

    def test_import_cannot_overwrite_member_created_after_lookup(self):
        with mock_aws():
            _create_tables(); _reload_lambda_modules()
            import admin_member_routes as routes
            table = boto3.resource("dynamodb").Table("rsvp-members-test")
            stopped = {"phone": "+15025551212", "status": "APPROVED", "optOut": True, "smsOptIn": False}
            def concurrent_stop(*args):
                table.put_item(Item=stopped)
                return {}
            with patch.object(routes, "_batch_get_existing", side_effect=concurrent_stop):
                result = self._import(routes, consentConfirmed=True)
            self.assertEqual(table.get_item(Key={"phone": stopped["phone"]})["Item"], stopped)
            self.assertEqual(result["imported"], 0)
            self.assertEqual(result["skipped"], 1)

    def test_existing_import_rejects_concurrent_stop_or_deletion(self):
        for changes in ({"optOut": True, "smsOptIn": False}, {"status": "DELETED", "smsOptIn": False}):
            with self.subTest(changes=changes), mock_aws():
                _create_tables(); _reload_lambda_modules()
                import admin_member_routes as routes
                table = boto3.resource("dynamodb").Table("rsvp-members-test")
                old = {"phone": "+15025551212", "status": "APPROVED", "smsOptIn": True}
                latest = {**old, **changes}
                table.put_item(Item=latest)
                with patch.object(routes, "_batch_get_existing", return_value={old["phone"]: old}):
                    result = self._import(routes, consentConfirmed=True)
                self.assertEqual(result["imported"], 0)
                self.assertEqual(table.get_item(Key={"phone": old["phone"]})["Item"], latest)

    def test_new_import_stores_location_and_attested_consent(self):
        with mock_aws():
            _create_tables(); _reload_lambda_modules()
            import admin_member_routes as routes
            result = self._import(routes, consentConfirmed=True)
            self.assertEqual(result["imported"], 1)
            table = boto3.resource("dynamodb").Table("rsvp-members-test")
            member = table.get_item(Key={"phone": "+15025551212"})["Item"]
            self.assertEqual(member["city"], "Louisville")
            self.assertTrue(member["smsOptIn"])
            self.assertEqual(member["smsOptInConfirmationSource"], "csv_import_attestation")

    def test_import_without_attestation_preserves_consent_provenance(self):
        with mock_aws():
            _create_tables(); _reload_lambda_modules()
            import admin_member_routes as routes
            table = boto3.resource("dynamodb").Table("rsvp-members-test")
            table.put_item(Item={"phone": "+15025551212", "status": "APPROVED", "smsOptIn": True,
                                "smsOptInConfirmedAt": "original", "smsOptInConfirmationSource": "web"})
            self._import(routes, consentConfirmed=False)
            member = table.get_item(Key={"phone": "+15025551212"})["Item"]
            self.assertEqual(member["smsOptInConfirmedAt"], "original")
            self.assertEqual(member["smsOptInConfirmationSource"], "web")

    def test_import_cannot_restore_deleted_member(self):
        with mock_aws():
            _create_tables(); _reload_lambda_modules()
            import admin_member_routes as routes
            table = boto3.resource("dynamodb").Table("rsvp-members-test")
            deleted = {"phone": "+15025551212", "status": "DELETED", "smsOptIn": False}
            table.put_item(Item=deleted)
            result = self._import(routes, consentConfirmed=True)
            self.assertEqual(table.get_item(Key={"phone": deleted["phone"]})["Item"], deleted)
            self.assertEqual(result["imported"], 0)

    def test_single_guest_cancel_releases_once_with_valid_transaction_parameters(self):
        from invite_capacity import transition_confirmed_invite
        import re
        with mock_aws():
            _create_tables()
            ddb = boto3.resource("dynamodb")
            invites = ddb.Table("rsvp-event-invites-test")
            events = ddb.Table("rsvp-events-test")
            events.put_item(Item={"eventId": "audit", "confirmedHeadcount": 1})
            invites.put_item(Item={"eventId": "audit", "phone": "+15025551212", "status": "CONFIRMED"})
            client = boto3.client("dynamodb")
            write = client.transact_write_items
            def strict_write(**kwargs):
                for action in kwargs["TransactItems"]:
                    update = action["Update"]
                    expression = update["UpdateExpression"] + " " + update["ConditionExpression"]
                    self.assertEqual(set(re.findall(r":[A-Za-z0-9_]+", expression)), set(update.get("ExpressionAttributeValues", {})))
                    if "ExpressionAttributeNames" in update:
                        self.assertTrue(update["ExpressionAttributeNames"])
                return write(**kwargs)
            client.transact_write_items = strict_write
            options = dict(invites_table=invites, events_table=events, ddb_client=client, event_id="audit",
                           phone="+15025551212", target_status="DECLINED", allowed_statuses={"CONFIRMED"})
            self.assertTrue(transition_confirmed_invite(**options))
            self.assertFalse(transition_confirmed_invite(**options))
            self.assertEqual(events.get_item(Key={"eventId": "audit"})["Item"]["confirmedHeadcount"], 0)

    def test_cancellation_clears_reservation_for_accented_guest_name(self):
        from invite_capacity import transition_confirmed_invite
        from sms_plus_one import plus_one_reservation_key
        with mock_aws():
            _create_tables()
            ddb = boto3.resource("dynamodb")
            events = ddb.Table("rsvp-events-test")
            invites = ddb.Table("rsvp-event-invites-test")
            phone, name = "+15025551212", "José Guest"
            key = plus_one_reservation_key(name, deps={})
            events.put_item(Item={"eventId": "audit", "confirmedHeadcount": 2, "plusOneReservations": {key: phone}})
            invites.put_item(Item={"eventId": "audit", "phone": phone, "status": "CONFIRMED", "plusOneName": name})
            transition_confirmed_invite(invites_table=invites, events_table=events, ddb_client=boto3.client("dynamodb"),
                                        event_id="audit", phone=phone, target_status="DECLINED", allowed_statuses={"CONFIRMED"})
            self.assertEqual(events.get_item(Key={"eventId": "audit"})["Item"]["plusOneReservations"], {})


class TestCancellationPolicy(unittest.TestCase):
    phone = "+15025551212"
    cutoff = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)

    def _seed(self):
        _create_tables(); _reload_lambda_modules()
        ddb = boto3.resource("dynamodb")
        self.members = ddb.Table("rsvp-members-test")
        self.invites = ddb.Table("rsvp-event-invites-test")
        self.events = ddb.Table("rsvp-events-test")
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True,
                                    "invitedCount": 4, "attendedCount": 3})
        self.events.put_item(Item={"eventId": "policy", "event_status": "LIVE", "capacity": 100,
                                   "date": "2026-10-03", "startTime": "20:00", "event_timezone": "America/New_York",
                                   "confirmedHeadcount": 1})
        self.invites.put_item(Item={"eventId": "policy", "phone": self.phone, "status": "CONFIRMED"})

    def _cancel(self, now):
        import invite_capacity, sms_handler
        with patch.object(invite_capacity, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            return sms_handler._cancel_confirmed_invite("policy", self.phone)

    def test_exactly_24_hours_is_exempt_and_retry_cannot_double_credit(self):
        from invite_logic import calc_tier
        with mock_aws():
            self._seed()
            self.assertTrue(self._cancel(self.cutoff))
            self.assertFalse(self._cancel(self.cutoff))
            member = self.members.get_item(Key={"phone": self.phone})["Item"]
            self.assertEqual(member["invitedCount"], 4)
            self.assertEqual(member["timelyCancellationCount"], 1)
            self.assertEqual(calc_tier(member), 1)
            invite = self.invites.get_item(Key={"eventId": "policy", "phone": self.phone})["Item"]
            self.assertEqual(invite["cancellationTiming"], "TIMELY")
            self.assertTrue(invite["tierExcused"])
            self.assertEqual(self.events.get_item(Key={"eventId": "policy"})["Item"]["confirmedHeadcount"], 0)

    def test_one_second_after_cutoff_is_late_and_counts_against_rate(self):
        from invite_logic import calc_tier
        with mock_aws():
            self._seed()
            self.assertTrue(self._cancel(self.cutoff + timedelta(seconds=1)))
            member = self.members.get_item(Key={"phone": self.phone})["Item"]
            self.assertEqual(member.get("timelyCancellationCount", 0), 0)
            self.assertEqual(calc_tier(member), 2)
            invite = self.invites.get_item(Key={"eventId": "policy", "phone": self.phone})["Item"]
            self.assertEqual(invite["cancellationTiming"], "LATE")
            self.assertFalse(invite["tierExcused"])

    def test_reconfirmation_removes_exemption_once_and_late_recancellation_stays_counted(self):
        with mock_aws():
            self._seed()
            import sms_handler
            self._cancel(self.cutoff)
            self.assertEqual(sms_handler._confirm_invite_with_capacity("policy", self.phone, 100), "CONFIRMED")
            self.assertEqual(sms_handler._confirm_invite_with_capacity("policy", self.phone, 100), "NOOP")
            self.assertEqual(self.members.get_item(Key={"phone": self.phone})["Item"]["timelyCancellationCount"], 0)
            self.assertTrue(self._cancel(self.cutoff + timedelta(hours=1)))
            self.assertEqual(self.members.get_item(Key={"phone": self.phone})["Item"]["timelyCancellationCount"], 0)

    def test_failed_reconfirmation_preserves_exemption_and_seat_count(self):
        with mock_aws():
            self._seed()
            import sms_handler
            self._cancel(self.cutoff)
            self.members.update_item(Key={"phone": self.phone}, UpdateExpression="SET optOut = :yes", ExpressionAttributeValues={":yes": True})
            self.assertEqual(sms_handler._confirm_invite_with_capacity("policy", self.phone, 100), "NOOP")
            self.assertEqual(self.members.get_item(Key={"phone": self.phone})["Item"]["timelyCancellationCount"], 1)
            self.assertEqual(self.events.get_item(Key={"eventId": "policy"})["Item"]["confirmedHeadcount"], 0)

    def test_missing_start_time_does_not_award_an_unverifiable_exemption(self):
        with mock_aws():
            self._seed()
            self.events.update_item(Key={"eventId": "policy"}, UpdateExpression="REMOVE startTime")
            self.assertTrue(self._cancel(self.cutoff))
            self.assertEqual(self.members.get_item(Key={"phone": self.phone})["Item"].get("timelyCancellationCount", 0), 0)
            self.assertEqual(self.invites.get_item(Key={"eventId": "policy", "phone": self.phone})["Item"]["cancellationTiming"], "UNKNOWN_TIME")

    def test_no_show_cannot_be_changed_to_an_excused_cancellation(self):
        with mock_aws():
            self._seed()
            self.invites.update_item(Key={"eventId": "policy", "phone": self.phone},
                                     UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                                     ExpressionAttributeValues={":s": "NO_SHOW"})
            self.assertFalse(self._cancel(self.cutoff))
            self.assertEqual(self.members.get_item(Key={"phone": self.phone})["Item"].get("timelyCancellationCount", 0), 0)

    def test_plus_one_swap_12_hours_before_preserves_headcount(self):
        with mock_aws():
            self._seed()
            import sms_handler
            from sms_plus_one import plus_one_reservation_key
            start = datetime.now(timezone.utc) + timedelta(hours=12)
            self.events.update_item(Key={"eventId": "policy"},
                UpdateExpression="SET #date = :date, startTime = :time, event_timezone = :tz",
                ExpressionAttributeNames={"#date": "date"},
                ExpressionAttributeValues={":date": start.strftime("%Y-%m-%d"), ":time": start.strftime("%H:%M:%S"), ":tz": "UTC"})
            self.assertEqual(sms_handler._set_plus_one("policy", self.phone, "Taylor Guest", False), "SAVED")
            self.assertEqual(sms_handler._set_plus_one("policy", self.phone, "Morgan Guest", False), "SAVED")
            event = self.events.get_item(Key={"eventId": "policy"})["Item"]
            self.assertEqual(event["confirmedHeadcount"], 2)
            self.assertEqual(event["plusOneReservations"], {plus_one_reservation_key("Morgan Guest", deps={}): self.phone})


class TestTransactionalAttendanceCounters(unittest.TestCase):
    def test_checkin_and_tier_counter_commit_together_and_retry_does_not_double_count(self):
        with mock_aws(), patch.dict(os.environ, {"RSVP_TEST_DISABLE_DDB_TRANSACTIONS": "false"}):
            _create_tables(); _reload_lambda_modules()
            ddb = boto3.resource("dynamodb")
            phone = "+15025551212"
            members = ddb.Table("rsvp-members-test")
            invites = ddb.Table("rsvp-event-invites-test")
            checkins = ddb.Table("rsvp-checkins-test")
            members.put_item(Item={"phone": phone, "status": "APPROVED", "attendedCount": 4})
            invites.put_item(Item={"eventId": "attendance", "phone": phone, "status": "CONFIRMED"})

            import attendance_store
            first = attendance_store.record_attendance(phone, True, "attendance")
            retry = attendance_store.record_attendance(phone, True, "attendance")

            self.assertTrue(first["ok"], first)
            self.assertFalse(retry["ok"], retry)
            self.assertEqual(members.get_item(Key={"phone": phone})["Item"]["attendedCount"], 5)
            self.assertEqual(invites.get_item(Key={"eventId": "attendance", "phone": phone})["Item"]["status"], "ATTENDED")
            self.assertIsNotNone(checkins.get_item(Key={"eventId": "attendance", "phone": phone}).get("Item"))

    def test_no_show_and_no_show_counter_commit_together(self):
        with mock_aws(), patch.dict(os.environ, {"RSVP_TEST_DISABLE_DDB_TRANSACTIONS": "false"}):
            _create_tables(); _reload_lambda_modules()
            ddb = boto3.resource("dynamodb")
            phone = "+15025551212"
            members = ddb.Table("rsvp-members-test")
            invites = ddb.Table("rsvp-event-invites-test")
            members.put_item(Item={"phone": phone, "status": "APPROVED", "noShowCount": 0})
            invites.put_item(Item={"eventId": "attendance", "phone": phone, "status": "CONFIRMED"})

            import attendance_store
            result = attendance_store.record_attendance(phone, False, "attendance")

            self.assertTrue(result["ok"], result)
            self.assertEqual(members.get_item(Key={"phone": phone})["Item"]["noShowCount"], 1)
            self.assertEqual(invites.get_item(Key={"eventId": "attendance", "phone": phone})["Item"]["status"], "NO_SHOW")

    def test_missing_member_aborts_checkin_without_partial_attendance_state(self):
        with mock_aws(), patch.dict(os.environ, {"RSVP_TEST_DISABLE_DDB_TRANSACTIONS": "false"}):
            _create_tables(); _reload_lambda_modules()
            ddb = boto3.resource("dynamodb")
            phone = "+15025551212"
            invites = ddb.Table("rsvp-event-invites-test")
            checkins = ddb.Table("rsvp-checkins-test")
            invites.put_item(Item={"eventId": "attendance", "phone": phone, "status": "CONFIRMED"})

            import attendance_store
            result = attendance_store.record_attendance(phone, True, "attendance")

            self.assertFalse(result["ok"], result)
            self.assertEqual(invites.get_item(Key={"eventId": "attendance", "phone": phone})["Item"]["status"], "CONFIRMED")
            self.assertIsNone(checkins.get_item(Key={"eventId": "attendance", "phone": phone}).get("Item"))


class TestRequestedAuditFixes(unittest.TestCase):
    phone = "+15025551212"

    def setUp(self):
        self.aws = mock_aws(); self.aws.start(); self.addCleanup(self.aws.stop)
        _create_tables(); _reload_lambda_modules()
        self.members = boto3.resource("dynamodb").Table("rsvp-members-test")

    def member(self):
        return self.members.get_item(Key={"phone": self.phone}, ConsistentRead=True)["Item"]

    def apply(self):
        import member_store
        return member_store.upsert_member(phone=self.phone, name="Jordan", last_name="Smith", sms_opt_in=True)

    def sms(self, message):
        import sms_handler
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), patch.object(sms_handler, "_verify_webhook_signature", return_value=True), patch.object(sms_handler, "get_host_phones", return_value=[]), patch.object(sms_handler, "send_sms") as send:
            sms_handler.handler({"body": json.dumps({"from": self.phone, "body": message})}, None)
            return send.call_args_list

    def test_approved_resubmit_succeeds_and_preserves_identity_and_consent(self):
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "name": "Original", "smsOptIn": False})
        row = self.apply()
        self.assertEqual(row["status"], "APPROVED")
        self.assertEqual(row["name"], "Original")
        self.assertFalse(row["smsOptIn"])

    def test_stopped_member_reapplies_then_host_approves(self):
        import member_store, sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        sms_handler._set_opt_out(self.phone)
        row = self.apply()
        self.assertEqual(row["status"], "PENDING")
        self.assertTrue(row["optOut"])
        self.assertFalse(row["smsOptIn"])
        member_store.set_status(self.phone, "APPROVED", expected_status="PENDING")
        row = self.member()
        self.assertTrue(row["smsOptIn"])
        self.assertNotIn("optOut", row)
        self.assertEqual(row["smsOptInConfirmationSource"], "web_reapplication_host_approved")

    def test_new_stop_after_reapplication_cannot_be_cleared_by_approval(self):
        import member_store, sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED"})
        sms_handler._set_opt_out(self.phone); self.apply(); sms_handler._set_opt_out(self.phone)
        member_store.set_status(self.phone, "APPROVED", expected_status="PENDING")
        self.assertTrue(self.member()["optOut"])
        self.assertFalse(self.member()["smsOptIn"])

    def test_stop_during_signup_rejects_stale_write(self):
        from botocore.exceptions import ClientError
        import member_store, sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        table = self.members
        original = table.update_item
        def stop_then_write(**kwargs):
            sms_handler._set_opt_out(self.phone)
            return original(**kwargs)
        with patch.object(table, "update_item", side_effect=stop_then_write), patch.object(member_store, "_table", return_value=table):
            with self.assertRaises(ClientError): self.apply()
        self.assertTrue(self.member()["optOut"])

    def test_admin_approval_does_not_invent_import_consent(self):
        import admin_member_routes as routes
        self.members.put_item(Item={"phone": self.phone, "status": "PENDING", "smsOptIn": False})
        with patch.object(routes, "log_action"):
            response = routes.set_member_status({"body": json.dumps({"phone": self.phone, "status": "APPROVED"})}, {}, "token")
        self.assertEqual(response["statusCode"], 200)
        self.assertFalse(self.member()["smsOptIn"])

    def test_import_preserves_existing_location_and_fills_missing_location(self):
        import admin_member_routes as routes
        for existing in ({"zipCode": "37201", "city": "Nashville", "state": "TN", "latitude": Decimal("36.16"), "longitude": Decimal("-86.78")}, {}):
            with self.subTest(existing=existing):
                self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": False, **existing})
                result = TestDeepAuditRegressions._import(self, routes, consentConfirmed=False)
                self.assertEqual(result["imported"], 1, result)
                row = self.member()
                self.assertEqual(row["zipCode"], existing.get("zipCode", "40205"))
                self.assertEqual(row["city"], existing.get("city", "Louisville"))
                self.assertFalse(row["smsOptIn"])

    def test_import_does_not_overwrite_concurrent_location_change(self):
        import admin_member_routes as routes
        old = {"phone": self.phone, "status": "APPROVED", "smsOptIn": False}
        latest = {**old, "zipCode": "37201", "city": "Nashville"}
        self.members.put_item(Item=latest)
        with patch.object(routes, "_batch_get_existing", return_value={self.phone: old}):
            result = TestDeepAuditRegressions._import(self, routes)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(self.member(), latest)

    def test_unknown_number_receives_one_link_and_expired_claim_can_renew(self):
        self.assertEqual(len(self.sms("hello")), 1)
        self.assertEqual(len(self.sms("hello again")), 0)
        self.assertNotIn("Item", self.members.get_item(Key={"phone": self.phone}))
        jobs = boto3.resource("dynamodb").Table("rsvp-invite-jobs-test")
        row = jobs.scan()["Items"][0]
        jobs.update_item(Key={"jobId": row["jobId"]}, UpdateExpression="SET expiresAt = :e", ExpressionAttributeValues={":e": 0})
        self.assertEqual(len(self.sms("hello")), 1)

    def test_host_approval_lookup_error_returns_retryable_response(self):
        import sms_handler
        host = "+15025550001"
        message_id = "host-approval-transient-read-error"
        event = {"body": json.dumps({"type": "message.received", "id": message_id, "from": host, "body": "Y 4821"})}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[host]), \
             patch.object(sms_handler, "_pending_approvals_table", side_effect=RuntimeError("temporary DDB error")):
            response = sms_handler.handler(event, None)
        self.assertEqual(response["statusCode"], 503)
        key = "INBOUND#" + sms_handler.hashlib.sha256(message_id.encode()).hexdigest()
        receipt = boto3.resource("dynamodb").Table("rsvp-invite-jobs-test").get_item(Key={"jobId": key})["Item"]
        self.assertEqual(receipt["receiptState"], "RETRY")
        self.assertNotIn(sms_handler._claim_inbound_message(message_id), ("BUSY", "DONE"))

    def test_cancel_error_notice_is_once_and_command_remains_retryable(self):
        import sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        _seed_active_event("cancel-retry", date="2099-01-01", confirmedHeadcount=1)
        invites = boto3.resource("dynamodb").Table("rsvp-event-invites-test")
        invites.put_item(Item={"eventId": "cancel-retry", "phone": self.phone, "status": "CONFIRMED"})
        event = {"body": json.dumps({"type": "message.received", "data": {"resource": {"id": "cancel-retry-msg", "from": self.phone, "body": "Cancel my RSVP"}}})}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), patch.object(sms_handler, "_verify_webhook_signature", return_value=True), patch.object(sms_handler, "get_host_phones", return_value=[]), patch.object(sms_handler, "send_sms") as send:
            with patch.object(sms_handler, "_cancel_confirmed_invite", side_effect=RuntimeError("temporary DDB failure")):
                for _ in range(3):
                    self.assertEqual(sms_handler.handler(event, None)["statusCode"], 503)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(sms_handler.handler(event, None)["statusCode"], 200)
            self.assertEqual(send.call_count, 2)
            self.assertIn("RSVP is canceled", send.call_args.args[1])
        self.assertEqual(invites.get_item(Key={"eventId": "cancel-retry", "phone": self.phone})["Item"]["status"], "DECLINED")

    def test_invalid_legacy_timezone_preserves_confirmed_event_context(self):
        import sms_handler
        _seed_active_event("bad-zone", event_timezone="Not/AZone", description="The party ends at 3 AM.", address="123 Event Street")
        boto3.resource("dynamodb").Table("rsvp-event-invites-test").put_item(Item={"eventId": "bad-zone", "phone": self.phone, "status": "CONFIRMED"})
        context = sms_handler._build_event_context(member={"phone": self.phone})
        self.assertIn("event_status: unknown", context)
        self.assertIn("The party ends at 3 AM.", context)
        self.assertIn("123 Event Street", context)
        anonymous = sms_handler._build_event_context()
        self.assertNotIn("123 Event Street", anonymous)
        self.assertNotIn("The party ends", anonymous)

    def test_timezone_abbreviations_rejected_before_event_write(self):
        import admin_event_routes as routes
        with patch.object(routes, "events_table") as table:
            result = routes.save_admin_event({"body": json.dumps({"eventSlug": "bad-abbrev", "event_timezone": "EST"})}, {}, "token")
        self.assertEqual(result["statusCode"], 400)
        table.return_value.put_item.assert_not_called()
        table.return_value.update_item.assert_not_called()

    def test_pending_member_throttled_and_opted_out_member_gets_no_link(self):
        self.members.put_item(Item={"phone": self.phone, "status": "PENDING", "smsOptIn": True})
        self.assertEqual(len(self.sms("hello")), 1)
        self.assertEqual(len(self.sms("again")), 0)
        self.members.put_item(Item={"phone": self.phone, "status": "PENDING", "optOut": True})
        self.assertEqual(len(self.sms("hello")), 0)

    def test_explicit_prose_stop_is_saved_without_jade(self):
        import sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        with patch.object(sms_handler, "_claude") as jade:
            self.sms("please stop texting me")
        jade.assert_not_called()
        self.assertTrue(self.member()["optOut"])

    def test_duplicate_event_cannot_overwrite_existing_event(self):
        import admin_event_routes as routes
        events = boto3.resource("dynamodb").Table("rsvp-events-test")
        old = {"eventId": "existing", "capacity": 500, "event_status": "LIVE"}
        events.put_item(Item=old)
        with patch.object(routes, "_get_event_by_slug", return_value={"eventId": "source"}), patch.object(routes, "_build_event_item", side_effect=lambda item, existing: item):
            response = routes.duplicate_admin_event({"body": json.dumps({"eventSlug": "source", "newEventSlug": "existing"})}, {}, "token")
        self.assertEqual(response["statusCode"], 409)
        self.assertEqual(events.get_item(Key={"eventId": "existing"})["Item"], old)

    def test_duplicate_event_clears_live_counters_and_keeps_configured_show_rate(self):
        import admin_event_routes as routes
        source = {"eventId": "source", "eventSlug": "source", "event_status": "ARCHIVED", "capacity": 10,
                  "confirmedHeadcount": 7, "confirmedHeadcountRevision": 9, "plusOneReservations": {"guest": self.phone},
                  "attendanceFinalized": True, "attendanceFinalizedAt": "old", "observedShowRate": Decimal("0.05"), "expectedShowRate": Decimal("0.65"),
                  "lastBlastWave": 3, "lastBlastAt": "old"}
        events = boto3.resource("dynamodb").Table("rsvp-events-test")
        events.put_item(Item=source)
        with patch.object(routes, "_get_event_by_slug", return_value=source), patch.object(routes, "log_action"):
            response = routes.duplicate_admin_event({"body": json.dumps({"eventSlug": "source", "newEventSlug": "new-copy"})}, {}, "token")
        self.assertEqual(response["statusCode"], 200, response)
        copy = events.get_item(Key={"eventId": "new-copy"})["Item"]
        self.assertEqual(copy["expectedShowRate"], Decimal("0.65"))
        for key in ("confirmedHeadcount", "confirmedHeadcountRevision", "plusOneReservations", "attendanceFinalized", "lastBlastAt"):
            self.assertNotIn(key, copy)

    def test_table_alert_matches_words_not_substrings(self):
        import sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        host = "+15025559999"
        for message, alerts in (("comfortable", 0), ("vegetable", 0), ("intersection", 0), ("Can I book a table?", 1)):
            with self.subTest(message=message), patch.dict(os.environ, {"SMS_ENABLED": "true"}), patch.object(sms_handler, "_verify_webhook_signature", return_value=True), patch.object(sms_handler, "get_host_phones", return_value=[host]), patch.object(sms_handler, "_claude", return_value="Reply"), patch.object(sms_handler, "send_sms") as send:
                sms_handler.handler({"body": json.dumps({"from": self.phone, "body": message})}, None)
                self.assertEqual(sum(c.args[0] == host for c in send.call_args_list), alerts)

    def test_counter_drift_cancellation_repairs_count_and_releases_seat(self):
        ddb = boto3.resource("dynamodb")
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        events = ddb.Table("rsvp-events-test")
        events.put_item(Item={"eventId": "current", "activeEventSlug": "drift"})
        events.put_item(Item={"eventId": "drift", "eventSlug": "drift", "event_status": "LIVE", "confirmedHeadcount": 0})
        invites = ddb.Table("rsvp-event-invites-test")
        original = {"eventId": "drift", "phone": self.phone, "status": "CONFIRMED"}
        invites.put_item(Item=original)
        replies = self.sms("can't make it")
        invite = invites.get_item(Key={"eventId": "drift", "phone": self.phone})["Item"]
        self.assertEqual(invite["status"], "DECLINED")
        self.assertEqual(len(replies), 1)
        self.assertNotIn("couldn't update your RSVP", replies[0].args[1])
        event = events.get_item(Key={"eventId": "drift"})["Item"]
        self.assertEqual(event["confirmedHeadcount"], 0)
        self.assertEqual(event["confirmedHeadcountRevision"], 2)

    def test_headcount_reconciliation_retries_when_counter_revision_changes(self):
        from invite_capacity import reconcile_confirmed_headcount
        events = boto3.resource("dynamodb").Table("rsvp-events-test")
        invites = boto3.resource("dynamodb").Table("rsvp-event-invites-test")
        events.put_item(Item={"eventId": "race", "confirmedHeadcount": 0})
        invites.put_item(Item={"eventId": "race", "phone": self.phone, "status": "CONFIRMED"})
        original = events.update_item
        raced = False
        def update_with_race(**kwargs):
            nonlocal raced
            if not raced:
                raced = True
                original(Key={"eventId": "race"}, UpdateExpression="SET confirmedHeadcount = :c ADD confirmedHeadcountRevision :r", ExpressionAttributeValues={":c": 2, ":r": 1})
            return original(**kwargs)
        with patch.object(events, "update_item", side_effect=update_with_race):
            count = reconcile_confirmed_headcount(events_table=events, invites_table=invites, event_id="race")
        self.assertEqual(count, 1)
        current = events.get_item(Key={"eventId": "race"})["Item"]
        self.assertEqual(current["confirmedHeadcount"], 1)
        self.assertEqual(current["confirmedHeadcountRevision"], 2)

    def test_event_finalization_runs_in_resumable_pages_and_closes_rsvp_window(self):
        import admin_event_routes as routes
        from admin_member_routes import record_member_attendance
        events = boto3.resource("dynamodb").Table("rsvp-events-test")
        invites = boto3.resource("dynamodb").Table("rsvp-event-invites-test")
        events.put_item(Item={"eventId": "close-test", "eventSlug": "close-test", "event_status": "LIVE", "capacity": 20})
        for n in range(105):
            phone = f"+1502555{n:04d}"
            self.members.put_item(Item={"phone": phone, "status": "APPROVED", "noShowCount": 0})
            row = {"eventId": "close-test", "phone": phone, "status": "CONFIRMED"}
            if n == 0: row["plusOneName"] = "Guest Name"
            invites.put_item(Item=row)
        first = json.loads(routes.finalize_admin_event_attendance({"body": json.dumps({"eventSlug": "close-test"})}, {}, "token")["body"])
        self.assertTrue(first["ok"])
        self.assertFalse(first["done"])
        self.assertEqual(first["rowsScanned"], 100)
        event = events.get_item(Key={"eventId": "close-test"})["Item"]
        self.assertEqual(event["event_status"], "ARCHIVED")
        self.assertEqual(event["attendanceFinalizationState"], "CLOSING")
        blocked = record_member_attendance({"body": json.dumps({"eventId": "close-test", "phone": "+15025550000", "attended": True})}, {}, "token")
        self.assertEqual(blocked["statusCode"], 409)
        second = json.loads(routes.finalize_admin_event_attendance({"body": json.dumps({"eventSlug": "close-test", "cursor": first["cursor"]})}, {}, "token")["body"])
        self.assertTrue(second["done"])
        event = events.get_item(Key={"eventId": "close-test"})["Item"]
        self.assertTrue(event["attendanceFinalized"])
        self.assertEqual(event["attendanceFinalizationState"], "COMPLETE")
        self.assertNotIn("observedShowRate", event)
        self.assertEqual(invites.get_item(Key={"eventId": "close-test", "phone": "+15025550000"})["Item"]["status"], "NO_SHOW")

    def test_inbound_provider_message_id_suppresses_webhook_redelivery(self):
        import sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        event = {"body": json.dumps({"type": "message.received", "data": {"resource": {"id": "quo-message-unique", "from": self.phone, "body": "Tell me about the event"}}})}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), patch.object(sms_handler, "_verify_webhook_signature", return_value=True), patch.object(sms_handler, "get_host_phones", return_value=[]), patch.object(sms_handler, "_claude", return_value="Jade response") as jade, patch.object(sms_handler, "send_sms") as send:
            first = sms_handler.handler(event, None)
            second = sms_handler.handler(event, None)
        self.assertEqual(first["statusCode"], 200)
        self.assertEqual(second["statusCode"], 200)
        self.assertEqual(jade.call_count, 1)
        self.assertEqual(send.call_count, 1)
        receipt = boto3.resource("dynamodb").Table("rsvp-invite-jobs-test").scan()["Items"]
        self.assertEqual(sum(row.get("kind") == "INBOUND_RECEIPT" for row in receipt), 1)

    def test_configured_show_rate_controls_shared_seat_target(self):
        from capacity_policy import target_confirmed_headcount
        self.assertEqual(target_confirmed_headcount(100, {"expectedShowRate": Decimal("0.8")}), 125)
        self.assertEqual(target_confirmed_headcount(100, {"expectedShowRate": Decimal("0.5")}), 200)
        self.assertEqual(target_confirmed_headcount(100, {}), 167)

    def test_switching_away_from_an_unclosed_live_event_is_rejected(self):
        import admin_event_routes as routes
        events = boto3.resource("dynamodb").Table("rsvp-events-test")
        events.put_item(Item={"eventId": "current", "activeEventSlug": "old"})
        events.put_item(Item={"eventId": "old", "event_status": "LIVE", "attendanceFinalized": False})
        new = {"eventId": "new", "eventSlug": "new", "event_status": "LIVE", "eventZipCode": "40205", "latitude": Decimal("38.22"), "longitude": Decimal("-85.68"), "promotionRadiusMiles": 50}
        with patch.object(routes, "_get_event_by_slug", side_effect=[new, events.get_item(Key={"eventId": "old"})["Item"]]):
            with self.assertRaisesRegex(ValueError, "Close the active event"):
                routes.set_active_event_by_slug("new")

    def test_provider_phone_number_response_shapes(self):
        import sms_adapter
        expected = [{"id": "PN1", "number": self.phone}]
        for response in (expected, {"data": expected}, {"items": expected}):
            with patch.object(sms_adapter, "_http_json", return_value=response):
                self.assertEqual(sms_adapter._list_phone_numbers("fake"), expected)


    def test_auto_wave_two_and_three_execute_without_explicit_size(self):
        import invite_handler
        self.members.put_item(Item={"phone": self.phone, "name": "Jordan", "status": "APPROVED", "smsOptIn": True})
        for wave in (2, 3):
            event_id = f"auto-wave-{wave}"
            _seed_active_event(event_id, expectedShowRate=Decimal("0.8"))
            with patch.dict(os.environ, {"SMS_ENABLED": "true"}), patch.object(invite_handler, "_assert_formal_wave_available"), patch.object(invite_handler, "send_sms", return_value=f"sms-{wave}") as send, patch.object(invite_handler, "log_action"), patch("invite_sender.time.sleep"), patch.object(invite_handler, "_resolve_wave_capacity", wraps=invite_handler._resolve_wave_capacity) as sizing:
                invite_handler._run_blast({"eventId": event_id, "capacity": 100, "waveNumber": wave, "phones": [self.phone]}, "", "token", event_id)
            job = boto3.resource("dynamodb").Table("rsvp-invite-jobs-test").get_item(Key={"jobId": event_id})["Item"]
            self.assertEqual(job["status"], "COMPLETE", job)
            send.assert_called_once()
            self.assertEqual(sizing.call_args.kwargs["actual_show_rate"], 0.8)

    def test_stop_bypasses_receipt_claim_and_preserves_profile_and_rsvp(self):
        import sms_handler
        self.members.put_item(Item={"phone": self.phone, "name": "Jordan", "email": "j@example.com", "status": "APPROVED", "smsOptIn": True})
        invites = boto3.resource("dynamodb").Table("rsvp-event-invites-test")
        invites.put_item(Item={"eventId": "party", "phone": self.phone, "status": "CONFIRMED"})
        event = {"body": json.dumps({"type": "message.received", "data": {"resource": {"id": "stop-1", "from": self.phone, "body": "STOP ALL"}}})}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), patch.object(sms_handler, "_claim_inbound_message", side_effect=RuntimeError("storage unavailable")) as claim, patch.object(sms_handler, "send_sms"):
            result = sms_handler.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        claim.assert_not_called()
        self.assertTrue(self.member()["optOut"])
        self.assertEqual(self.member()["name"], "Jordan")
        self.assertEqual(self.member()["email"], "j@example.com")
        self.assertEqual(invites.get_item(Key={"eventId": "party", "phone": self.phone})["Item"]["status"], "CONFIRMED")

    def test_receipt_storage_failure_is_not_a_duplicate(self):
        import sms_handler
        with patch.object(sms_handler, "_DDB") as ddb:
            ddb.Table.return_value.put_item.side_effect = RuntimeError("throttled")
            self.assertEqual(sms_handler._claim_inbound_message("message-1"), "")

    def test_inbound_processing_lease_recovers_after_interruption(self):
        import sms_handler
        message_id = "interrupted-message"
        owner = sms_handler._claim_inbound_message(message_id)
        self.assertNotIn(owner, ("", "BUSY", "DONE"))
        self.assertEqual(sms_handler._claim_inbound_message(message_id), "BUSY")
        table = boto3.resource("dynamodb").Table("rsvp-invite-jobs-test")
        key = "INBOUND#" + sms_handler.hashlib.sha256(message_id.encode()).hexdigest()
        table.update_item(Key={"jobId": key}, UpdateExpression="SET expiresAt = :zero", ExpressionAttributeValues={":zero": 0})
        new_owner = sms_handler._claim_inbound_message(message_id)
        self.assertNotEqual(new_owner, owner)
        sms_handler._finish_inbound_message(message_id, new_owner, True)
        self.assertEqual(sms_handler._claim_inbound_message(message_id), "DONE")

    def test_failed_message_processing_releases_receipt_for_retry(self):
        import sms_handler
        event = {"body": json.dumps({"type": "message.received", "data": {"resource": {"id": "retry-failure", "from": self.phone, "body": "Yes"}}})}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), patch.object(sms_handler, "get_host_phones", return_value=[]), patch.object(sms_handler, "get_member", side_effect=RuntimeError("throttled")):
            result = sms_handler.handler(event, None)
        self.assertEqual(result["statusCode"], 503)
        self.assertNotIn(sms_handler._claim_inbound_message("retry-failure"), ("BUSY", "DONE", ""))

    def test_future_event_has_pending_attendance_rates_and_no_ghosts(self):
        import admin_event_routes as routes
        _seed_active_event("future", date="2099-01-01")
        boto3.resource("dynamodb").Table("rsvp-event-invites-test").put_item(Item={"eventId": "future", "phone": self.phone, "status": "CONFIRMED", "waveNumber": 1, "plusOneName": "Guest Name"})
        result = routes.get_analytics({"queryStringParameters": {"eventId": "future"}}, {}, "token")
        analytics = json.loads(result["body"])["analytics"]
        self.assertFalse(analytics["attendanceSettled"])
        self.assertIsNone(analytics["rates"]["show_rate"])
        self.assertIsNone(analytics["rates"]["no_show_rate"])
        self.assertEqual(analytics["totals"]["no_show_headcount"], 0)
        self.assertEqual(analytics["by_wave"]["1"]["no_show_headcount"], 0)

    def test_public_event_route_returns_no_details_for_live_event(self):
        import admin_event_routes as routes
        _seed_active_event("private", venue="Secret Place", dresscode="All white")
        self.assertEqual(json.loads(routes.get_public_event({})["body"])["event"], {})

    def test_invalid_timezone_rejected_before_event_write(self):
        import admin_event_routes as routes
        for zone in ("Eastern", "Not/AZone", "../../etc/passwd"):
            result = routes.save_admin_event({"body": json.dumps({"eventSlug": "bad-zone", "event_timezone": zone})}, {}, "token")
            self.assertEqual(result["statusCode"], 400, result)
        self.assertIsNone(boto3.resource("dynamodb").Table("rsvp-events-test").get_item(Key={"eventId": "bad-zone"}).get("Item"))

    def test_jade_uses_local_event_time_and_overnight_end(self):
        import jade_service, sms_handler
        _seed_active_event("overnight-jade", date="2026-09-30", startTime="21:00", endTime="02:00", event_timezone="America/New_York")
        boto3.resource("dynamodb").Table("rsvp-event-invites-test").put_item(Item={"eventId": "overnight-jade", "phone": self.phone, "status": "CONFIRMED"})
        for hour, expected in ((0, "upcoming"), (5, "upcoming"), (7, "past")):
            instant = datetime(2026, 10, 1, hour, tzinfo=timezone.utc)
            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return instant.astimezone(tz)
            with patch.object(jade_service, "datetime", FrozenDateTime):
                context = sms_handler._build_event_context({"phone": self.phone})
            self.assertIn(f"event_status: {expected}", context)

    def test_explicit_cancel_rsvp_phrase_releases_seat(self):
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        _seed_active_event("cancel-phrase", date="2099-01-01", confirmedHeadcount=1)
        ddb = boto3.resource("dynamodb")
        ddb.Table("rsvp-event-invites-test").put_item(Item={"eventId": "cancel-phrase", "phone": self.phone, "status": "CONFIRMED"})
        self.sms("cancel my RSVP")
        self.assertFalse(self.member().get("optOut", False))
        self.assertNotEqual(ddb.Table("rsvp-event-invites-test").get_item(Key={"eventId": "cancel-phrase", "phone": self.phone})["Item"]["status"], "CONFIRMED")
        self.assertEqual(ddb.Table("rsvp-events-test").get_item(Key={"eventId": "cancel-phrase"})["Item"]["confirmedHeadcount"], 0)

    def test_locked_recipient_read_failure_cannot_complete_job(self):
        import invite_sender
        table = MagicMock(); table.name = "members"
        table.meta.client.batch_get_item.side_effect = RuntimeError("throttled")
        with self.assertRaisesRegex(RuntimeError, "throttled"):
            invite_sender._get_members_for_locked_phones([self.phone], table, calc_tier=lambda m: 2, logger=MagicMock())

    def test_deployed_lambda_rejects_unsigned_webhook_even_with_dev_flag(self):
        import sms_webhook
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "true", "AWS_LAMBDA_FUNCTION_NAME": "rsvp-sms-handler"}):
            self.assertFalse(sms_webhook._verify_webhook_signature({"body": "{}"}))

    def test_zip_lookup_rejects_http_and_invalid_coordinates(self):
        import location_resolver
        with patch.dict(os.environ, {"ZIP_LOOKUP_BASE_URL": "http://example.com/us"}), patch.object(location_resolver, "urlopen") as fetch:
            with self.assertRaises(location_resolver.ZipLookupUnavailable):
                location_resolver.resolve_us_zip("40205")
            fetch.assert_not_called()
        response = MagicMock(); response.__enter__.return_value = response
        response.read.return_value = json.dumps({"places": [{"place name": "Louisville", "state abbreviation": "KY", "latitude": "nan", "longitude": "-85"}]}).encode()
        with patch.dict(os.environ, {"ZIP_LOOKUP_BASE_URL": "https://example.com/us"}), patch.object(location_resolver, "urlopen", return_value=response):
            with self.assertRaises(location_resolver.ZipLookupUnavailable):
                location_resolver.resolve_us_zip("40205")


    def test_job_poll_uses_saved_counters_without_reading_all_invites(self):
        import invite_handler, invite_job_store
        jobs = boto3.resource("dynamodb").Table("rsvp-invite-jobs-test")
        jobs.put_item(Item={"jobId": "poll-job", "status": "PROCESSING", "recipientCount": 500, "invitesWritten": 20, "smsSent": 20, "failed": 0})
        with patch.object(invite_job_store, "fallback_job_summary") as fallback:
            result = invite_job_store.handle_job_status({"jobId": "poll-job"}, "", deps=invite_handler._job_deps())
        self.assertEqual(json.loads(result["body"])["smsSent"], 20)
        fallback.assert_not_called()

    def test_coffee_question_is_not_misread_as_a_fee_question(self):
        import sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        with patch.object(sms_handler, "_claude", return_value="Let me check.") as jade:
            self.sms("Can I get coffee?")
        jade.assert_called_once()


    def test_amenity_questions_reach_jade_with_full_notes_instead_of_word_guess(self):
        import sms_handler
        self.members.put_item(Item={"phone": self.phone, "status": "APPROVED", "smsOptIn": True})
        _seed_active_event("amenities", description="No food. No cover charge. Hookah is available.")
        boto3.resource("dynamodb").Table("rsvp-event-invites-test").put_item(Item={"eventId": "amenities", "phone": self.phone, "status": "CONFIRMED"})
        with patch.object(sms_handler, "_claude", return_value="No food. Hookah is available.") as jade:
            replies = self.sms("Is there food or hookah?")
        jade.assert_called_once()
        self.assertEqual(replies[0].args[1], "No food. Hookah is available.")
        self.assertIn("No food. No cover charge. Hookah is available.", sms_handler._build_event_context({"phone": self.phone}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
