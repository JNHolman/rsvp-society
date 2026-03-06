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
import json
import os
import sys
import types
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
os.environ.setdefault("CHECKINS_TABLE_NAME",     "rsvp-checkins-test")
os.environ.setdefault("AUDIT_TABLE_NAME",        "rsvp-audit-log-test")
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
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "phone-index",
            "KeySchema": [{"AttributeName": "phone", "KeyType": "HASH"}],
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
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _stub_secret(token: str = ADMIN_TOKEN):
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    sm.create_secret(
        Name="rsvp/admin-token-test",
        SecretString=json.dumps({"token": token}),
    )


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

class TestVenueReveal(unittest.TestCase):
    """
    Public /event endpoint must hide venue+address when revealVenue=False
    and expose them when revealVenue=True.
    """

    def _run(self, reveal: bool) -> dict:
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            import boto3 as _boto3
            _boto3.resource("dynamodb").Table("rsvp-events-test").put_item(Item={
                "eventId":    "current",
                "eventSlug":  "Swim Test",
                "date":       "Friday April 18, 2026",
                "venue":      "Tribe",
                "address":    "100 Charlestown Court",
                "revealVenue": reveal,
            })

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

    def test_venue_visible_when_reveal_true(self):
        event = self._run(reveal=True)
        self.assertIn("venue",   event)
        self.assertIn("address", event)
        self.assertEqual(event["venue"],   "Tribe")
        self.assertEqual(event["address"], "100 Charlestown Court")


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
            boto3.resource("dynamodb").Table("rsvp-events-test").put_item(Item={
                "eventId": "current", "eventSlug": "Swim Test",
                "date": "Friday April 18, 2026", "revealVenue": False,
            })
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
            members_t = boto3.resource("dynamodb").Table("rsvp-members-test")
            invites_t = boto3.resource("dynamodb").Table("rsvp-event-invites-test")

            members_t.put_item(Item={
                "phone": phone, "name": "Delete Me", "email": "del@test.com",
                "status": "APPROVED", "smsOptIn": True,
            })
            invites_t.put_item(Item={"eventId": "current", "phone": phone, "status": "CONFIRMED"})
            invites_t.put_item(Item={"eventId": "event-2", "phone": phone, "status": "INVITED"})

            from admin_handler import handler
            resp = handler(_event("DELETE", "/admin/members", body={"phone": phone}), None)
            self.assertEqual(resp["statusCode"], 200)

            member = members_t.get_item(Key={"phone": phone})["Item"]
            self.assertEqual(member["status"], "DELETED")
            self.assertNotIn("name",     member, "name must be wiped")
            self.assertNotIn("email",    member, "email must be wiped")
            self.assertNotIn("smsOptIn", member, "smsOptIn must be wiped")

            for event_id in ("current", "event-2"):
                invite = invites_t.get_item(Key={"eventId": event_id, "phone": phone})["Item"]
                self.assertEqual(invite["status"], "DELETED",
                                 f"invite for {event_id} must be tombstoned")


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
            invites_t.put_item(Item={"eventId": "current", "phone": phone, "status": "CONFIRMED"})

            body = {"phone": phone, "attended": True, "eventId": "current"}

            from admin_handler import handler

            r1 = handler(_event("POST", "/admin/members/attendance", body=body), None)
            b1 = json.loads(r1["body"])
            self.assertEqual(r1["statusCode"], 200)
            self.assertFalse(b1.get("alreadyCheckedIn"), "first tap must not be flagged")

            r2 = handler(_event("POST", "/admin/members/attendance", body=body), None)
            b2 = json.loads(r2["body"])
            self.assertEqual(r2["statusCode"], 200)
            self.assertTrue(b2.get("alreadyCheckedIn"), "second tap must be flagged as duplicate")


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
        resp = self._save({"date": "2026-04-18", "capacity": -1})
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
            "eventSlug":  "Derby Night",
            "capacity":   120,
            "venue":      "The Venue",
            "revealVenue": False,
        })
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["event"]["eventSlug"], "Derby Night")
        self.assertEqual(body["event"]["capacity"],  120)
        self.assertIs(body["event"]["revealVenue"],  False)

    def test_zero_capacity_accepted(self):
        resp = self._save({"date": "2026-04-18", "capacity": 0})
        self.assertEqual(resp["statusCode"], 200)


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestVenueReveal,
        TestUnauthorizedRejection,
        TestFirstApprovalWelcome,
        TestDeleteTombstoning,
        TestDuplicateAttendance,
        TestEventSaveValidation,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
