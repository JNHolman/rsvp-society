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
from datetime import datetime, timezone
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
os.environ.setdefault("EVENT_HISTORY_TABLE_NAME", "rsvp-event-history-test")
os.environ.setdefault("AUDIT_LOG_TABLE_NAME",    "rsvp-audit-log-test")
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

            table_name = os.environ["EVENTS_TABLE_NAME"]
            boto3.resource("dynamodb", region_name="us-east-1").Table(table_name).put_item(Item={"eventId": "current", "date": "2026-04-18"})

            resp = handler(_event("POST", "/admin/members/attendance", body={"phone": phone, "attended": "false"}), None)
            self.assertEqual(resp["statusCode"], 200)
            body = json.loads(resp["body"])
            self.assertFalse(body.get("checkedIn"))
            self.assertFalse(body.get("alreadyCheckedIn"))



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

    def test_event_history_keeps_multiple_versions_for_same_slug(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            from admin_handler import handler

            handler(_event("POST", "/admin/event", body={
                "date": "2026-04-18",
                "eventSlug": "derby-night",
                "capacity": 120,
                "venue": "Venue A",
            }), None)
            handler(_event("POST", "/admin/event", body={
                "date": "2026-04-18",
                "eventSlug": "derby-night",
                "capacity": 120,
                "venue": "Venue B",
            }), None)
            handler(_event("POST", "/admin/event", body={
                "date": "2026-04-18",
                "eventSlug": "derby-night",
                "capacity": 120,
                "venue": "Venue C",
            }), None)

            history = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-event-history-test")
            rows = history.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("historyPk").eq("EVENT"))["Items"]
            derby_rows = [row for row in rows if str(row.get("slug") or row.get("eventSlug") or row.get("eventId") or "") == "derby-night"]
            self.assertEqual(len(derby_rows), 2)
            venues = {row.get("venue") for row in derby_rows}
            self.assertEqual(venues, {"Venue A", "Venue B"})

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

            boto3.resource("dynamodb").Table("rsvp-events-test").put_item(Item={
                "eventId": "current",
                "eventSlug": "precision-night",
                "date": "2026-05-15",
                "event_timezone": "America/New_York",
                "reminderTiming": "day_before",
                "day_before_send_time": "17:30",
            })

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

            from access_request import handler
            resp = handler({
                "httpMethod": "POST",
                "path": "/access",
                "headers": {"origin": "https://admin.rsvpsociety.com"},
                "body": json.dumps({
                    "firstName": "Josh",
                    "lastName": "Holman",
                    "phone": "2702269660",
                    "source": "web",
                    "smsOptIn": True,
                }),
            }, None)

            self.assertEqual(resp["statusCode"], 200)
            item = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-members-test").get_item(
                Key={"phone": "+12702269660"}
            )["Item"]
            self.assertEqual(item["name"], "Josh")
            self.assertEqual(item["lastName"], "Holman")

    def test_access_request_splits_legacy_name_as_fallback(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            from access_request import handler
            resp = handler({
                "httpMethod": "POST",
                "path": "/access",
                "headers": {"origin": "https://admin.rsvpsociety.com"},
                "body": json.dumps({
                    "name": "Josh Holman",
                    "phone": "2702269660",
                    "source": "web",
                    "smsOptIn": True,
                }),
            }, None)

            self.assertEqual(resp["statusCode"], 200)
            item = boto3.resource("dynamodb", region_name="us-east-1").Table("rsvp-members-test").get_item(
                Key={"phone": "+12702269660"}
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
                "phone": "+12702269660",
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
                "phone": "+12702269660",
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
                "phone": "+12702269660",
                "name": "Josh Holman",
                "status": "APPROVED",
                "createdAt": "2026-03-06T12:00:00+00:00",
            })
            ddb.Table("rsvp-event-invites-test").put_item(Item={
                "eventId": "Swim Test",
                "phone": "+12702269660",
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

            import access_request
            with patch("access_request.send_sms", return_value=True):
                r1 = access_request.handler(_event("POST", "/access", {
                    "phone": "+15025550001", "name": "Alice", "lastName": "A"
                }, token=""), None)
                r2 = access_request.handler(_event("POST", "/access", {
                    "phone": "+15025550002", "name": "Bob", "lastName": "B"
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
        import boto3 as b3
        events_t = b3.resource("dynamodb", region_name="us-east-1").Table("rsvp-events-test")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Queue key format: pending_approval:{host_phone}:{member_phone}
        # Each signup gets its own row — no last-signup-wins overwrite
        for host in [host1, "+15559990002"]:
            events_t.put_item(Item={
                "eventId": f"pending_approval:{host}:{member_phone}",
                "memberPhone": member_phone,
                "memberName": "Test User",
                "hostPhone": host,
                "storedAt": now,
                "status": "PENDING",
                "approvalCode": "1234",  # fixed code for test predictability
            })

    def test_second_host_cannot_reverse_after_first_approves(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()
            os.environ["HOST_PHONE_1"] = "+15559990001"
            os.environ["HOST_PHONE_2"] = "+15559990002"

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
                    "from": "+15559990001", "body": "Y 1234"
                }, token=""), None)

                # Host 2 tries to deny — should be ignored (record finalized)
                sms_handler.handler(_event("POST", "/sms/inbound", {
                    "from": "+15559990002", "body": "N 1234"
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
            ddb.Table("rsvp-events-test").put_item(Item={
                "eventId": "current",
                "date": "2026-04-05",
                "startTime": "21:00",
                "venue": "The Venue",
                "reminderTiming": "day_before",
            })

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


class TestInviteWriteWithoutSend(unittest.TestCase):
    """Opted-out member gets an invite row but no SMS — consent check skips the send."""

    def test_opted_out_member_gets_invite_row_but_no_sms(self):
        with mock_aws():
            _create_tables()
            _stub_secret()
            _reload_lambda_modules()

            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.Table("rsvp-members-test").put_item(Item={
                "phone": "+15025550020", "name": "Fail", "lastName": "Test",
                "status": "APPROVED",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            ddb.Table("rsvp-events-test").put_item(Item={
                "eventId": "current",
                "date": "2026-04-05",
                "startTime": "21:00",
                "venue": "The Venue",
            })

            # Invite record is always written before SMS attempt (write-before-send contract).
            # Member has smsOptIn=False so SMS is skipped.
    # With the SKIPPED_CONSENT fix, record should be SKIPPED_CONSENT (not INVITED).
    # invitedCount should NOT be incremented for this member.
            ddb.Table("rsvp-members-test").update_item(
                Key={"phone": "+15025550020"},
                UpdateExpression="SET smsOptIn = :f",
                ExpressionAttributeValues={":f": False},
            )
            import invite_handler
            sent_to = []
            with patch.object(invite_handler, "send_sms", side_effect=lambda p, m: sent_to.append(p) or True):
                invite_handler.handle_send(
                    body={
                        "confirmSend": True,
                        "eventId": "current",
                        "capacity": 10,
                        "waveNumber": 1,
                        "waveSize": 1,
                        "femalePercent": 60,
                        "tier2BufferPct": 30,
                        "phones": ["+15025550020"],
                        "removedPhones": [],
                    },
                    origin="https://admin.rsvpsociety.com",
                    token="test-token",
                )

            invite = ddb.Table("rsvp-event-invites-test").get_item(
                Key={"eventId": "current", "phone": "+15025550020"}
            ).get("Item")
            self.assertIsNotNone(invite, "Invite record must be written even when SMS is skipped")
            self.assertEqual(invite["status"], "SKIPPED_CONSENT")  # Fixed: no-consent never gets INVITED
            self.assertNotIn("+15025550020", sent_to, "No SMS should be sent to opted-out member")


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
            "eventId": "current", "event_label": "Test",
            "date": "2026-06-01", "ticketUrl": "https://posh.vip/test",
            "revealVenue": True, "event_status": "LIVE",
        }
        fake_member = {"phone": "+15550001", "status": "INVITED"}
        orig = sms_handler._events_table
        sms_handler._events_table = lambda: type("T", (), {
            "get_item": lambda self, **kw: {"Item": fake_ev}
        })()
        orig_inv = sms_handler._invites_table
        sms_handler._invites_table = lambda: type("T", (), {
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
            "eventId": "current", "event_label": "Test",
            "date": "2026-06-01", "parkingInfo": "Parking behind The Tribe.",
            "revealVenue": True, "event_status": "LIVE",
        }
        fake_member = {"phone": "+15550001", "status": "INVITED"}
        orig = sms_handler._events_table
        sms_handler._events_table = lambda: type("T", (), {
            "get_item": lambda self, **kw: {"Item": fake_ev}
        })()
        orig_inv = sms_handler._invites_table
        sms_handler._invites_table = lambda: type("T", (), {
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
            "eventId": "current", "event_label": "Test",
            "date": "2026-06-01", "ticketUrl": "https://posh.vip/test",
            "revealVenue": True, "event_status": "LIVE",
        }
        fake_member = {"phone": "+15550001", "status": "CONFIRMED"}
        orig = sms_handler._events_table
        sms_handler._events_table = lambda: type("T", (), {
            "get_item": lambda self, **kw: {"Item": fake_ev}
        })()
        orig_inv = sms_handler._invites_table
        sms_handler._invites_table = lambda: type("T", (), {
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
