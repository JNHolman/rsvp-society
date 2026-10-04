#!/usr/bin/env python3
"""Behavior-preserving characterization tests for RSVP Society.

These tests intentionally target business behavior that README/JADE say must be
preserved while dead code is removed and large modules are split. They do not
encode known defects as correct behavior.

Run without AWS/moto:
    python -m unittest characterization_tests -v
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("MEMBERS_TABLE_NAME", "rsvp-members-test")
os.environ.setdefault("INVITES_TABLE_NAME", "rsvp-event-invites-test")
os.environ.setdefault("EVENTS_TABLE_NAME", "rsvp-events-test")
os.environ.setdefault("INVITE_JOBS_TABLE_NAME", "rsvp-invite-jobs-test")
os.environ.setdefault("CHECKINS_TABLE_NAME", "rsvp-checkins-test")
os.environ.setdefault("PENDING_APPROVALS_TABLE_NAME", "rsvp-pending-approvals-test")
os.environ.setdefault("ADMIN_TOKEN_SECRET_ID", "rsvp/admin-token-test")
os.environ.setdefault("SMS_ENABLED", "false")

import access_request
import audit_log
import attendance_store
import admin_event_routes
import admin_handler
import admin_member_routes
import invite_handler
import invite_wave_schedule
import invite_sender
import jade_service
import member_store
import reminder_handler
import reminder_schedule
import route_contract_audit
import sms_handler
import sms_plus_one
import sms_adapter
import sms_webhook
import sms_intent


def _body(response: dict) -> dict:
    raw = response.get("body") or "{}"
    return json.loads(raw)


LOUISVILLE_ZIP = {
    "zipCode": "40205",
    "city": "Louisville",
    "state": "KY",
    "latitude": 38.2231,
    "longitude": -85.6836,
}


class TestSignupContract(unittest.TestCase):
    def test_public_signup_requires_explicit_sms_opt_in(self):
        event = {
            "httpMethod": "POST",
            "headers": {},
            "body": json.dumps({
                "firstName": "Jordan",
                "lastName": "Smith",
                "phone": "5025551212",
                "smsOptIn": False,
            }),
        }
        response = access_request.handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        self.assertIn("SMS opt-in", _body(response)["error"])

    def test_public_signup_rejects_bad_phone_before_writing(self):
        event = {
            "httpMethod": "POST",
            "headers": {},
            "body": json.dumps({
                "firstName": "Jordan",
                "lastName": "Smith",
                "phone": "123",
                "smsOptIn": True,
            }),
        }
        with patch.object(access_request, "upsert_member") as upsert:
            response = access_request.handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        upsert.assert_not_called()

    def test_structured_signup_preserves_first_and_last_name(self):
        fake_scan_table = MagicMock()
        fake_scan_table.scan.return_value = {"Items": []}
        fake_ddb = MagicMock()
        fake_ddb.Table.return_value = fake_scan_table
        saved = {
            "phone": "+15025551212",
            "name": "Jordan",
            "lastName": "Smith",
            "status": "PENDING",
            "smsOptIn": True,
        }
        event = {
            "httpMethod": "POST",
            "headers": {},
            "body": json.dumps({
                "firstName": "Jordan",
                "lastName": "Smith",
                "phone": "502-555-1212",
                "email": "jordan@example.com",
                "zipCode": "40205",
                "smsOptIn": True,
            }),
        }
        with patch.object(access_request.boto3, "resource", return_value=fake_ddb), \
             patch.object(access_request, "get_member", return_value={}), \
             patch.object(access_request, "resolve_us_zip", return_value=LOUISVILLE_ZIP), \
             patch.object(access_request, "upsert_member", return_value=saved) as upsert, \
             patch.object(access_request, "get_host_phones", return_value=[]), \
             patch.object(access_request, "maybe_send_welcome"):
            response = access_request.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        kwargs = upsert.call_args.kwargs
        self.assertEqual(kwargs["phone"], "+15025551212")
        self.assertEqual(kwargs["name"], "Jordan")
        self.assertEqual(kwargs["last_name"], "Smith")
        self.assertTrue(kwargs["sms_opt_in"])
        self.assertEqual(kwargs["zip_code"], "40205")
        self.assertEqual(kwargs["city"], "Louisville")
        self.assertEqual(kwargs["state"], "KY")
        self.assertEqual(kwargs["latitude"], LOUISVILLE_ZIP["latitude"])
        self.assertEqual(kwargs["longitude"], LOUISVILLE_ZIP["longitude"])

    def test_public_signup_zip_lookup_outage_fails_closed(self):
        event = {
            "httpMethod": "POST",
            "headers": {},
            "body": json.dumps({
                "firstName": "Jordan",
                "lastName": "Smith",
                "phone": "5025551212",
                "zipCode": "40205",
                "smsOptIn": True,
            }),
        }
        with patch.object(access_request, "resolve_us_zip", side_effect=access_request.ZipLookupUnavailable("down")), \
             patch.object(access_request, "upsert_member") as upsert:
            response = access_request.handler(event, None)
        self.assertEqual(response["statusCode"], 503)
        upsert.assert_not_called()


class TestAdminRoutingContract(unittest.TestCase):
    def test_public_event_route_does_not_require_admin_auth(self):
        fn, requires_auth = admin_handler._match_route("GET", "/event/current")
        self.assertIs(fn, admin_handler.get_public_event)
        self.assertFalse(requires_auth)

    def test_member_mutation_routes_require_admin_auth(self):
        fn, requires_auth = admin_handler._match_route("POST", "/admin/members/status")
        self.assertIs(fn, admin_handler.set_member_status)
        self.assertTrue(requires_auth)

    def test_finalize_attendance_is_a_registered_admin_route(self):
        fn, requires_auth = admin_handler._match_route("POST", "/admin/events/finalize-attendance")
        self.assertIs(fn, admin_handler.finalize_admin_event_attendance)
        self.assertTrue(requires_auth)

    def test_authenticated_admin_response_is_no_store(self):
        route = MagicMock(return_value={"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": "{}"})
        with patch.object(admin_handler, "_match_route", return_value=(route, True)), \
             patch.object(admin_handler, "get_admin_token", return_value="token"):
            result = admin_handler.handler({"httpMethod": "GET", "path": "/admin/members", "headers": {"x-admin-token": "token"}}, None)
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")

    def test_public_event_response_is_not_forced_no_store(self):
        route = MagicMock(return_value={"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": "{}"})
        with patch.object(admin_handler, "_match_route", return_value=(route, False)):
            result = admin_handler.handler({"httpMethod": "GET", "path": "/event/current", "headers": {}}, None)
        self.assertNotIn("Cache-Control", result.get("headers", {}))

    def test_unknown_route_does_not_fall_through_to_event(self):
        fn, requires_auth = admin_handler._match_route("GET", "/admin/not-a-route")
        self.assertIsNone(fn)
        self.assertFalse(requires_auth)


class TestCurrentEventContract(unittest.TestCase):
    def test_event_slug_is_strict_and_cannot_be_current(self):
        self.assertEqual(admin_event_routes._event_slug_from_data({"eventSlug": "rooftop-sept2026"}), "rooftop-sept2026")
        with self.assertRaises(ValueError):
            admin_event_routes._event_slug_from_data({"eventSlug": "current"})
        with self.assertRaises(ValueError):
            admin_event_routes._event_slug_from_data({"eventSlug": "Rooftop Sept"})

    def test_documented_lifecycle_supports_draft_live_archive(self):
        self.assertTrue(admin_event_routes._can_transition("DRAFT", "LIVE"))
        self.assertTrue(admin_event_routes._can_transition("LIVE", "ARCHIVED"))
        self.assertFalse(admin_event_routes._can_transition("ARCHIVED", "LIVE"))

    def test_live_validation_requires_real_capacity(self):
        base = {
            "eventSlug": "rooftop-sept2026",
            "event_label": "RSVP Society",
            "date": "2026-09-30",
            "startTime": "21:00",
            "event_timezone": "America/Kentucky/Louisville",
            "capacity": 0,
        }
        errors = admin_event_routes._validate_for_live(base)
        self.assertIn("capacity must be at least 1", errors)
        base["capacity"] = 100
        self.assertEqual(admin_event_routes._validate_for_live(base), [])


class TestEventGeographyContract(unittest.TestCase):
    def _event_data(self, **overrides):
        data = {
            "eventSlug": "rooftop-sept2026",
            "event_label": "RSVP Society",
            "date": "2026-09-30",
            "startTime": "21:00",
            "event_timezone": "America/Kentucky/Louisville",
            "capacity": 100,
            "event_status": "DRAFT",
            "eventZipCode": "40205",
            "promotionRadiusMiles": "60",
            "city": "WRONG CLIENT CITY",
        }
        data.update(overrides)
        return data

    def test_event_zip_is_authoritative_for_city_state_and_coordinates(self):
        with patch.object(admin_event_routes, "resolve_us_zip", return_value=LOUISVILLE_ZIP):
            item = admin_event_routes._build_event_item(self._event_data())
        self.assertEqual(item["eventZipCode"], "40205")
        self.assertEqual(item["city"], "Louisville")
        self.assertEqual(item["state"], "KY")
        self.assertEqual(item["latitude"], Decimal("38.2231"))
        self.assertEqual(item["longitude"], Decimal("-85.6836"))
        self.assertEqual(item["promotionRadiusMiles"], Decimal("60"))

    def test_live_event_requires_zip_and_promotion_radius(self):
        with self.assertRaisesRegex(ValueError, "eventZipCode"):
            admin_event_routes._build_event_item(self._event_data(
                event_status="LIVE", eventZipCode="", promotionRadiusMiles=""
            ))

    def test_event_zip_lookup_outage_returns_503(self):
        event = {"body": json.dumps(self._event_data())}
        with patch.object(admin_event_routes, "_save_event_record", side_effect=admin_event_routes.ZipLookupUnavailable("down")):
            response = admin_event_routes.create_or_update_admin_event(event, {}, "token")
        self.assertEqual(response["statusCode"], 503)


class TestGeographicAudienceContract(unittest.TestCase):
    def test_event_radius_uses_member_coordinates_not_phone_area_code(self):
        event = {
            "eventZipCode": "40205",
            "latitude": Decimal("38.2231"),
            "longitude": Decimal("-85.6836"),
            "promotionRadiusMiles": Decimal("60"),
        }
        members = [
            {
                "name": "Louisville",
                "phone": "+16155550001",  # Nashville area code is irrelevant.
                "city": "Louisville", "state": "KY",
                "latitude": Decimal("38.2231"), "longitude": Decimal("-85.6836"),
                "gender": "M", "tierOverride": 1,
            },
            {
                "name": "Lexington",
                "phone": "+15025550002",  # Louisville area code is irrelevant.
                "city": "Lexington", "state": "KY",
                "latitude": Decimal("38.0406"), "longitude": Decimal("-84.5037"),
                "gender": "F", "tierOverride": 1,
            },
            {
                "name": "Legacy Unknown",
                "phone": "+15025550003",
                "gender": "F", "tierOverride": 1,
            },
        ]
        result = invite_handler._apply_audience_filters(members, {"market": "All"}, event)
        self.assertEqual([m["name"] for m in result], ["Louisville"])

    def test_no_event_radius_preserves_all_markets_legacy_behavior(self):
        members = [{"name": "Legacy Unknown", "phone": "+15025550003", "gender": "F", "tierOverride": 1}]
        result = invite_handler._apply_audience_filters(members, {"market": "All"})
        self.assertEqual([m["name"] for m in result], ["Legacy Unknown"])


class TestInviteContract(unittest.TestCase):
    def test_tier_calculation_preserves_current_business_thresholds(self):
        self.assertEqual(invite_handler.calc_tier({"invitedCount": 2, "attendedCount": 0}), 2)
        self.assertEqual(invite_handler.calc_tier({"invitedCount": 10, "attendedCount": 8}), 1)
        self.assertEqual(invite_handler.calc_tier({"invitedCount": 10, "attendedCount": 5}), 2)
        self.assertEqual(invite_handler.calc_tier({"invitedCount": 10, "attendedCount": 2}), 3)
        self.assertEqual(invite_handler.calc_tier({"tierOverride": 3, "invitedCount": 10, "attendedCount": 10}), 3)

    def test_audience_filters_preserve_search_gender_and_tier(self):
        members = [
            {"name": "Alex", "lastName": "Jones", "phone": "+15025550001", "gender": "M", "tierOverride": 1},
            {"name": "Taylor", "lastName": "Smith", "phone": "+15025550002", "gender": "F", "tierOverride": 2},
        ]
        result = invite_handler._apply_audience_filters(
            members,
            {"query": "alex", "gender": "M", "tier": "1"},
        )
        self.assertEqual([m["phone"] for m in result], ["+15025550001"])

    def test_saved_invite_template_substitutes_first_name(self):
        event = {
            "invite_template": "{name}. RSVP Society. September 30. 9 PM. Let me know.",
            "venue": "Secret Room",
            "address": "123 Hidden St",
        }
        text = invite_handler._build_sms_message({"name": "Jordan Smith"}, event)
        self.assertTrue(text.startswith("Jordan."))
        self.assertNotIn("Secret Room", text)
        self.assertNotIn("123 Hidden St", text)

    def test_manual_invite_copy_cannot_leak_gated_information(self):
        event = {"venue": "Secret Room", "address": "123 Hidden St"}
        with self.assertRaises(ValueError):
            invite_handler._build_sms_message(
                {"name": "Jordan Smith"},
                event,
                "Jordan. Pull up to Secret Room at 123 Hidden St.",
            )

    def test_specific_market_filter_excludes_unknown_member_market(self):
        members = [{
            "name": "Alex",
            "phone": "+16155551212",
            "gender": "M",
            "tierOverride": 1,
        }]
        result = invite_handler._apply_audience_filters(members, {"market": "Louisville"})
        self.assertEqual(result, [])

    def test_specific_market_filter_uses_stored_market_not_phone_area_code(self):
        members = [{
            "name": "Alex",
            "phone": "+16155551212",  # Nashville area code
            "market": "Louisville",
            "gender": "M",
            "tierOverride": 1,
        }]
        result = invite_handler._apply_audience_filters(members, {"market": "Louisville"})
        self.assertEqual([m["phone"] for m in result], ["+16155551212"])

    def test_invite_message_metadata_distinguishes_override_from_template(self):
        event = {"invite_template": "Saved event copy"}
        saved = invite_handler._invite_message_metadata(event)
        manual = invite_handler._invite_message_metadata(event, "One-off copy")
        self.assertEqual(saved["inviteMessageSource"], "event_template")
        self.assertEqual(manual["inviteMessageSource"], "manual_override")
        self.assertNotEqual(saved["inviteMessageHash"], manual["inviteMessageHash"])


class TestSmsContract(unittest.TestCase):
    def setUp(self):
        event_patch = patch.object(sms_handler, "_get_current_event", return_value={"eventSlug": "audit", "date": "2099-10-17", "startTime": "19:00", "event_timezone": "UTC", "event_status": "LIVE", "allowPlusOnes": True})
        event_patch.start()
        self.addCleanup(event_patch.stop)
        confirmation_patch = patch.object(sms_handler, "_build_confirmation_message", return_value="See you October 17.")
        confirmation_patch.start()
        self.addCleanup(confirmation_patch.stop)

    def test_natural_yes_is_a_confirm(self):
        self.assertEqual(sms_handler._detect_rsvp_intent("Yes I'm in!", "YES I'M IN!"), "confirm")
        self.assertEqual(sms_handler._detect_rsvp_intent("count me in", "COUNT ME IN"), "confirm")

    def test_questions_never_change_rsvp_state(self):
        for text in ("Is it in Nashville?", "Who else is in?", "Ok what's the address"):
            with self.subTest(text=text):
                self.assertEqual(sms_handler._detect_rsvp_intent(text, text.upper()), "")

    def test_short_rsvp_words_only_match_as_the_entire_message(self):
        for text in ("in", "ok", "sure"):
            with self.subTest(text=text):
                self.assertEqual(sms_handler._detect_rsvp_intent(text, text.upper()), "confirm")
        for text in ("pass", "no"):
            with self.subTest(text=text):
                self.assertEqual(sms_handler._detect_rsvp_intent(text, text.upper()), "decline")
        self.assertEqual(sms_handler._detect_rsvp_intent("OK what's up", "OK WHAT'S UP"), "")
        self.assertEqual(sms_handler._detect_rsvp_intent("No, where is it?", "NO, WHERE IS IT?"), "")

    def test_stop_keyword_matching_normalizes_punctuation_but_not_prose(self):
        for text in ("STOP", "Stop.", " stop! ", "Unsubscribe.", "CANCEL!!!"):
            with self.subTest(text=text):
                self.assertTrue(sms_handler._is_opt_out_message(text))
        for text in ("STOP PLEASE", "PLEASE CANCEL", "I can't come"):
            with self.subTest(text=text):
                self.assertFalse(sms_handler._is_opt_out_message(text))

    def test_decline_from_confirmed_invite_uses_cancellation_path(self):
        confirmed_invite = {"eventId": "event-1", "phone": "+15025551212", "status": "CONFIRMED"}
        event = {"date": "2099-10-17", "startTime": "19:00", "eventSlug": "event-1", "event_status": "LIVE", "attendanceFinalized": False}
        fake_invites = MagicMock()
        fake_invites.get_item.return_value = {"Item": confirmed_invite}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value={"status": "APPROVED", "smsOptIn": True}), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=confirmed_invite), \
             patch.object(sms_handler, "_get_pending_invite", return_value=None), \
             patch.object(sms_handler, "_get_current_event", return_value=event), \
             patch.object(sms_handler, "_invites_table", return_value=fake_invites), \
             patch.object(sms_handler, "_cancel_confirmed_invite", return_value=True) as cancel, \
             patch.object(sms_handler, "send_sms") as send_sms:
            response = sms_handler.handler(_legacy_sms_event("No"), None)
        self.assertEqual(response["statusCode"], 200)
        cancel.assert_called_once_with("event-1", "+15025551212")
        send_sms.assert_called_once_with("+15025551212", "Got it — your RSVP is canceled and your spot is open.")

    def test_natural_no_is_a_decline(self):
        self.assertEqual(sms_handler._detect_rsvp_intent("can't make it", "CAN'T MAKE IT"), "decline")
        self.assertEqual(sms_handler._detect_rsvp_intent("maybe next time", "MAYBE NEXT TIME"), "decline")

    def test_status_question_is_not_a_fresh_rsvp(self):
        normalized = "AM I CONFIRMED"
        self.assertTrue(sms_handler._is_status_question(normalized))
        self.assertEqual(sms_handler._detect_rsvp_intent("Am I confirmed?", normalized), "")

    def test_long_message_is_not_silently_converted_to_rsvp(self):
        text = "Yes but what time does the event start"
        self.assertEqual(sms_handler._detect_rsvp_intent(text, text.upper()), "")

    def test_plus_one_question_does_not_look_like_a_name(self):
        self.assertTrue(sms_intent._is_question_like_text("Where is parking?", "WHERE IS PARKING?"))
        self.assertFalse(sms_intent._is_question_like_text("Parker Smith", "PARKER SMITH"))

    def test_legacy_quo_envelope_extraction_is_preserved(self):
        event_type, phone, text = sms_handler._extract_inbound_message({
            "type": "message.received",
            "data": {"object": {"from": "5025551212", "body": "YES"}},
        })
        self.assertEqual(event_type, "message.received")
        self.assertEqual(phone, "+15025551212")
        self.assertEqual(text, "YES")

    def test_current_quo_resource_envelope_is_supported(self):
        event_type, phone, text = sms_handler._extract_inbound_message({
            "type": "message.received",
            "apiVersion": "2026-03-30",
            "data": {"resource": {"from": "5025551212", "text": "YES"}},
        })
        self.assertEqual(event_type, "message.received")
        self.assertEqual(phone, "+15025551212")
        self.assertEqual(text, "YES")


class TestReminderContract(unittest.TestCase):
    def test_reminder_window_is_exact_five_minute_window(self):
        self.assertTrue(reminder_handler._within_scheduled_window(
            datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc), 18, 0
        ))
        self.assertTrue(reminder_handler._within_scheduled_window(
            datetime(2026, 9, 30, 18, 4, tzinfo=timezone.utc), 18, 0
        ))
        self.assertFalse(reminder_handler._within_scheduled_window(
            datetime(2026, 9, 30, 18, 5, tzinfo=timezone.utc), 18, 0
        ))

    def test_day_specific_template_wins_over_legacy_template(self):
        event = {
            "_is_day_of": True,
            "day_of_template": "{name}, tonight.",
            "reminder_template": "{name}, legacy.",
        }
        self.assertEqual(reminder_handler._build_reminder("Jordan Smith", event), "Jordan, tonight.")


class TestMemberRecordContract(unittest.TestCase):
    def test_phone_normalization_is_stable(self):
        self.assertEqual(member_store.normalize_phone("(502) 555-1212"), "+15025551212")
        self.assertEqual(member_store.normalize_phone("+1 502 555 1212"), "+15025551212")
        with self.assertRaises(ValueError):
            member_store.normalize_phone("123")

    def test_legacy_full_name_normalizes_to_first_and_last(self):
        member = member_store.normalize_member_record({"phone": "+15025551212", "name": "Jordan A Smith"})
        self.assertEqual(member["name"], "Jordan")
        self.assertEqual(member["lastName"], "A Smith")

    def test_explicit_close_marks_attendance_settled(self):
        self.assertTrue(member_store.attendance_is_settled({"attendanceFinalized": True}))
        self.assertFalse(member_store.attendance_is_settled({"attendanceFinalized": False}))


class TestAdminMemberMutationContract(unittest.TestCase):
    def test_gender_validation_happens_before_storage(self):
        event = {"body": json.dumps({"phone": "5025551212", "gender": "X"})}
        with patch.object(admin_member_routes, "set_gender") as setter:
            response = admin_member_routes.set_member_gender(event, {}, "token")
        self.assertEqual(response["statusCode"], 400)
        setter.assert_not_called()

    def test_first_approval_preserves_consent_and_claims_welcome(self):
        event = {"body": json.dumps({"phone": "5025551212", "status": "APPROVED"})}
        before = {"phone": "+15025551212", "status": "PENDING"}
        after = {"phone": "+15025551212", "status": "APPROVED", "smsOptIn": True}
        with patch.object(admin_member_routes, "get_member", side_effect=[before, after]), \
             patch.object(admin_member_routes, "set_status") as set_status, \
             patch.object(admin_member_routes, "_clear_pending_approvals_for_member"), \
             patch.object(admin_member_routes, "claim_welcome_send", return_value=True), \
             patch.object(admin_member_routes, "maybe_send_welcome", return_value=True), \
             patch.object(admin_member_routes, "mark_welcome_sent") as mark_sent, \
             patch.object(admin_member_routes, "log_action"):
            response = admin_member_routes.set_member_status(event, {}, "token")
        self.assertEqual(response["statusCode"], 200)
        set_status.assert_called_once_with("+15025551212", "APPROVED")
        mark_sent.assert_called_once_with("+15025551212")


class TestImportContract(unittest.TestCase):
    def test_import_requires_members_array(self):
        response = admin_member_routes.import_members({"body": json.dumps({})}, {}, "token")
        self.assertEqual(response["statusCode"], 400)
        self.assertIn("members array required", _body(response)["error"])

    def test_batch_lookup_collects_records_across_chunks(self):
        client = MagicMock()
        table = MagicMock()
        table.name = "rsvp-members-test"
        table.meta.client.batch_write_item.return_value = {"UnprocessedItems": {}}
        table.meta.client = client

        calls = []
        def batch_get_item(RequestItems):
            keys = RequestItems[table.name]["Keys"]
            calls.append(len(keys))
            return {
                "Responses": {table.name: [{"phone": key["phone"], "status": "APPROVED"} for key in keys]},
                "UnprocessedKeys": {},
            }
        client.batch_get_item.side_effect = batch_get_item
        phones = [f"+1502555{i:04d}" for i in range(30)]
        existing = admin_member_routes._batch_get_existing(table, phones)
        self.assertEqual(calls, [25, 5])
        self.assertEqual(set(existing), set(phones))

    def test_csv_import_does_not_opt_in_without_file_level_attestation(self):
        table = MagicMock()
        table.name = "rsvp-members-test"
        table.meta.client.batch_write_item.return_value = {"UnprocessedItems": {}}
        raw_client = MagicMock()
        ddb = MagicMock()
        ddb.Table.return_value = table
        event = {"body": json.dumps({
            "members": [{"phone": "5025551212", "name": "Jordan", "zipCode": "40205"}],
            "source": "csv",
            "consentConfirmed": False,
        })}
        with patch.object(admin_member_routes.boto3, "resource", return_value=ddb), \
             patch.object(admin_member_routes.boto3, "client", return_value=raw_client), \
             patch.object(admin_member_routes, "resolve_us_zip", return_value={"zipCode": "40205", "city": "Louisville", "state": "KY", "latitude": 38.223, "longitude": -85.683}), \
             patch.object(admin_member_routes, "_batch_get_existing", return_value={}), \
             patch.object(admin_member_routes, "log_action"):
            result = admin_member_routes.import_members(event, {}, "token")
        self.assertEqual(result["statusCode"], 200)
        from boto3.dynamodb.types import TypeDeserializer
        request = raw_client.put_item.call_args.kwargs
        item = {k: TypeDeserializer().deserialize(v) for k, v in request["Item"].items()}
        self.assertEqual(request["ConditionExpression"], "attribute_not_exists(phone)")
        self.assertFalse(item["smsOptIn"])
        self.assertEqual(item["source"], "csv")
        self.assertEqual(item["zipCode"], "40205")
        self.assertEqual(item["city"], "Louisville")
        self.assertNotIn("smsOptInConfirmedAt", item)

    def test_csv_import_records_attested_opt_in_and_source(self):
        table = MagicMock()
        table.name = "rsvp-members-test"
        table.meta.client.batch_write_item.return_value = {"UnprocessedItems": {}}
        raw_client = MagicMock()
        ddb = MagicMock()
        ddb.Table.return_value = table
        event = {"body": json.dumps({
            "members": [{"phone": "5025551212", "name": "Jordan", "zipCode": "40205"}],
            "source": "csv",
            "consentConfirmed": True,
        })}
        with patch.object(admin_member_routes.boto3, "resource", return_value=ddb), \
             patch.object(admin_member_routes.boto3, "client", return_value=raw_client), \
             patch.object(admin_member_routes, "resolve_us_zip", return_value={"zipCode": "40205", "city": "Louisville", "state": "KY", "latitude": 38.223, "longitude": -85.683}), \
             patch.object(admin_member_routes, "_batch_get_existing", return_value={}), \
             patch.object(admin_member_routes, "log_action"):
            result = admin_member_routes.import_members(event, {}, "token")
        self.assertEqual(result["statusCode"], 200)
        from boto3.dynamodb.types import TypeDeserializer
        request = raw_client.put_item.call_args.kwargs
        item = {k: TypeDeserializer().deserialize(v) for k, v in request["Item"].items()}
        self.assertEqual(request["ConditionExpression"], "attribute_not_exists(phone)")
        self.assertTrue(item["smsOptIn"])
        self.assertIn("smsOptInAt", item)
        self.assertIn("smsOptInConfirmedAt", item)
        self.assertEqual(item["smsOptInConfirmationSource"], "csv_import_attestation")
        self.assertEqual(item["source"], "csv")

    def test_csv_import_accepts_rows_without_zip(self):
        table = MagicMock()
        table.name = "rsvp-members-test"
        ddb = MagicMock()
        ddb.Table.return_value = table
        event = {"body": json.dumps({"members": [{"phone": "5025551212", "name": "Jordan"}]})}
        with patch.object(admin_member_routes.boto3, "client", return_value=MagicMock()), patch.object(admin_member_routes.boto3, "resource", return_value=ddb), \
             patch.object(admin_member_routes, "resolve_us_zip") as resolve_zip, \
             patch.object(admin_member_routes, "_batch_get_existing", return_value={}), \
             patch.object(admin_member_routes, "log_action"):
            result = admin_member_routes.import_members(event, {}, "token")
        payload = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(payload["excludedNoLocation"], 0)
        self.assertEqual(payload["imported"], 1)
        resolve_zip.assert_not_called()


def _legacy_sms_event(text: str, phone: str = "5025551212") -> dict:
    return {
        "body": json.dumps({
            "type": "message.received",
            "data": {"object": {"from": phone, "body": text}},
        }),
        "headers": {"openphone-signature": "test"},
    }


class TestSmsHandlerFlowContract(unittest.TestCase):
    def setUp(self):
        event_patch = patch.object(sms_handler, "_get_current_event", return_value={"eventSlug": "audit", "date": "2099-10-17", "startTime": "19:00", "event_timezone": "UTC", "event_status": "LIVE", "allowPlusOnes": True})
        event_patch.start()
        self.addCleanup(event_patch.stop)
        confirmation_patch = patch.object(sms_handler, "_build_confirmation_message", return_value="See you October 17.")
        confirmation_patch.start()
        self.addCleanup(confirmation_patch.stop)

    def test_pending_plus_one_last_name_is_saved_instead_of_treated_as_unsolicited_name(self):
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value={"status": "APPROVED", "smsOptIn": True}), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value={"eventId": "audit", "awaitingPlusOneLastName": "Taylor"}), \
             patch.object(sms_handler, "_validate_plus_one_candidate", return_value=(True, "", False)), \
             patch.object(sms_handler, "_set_plus_one", return_value="SAVED") as save, \
             patch.object(sms_handler, "send_sms"):
            sms_handler.handler(_legacy_sms_event("Smith"), None)
        save.assert_called_once_with("audit", "+15025551212", "Taylor Smith", is_member=False)

    def test_cancellation_cutoff_is_24_elapsed_hours_across_daylight_saving_change(self):
        from invite_capacity import cancellation_timing
        from datetime import timedelta
        event = {"date": "2026-11-01", "startTime": "18:00", "event_timezone": "America/New_York"}
        cutoff = datetime(2026, 10, 31, 23, tzinfo=timezone.utc)
        self.assertEqual(cancellation_timing(event, cutoff), "TIMELY")
        self.assertEqual(cancellation_timing(event, cutoff + timedelta(seconds=1)), "LATE")

    def test_explicit_cancellation_cancels_even_while_collecting_plus_one_name(self):
        for flag, value in (("awaitingPlusOneName", True), ("awaitingPlusOneLastName", "Taylor")):
            with self.subTest(flag=flag):
                table = MagicMock()
                table.get_item.return_value = {"Item": {"status": "CONFIRMED"}}
                with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
                     patch.object(sms_handler, "get_host_phones", return_value=[]), \
                     patch.object(sms_handler, "get_member", return_value={"status": "APPROVED", "smsOptIn": True}), \
                     patch.object(sms_handler, "_get_confirmed_invite", return_value={"eventId": "audit", flag: value}), \
                     patch.object(sms_handler, "_get_pending_invite", return_value=None), \
                     patch.object(sms_handler, "_get_current_event", return_value={"eventSlug": "audit", "event_status": "LIVE", "date": "2099-10-17", "startTime": "19:00"}), \
                     patch.object(sms_handler, "_invites_table", return_value=table), \
                     patch.object(sms_handler, "_cancel_confirmed_invite", return_value=True) as cancel, \
                     patch.object(sms_handler, "send_sms"):
                    sms_handler.handler(_legacy_sms_event("I can’t come"), None)
                cancel.assert_called_once_with("audit", "+15025551212")
                table.update_item.assert_not_called()

    def test_stop_short_circuits_before_member_or_host_routing(self):
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "_set_opt_out") as set_opt_out, \
             patch.object(sms_handler, "get_host_phones") as host_phones, \
             patch.object(sms_handler, "get_member") as get_member:
            response = sms_handler.handler(_legacy_sms_event("STOP"), None)
        self.assertEqual(response["statusCode"], 200)
        set_opt_out.assert_called_once_with("+15025551212")
        host_phones.assert_not_called()
        get_member.assert_not_called()

    def test_host_approval_command_updates_status_and_clears_every_host_queue(self):
        host = "+15025550001"
        other_host = "+15025550002"
        pending = {
            "memberPhone": "+15025551212",
            "memberName": "Jordan Smith",
            "approvalCode": "4821",
        }
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[host, other_host]), \
             patch.object(sms_handler, "_get_pending_approval", return_value=pending), \
             patch.object(sms_handler, "set_status") as set_status, \
             patch.object(sms_handler, "_clear_pending_approval") as clear_pending, \
             patch.object(sms_handler, "get_member", side_effect=[
                 {"phone": pending["memberPhone"], "status": "PENDING"},
                 {"phone": pending["memberPhone"], "status": "APPROVED", "welcomeSentAt": "already"},
             ]):
            response = sms_handler.handler(_legacy_sms_event("Y 4821", host), None)
        self.assertEqual(response["statusCode"], 200)
        set_status.assert_called_once_with(pending["memberPhone"], "APPROVED", expected_status="PENDING")
        self.assertEqual(
            clear_pending.call_args_list,
            [
                unittest.mock.call(host, pending["memberPhone"]),
                unittest.mock.call(other_host, pending["memberPhone"]),
            ],
        )

    def test_yes_routes_through_handler_to_confirmed_transition(self):
        member = {
            "phone": "+15025551212",
            "name": "Jordan",
            "status": "APPROVED",
            "smsOptIn": True,
        }
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "capacity": 100, "allowPlusOnes": False}
        invite = {"eventId": "rooftop-sept2026", "phone": member["phone"], "status": "INVITED"}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_handler, "_get_current_event", return_value=event), \
             patch.object(sms_handler, "_get_reconfirmable_invite", return_value=invite), \
             patch.object(sms_handler, "_confirm_invite_with_capacity", return_value="CONFIRMED") as confirm_atomic:
            response = sms_handler.handler(_legacy_sms_event("YES"), None)
        self.assertEqual(response["statusCode"], 200)
        confirm_atomic.assert_called_once_with("rooftop-sept2026", "+15025551212", 167)

    def test_status_question_does_not_reconfirm_through_handler(self):
        member = {"phone": "+15025551212", "name": "Jordan", "status": "APPROVED", "smsOptIn": True}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_handler, "_get_current_invite_status", return_value="CONFIRMED"), \
             patch.object(sms_handler, "_update_invite_status") as update_status:
            response = sms_handler.handler(_legacy_sms_event("Am I confirmed?"), None)
        self.assertEqual(response["statusCode"], 200)
        update_status.assert_not_called()


    def test_awaiting_plus_one_name_is_captured_before_jade_routing(self):
        member = {"phone": "+15025551212", "name": "Jordan", "status": "APPROVED", "smsOptIn": True}
        invite = {"eventId": "rooftop-sept2026", "phone": member["phone"], "status": "CONFIRMED", "awaitingPlusOneName": True}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=invite), \
             patch.object(sms_handler, "_validate_plus_one_candidate", return_value=(True, "", False)), \
             patch.object(sms_handler, "_set_plus_one", return_value="SAVED") as set_plus_one, \
             patch.object(sms_handler, "_claude") as claude:
            response = sms_handler.handler(_legacy_sms_event("Taylor Smith"), None)
        self.assertEqual(response["statusCode"], 200)
        set_plus_one.assert_called_once_with("rooftop-sept2026", "+15025551212", "Taylor Smith", is_member=False)
        claude.assert_not_called()

    def test_general_member_question_routes_to_jade_after_deterministic_guards(self):
        member = {"phone": "+15025551212", "name": "Jordan", "status": "APPROVED", "smsOptIn": True}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_handler, "_get_current_invite_status", return_value="INVITED"), \
             patch.object(sms_handler, "_get_current_event", return_value={}), \
             patch.object(sms_handler, "_claude", return_value="Keep it sharp.") as claude:
            response = sms_handler.handler(_legacy_sms_event("What is the dress code?"), None)
        self.assertEqual(response["statusCode"], 200)
        claude.assert_called_once_with("What is the dress code?", mode="general", member=member)


class TestAuditLogSecurityContract(unittest.TestCase):
    def test_actor_fingerprint_never_contains_token_characters(self):
        token = "super-secret-admin-token-ABCDEFGH"
        fingerprint = audit_log._actor_tag(token)
        self.assertEqual(len(fingerprint), 12)
        self.assertNotIn(token[-8:], fingerprint)
        self.assertEqual(fingerprint, audit_log._actor_tag(token))


class TestInviteExecutionFlowContract(unittest.TestCase):
    def test_wave_reads_do_not_use_partial_results_after_database_failure(self):
        error = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")
        for read in (invite_handler._get_next_wave_number, invite_handler._get_analytics):
            with self.subTest(read=read.__name__):
                table = MagicMock()
                table.query.side_effect = [
                    {"Items": [{"phone": "+15025550001", "status": "CONFIRMED", "waveNumber": 1}],
                     "LastEvaluatedKey": {"eventId": "event-1", "phone": "+15025550001"}},
                    error,
                ]
                with patch.object(invite_handler, "_invites_table", return_value=table):
                    with self.assertRaises(ClientError):
                        read("event-1")

    def test_failed_wave_analytics_cannot_lock_a_preview(self):
        error = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")
        with patch.object(invite_handler, "_get_next_wave_number", return_value=2), \
             patch.object(invite_handler, "_event_for_guard", return_value={}), \
             patch.object(invite_handler, "_event_promotion_geography", return_value=True), \
             patch.object(invite_handler, "_validate_initial_invite_text"), \
             patch.object(invite_handler, "_get_existing_invited_phones", return_value=set()), \
             patch.object(invite_handler, "_get_existing_invite_map", return_value={}), \
             patch.object(invite_handler, "_get_analytics", side_effect=error), \
             patch.object(invite_handler, "_get_approved_members") as members, \
             patch.object(invite_handler, "_write_preview_lock") as lock:
            with self.assertRaises(ClientError):
                invite_handler.handle_preview({"eventId": "event-1", "capacity": 200}, "")
        members.assert_not_called()
        lock.assert_not_called()

    def test_later_wave_analytics_count_confirmed_plus_ones_as_seats(self):
        rows = [
            {"phone": "+15025550001", "status": "CONFIRMED", "plusOneName": "Guest One", "attendedAt": "2026-09-01T20:00:00Z", "plusOneAttendedAt": "2026-09-01T20:00:00Z"},
            {"phone": "+15025550002", "status": "INVITED"},
            {"phone": "+15025550003", "status": "DECLINED"},
        ]
        table = MagicMock()
        table.query.return_value = {"Items": rows}
        with patch.object(invite_handler, "_invites_table", return_value=table):
            analytics = invite_handler._get_analytics("event-1")
        self.assertEqual(analytics["confirmed"], 1)
        self.assertEqual(analytics["confirmedHeadcount"], 2)
        self.assertAlmostEqual(analytics["confirmRate"], 2 / 3)
        self.assertEqual(analytics["showRate"], 1.0)

    def test_wave_one_target_uses_tier_one_attendance_floor(self):
        for capacity in (100, 200, 500, 1000):
            with self.subTest(capacity=capacity):
                self.assertEqual(invite_handler._resolve_wave_capacity(capacity, 1, 0), math.ceil(capacity / 0.8))


class TestAutomaticWaveScheduling(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        self.event = {
            "eventId": "party-one", "eventSlug": "party-one", "event_status": "LIVE",
            "date": "2026-10-10", "startTime": "19:00", "event_timezone": "UTC", "capacity": 200,
        }

    def test_response_window_is_48_hours_when_event_is_far_enough(self):
        spec = invite_wave_schedule.next_wave_schedule_spec(self.event, 1, now=self.now)
        self.assertEqual(spec["responseWindowHours"], 48)
        self.assertEqual(spec["waveNumber"], 2)
        self.assertEqual(spec["fireAt"], self.now + timedelta(hours=48))

    def test_response_window_is_not_compressed_for_close_events(self):
        event = {**self.event, "date": "2026-10-03", "startTime": "12:00"}
        spec = invite_wave_schedule.next_wave_schedule_spec(event, 1, now=self.now)
        self.assertIsNone(spec)
        too_close = {**event, "date": "2026-10-02", "startTime": "23:00"}
        self.assertIsNone(invite_wave_schedule.next_wave_schedule_spec(too_close, 1, now=self.now))

    def test_wave_three_and_archived_events_do_not_schedule_followups(self):
        self.assertIsNone(invite_wave_schedule.next_wave_schedule_spec(self.event, 3, now=self.now))
        archived = {**self.event, "event_status": "ARCHIVED"}
        self.assertIsNone(invite_wave_schedule.next_wave_schedule_spec(archived, 1, now=self.now))

    def test_schedule_is_one_time_and_preserves_filters_without_member_data(self):
        client = MagicMock()
        with patch.dict(os.environ, {
            "AUTO_WAVE_SCHEDULER_ROLE_ARN": "arn:aws:iam::123:role/scheduler",
            "INVITE_HANDLER_ARN": "arn:aws:lambda:us-east-1:123:function:invite",
        }), patch.object(invite_wave_schedule.boto3, "client", return_value=client):
            result = invite_wave_schedule.schedule_next_wave(
                self.event, 1, female_percent=65,
                audience_filters={"market": "Louisville"}, now=self.now,
            )
        self.assertTrue(result["scheduled"])
        call = client.create_schedule.call_args.kwargs
        self.assertEqual(call["ScheduleExpression"], "at(2026-10-03T12:00:00)")
        self.assertEqual(call["ActionAfterCompletion"], "DELETE")
        payload = json.loads(call["Target"]["Input"])
        self.assertEqual(payload["femalePercent"], 65)
        self.assertEqual(payload["audienceFilters"], {"market": "Louisville"})
        self.assertNotIn("phones", payload)

    def test_scheduled_follow_up_rechecks_event_and_uses_locked_preview(self):
        preview = {"statusCode": 200, "body": json.dumps({
            "ok": True, "previewSessionId": "locked-1",
            "summary": {"waveSize": 200}, "members": [{"phone": "+15025550123"}],
        })}
        sent = {"statusCode": 202, "body": json.dumps({"jobId": "auto-job", "status": "QUEUED"})}
        schedule_event = {
            "source": "rsvp.auto-wave", "eventId": "party-one", "previousWaveNumber": 1,
            "femalePercent": 65, "audienceFilters": {"market": "Louisville"},
        }
        with patch.object(invite_handler, "_get_current_event_strict", return_value={**self.event, "activeEventSlug": "party-one"}), \
             patch.object(invite_handler, "_get_next_wave_number", return_value=2), \
             patch.object(invite_handler, "_event_start_utc", return_value=self.now + timedelta(days=5)), \
             patch.object(invite_handler, "handle_preview", return_value=preview) as preview_mock, \
             patch.object(invite_handler, "handle_send", return_value=sent) as send_mock, \
             patch.object(invite_handler, "_admin_token", return_value="host-token"):
            result = invite_handler._handle_auto_wave_schedule(schedule_event)
        self.assertEqual(result["statusCode"], 200)
        preview_mock.assert_called_once()
        body = send_mock.call_args.args[0]
        self.assertEqual(body["phones"], ["+15025550123"])
        self.assertEqual(body["previewSessionId"], "locked-1")
        self.assertEqual(body["femalePercent"], 65)
        self.assertEqual(body["audienceFilters"], {"market": "Louisville"})
        self.assertTrue(body["automaticWave"])
        self.assertEqual(send_mock.call_args.kwargs["job_id_override"], invite_handler._auto_wave_job_id("party-one", 2))

    def test_scheduled_follow_up_does_not_send_if_event_is_no_longer_active(self):
        schedule_event = {"source": "rsvp.auto-wave", "eventId": "party-one", "previousWaveNumber": 1}
        with patch.object(invite_handler, "_get_current_event_strict", return_value={"activeEventSlug": "party-two"}), \
             patch.object(invite_handler, "handle_preview") as preview:
            result = invite_handler._handle_auto_wave_schedule(schedule_event)
        self.assertIn("no longer active", result["body"])
        preview.assert_not_called()

    def test_automatic_wave_redelivery_reuses_queued_job_without_dispatch(self):
        jobs = MagicMock()
        jobs.get_item.return_value = {"Item": {"status": "PROCESSING", "recipientCount": 8}}
        with patch.object(invite_handler, "_invite_jobs_table", return_value=jobs), \
             patch.object(invite_handler, "_write_job") as write_job, \
             patch.object(invite_handler.boto3, "client") as aws_client:
            response = invite_handler.handle_send(
                {"confirmSend": True, "eventId": "party-one"}, "", "token",
                job_id_override="AUTO-WAVE-party-w2",
            )
        self.assertEqual(response["statusCode"], 202)
        self.assertTrue(json.loads(response["body"])["duplicate"])
        write_job.assert_not_called()
        aws_client.assert_not_called()

    def test_automatic_wave_job_creation_race_reads_existing_job_without_redispatch(self):
        jobs = MagicMock()
        jobs.get_item.side_effect = [
            {}, {"Item": {"status": "QUEUED", "recipientCount": 5}},
        ]
        conditional_conflict = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        body = {
            "confirmSend": True, "eventId": "party-one", "waveNumber": 2,
            "autoWave": False, "lockedWave": True, "phones": ["+15025550123"],
        }
        with patch.object(invite_handler, "_invite_jobs_table", return_value=jobs), \
             patch.object(invite_handler, "_resolve_active_invitable_event", return_value=self.event), \
             patch.object(invite_handler, "_validate_initial_invite_text"), \
             patch.object(invite_handler, "_assert_formal_wave_available"), \
             patch.object(invite_handler, "_get_next_wave_number", return_value=2), \
             patch.object(invite_handler, "_write_job", side_effect=conditional_conflict), \
             patch.object(invite_handler.boto3, "client") as aws_client:
            response = invite_handler.handle_send(body, "", "token", job_id_override="AUTO-WAVE-party-w2")
        self.assertEqual(response["statusCode"], 202)
        self.assertTrue(json.loads(response["body"])["duplicate"])
        aws_client.assert_not_called()

    def test_wave_one_uses_tier_two_only_when_no_tier_one_candidates_exist(self):
        cold = [{"phone": "+15025550001", "gender": "F", "_tier": 2}]
        result = invite_handler._build_invite_list(cold, 10, 50, wave_number=1)
        self.assertEqual([m["phone"] for m in result["members"]], ["+15025550001"])
        self.assertTrue(result["summary"]["breakdown"]["coldStartTier2Fallback"])
        warm = [*cold, {"phone": "+15025550002", "gender": "F", "_tier": 1}]
        result = invite_handler._build_invite_list(warm, 10, 50, wave_number=1)
        self.assertEqual([m["phone"] for m in result["members"]], ["+15025550002"])
        self.assertFalse(result["summary"]["breakdown"]["coldStartTier2Fallback"])

    def test_explicit_wave_size_overrides_recommendation(self):
        self.assertEqual(invite_handler._resolve_wave_capacity(1000, 1, 1600), 1600)

    def test_auto_followup_waves_are_bounded_and_shrink_with_confirmations(self):
        for capacity in (100, 200, 500, 1000):
            for wave in (2, 3):
                self.assertEqual(invite_handler._resolve_wave_capacity(capacity, wave, 0), math.ceil(capacity * 2.5))
        self.assertEqual(invite_handler._resolve_wave_capacity(200, 2, 0, confirmed=330), 14)
        self.assertEqual(invite_handler._resolve_wave_capacity(200, 3, 0, confirmed=334), 0)
        self.assertEqual(invite_handler._resolve_wave_capacity(200, 2, 700), 700)

    def test_formal_waves_expand_from_tier_one_to_tier_three(self):
        members = []
        for tier in (1, 2, 3):
            for i in range(100):
                members.append({
                    "phone": f"+150255{tier}{i:04d}",
                    "gender": "F" if i % 2 else "M",
                    "_tier": tier,
                })
        wave_one = invite_handler._build_invite_list(members, 150, 50, wave_number=1)
        wave_two = invite_handler._build_invite_list(members, 150, 50, wave_number=2)
        wave_three = invite_handler._build_invite_list(members, 300, 50, wave_number=3)
        self.assertEqual(wave_one["summary"]["totalInvites"], 100)
        self.assertEqual(wave_one["summary"]["breakdown"]["tier3Skipped"], 100)
        self.assertEqual(wave_two["summary"]["totalInvites"], 150)
        self.assertEqual(wave_two["summary"]["breakdown"]["tier3Skipped"], 100)
        self.assertEqual(wave_three["summary"]["totalInvites"], 300)
        self.assertEqual(wave_three["summary"]["breakdown"]["tier3Skipped"], 0)

    @patch("invite_sender.reserve_invite", new=lambda invites, members, item, expected_status=None: invites.put_item(Item=item))
    def test_execute_send_persists_selected_invite_and_updates_member_counter(self):
        phone = "+15025551212"
        member = {"phone": phone, "name": "Jordan", "lastName": "Smith", "gender": "F", "tierOverride": 1, "market": "Louisville", "_tier": 1}
        invites = MagicMock()
        members = MagicMock()
        events = MagicMock()
        ddb = MagicMock()
        ddb.Table.return_value = events
        body = {
            "eventId": "rooftop-sept2026",
            "capacity": 100,
            "waveNumber": 1,
            "phones": [phone],
            "audienceFilters": {"market": "Louisville"},
        }
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "invite_template": "{name}. RSVP Society."}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(invite_handler, "_resolve_active_invitable_event", return_value=event), \
             patch.object(invite_handler, "_assert_formal_wave_available"), \
             patch.object(invite_handler, "_get_existing_invited_phones", return_value=set()), \
             patch.object(invite_handler, "_get_approved_members", return_value=[member]), \
             patch.object(invite_handler, "_invites_table", return_value=invites), \
             patch.object(invite_handler, "members_table", return_value=members), \
             patch.object(invite_handler.boto3, "resource", return_value=ddb), \
             patch.object(invite_handler, "_update_job") as update_job, \
             patch.object(invite_handler, "send_sms", return_value="quo-message-1"), \
             patch.object(invite_handler, "schedule_next_wave", return_value={
                 "scheduled": True, "waveNumber": 2, "responseWindowHours": 48,
             }) as schedule_wave, \
             patch.object(invite_handler, "log_action"):
            invite_handler._execute_send(body, "", "token", "job-1")
        written = invites.put_item.call_args.kwargs["Item"]
        self.assertEqual(written["phone"], phone)
        self.assertEqual(written["status"], "INVITED")
        self.assertEqual(written["eventId"], "rooftop-sept2026")
        members.update_item.assert_called_once()
        self.assertEqual(members.update_item.call_args.kwargs["Key"], {"phone": phone})
        job_updates = [
            call.kwargs.get("updates") or (call.args[1] if len(call.args) > 1 else {})
            for call in update_job.call_args_list
        ]
        schedule_wave.assert_called_once_with(event, 1, female_percent=60, audience_filters={"market": "Louisville"})
        self.assertTrue(any(
            update.get("status") == "COMPLETE" and update.get("autoWaveStatus") == "SCHEDULED"
            for update in job_updates
        ))

    @patch("invite_sender.reserve_invite", new=lambda invites, members, item, expected_status=None: invites.put_item(Item=item))
    def test_execute_send_chunks_large_locked_audience_and_continues_same_job(self):
        phones = [f"+1502555{i:04d}" for i in range(6)]
        members_list = [
            {"phone": phone, "name": f"Guest {index}", "gender": "F", "tierOverride": 1, "_tier": 1}
            for index, phone in enumerate(phones)
        ]
        invites = MagicMock()
        members = MagicMock()
        lambda_client = MagicMock()
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "invite_template": "{name}. RSVP Society."}
        body = {"eventId": "rooftop-sept2026", "capacity": 100, "waveNumber": 1, "phones": phones, "lockedWave": True}
        with patch.dict(os.environ, {"SMS_ENABLED": "false", "INVITE_MAX_PER_INVOCATION": "250", "AWS_LAMBDA_FUNCTION_NAME": "rsvp-invite-handler"}), \
             patch.object(invite_handler, "_resolve_active_invitable_event", return_value=event), \
             patch.object(invite_handler, "_assert_formal_wave_available"), \
             patch.object(invite_handler, "_get_existing_invited_phones", return_value=set()), \
             patch.object(invite_handler, "_get_approved_members", return_value=members_list), \
             patch.object(invite_handler, "_invites_table", return_value=invites), \
             patch.object(invite_handler, "members_table", return_value=members), \
             patch.object(invite_handler, "_update_job") as update_job, \
             patch.object(invite_handler, "log_action") as log_action, \
             patch.object(invite_sender.boto3, "client", return_value=lambda_client):
            invite_handler._execute_send(body, "", "token", "job-chunk")

        self.assertEqual(invites.put_item.call_count, 4)
        log_action.assert_not_called()
        lambda_client.invoke.assert_called_once()
        payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"].decode())
        self.assertEqual(payload["jobId"], "job-chunk")
        self.assertTrue(payload["continuation"])
        self.assertEqual(payload["blastBody"]["phones"], phones[4:])
        self.assertEqual(payload["blastBody"]["_continuationProgress"]["queued"], 4)
        self.assertEqual(payload["blastBody"]["waveSize"], 125)
        statuses = [call.args[1].get("status") for call in update_job.call_args_list if len(call.args) > 1]
        self.assertIn("PROCESSING", statuses)
        self.assertNotIn("COMPLETE", statuses)

    @patch("invite_sender.reserve_invite", new=lambda invites, members, item, expected_status=None: invites.put_item(Item=item))
    def test_send_continuation_batch_reads_only_locked_recipients(self):
        phone = "+15025551212"
        invites = MagicMock()
        members = MagicMock()
        members.name = "rsvp-members-test"
        members.meta.client.batch_get_item.return_value = {"Responses": {"rsvp-members-test": [{
            "phone": phone, "name": "Jordan", "lastName": "Smith", "gender": "F",
            "status": "APPROVED", "smsOptIn": True,
        }]}}
        events = MagicMock()
        ddb = MagicMock(); ddb.Table.return_value = events
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "invite_template": "{name}. RSVP Society."}
        body = {
            "eventId": "rooftop-sept2026", "capacity": 100, "waveNumber": 2,
            "waveSize": 100, "phones": [phone], "lockedWave": True,
            "_serverContinuation": True, "_continuationProgress": {"queued": 1},
        }
        with patch.dict(os.environ, {"SMS_ENABLED": "false"}), \
             patch.object(invite_handler, "_resolve_active_invitable_event", return_value=event), \
             patch.object(invite_handler, "_assert_formal_wave_available"), \
             patch.object(invite_handler, "_get_existing_invited_phones", side_effect=AssertionError("full invite query repeated")), \
             patch.object(invite_handler, "_get_approved_members", side_effect=AssertionError("full approved pool repeated")), \
             patch.object(invite_handler, "_get_analytics", side_effect=AssertionError("wave analytics repeated")), \
             patch.object(invite_handler, "_invites_table", return_value=invites), \
             patch.object(invite_handler, "members_table", return_value=members), \
             patch.object(invite_handler.boto3, "resource", return_value=ddb), \
             patch.object(invite_handler, "_update_job"), patch.object(invite_handler, "log_action"):
            invite_handler._execute_send(body, "", "token", "job-continuation")
        members.meta.client.batch_get_item.assert_called_once()
        invites.put_item.assert_called_once()

    def test_confirmed_update_uses_bounded_same_job_continuations(self):
        phones = [f"+1502555{i:04d}" for i in range(251)]
        lambda_client = MagicMock()
        body = {
            "eventId": "rooftop-sept2026",
            "confirmedUpdate": True,
            "messageOverride": "A quick event update, {name}.",
            "confirmedUpdatePhones": phones,
            "confirmedUpdateRecipientCount": len(phones),
        }
        with patch.dict(os.environ, {
            "SMS_ENABLED": "false",
            "CONFIRMED_UPDATE_MAX_PER_INVOCATION": "250",
            "AWS_LAMBDA_FUNCTION_NAME": "rsvp-invite-handler",
        }), patch.object(invite_handler, "_confirmed_update_recipients", side_effect=lambda event_id, batch: [
            {"phone": phone, "name": "Guest"} for phone in batch
        ]) as get_recipients, patch.object(invite_handler, "_update_job") as update_job, \
             patch.object(invite_handler, "_invite_jobs_table", return_value=MagicMock()), \
             patch.object(invite_handler, "log_action") as log_action, \
             patch.object(invite_handler.boto3, "client", return_value=lambda_client):
            invite_handler._execute_confirmed_update(body, {}, "token", "job-update")

        self.assertEqual(get_recipients.call_args.args[1], phones[:15])
        log_action.assert_not_called()
        payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"].decode())
        self.assertTrue(payload["continuation"])
        self.assertEqual(payload["blastBody"]["phones"], phones[15:])
        self.assertNotIn("confirmedUpdatePhones", payload["blastBody"])
        self.assertEqual(payload["blastBody"]["_continuationProgress"]["recipientCount"], 15)
        self.assertIn("PROCESSING", [call.args[1]["status"] for call in update_job.call_args_list])

    def test_confirmed_update_skips_recipient_with_sent_marker(self):
        from botocore.exceptions import ClientError
        phone = "+15025551212"
        jobs = MagicMock()
        jobs.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        jobs.get_item.return_value = {"Item": {"status": "SENT"}}
        with patch.dict(os.environ, {"SMS_ENABLED": "true", "INVITE_MAX_PER_INVOCATION": "250"}), \
             patch.object(invite_handler, "_confirmed_update_recipients", return_value=[
                 {"phone": phone, "name": "Guest"}
             ]), patch.object(invite_handler, "_invite_jobs_table", return_value=jobs), \
             patch.object(invite_handler, "_update_job"), patch.object(invite_handler, "send_sms") as send_sms, \
             patch.object(invite_handler, "log_action"):
            invite_handler._execute_confirmed_update({
                "eventId": "event-1", "confirmedUpdatePhones": [phone],
                "messageOverride": "Update {name}",
            }, {}, "token", "job-update")
        send_sms.assert_not_called()

    @patch("invite_sender.reserve_invite", new=lambda invites, members, item, expected_status=None: invites.put_item(Item=item))
    def test_execute_send_skips_member_with_existing_real_invite(self):
        phone = "+15025551212"
        member = {"phone": phone, "name": "Jordan", "gender": "F", "tierOverride": 1, "_tier": 1}
        invites = MagicMock()
        from botocore.exceptions import ClientError
        invites.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        invites.get_item.return_value = {"Item": {"status": "CONFIRMED"}}
        members = MagicMock()
        events = MagicMock()
        ddb = MagicMock(); ddb.Table.return_value = events
        body = {"eventId": "rooftop-sept2026", "capacity": 100, "waveNumber": 1, "phones": [phone]}
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "invite_template": "{name}. RSVP Society."}
        with patch.dict(os.environ, {"SMS_ENABLED": "false"}), \
             patch.object(invite_handler, "_resolve_active_invitable_event", return_value=event), \
             patch.object(invite_handler, "_assert_formal_wave_available"), \
             patch.object(invite_handler, "_get_existing_invited_phones", return_value=set()), \
             patch.object(invite_handler, "_get_approved_members", return_value=[member]), \
             patch.object(invite_handler, "_invites_table", return_value=invites), \
             patch.object(invite_handler, "members_table", return_value=members), \
             patch.object(invite_handler.boto3, "resource", return_value=ddb), \
             patch.object(invite_handler, "_update_job"), \
             patch.object(invite_handler, "log_action"):
            invite_handler._execute_send(body, "", "token", "job-2")
        members.update_item.assert_not_called()
        invites.delete_item.assert_not_called()


class TestAttendanceFlowContract(unittest.TestCase):
    def test_record_attendance_uses_transaction_and_counts_after_success(self):
        checkins = MagicMock(); checkins.name = "rsvp-checkins-test"; checkins.get_item.return_value = {}
        invites = MagicMock(); invites.name = "rsvp-event-invites-test"
        members = MagicMock(); members.name = "rsvp-members-test"
        client = MagicMock()
        with patch.dict(os.environ, {"RSVP_TEST_DISABLE_DDB_TRANSACTIONS": "false"}), \
             patch.object(attendance_store, "_checkins_table", return_value=checkins), \
             patch.object(attendance_store, "_invites_table", return_value=invites), \
             patch.object(attendance_store, "_table", return_value=members), \
             patch.object(attendance_store.boto3, "client", return_value=client):
            result = attendance_store.record_attendance("5025551212", attended=True, event_id="rooftop-sept2026")
        self.assertTrue(result["ok"])
        tx = client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(tx), 3)
        self.assertIn("#s IN (:confirmed, :noshow_existing)", tx[0]["Update"]["ConditionExpression"])
        self.assertEqual(tx[1]["Put"]["TableName"], "rsvp-checkins-test")
        self.assertEqual(tx[2]["Update"]["TableName"], "rsvp-members-test")
        self.assertIn("attendedCount", tx[2]["Update"]["UpdateExpression"])

    def test_record_no_show_requires_confirmed_and_counts_after_success(self):
        checkins = MagicMock(); checkins.name = "rsvp-checkins-test"; checkins.get_item.return_value = {}
        invites = MagicMock(); invites.name = "rsvp-event-invites-test"
        members = MagicMock(); members.name = "rsvp-members-test"
        client = MagicMock()
        with patch.dict(os.environ, {"RSVP_TEST_DISABLE_DDB_TRANSACTIONS": "false"}), \
             patch.object(attendance_store, "_checkins_table", return_value=checkins), \
             patch.object(attendance_store, "_invites_table", return_value=invites), \
             patch.object(attendance_store, "_table", return_value=members), \
             patch.object(attendance_store.boto3, "client", return_value=client):
            result = attendance_store.record_attendance("5025551212", attended=False, event_id="rooftop-sept2026")
        self.assertTrue(result["ok"])
        tx = client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(tx), 2)
        self.assertIn("#s = :confirmed", tx[0]["Update"]["ConditionExpression"])
        self.assertEqual(tx[1]["Update"]["TableName"], "rsvp-members-test")
        self.assertIn("noShowCount", tx[1]["Update"]["UpdateExpression"])

    def test_finalization_marks_confirmed_member_and_plus_one_no_shows_on_success(self):
        invites = MagicMock()
        invites.query.return_value = {"Items": [{
            "eventId": "rooftop-sept2026",
            "phone": "+15025551212",
            "status": "CONFIRMED",
            "plusOneName": "Taylor Smith",
        }]}
        with patch.object(attendance_store, "_invites_table", return_value=invites), \
             patch.object(attendance_store, "record_attendance", return_value={"ok": True}) as record:
            result = attendance_store.finalize_event_attendance({"eventSlug": "rooftop-sept2026"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["memberNoShows"], 1)
        self.assertEqual(result["plusOneNoShows"], 1)
        record.assert_called_once_with("+15025551212", attended=False, event_id="rooftop-sept2026")
        invites.update_item.assert_called_once()


class TestMarketSemanticsContract(unittest.TestCase):
    def test_louisville_and_lexington_are_distinct_markets(self):
        members = [
            {"name": "Lou", "phone": "+16155550001", "market": "Louisville", "gender": "M", "tierOverride": 1},
            {"name": "Lex", "phone": "+15025550002", "market": "Lexington", "gender": "F", "tierOverride": 1},
        ]
        louisville = invite_handler._apply_audience_filters(members, {"market": "Louisville"})
        lexington = invite_handler._apply_audience_filters(members, {"market": "Lexington"})
        self.assertEqual([m["name"] for m in louisville], ["Lou"])
        self.assertEqual([m["name"] for m in lexington], ["Lex"])

    def test_all_markets_keeps_unknown_market_members(self):
        members = [{"name": "Unknown", "phone": "+16155550001", "gender": "M", "tierOverride": 1}]
        result = invite_handler._apply_audience_filters(members, {"market": "All"})
        self.assertEqual([m["name"] for m in result], ["Unknown"])


class TestSmsHandlerAdditionalFlowContract(unittest.TestCase):
    def setUp(self):
        event_patch = patch.object(sms_handler, "_get_current_event", return_value={"eventSlug": "audit", "date": "2099-10-17", "startTime": "19:00", "event_timezone": "UTC", "event_status": "LIVE", "allowPlusOnes": True})
        event_patch.start()
        self.addCleanup(event_patch.stop)
        confirmation_patch = patch.object(sms_handler, "_build_confirmation_message", return_value="See you October 17.")
        confirmation_patch.start()
        self.addCleanup(confirmation_patch.stop)

    def _approved_member(self):
        return {
            "phone": "+15025551212",
            "name": "Jordan",
            "lastName": "Smith",
            "status": "APPROVED",
            "smsOptIn": True,
        }

    def test_deleted_member_plus_one_does_not_block_guest_forever(self):
        table = MagicMock()
        table.query.return_value = {"Items": [{
            "eventId": "rooftop-sept2026", "phone": "+15025550000",
            "status": "DELETED", "plusOneName": "Taylor Smith",
        }]}
        deps = {"invites_table": lambda: table, "logger": MagicMock()}
        result = sms_plus_one.lookup_existing_plus_one_assignment(
            "Taylor Smith", "rooftop-sept2026", "+15025551212", deps=deps
        )
        self.assertEqual(result, {"exists": False})

        table.query.return_value = {"Items": [{
            "eventId": "rooftop-sept2026", "phone": "+15025550000",
            "status": "CONFIRMED", "plusOneName": "Taylor Smith",
        }]}
        result = sms_plus_one.lookup_existing_plus_one_assignment(
            "Taylor Smith", "rooftop-sept2026", "+15025551212", deps=deps
        )
        self.assertTrue(result["exists"])
        self.assertEqual(result["phone"], "+15025550000")

    def test_decline_routes_through_handler_to_declined_transition(self):
        member = self._approved_member()
        invite = {"eventId": "rooftop-sept2026", "phone": member["phone"], "status": "INVITED"}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_handler, "_get_pending_invite", return_value=invite), \
             patch.object(sms_handler, "_update_invite_status") as update_status:
            response = sms_handler.handler(_legacy_sms_event("Can't make it"), None)
        self.assertEqual(response["statusCode"], 200)
        update_status.assert_called_once_with("rooftop-sept2026", "+15025551212", "DECLINED")

    def test_capacity_guard_does_not_confirm_when_target_is_full(self):
        member = self._approved_member()
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "capacity": 50, "allowPlusOnes": False}
        invite = {"eventId": "rooftop-sept2026", "phone": member["phone"], "status": "INVITED"}
        with patch.object(sms_handler, "_invites_table", return_value=MagicMock()), patch.object(sms_handler, "_queue_host_request"), patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_handler, "_get_current_event", return_value=event), \
             patch.object(sms_handler, "_get_reconfirmable_invite", return_value=invite), \
             patch.object(sms_handler, "_confirm_invite_with_capacity", return_value="FULL") as confirm_atomic, \
             patch.object(sms_handler, "_update_invite_status") as update_status:
            response = sms_handler.handler(_legacy_sms_event("YES"), None)
        self.assertEqual(response["statusCode"], 200)
        confirm_atomic.assert_called_once_with("rooftop-sept2026", "+15025551212", 84)
        update_status.assert_not_called()


    def test_atomic_confirmation_reserves_event_headcount_with_invite_transition(self):
        client = MagicMock()
        members = MagicMock()
        invites = MagicMock()
        invites.get_item.return_value = {}
        with patch.object(sms_handler, "_ensure_event_headcount_counter", return_value=83), \
             patch.object(sms_handler, "_invites_table", return_value=invites), \
             patch.object(sms_handler.boto3, "client", return_value=client), \
             patch.object(sms_handler, "_members_table", return_value=members):
            result = sms_handler._confirm_invite_with_capacity(
                "rooftop-sept2026", "+15025551212", 84
            )
        self.assertEqual(result, "CONFIRMED")
        tx = client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(tx), 3)
        self.assertIn("#s IN (:invited, :declined)", tx[0]["Update"]["ConditionExpression"])
        self.assertIn("confirmedHeadcount < :target", tx[1]["Update"]["ConditionExpression"])
        self.assertIn("event_status = :live", tx[1]["Update"]["ConditionExpression"])
        consent = tx[2]["ConditionCheck"]
        self.assertIn("attribute_not_exists(optOut) OR optOut = :false", consent["ConditionExpression"])
        self.assertIn("attribute_type(smsOptIn, :null_type)", consent["ConditionExpression"])
        self.assertIn("smsOptIn = :true", consent["ConditionExpression"])
        self.assertEqual(consent["TableName"], os.getenv("MEMBERS_TABLE_NAME", "rsvp-members"))
        members.update_item.assert_called_once()

    def test_headcount_counter_initialization_never_overwrites_existing_counter(self):
        events = MagicMock()
        events.get_item.return_value = {"Item": {"confirmedHeadcount": 84}}
        with patch.object(sms_handler, "_get_confirmed_count") as count_invites, \
             patch.object(sms_handler, "_events_table", return_value=events):
            value = sms_handler._ensure_event_headcount_counter("rooftop-sept2026")
        self.assertEqual(value, 84)
        events.get_item.assert_called_once_with(
            Key={"eventId": "rooftop-sept2026"},
            ProjectionExpression="confirmedHeadcount",
            ConsistentRead=True,
        )
        count_invites.assert_not_called()
        events.update_item.assert_not_called()

    def test_headcount_counter_falls_back_to_invite_query_only_when_missing(self):
        events = MagicMock()
        events.get_item.return_value = {"Item": {"eventId": "rooftop-sept2026"}}
        events.update_item.return_value = {"Attributes": {"confirmedHeadcount": 83}}
        with patch.object(sms_handler, "_get_confirmed_count", return_value=83) as count_invites, \
             patch.object(sms_handler, "_events_table", return_value=events):
            value = sms_handler._ensure_event_headcount_counter("rooftop-sept2026")
        self.assertEqual(value, 83)
        count_invites.assert_called_once_with("rooftop-sept2026")
        kwargs = events.update_item.call_args.kwargs
        self.assertIn("if_not_exists", kwargs["UpdateExpression"])
        self.assertIn("attribute_exists(eventId)", kwargs["ConditionExpression"])


    def test_first_plus_one_reservation_is_atomic_with_headcount_increment(self):
        invites = MagicMock()
        invites.get_item.return_value = {"Item": {"status": "CONFIRMED"}}
        events = MagicMock()
        events.get_item.return_value = {"Item": {
            "capacity": 50, "confirmedHeadcount": 83, "event_status": "LIVE",
            "plusOneReservations": {},
        }}
        client = MagicMock()
        with patch.object(sms_handler, "_invites_table", return_value=invites), \
             patch.object(sms_handler, "_events_table", return_value=events), \
             patch.object(sms_handler, "_ensure_plus_one_reservation_map"), \
             patch.object(sms_handler, "_ensure_event_headcount_counter", return_value=83), \
             patch.object(sms_handler.boto3, "client", return_value=client):
            result = sms_handler._set_plus_one(
                "rooftop-sept2026", "+15025551212", "Taylor Smith", False
            )
        self.assertEqual(result, "SAVED")
        tx = client.transact_write_items.call_args.kwargs["TransactItems"]
        event_update = tx[0]["Update"]
        invite_update = tx[1]["Update"]
        self.assertIn("ADD confirmedHeadcount :one", event_update["UpdateExpression"])
        self.assertIn("attribute_not_exists(#r.#new)", event_update["ConditionExpression"])
        self.assertIn("confirmedHeadcount < :target", event_update["ConditionExpression"])
        self.assertIn("plusOneName = :n", invite_update["UpdateExpression"])
        self.assertIn("attribute_not_exists(plusOneName)", invite_update["ConditionExpression"])

    def test_plus_one_change_releases_old_reservation_without_adding_headcount(self):
        invites = MagicMock()
        invites.get_item.return_value = {"Item": {"status": "CONFIRMED", "plusOneName": "Old Guest"}}
        events = MagicMock()
        events.get_item.return_value = {"Item": {
            "capacity": 50, "confirmedHeadcount": 40, "event_status": "LIVE",
            "plusOneReservations": {},
        }}
        client = MagicMock()
        with patch.object(sms_handler, "_invites_table", return_value=invites), \
             patch.object(sms_handler, "_events_table", return_value=events), \
             patch.object(sms_handler, "_ensure_plus_one_reservation_map"), \
             patch.object(sms_handler, "_ensure_event_headcount_counter", return_value=40), \
             patch.object(sms_handler.boto3, "client", return_value=client):
            result = sms_handler._set_plus_one(
                "rooftop-sept2026", "+15025551212", "New Guest", False
            )
        self.assertEqual(result, "SAVED")
        event_update = client.transact_write_items.call_args.kwargs["TransactItems"][0]["Update"]
        self.assertIn("REMOVE #r.#old", event_update["UpdateExpression"])
        self.assertNotIn("ADD confirmedHeadcount", event_update["UpdateExpression"])

    def test_pending_approval_lookup_enforces_expiry_before_dynamodb_ttl_cleanup(self):
        table = MagicMock()
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        table.query.return_value = {"Items": [
            {"memberPhone": "+15025550001", "approvalCode": "1111", "storedAt": "2026-09-01T00:00:00+00:00", "expiresAt": now_epoch - 10},
            {"memberPhone": "+15025550002", "approvalCode": "2222", "storedAt": "2026-09-02T00:00:00+00:00", "expiresAt": now_epoch + 600},
        ]}
        with patch.object(sms_handler, "_pending_approvals_table", return_value=table):
            self.assertIsNone(sms_handler._get_pending_approval("+15025550000", approval_code="1111"))
            active = sms_handler._get_pending_approval("+15025550000")
        self.assertEqual(active["approvalCode"], "2222")

    def test_plus_one_update_intent_reopens_name_capture(self):
        member = self._approved_member()
        invite = {"eventId": "rooftop-sept2026", "phone": member["phone"], "status": "CONFIRMED"}
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=invite), \
             patch.object(sms_handler, "_set_awaiting_plus_one") as set_awaiting, \
             patch.object(sms_handler, "_claude") as claude:
            response = sms_handler.handler(_legacy_sms_event("change my plus one"), None)
        self.assertEqual(response["statusCode"], 200)
        set_awaiting.assert_called_once_with("rooftop-sept2026", "+15025551212")
        claude.assert_not_called()

    def test_inline_natural_plus_one_change_saves_name_without_jade_or_extra_prompt(self):
        member = self._approved_member()
        invite = {"eventId": "rooftop-sept2026", "phone": member["phone"], "status": "CONFIRMED", "plusOneName": "Raven Gillespie"}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=invite), \
             patch.object(sms_handler, "_validate_plus_one_candidate", return_value=(True, "", False)) as validate, \
             patch.object(sms_handler, "_set_plus_one", return_value="SAVED") as save, \
             patch.object(sms_handler, "_set_awaiting_plus_one") as set_awaiting, \
             patch.object(sms_handler, "send_sms") as send_sms, \
             patch.object(sms_handler, "_claude") as claude:
            response = sms_handler.handler(
                _legacy_sms_event("I'm changing my plus 1 to Ericka Jackson"), None
            )
        self.assertEqual(response["statusCode"], 200)
        validate.assert_called_once_with("Ericka Jackson", "rooftop-sept2026", "+15025551212")
        save.assert_called_once_with("rooftop-sept2026", "+15025551212", "Ericka Jackson", is_member=False)
        set_awaiting.assert_not_called()
        send_sms.assert_called_once_with("+15025551212", "Ericka Jackson. Got it.")
        claude.assert_not_called()

    def test_unconfirmed_member_cannot_receive_private_parking_details(self):
        member = self._approved_member()
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_handler, "_get_current_invite_status", return_value="INVITED"), \
             patch.object(sms_handler, "send_sms") as send_sms, \
             patch.object(sms_handler, "_claude") as claude:
            response = sms_handler.handler(_legacy_sms_event("Where do I park?"), None)
        self.assertEqual(response["statusCode"], 200)
        send_sms.assert_called_once_with("+15025551212", "Once you're confirmed, I'll send what you need.")
        claude.assert_not_called()

    def test_unknown_number_is_routed_to_public_site_only(self):
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=None), \
             patch.object(sms_handler, "_claim_access_reply", return_value=True), \
             patch.object(sms_handler, "send_sms") as send_sms, \
             patch.object(sms_handler, "_claude") as claude:
            response = sms_handler.handler(_legacy_sms_event("hello"), None)
        self.assertEqual(response["statusCode"], 200)
        send_sms.assert_called_once_with("+15025551212", "rsvpsociety.com")
        claude.assert_not_called()


    def test_first_delivery_webhook_stamps_invite_and_event_counter_once(self):
        invite = {"eventId": "rooftop-sept2026", "phone": "+15025551212", "waveNumber": 1}
        invite_table = MagicMock()
        event_table = MagicMock(); event_table.get_item.return_value = {"Item": {"lastBlastWave": 1}}
        event = {
            "headers": {"openphone-signature": "test"},
            "body": json.dumps({"type": "message.delivered", "data": {"object": {"id": "msg-1", "to": "5025551212", "from": "5025550000"}}}),
        }
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "_find_invite_by_message_id", return_value=invite), \
             patch.object(sms_handler, "_invites_table", return_value=invite_table), \
             patch.object(sms_handler, "_events_table", return_value=event_table):
            response = sms_handler.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(invite_table.update_item.call_count, 1)
        event_table.update_item.assert_called_once()
        self.assertIn("deliveredCount", event_table.update_item.call_args.kwargs["UpdateExpression"])

    def test_current_quo_delivery_resource_envelope_updates_invite(self):
        invite = {"eventId": "rooftop-sept2026", "phone": "+15025551212", "waveNumber": 1}
        invite_table = MagicMock()
        event_table = MagicMock(); event_table.get_item.return_value = {"Item": {"lastBlastWave": 1}}
        event = {
            "headers": {"webhook-id": "msg_123", "webhook-timestamp": "1", "webhook-signature": "v1,test"},
            "body": json.dumps({
                "type": "message.delivered",
                "data": {"resource": {"id": "msg-current-1", "to": "+15025551212", "from": "+15025550000"}},
            }),
        }
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "_find_invite_by_message_id", return_value=invite) as find_invite, \
             patch.object(sms_handler, "_invites_table", return_value=invite_table), \
             patch.object(sms_handler, "_events_table", return_value=event_table):
            response = sms_handler.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        find_invite.assert_called_once_with("msg-current-1", "+15025551212")
        invite_table.update_item.assert_called_once()
        event_table.update_item.assert_called_once()

    def test_duplicate_delivery_webhook_does_not_increment_event_counter_twice(self):
        from botocore.exceptions import ClientError
        invite = {"eventId": "rooftop-sept2026", "phone": "+15025551212", "waveNumber": 1}
        invite_table = MagicMock()
        invite_table.update_item.side_effect = [
            ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "duplicate"}}, "UpdateItem"),
            None,
        ]
        event_table = MagicMock()
        event = {
            "headers": {"openphone-signature": "test"},
            "body": json.dumps({"type": "message.delivered", "data": {"object": {"id": "msg-1", "to": "5025551212", "from": "5025550000"}}}),
        }
        with patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "_find_invite_by_message_id", return_value=invite), \
             patch.object(sms_handler, "_invites_table", return_value=invite_table), \
             patch.object(sms_handler, "_events_table", return_value=event_table):
            response = sms_handler.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(invite_table.update_item.call_count, 2)
        event_table.update_item.assert_not_called()

    def test_running_late_is_deterministic_and_does_not_call_jade(self):
        member = self._approved_member()
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_intent, "_is_bare_name_for_catch", return_value=False), \
             patch.object(sms_handler, "send_sms") as send_sms, \
             patch.object(sms_handler, "_claude") as claude:
            response = sms_handler.handler(_legacy_sms_event("running late"), None)
        self.assertEqual(response["statusCode"], 200)
        send_sms.assert_called_once_with("+15025551212", "See you there.")
        claude.assert_not_called()

    def test_confirmed_member_ticket_question_can_receive_ticket_url(self):
        member = self._approved_member()
        invite_table = MagicMock(); invite_table.get_item.return_value = {"Item": {"status": "CONFIRMED"}}
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "ticketUrl": "https://tickets.example.test/x"}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value={"eventId": "rooftop-sept2026", "status": "CONFIRMED"}), \
             patch.object(sms_handler, "_get_pending_invite", return_value=None), \
             patch.object(sms_handler, "_get_current_event", return_value=event), \
             patch.object(sms_handler, "_invites_table", return_value=invite_table), \
             patch.object(sms_handler, "send_sms") as send_sms:
            response = sms_handler.handler(_legacy_sms_event("Do I need a ticket?"), None)
        self.assertEqual(response["statusCode"], 200)
        send_sms.assert_called_once_with("+15025551212", "Grab your ticket: https://tickets.example.test/x")

    def test_confirmed_member_receives_explicit_parking_detail_without_jade(self):
        member = self._approved_member()
        event = {"eventSlug": "rooftop-sept2026", "parkingInfo": "Use the garage on Main St"}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_handler, "_get_current_invite_status", return_value="CONFIRMED"), \
             patch.object(sms_handler, "_get_current_event", return_value=event), \
             patch.object(sms_handler, "send_sms") as send_sms, \
             patch.object(sms_handler, "_claude") as claude:
            response = sms_handler.handler(_legacy_sms_event("Where is parking?"), None)
        self.assertEqual(response["statusCode"], 200)
        send_sms.assert_called_once_with("+15025551212", "Use the garage on Main St.")
        claude.assert_not_called()

    def test_ambiguous_reply_uses_jade_ambiguous_mode(self):
        member = self._approved_member()
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(sms_handler, "_verify_webhook_signature", return_value=True), \
             patch.object(sms_handler, "get_host_phones", return_value=[]), \
             patch.object(sms_handler, "get_member", return_value=member), \
             patch.object(sms_handler, "_get_confirmed_invite", return_value=None), \
             patch.object(sms_intent, "_is_bare_name_for_catch", return_value=False), \
             patch.object(sms_handler, "_get_current_invite_status", return_value="INVITED"), \
             patch.object(sms_handler, "_get_current_event", return_value={}), \
             patch.object(sms_handler, "_claude", return_value="Want me to hold it?") as claude, \
             patch.object(sms_handler, "send_sms") as send_sms:
            response = sms_handler.handler(_legacy_sms_event("maybe"), None)
        self.assertEqual(response["statusCode"], 200)
        claude.assert_called_once_with("maybe", mode="ambiguous", member=member)
        send_sms.assert_called_once_with("+15025551212", "Want me to hold it?")


class TestInvitePreviewAndQueueContract(unittest.TestCase):
    def test_preview_persists_exact_send_eligible_phone_lock(self):
        member = {
            "phone": "+15025551212", "name": "Jordan", "lastName": "Smith",
            "market": "Louisville", "gender": "F", "tierOverride": 1, "_tier": 1,
        }
        result = {
            "members": [member],
            "summary": {"totalInvites": 1, "tier1": 1, "tier2": 0, "tier3": 0},
        }
        with patch.object(invite_handler, "_get_next_wave_number", return_value=1), \
             patch.object(invite_handler, "_assert_formal_wave_available"), \
             patch.object(invite_handler, "_event_for_guard", return_value={
                 "eventSlug": "rooftop-sept2026", "eventZipCode": "40205",
                 "latitude": Decimal("38.2231"), "longitude": Decimal("-85.6836"),
                 "promotionRadiusMiles": Decimal("60"),
             }), \
             patch.object(invite_handler, "_get_existing_invited_phones", return_value=set()), \
             patch.object(invite_handler, "_get_existing_invite_map", return_value={}), \
             patch.object(invite_handler, "_get_analytics", return_value={}), \
             patch.object(invite_handler, "_resolve_wave_capacity", return_value=1), \
             patch.object(invite_handler, "_get_approved_members", return_value=[member]), \
             patch.object(invite_handler, "_apply_audience_filters", return_value=[member]), \
             patch.object(invite_handler, "_build_invite_list", return_value=result), \
             patch.object(invite_handler, "_write_preview_lock", return_value="lock-123") as write_lock:
            response = invite_handler.handle_preview({
                "eventId": "rooftop-sept2026",
                "capacity": 100,
                "audienceFilters": {"market": "Louisville"},
            }, "")
        self.assertEqual(response["statusCode"], 200)
        payload = _body(response)
        self.assertEqual(payload["previewSessionId"], "lock-123")
        self.assertEqual(payload["members"][0]["phone"], "+15025551212")
        self.assertEqual(write_lock.call_args.kwargs["locked_phones"], ["+15025551212"])

    def test_locked_send_body_accepts_only_subset_of_locked_phones(self):
        lock = {
            "eventId": "rooftop-sept2026",
            "waveNumber": 2,
            "waveSize": 50,
            "lockedPhonesList": ["+15025550001", "+15025550002"],
        }
        body = {
            "eventId": "rooftop-sept2026",
            "previewSessionId": "lock-123",
            "phones": ["+15025550002"],
        }
        with patch.object(invite_handler, "_read_preview_lock", return_value=lock), \
             patch.object(invite_handler, "_claim_preview_lock", return_value=lock):
            resolved = invite_handler._resolve_locked_send_body(body, "job-1")
        self.assertEqual(resolved["phones"], ["+15025550002"])
        self.assertEqual(resolved["waveNumber"], 2)
        self.assertTrue(resolved["lockedWave"])
        self.assertFalse(resolved["autoWave"])

    def test_locked_send_body_rejects_phone_outside_preview(self):
        lock = {
            "eventId": "rooftop-sept2026",
            "waveNumber": 1,
            "waveSize": 50,
            "lockedPhonesList": ["+15025550001"],
        }
        body = {
            "eventId": "rooftop-sept2026",
            "previewSessionId": "lock-123",
            "phones": ["+15025550002"],
        }
        with patch.object(invite_handler, "_read_preview_lock", return_value=lock), \
             patch.object(invite_handler, "_claim_preview_lock", return_value=lock):
            with self.assertRaises(ValueError):
                invite_handler._resolve_locked_send_body(body, "job-1")

    def test_handle_send_queues_locked_body_and_async_job(self):
        captured = {}
        def fake_resolve(body, job_id):
            next_body = dict(body)
            next_body.update({"phones": ["+15025550001"], "waveNumber": 1, "lockedWave": True, "autoWave": False})
            return next_body
        lambda_client = MagicMock()
        with patch.object(invite_handler, "_resolve_active_invitable_event", return_value={"eventSlug": "rooftop-sept2026"}), \
             patch.object(invite_handler, "_validate_initial_invite_text"), \
             patch.object(invite_handler, "_get_next_wave_number", return_value=1), \
             patch.object(invite_handler, "_resolve_locked_send_body", side_effect=fake_resolve), \
             patch.object(invite_handler, "_write_job", side_effect=lambda job_id, body, origin: captured.update({"job_id": job_id, "body": dict(body)})), \
             patch("boto3.client", return_value=lambda_client):
            response = invite_handler.handle_send({
                "confirmSend": True,
                "eventId": "rooftop-sept2026",
                "previewSessionId": "lock-123",
                "phones": ["+15025550001"],
            }, "", "token")
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(captured["body"]["phones"], ["+15025550001"])
        self.assertTrue(captured["body"]["lockedWave"])
        lambda_client.invoke.assert_called_once()

    @patch("invite_sender.reserve_invite", new=lambda invites, members, item, expected_status=None: invites.put_item(Item=item))
    def test_execute_send_marks_nonretryable_sms_failure_as_failed(self):
        phone = "+15025551212"
        member = {"phone": phone, "name": "Jordan", "status": "APPROVED", "smsOptIn": True, "gender": "F", "tierOverride": 1, "_tier": 1}
        invites = MagicMock()
        members = MagicMock()
        ddb = MagicMock(); ddb.Table.return_value = MagicMock()
        body = {"eventId": "rooftop-sept2026", "capacity": 100, "waveNumber": 1, "phones": [phone]}
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "invite_template": "{name}. RSVP Society."}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(invite_handler, "_resolve_active_invitable_event", return_value=event), \
             patch.object(invite_handler, "_assert_formal_wave_available"), \
             patch.object(invite_handler, "_get_existing_invited_phones", return_value=set()), \
             patch.object(invite_handler, "_get_approved_members", return_value=[member]), \
             patch.object(invite_handler, "_invites_table", return_value=invites), \
             patch.object(invite_handler, "members_table", return_value=members), \
             patch.object(invite_handler, "send_sms", side_effect=RuntimeError("provider failure")), \
             patch.object(invite_handler.time, "sleep"), \
             patch.object(invite_handler.boto3, "resource", return_value=ddb), \
             patch.object(invite_handler, "_update_job"), \
             patch.object(invite_handler, "log_action"):
            invite_handler._execute_send(body, "", "token", "job-fail")
        invites.update_item.assert_called_once()
        self.assertEqual(invites.update_item.call_args.kwargs["ExpressionAttributeValues"][":failed"], "FAILED")
        members.update_item.assert_not_called()


class TestMemberLocationStorageContract(unittest.TestCase):
    def test_member_coordinates_are_dynamodb_safe_decimals(self):
        table = MagicMock()
        table.get_item.side_effect = [{"Item": {}}, {"Item": {"phone": "+15025551212"}}]
        with patch.object(member_store, "_table", return_value=table):
            member_store.upsert_member(
                phone="5025551212", name="Jordan", sms_opt_in=True,
                zip_code="40205", city="Louisville", state="KY",
                latitude=38.2231, longitude=-85.6836,
            )
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        self.assertIsInstance(values[":lat"], Decimal)
        self.assertIsInstance(values[":lon"], Decimal)
        self.assertEqual(values[":lat"], Decimal("38.2231"))
        self.assertEqual(values[":lon"], Decimal("-85.6836"))


class TestMemberStoreAdditionalContract(unittest.TestCase):
    def test_approved_signup_does_not_add_pending_ttl(self):
        table = MagicMock()
        table.get_item.side_effect = [
            {"Item": {"phone": "+15025551212", "name": "Jordan", "status": "APPROVED"}},
            {"Item": {"phone": "+15025551212", "name": "Jordan", "status": "APPROVED"}},
        ]
        with patch.object(member_store, "_table", return_value=table):
            member_store.upsert_member(phone="5025551212", name="Jordan", sms_opt_in=True)
        call = table.update_item.call_args.kwargs
        # Existing approved members must not receive an expiry assignment. The
        # expression removes stale expiry fields if present, which is expected.
        self.assertNotIn("pendingExpiresAtEpoch =", call["UpdateExpression"])
        self.assertIn("REMOVE pendingExpiresAt, pendingExpiresAtEpoch", call["UpdateExpression"])
        self.assertNotIn(":pex_epoch", call["ExpressionAttributeValues"])

    def test_set_status_approved_removes_pending_expiry(self):
        table = MagicMock()
        with patch.object(member_store, "_table", return_value=table):
            member_store.set_status("5025551212", "APPROVED")
        call = table.update_item.call_args.kwargs
        self.assertIn("REMOVE pendingExpiresAt, pendingExpiresAtEpoch", call["UpdateExpression"])
        self.assertEqual(call["ExpressionAttributeValues"][":s"], "APPROVED")

    def test_set_status_pending_sets_ttl_deadline(self):
        table = MagicMock()
        with patch.object(member_store, "_table", return_value=table):
            member_store.set_status("5025551212", "PENDING")
        call = table.update_item.call_args.kwargs
        self.assertIn("pendingExpiresAtEpoch = :pex_epoch", call["UpdateExpression"])
        self.assertGreater(call["ExpressionAttributeValues"][":pex_epoch"], 0)

    def test_pending_list_filters_expired_records(self):
        table = MagicMock()
        table.query.return_value = {
            "Items": [
                {"phone": "+15025550001", "name": "Old", "status": "PENDING", "pendingExpiresAt": "2000-01-01T00:00:00+00:00"},
                {"phone": "+15025550002", "name": "Current", "status": "PENDING", "pendingExpiresAt": "2999-01-01T00:00:00+00:00"},
            ]
        }
        with patch.object(member_store, "_table", return_value=table):
            members = member_store.list_members_by_status("PENDING")
        self.assertEqual([m["phone"] for m in members], ["+15025550002"])
        self.assertEqual(table.update_item.call_count, 2)
        self.assertEqual(table.update_item.call_args.kwargs["ExpressionAttributeValues"][":pending"], "PENDING")

    def test_welcome_claim_returns_false_when_active_claim_exists(self):
        from botocore.exceptions import ClientError
        table = MagicMock()
        err = ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "claimed"}}, "UpdateItem")
        table.update_item.side_effect = [err, err]
        with patch.object(member_store, "_table", return_value=table):
            self.assertFalse(member_store.claim_welcome_send("5025551212"))
        self.assertEqual(table.update_item.call_count, 2)

    def test_mark_welcome_sent_is_idempotent(self):
        from botocore.exceptions import ClientError
        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already sent"}},
            "UpdateItem",
        )
        with patch.object(member_store, "_table", return_value=table):
            self.assertFalse(member_store.mark_welcome_sent("5025551212"))


class TestReminderExecutionContract(unittest.TestCase):
    def test_custom_manual_reminder_route_returns_accepted_job(self):
        job = {"queued": True, "jobId": "manual-job", "recipientCount": 2500}
        request = {
            "httpMethod": "POST",
            "headers": {"x-admin-token": "test-admin-token-abc123"},
            "body": json.dumps({
                "timing": "day_of", "custom_message": "Hi {name}, event update.",
            }),
        }
        current_event = {
            "eventId": "event-one", "eventSlug": "event-one", "event_status": "LIVE",
        }
        with patch.object(reminder_handler, "get_secret_string", return_value="test-admin-token-abc123"), \
             patch.object(reminder_handler, "_get_current_event", return_value=current_event), \
             patch.object(reminder_handler, "_start_manual_reminder_job", return_value=job) as start_job, \
             patch.object(reminder_handler, "send_reminders") as inline_send:
            response = reminder_handler.handler(request, None)
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(body["jobId"], "manual-job")
        self.assertEqual(body["recipientCount"], 2500)
        start_job.assert_called_once()
        inline_send.assert_not_called()

    def test_manual_custom_reminder_creates_job_and_queues_without_synchronous_sends(self):
        invites = [
            {"eventId": "event-one", "phone": f"+1502555{i:04d}", "status": "CONFIRMED"}
            for i in range(251)
        ]
        table = MagicMock()
        lambda_client = MagicMock()
        lambda_client.invoke.return_value = {"StatusCode": 202}
        event = {"eventSlug": "event-one"}
        with patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_NAME": "rsvp-reminder-handler"}), \
             patch.object(reminder_handler, "_get_confirmed_invites", return_value=invites), \
             patch.object(reminder_handler, "_invite_jobs_table", return_value=table), \
             patch.object(reminder_handler.boto3, "client", return_value=lambda_client), \
             patch.object(reminder_handler, "send_sms") as send_sms, \
             patch.object(reminder_handler, "log_action"):
            result = reminder_handler._start_manual_reminder_job(event, True, "{name}, update", "admin-token")
        self.assertTrue(result["queued"])
        self.assertEqual(result["recipientCount"], 251)
        send_sms.assert_not_called()
        master = table.put_item.call_args.kwargs["Item"]
        self.assertEqual(master["kind"], "MANUAL_REMINDER_JOB")
        self.assertEqual(len(json.loads(master["phoneKeys"])), 251)
        payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["source"], "manual-reminder-continuation")
        self.assertEqual(payload["offset"], 0)

    def test_manual_reminder_job_processes_one_chunk_and_queues_the_next(self):
        phones = ["+15025551212", "+15025551213"]
        job = {
            "jobId": "manual-job", "kind": "MANUAL_REMINDER_JOB",
            "eventId": "event-one", "status": "QUEUED", "nextOffset": 0,
            "phoneKeys": json.dumps(phones), "customMessage": "Hi {name}",
        }
        jobs_table = MagicMock()
        jobs_table.get_item.return_value = {"Item": job}
        lambda_client = MagicMock()
        lambda_client.invoke.return_value = {"StatusCode": 202}
        with patch.dict(os.environ, {"SMS_ENABLED": "true", "AWS_LAMBDA_FUNCTION_NAME": "rsvp-reminder-handler"}), \
             patch.object(reminder_handler, "MAX_REMINDER_RECIPIENTS_PER_INVOCATION", 1), \
             patch.object(reminder_handler, "_invite_jobs_table", return_value=jobs_table), \
             patch.object(reminder_handler, "_batch_get_invites", return_value={phones[0]: {
                 "eventId": "event-one", "phone": phones[0], "name": "Jordan Smith", "status": "CONFIRMED",
             }}), \
             patch.object(reminder_handler, "_batch_get_members", return_value={phones[0]: {
                 "phone": phones[0], "smsOptIn": True, "optOut": False,
             }}), \
             patch.object(reminder_handler.boto3, "client", return_value=lambda_client), \
             patch.object(reminder_handler, "send_sms") as send_sms, \
             patch.object(reminder_handler.time, "sleep"), \
             patch.object(reminder_handler, "log_action"):
            result = reminder_handler._process_manual_reminder_job(
                {"jobId": "manual-job", "offset": 0}, {"eventSlug": "event-one"}
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["remainingRecipients"], 1)
        send_sms.assert_called_once_with(phones[0], "Hi Jordan")
        self.assertEqual(jobs_table.put_item.call_count, 1)
        self.assertEqual(jobs_table.update_item.call_count, 2)
        payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["jobId"], "manual-job")
        self.assertEqual(payload["offset"], 1)

    def test_manual_reminder_retry_skips_recipient_with_existing_job_marker(self):
        phone = "+15025551212"
        job = {
            "jobId": "manual-job", "kind": "MANUAL_REMINDER_JOB",
            "eventId": "event-one", "status": "QUEUED", "nextOffset": 0,
            "phoneKeys": json.dumps([phone]), "customMessage": "Hi {name}",
        }
        jobs_table = MagicMock()
        jobs_table.get_item.side_effect = lambda **kwargs: {
            "Item": job if kwargs["Key"]["jobId"] == "manual-job" else {"status": "SENT"}
        }
        duplicate = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        jobs_table.put_item.side_effect = duplicate
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(reminder_handler, "_invite_jobs_table", return_value=jobs_table), \
             patch.object(reminder_handler, "_batch_get_invites", return_value={phone: {
                 "eventId": "event-one", "phone": phone, "name": "Jordan", "status": "CONFIRMED",
             }}), \
             patch.object(reminder_handler, "_batch_get_members", return_value={phone: {
                 "phone": phone, "smsOptIn": True, "optOut": False,
             }}), \
             patch.object(reminder_handler, "send_sms") as send_sms, \
             patch.object(reminder_handler, "log_action"):
            result = reminder_handler._process_manual_reminder_job(
                {"jobId": "manual-job", "offset": 0}, {"eventSlug": "event-one"}
            )
        self.assertEqual(result["skippedAlreadySent"], 1)
        send_sms.assert_not_called()

    def test_scheduled_reminder_claims_sends_and_stamps_once(self):
        invite = {"eventId": "rooftop-sept2026", "phone": "+15025551212", "name": "Jordan Smith", "status": "CONFIRMED"}
        table = MagicMock()
        event = {"eventSlug": "rooftop-sept2026", "day_of_template": "{name}, tonight."}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(reminder_handler, "_get_confirmed_invites", return_value=[invite]), \
             patch.object(reminder_handler, "_batch_get_members", return_value={invite["phone"]: {"phone": invite["phone"], "smsOptIn": True, "optOut": False}}), \
             patch.object(reminder_handler, "_invites_table", return_value=table), \
             patch.object(reminder_handler, "send_sms") as send_sms, \
             patch.object(reminder_handler.time, "sleep"), \
             patch.object(reminder_handler, "log_action"):
            result = reminder_handler.send_reminders(event, True)
        self.assertEqual(result, {
            "sent": 1, "failed": 0, "skippedAlreadySent": 0, "skippedOptOut": 0,
            "continuationQueued": False, "remainingRecipients": 0,
        })
        send_sms.assert_called_once_with("+15025551212", "Jordan, tonight.")
        self.assertEqual(table.update_item.call_count, 2)
        self.assertIn("ClaimedAt", table.update_item.call_args_list[0].kwargs["UpdateExpression"])
        self.assertIn("ReminderSentAt", table.update_item.call_args_list[1].kwargs["UpdateExpression"])

    def test_large_scheduled_reminder_queues_only_unprocessed_phone_keys(self):
        invites = [
            {"eventId": "rooftop-sept2026", "phone": f"+1502555{i:04d}", "name": "Jordan", "status": "CONFIRMED"}
            for i in range(251)
        ]
        members = {item["phone"]: {"phone": item["phone"], "smsOptIn": True, "optOut": False} for item in invites[:250]}
        table = MagicMock()
        lambda_client = MagicMock()
        lambda_client.invoke.return_value = {"StatusCode": 202}
        context = {
            "source": "scheduler", "eventSlug": "rooftop-sept2026",
            "timing": "day_of", "expectedDate": "2026-09-29",
            "expectedSendTime": "11:00", "expectedTimezone": "America/New_York",
        }
        with patch.dict(os.environ, {"SMS_ENABLED": "false", "AWS_LAMBDA_FUNCTION_NAME": "rsvp-reminder-handler"}), \
             patch.object(reminder_handler, "_get_confirmed_invites", return_value=invites), \
             patch.object(reminder_handler, "_batch_get_members", return_value=members) as batch_members, \
             patch.object(reminder_handler, "_invites_table", return_value=table), \
             patch.object(reminder_handler.boto3, "client", return_value=lambda_client), \
             patch.object(reminder_handler.time, "sleep"), \
             patch.object(reminder_handler, "log_action"):
            result = reminder_handler.send_reminders(
                {"eventSlug": "rooftop-sept2026"}, True, continuation_context=context
            )
        self.assertEqual(result["sent"], 15)
        self.assertTrue(result["continuationQueued"])
        self.assertEqual(result["remainingRecipients"], 236)
        batch_members.assert_called_once_with([item["phone"] for item in invites[:15]])
        payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["continuationPhoneKeys"], [item["phone"] for item in invites[15:]])
        self.assertEqual(payload["expectedTimezone"], "America/New_York")

    def test_reminder_continuation_rereads_only_locked_invite_keys(self):
        phone = "+15025551212"
        invite = {
            "eventId": "rooftop-sept2026", "phone": phone,
            "name": "Jordan Smith", "status": "CONFIRMED",
        }
        table = MagicMock()
        with patch.dict(os.environ, {"SMS_ENABLED": "false"}), \
             patch.object(reminder_handler, "_get_confirmed_invites") as full_query, \
             patch.object(reminder_handler, "_batch_get_invites", return_value={phone: invite}) as batch_invites, \
             patch.object(reminder_handler, "_batch_get_members", return_value={phone: {"phone": phone, "smsOptIn": True, "optOut": False}}), \
             patch.object(reminder_handler, "_invites_table", return_value=table), \
             patch.object(reminder_handler.time, "sleep"), \
             patch.object(reminder_handler, "log_action"):
            result = reminder_handler.send_reminders(
                {"eventSlug": "rooftop-sept2026"}, True, phone_keys=[phone],
                continuation_context={"source": "scheduler", "eventSlug": "rooftop-sept2026"},
            )
        self.assertEqual(result["sent"], 1)
        batch_invites.assert_called_once_with("rooftop-sept2026", [phone])
        full_query.assert_not_called()

    def test_scheduled_reminder_skips_explicit_opt_out(self):
        invite = {"eventId": "rooftop-sept2026", "phone": "+15025551212", "name": "Jordan Smith", "status": "CONFIRMED"}
        with patch.object(reminder_handler, "_get_confirmed_invites", return_value=[invite]), \
             patch.object(reminder_handler, "_batch_get_members", return_value={invite["phone"]: {"phone": invite["phone"], "smsOptIn": True, "optOut": True}}), \
             patch.object(reminder_handler, "_invites_table", return_value=MagicMock()), \
             patch.object(reminder_handler, "send_sms") as send_sms, \
             patch.object(reminder_handler, "log_action"):
            result = reminder_handler.send_reminders({"eventSlug": "rooftop-sept2026"}, True)
        self.assertEqual(result["skippedOptOut"], 1)
        send_sms.assert_not_called()



class TestWebhookVerificationContract(unittest.TestCase):
    @staticmethod
    def _signed_event(*, body='{"type":"message.received"}', secret=b'rsvp-webhook-secret', timestamp=None):
        if timestamp is None:
            timestamp = int(datetime.now(timezone.utc).timestamp())
        ts_raw = str(timestamp)
        digest = base64.b64encode(
            hmac.new(secret, f"{ts_raw}.{body}".encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        return {
            "headers": {"openphone-signature": f"hmac;1;{ts_raw};{digest}"},
            "body": body,
            "isBase64Encoded": False,
        }

    def test_webhook_accepts_valid_signature(self):
        secret = b"rsvp-webhook-secret"
        event = self._signed_event(secret=secret)
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "rsvp/quo-webhook", "WEBHOOK_SECRET_ID_2": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "false"}, clear=False), \
             patch.object(sms_webhook, "get_secret_string", return_value=base64.b64encode(secret).decode("ascii")):
            self.assertTrue(sms_webhook._verify_webhook_signature(event))

    def test_webhook_rejects_invalid_signature(self):
        event = self._signed_event(secret=b"wrong-key")
        real_secret = base64.b64encode(b"rsvp-webhook-secret").decode("ascii")
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "rsvp/quo-webhook", "WEBHOOK_SECRET_ID_2": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "false"}, clear=False), \
             patch.object(sms_webhook, "get_secret_string", return_value=real_secret):
            self.assertFalse(sms_webhook._verify_webhook_signature(event))

    def test_webhook_rejects_expired_timestamp(self):
        expired = int(datetime.now(timezone.utc).timestamp()) - 301
        event = self._signed_event(timestamp=expired)
        secret = base64.b64encode(b"rsvp-webhook-secret").decode("ascii")
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "rsvp/quo-webhook", "WEBHOOK_SECRET_ID_2": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "false"}, clear=False), \
             patch.object(sms_webhook, "get_secret_string", return_value=secret):
            self.assertFalse(sms_webhook._verify_webhook_signature(event))

    def test_webhook_rejects_when_signing_secret_is_missing(self):
        event = {"headers": {}, "body": "{}"}
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "false"}, clear=False):
            self.assertFalse(sms_webhook._verify_webhook_signature(event))

    def test_webhook_dev_bypass_requires_explicit_flag(self):
        event = {"headers": {}, "body": "{}"}
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "true"}, clear=False):
            self.assertTrue(sms_webhook._verify_webhook_signature(event))


class TestCurrentQuoWebhookContract(unittest.TestCase):
    @staticmethod
    def _signed_event(*, body='{"type":"message.received"}', secret=b'rsvp-current-webhook-secret', timestamp=None, webhook_id="msg_123"):
        if timestamp is None:
            timestamp = int(datetime.now(timezone.utc).timestamp())
        ts_raw = str(timestamp)
        signed = f"{webhook_id}.{ts_raw}.{body}".encode("utf-8")
        digest = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode("ascii")
        return {
            "headers": {
                "webhook-id": webhook_id,
                "webhook-timestamp": ts_raw,
                "webhook-signature": f"v1,{digest}",
            },
            "body": body,
            "isBase64Encoded": False,
        }

    def test_current_standard_webhook_signature_is_accepted(self):
        secret = b"rsvp-current-webhook-secret"
        stored = "whsec_" + base64.b64encode(secret).decode("ascii")
        event = self._signed_event(secret=secret)
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "rsvp/quo-current", "WEBHOOK_SECRET_ID_2": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "false"}, clear=False), \
             patch.object(sms_webhook, "get_secret_string", return_value=stored):
            self.assertTrue(sms_webhook._verify_webhook_signature(event))

    def test_current_standard_webhook_rejects_expired_timestamp(self):
        secret = b"rsvp-current-webhook-secret"
        stored = "whsec_" + base64.b64encode(secret).decode("ascii")
        event = self._signed_event(secret=secret, timestamp=int(datetime.now(timezone.utc).timestamp()) - 301)
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "rsvp/quo-current", "WEBHOOK_SECRET_ID_2": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "false"}, clear=False), \
             patch.object(sms_webhook, "get_secret_string", return_value=stored):
            self.assertFalse(sms_webhook._verify_webhook_signature(event))

    def test_incomplete_standard_webhook_headers_fail_closed(self):
        event = {"headers": {"webhook-id": "msg_123"}, "body": "{}"}
        with patch.dict(os.environ, {"WEBHOOK_SECRET_ID": "rsvp/quo-current", "WEBHOOK_SECRET_ID_2": "", "ALLOW_UNSIGNED_WEBHOOK_DEV": "false"}, clear=False), \
             patch.object(sms_webhook, "get_secret_string", return_value="whsec_dGVzdA=="):
            self.assertFalse(sms_webhook._verify_webhook_signature(event))


class TestExternalRuntimeCompatibilityContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[2]
        cls.workflow = (cls.root / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        cls.main_tf = (cls.root / "backend" / "terraform" / "main.tf").read_text(encoding="utf-8")
        cls.eventbridge_tf = (cls.root / "backend" / "terraform" / "eventbridge.tf").read_text(encoding="utf-8")
        cls.cloudfront_tf = (cls.root / "backend" / "terraform" / "cloudfront_api.tf").read_text(encoding="utf-8")
        cls.jade_source = (cls.root / "backend" / "lambda" / "jade_service.py").read_text(encoding="utf-8")

    def test_quo_api_defaults_to_current_https_base_and_is_configurable(self):
        with patch.dict(os.environ, {"QUO_API_BASE_URL": ""}, clear=False):
            self.assertEqual(sms_adapter._quo_api_url("/v1/messages"), "https://api.quo.com/v1/messages")
        with patch.dict(os.environ, {"QUO_API_BASE_URL": "https://example.test/"}, clear=False):
            self.assertEqual(sms_adapter._quo_api_url("v1/messages"), "https://example.test/v1/messages")
        with patch.dict(os.environ, {"QUO_API_BASE_URL": "http://insecure.test"}, clear=False):
            with self.assertRaises(RuntimeError):
                sms_adapter._quo_api_url("v1/messages")

    def test_lambda_and_ci_use_python_313(self):
        self.assertNotIn('runtime       = "python3.11"', self.main_tf + self.eventbridge_tf)
        self.assertGreaterEqual((self.main_tf + self.eventbridge_tf).count('runtime       = "python3.13"'), 5)
        self.assertIn('python-version: "3.13"', self.workflow)

    def test_production_terraform_apply_requires_manual_dispatch(self):
        # Pushes to main may run tests, but production apply must be an explicit action.
        self.assertIn("if: github.event_name == 'workflow_dispatch'", self.workflow)
        self.assertIn("branches:\n      - main", self.workflow)

    def test_current_ci_toolchain_versions_are_pinned(self):
        self.assertRegex(self.workflow, r"uses: actions/checkout@[0-9a-f]{40} # v7")
        self.assertRegex(self.workflow, r"uses: actions/setup-python@[0-9a-f]{40} # v7")
        self.assertRegex(self.workflow, r"uses: actions/setup-node@[0-9a-f]{40} # v7")
        self.assertIn('node-version: "24"', self.workflow)
        self.assertRegex(self.workflow, r"uses: aws-actions/configure-aws-credentials@[0-9a-f]{40} # v6")
        self.assertRegex(self.workflow, r"uses: hashicorp/setup-terraform@[0-9a-f]{40} # v4")
        self.assertIn('terraform_version: "1.16.4"', self.workflow)
        self.assertIn('required_version = "~> 1.16.0"', self.main_tf)

    def test_cloudfront_forwards_current_and_legacy_quo_signature_headers(self):
        for header in ("openphone-signature", "webhook-id", "webhook-timestamp", "webhook-signature"):
            self.assertIn(f'"{header}"', self.cloudfront_tf)

    def test_jade_model_is_runtime_configurable_and_legacy_prompt_cache_beta_is_removed(self):
        self.assertIn('os.getenv("CLAUDE_MODEL")', self.jade_source)
        self.assertIn('"cache_control": {"type": "ephemeral"}', self.jade_source)
        self.assertNotIn('prompt-caching-2024-07-31', self.jade_source)
        self.assertIn('CLAUDE_MODEL', self.main_tf)

    def test_jade_runtime_request_uses_configured_model_without_legacy_beta_header(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"content": [{"text": "Got you."}]}).encode("utf-8")
        response.__enter__.return_value = response
        deps = {
            "JADE_SYSTEM_PROMPT": "You are Jade.",
            "_build_event_context": lambda member=None: "[EVENT CONTEXT]\nevent_status: upcoming\n[END EVENT CONTEXT]",
            "get_secret_string": lambda _secret_id: "anthropic-test-key",
        }
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-test-configured-model"}, clear=False), \
             patch.object(jade_service.urllib.request, "urlopen", return_value=response) as urlopen:
            text = jade_service.claude("hello", deps=deps)
        self.assertEqual(text, "Got you.")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "claude-test-configured-model")
        self.assertEqual(payload["system"][0]["cache_control"], {"type": "ephemeral"})
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["x-api-key"], "anthropic-test-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("anthropic-beta", headers)


class TestWafContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_tf = (Path(__file__).resolve().parents[1] / "terraform" / "main.tf").read_text()

    def test_public_access_rate_limit_is_human_scale(self):
        block = self.main_tf.split('name     = "rate-limit-access"', 1)[1].split('visibility_config', 1)[0]
        self.assertIn("limit              = 60", block)

    def test_strict_admin_rate_limit_does_not_match_status_polling(self):
        block = self.main_tf.split('name     = "rate-limit-admin-blast"', 1)[1].split('visibility_config', 1)[0]
        self.assertIn('/admin/invite/send', block)
        self.assertIn('/admin/invite/reminder', block)
        self.assertIn('/admin/event/draft-message', block)
        self.assertNotIn('search_string = "/admin/invite"', block)

    def test_general_admin_rate_limit_covers_all_admin_routes(self):
        block = self.main_tf.split('name     = "rate-limit-admin-general"', 1)[1].split('visibility_config', 1)[0]
        self.assertIn("limit              = 120", block)
        self.assertIn('search_string = "/admin/"', block)

    def test_rate_limits_use_the_sanitized_viewer_ip_and_require_cloudfront_origin_secret(self):
        for rule_name in ("rate-limit-access", "rate-limit-admin-blast", "rate-limit-admin-general"):
            block = self.main_tf.split(f'name     = "{rule_name}"', 1)[1].split('visibility_config', 1)[0]
            self.assertIn('header_name       = "X-Forwarded-For"', block)
            self.assertIn('fallback_behavior = "MATCH"', block)
        self.assertIn('name     = "block-untrusted-api-origin"', self.main_tf)
        self.assertIn('name = "x-rsvp-origin-verify"', self.main_tf)
        self.assertIn("length(var.cloudfront_origin_verify_header) >= 32", self.main_tf)
        cloudfront_tf = (Path(__file__).resolve().parents[1] / "terraform" / "cloudfront_api.tf").read_text()
        self.assertIn("delete request.headers['x-forwarded-for']", cloudfront_tf)
        self.assertIn('name  = "X-RSVP-Origin-Verify"', cloudfront_tf)


class TestTerraformDeploymentContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tf_root = Path(__file__).resolve().parents[1] / "terraform"
        cls.main_tf = (cls.tf_root / "main.tf").read_text(encoding="utf-8")
        cls.iam_tf = (cls.tf_root / "iam_per_function.tf").read_text(encoding="utf-8")
        cls.events_tf = (cls.tf_root / "events_endpoint.tf").read_text(encoding="utf-8")

    def test_close_event_route_is_deployed(self):
        self.assertIn('path_part   = "finalize-attendance"', self.events_tf)
        self.assertIn('resource "aws_api_gateway_method" "admin_events_finalize_attendance_post"', self.events_tf)
        self.assertIn('resource "aws_api_gateway_integration" "admin_events_finalize_attendance_post"', self.events_tf)
        self.assertIn('aws_api_gateway_integration.admin_events_finalize_attendance_post', self.main_tf)

    def test_invite_async_retry_is_bounded_and_deploy_requires_secret(self):
        jobs_tf = (self.tf_root / "invite_jobs.tf").read_text(encoding="utf-8")
        self.assertIn('resource "aws_lambda_function_event_invoke_config" "invite_handler"', jobs_tf)
        self.assertIn("maximum_retry_attempts       = 0", jobs_tf)
        workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn("TF_VAR_cloudfront_origin_verify_header: ${{ secrets.CLOUDFRONT_ORIGIN_VERIFY_HEADER }}", workflow)
        self.assertIn("Apply CloudFront origin header and viewer-IP function first", workflow)
        self.assertIn("Wait for CloudFront deployment", workflow)

    def test_admin_lambda_has_claude_secret_runtime_and_permission(self):
        admin_block = self.main_tf.split('resource "aws_lambda_function" "admin_handler"', 1)[1].split('resource "aws_lambda_function" "sms_handler"', 1)[0]
        self.assertIn('CLAUDE_API_KEY_SECRET_ID', admin_block)
        admin_iam = self.iam_tf.split('resource "aws_iam_role_policy" "lambda_admin_handler"', 1)[1].split('# =============================================================================\n# sms_handler', 1)[0]
        self.assertIn('secret:rsvp/claude-api-key*', admin_iam)

    def test_sms_lambda_can_run_atomic_rsvp_transactions(self):
        sms_iam = self.iam_tf.split('resource "aws_iam_role_policy" "lambda_sms_handler"', 1)[1].split('# =============================================================================\n# invite_handler', 1)[0]
        self.assertIn('dynamodb:TransactWriteItems', sms_iam)
        self.assertIn('Resource = [local.events_arn, local.invites_arn, local.members_arn]', sms_iam)
        self.assertIn('Action   = ["dynamodb:ConditionCheckItem"]', sms_iam)
        self.assertIn('Resource = [local.members_arn, local.invites_arn]', sms_iam)

    def test_admin_lambda_can_clear_pending_sms_approvals(self):
        admin_block = self.main_tf.split('resource "aws_lambda_function" "admin_handler"', 1)[1].split('resource "aws_lambda_function" "sms_handler"', 1)[0]
        self.assertIn('PENDING_APPROVALS_TABLE_NAME', admin_block)
        admin_iam = self.iam_tf.split('resource "aws_iam_role_policy" "lambda_admin_handler"', 1)[1].split('# =============================================================================\n# sms_handler', 1)[0]
        self.assertIn('local.pending_approvals_arn', admin_iam)

    def test_draft_message_changes_force_api_redeployment(self):
        self.assertIn('filesha1("${path.module}/draft_message_endpoint.tf")', self.main_tf)
        self.assertIn('aws_api_gateway_integration.admin_event_draft_message_post', self.main_tf)
        self.assertIn('aws_api_gateway_integration.admin_event_draft_message_options', self.main_tf)



class TestDeploymentPipelineContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[2]
        cls.workflow = (cls.root / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        cls.tf_main = (cls.root / "backend" / "terraform" / "main.tf").read_text(encoding="utf-8")
        cls.export_clean = (cls.root / "backend" / "lambda" / "export_clean.sh").read_text(encoding="utf-8")

    def test_route_audit_sees_wrapped_frontend_calls(self):
        summary = route_contract_audit.build_summary(self.root)
        self.assertEqual(summary["frontend_contracts_detected"]["ADMIN_EVENTS_FINALIZE"]["methods"], ["POST"])
        self.assertEqual(summary["frontend_contracts_detected"]["ADMIN_MEMBER_CONFIRMED"]["methods"], ["GET"])
        self.assertEqual(summary["missing_in_terraform"], {})

    def test_ci_runs_declared_regression_suite_before_terraform(self):
        self.assertIn("pip install -r requirements-dev.txt", self.workflow)
        self.assertIn("bash run_step1_tests.sh", self.workflow)
        self.assertNotIn("aws lambda update-function-code", self.workflow)
        self.assertIn("needs: test", self.workflow)

    def test_terraform_deploys_the_canonical_build_lambda_bundle(self):
        build_script = (self.root / "backend" / "lambda" / "build_lambda.sh").read_text(encoding="utf-8")
        self.assertIn('OUTPUT="$SCRIPT_DIR/../terraform/lambda_bundle.zip"', build_script)
        self.assertIn('lambda_bundle_path = "${path.module}/lambda_bundle.zip"', self.tf_main)
        self.assertIn('filebase64sha256(local.lambda_bundle_path)', self.tf_main)
        self.assertNotIn('data "archive_file" "lambda_bundle"', self.tf_main)
        self.assertIn('bash backend/lambda/build_lambda.sh', self.workflow)

    def test_clean_frontend_export_contains_public_and_admin_site(self):
        self.assertIn('FRONTEND_DIR="$REPO_ROOT/frontend"', self.export_clean)
        self.assertNotIn('FRONTEND_DIR="$REPO_ROOT/frontend/admin"', self.export_clean)

    def test_clean_export_reuses_canonical_lambda_bundle_and_terraform_owns_deploy(self):
        self.assertIn('"$LAMBDA_DIR/build_lambda.sh"', self.export_clean)
        self.assertIn('lambda_bundle.zip', self.export_clean)
        self.assertIn('it deploys CloudFront and waits before applying WAF', self.export_clean)
        self.assertIn('do not use a one-step apply', self.export_clean)
        self.assertNotIn('Deploy dist/lambda-bundle.zip to Lambda', self.export_clean)



class TestReminderSchedulerContract(unittest.TestCase):
    def test_active_event_creates_only_future_one_time_reminder_schedules(self):
        event = {
            "eventId": "louisville-oct",
            "eventSlug": "louisville-oct",
            "event_status": "LIVE",
            "date": "2026-10-17",
            "event_timezone": "America/New_York",
            "venueReleaseMode": "confirmation",
            "reminderTiming": "both",
            "day_before_send_time": "18:00",
            "day_of_send_time": "11:00",
        }
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        specs = reminder_schedule.desired_schedule_specs(event, now=now)
        self.assertEqual([s["timing"] for s in specs], ["day_before", "day_of"])
        self.assertEqual(specs[0]["scheduleExpression"], "at(2026-10-16T18:00:00)")
        self.assertEqual(specs[1]["scheduleExpression"], "at(2026-10-17T11:00:00)")
        self.assertEqual({s["timezone"] for s in specs}, {"America/New_York"})

    def test_scheduler_sync_replaces_old_jobs_with_auto_deleting_one_time_jobs(self):
        event = {
            "eventId": "louisville-oct", "eventSlug": "louisville-oct",
            "event_status": "LIVE", "date": "2099-10-17",
            "event_timezone": "America/New_York", "venueReleaseMode": "confirmation", "reminderTiming": "both",
            "day_before_send_time": "18:00", "day_of_send_time": "11:00",
        }
        client = MagicMock()
        client.delete_schedule.side_effect = [
            ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}}, "DeleteSchedule"),
            ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}}, "DeleteSchedule"),
        ]
        with patch.dict(os.environ, {
            "REMINDER_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123:function:rsvp-reminder-handler",
            "REMINDER_SCHEDULER_ROLE_ARN": "arn:aws:iam::123:role/rsvp-reminder-scheduler-invoker",
        }), patch.object(reminder_schedule.boto3, "client", return_value=client):
            result = reminder_schedule.sync_event_reminder_schedules(event, active=True)
        self.assertEqual(result["scheduled"], 2)
        self.assertEqual(client.create_schedule.call_count, 2)
        for call in client.create_schedule.call_args_list:
            kwargs = call.kwargs
            self.assertEqual(kwargs["ActionAfterCompletion"], "DELETE")
            self.assertEqual(kwargs["FlexibleTimeWindow"], {"Mode": "OFF"})
            self.assertTrue(kwargs["ScheduleExpression"].startswith("at("))
            payload = json.loads(kwargs["Target"]["Input"])
            self.assertEqual(payload["source"], "scheduler")
            self.assertEqual(payload["eventSlug"], "louisville-oct")

    def test_stale_one_time_schedule_cannot_send_for_new_active_event(self):
        current = {
            "eventId": "new-event", "eventSlug": "new-event", "event_status": "LIVE",
            "date": "2099-10-17", "event_timezone": "America/New_York",
            "reminderTiming": "day_of", "day_of_send_time": "11:00",
        }
        payload = {
            "source": "scheduler", "timing": "day_of", "eventSlug": "old-event",
            "expectedDate": "2099-10-17", "expectedSendTime": "11:00",
            "expectedTimezone": "America/New_York",
        }
        with patch.object(reminder_handler, "_get_current_event", return_value=current), \
             patch.object(reminder_handler, "send_reminders") as send:
            result = reminder_handler.handler(payload, None)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "stale schedule")
        send.assert_not_called()

    def test_reminder_continuation_revalidates_event_and_uses_locked_recipient_keys(self):
        current = {
            "eventId": "event-one", "eventSlug": "event-one", "event_status": "LIVE",
            "date": "2026-09-29", "event_timezone": "America/New_York",
            "reminderTiming": "manual", "day_of_send_time": "11:00",
        }
        phone = "+15025551212"
        payload = {
            "source": "reminder-continuation", "timing": "day_of",
            "eventSlug": "event-one", "expectedDate": "2026-09-29",
            "expectedSendTime": "11:00", "expectedTimezone": "America/New_York",
            "expectedReminderTiming": "manual", "continuationPhoneKeys": [phone],
        }
        with patch.object(reminder_handler, "_get_current_event", return_value=current), \
             patch.object(reminder_handler, "send_reminders", return_value={"sent": 1}) as send:
            result = reminder_handler.handler(payload, None)
        self.assertEqual(result, {"ok": True, "sent": 1})
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["phone_keys"], [phone])

    def test_terraform_has_no_perpetual_five_minute_reminder_pollers(self):
        root = Path(__file__).resolve().parents[2]
        tf = (root / "backend" / "terraform" / "eventbridge.tf").read_text(encoding="utf-8")
        self.assertNotIn("cron(0/5", tf)
        self.assertNotIn('aws_cloudwatch_event_rule" "reminder_day_before', tf)
        self.assertIn('aws_iam_role" "reminder_scheduler_invoker', tf)
        self.assertIn('scheduler.amazonaws.com', tf)
        iam = (root / "backend" / "terraform" / "iam_per_function.tf").read_text(encoding="utf-8")
        self.assertIn('"lambda:InvokeFunction"', iam)
        self.assertIn('"dynamodb:BatchGetItem"]', iam)



class TestStorageCostContract(unittest.TestCase):
    def test_photo_bucket_expires_noncurrent_versions(self):
        root = Path(__file__).resolve().parents[2]
        tf = (root / "backend" / "terraform" / "storage.tf").read_text(encoding="utf-8")
        self.assertIn('resource "aws_s3_bucket_lifecycle_configuration" "pics"', tf)
        self.assertIn('id     = "expire-noncurrent-photo-versions"', tf)
        self.assertIn("noncurrent_version_expiration", tf)
        self.assertIn("noncurrent_days = 30", tf)


class TestEventHistoryProtectionContract(unittest.TestCase):
    def test_event_history_has_deletion_protection_and_pitr(self):
        root = Path(__file__).resolve().parents[2]
        tf = (root / "backend" / "terraform" / "main.tf").read_text(encoding="utf-8")
        start = tf.index('resource "aws_dynamodb_table" "event_history"')
        end = tf.index('\n}', start)
        block = tf[start:end]
        self.assertIn('name         = "rsvp-event-history"', block)
        self.assertIn('deletion_protection_enabled = true', block)
        self.assertIn('point_in_time_recovery {\n    enabled = true', block)



class TestPhotoStorageCostContract(unittest.TestCase):
    def test_noncurrent_photo_versions_expire_after_recovery_window(self):
        root = Path(__file__).resolve().parents[2]
        tf = (root / "backend" / "terraform" / "storage.tf").read_text(encoding="utf-8")
        self.assertIn('resource "aws_s3_bucket_lifecycle_configuration" "pics"', tf)
        self.assertIn('noncurrent_version_expiration', tf)
        self.assertIn('noncurrent_days = 30', tf)
        self.assertIn('depends_on = [aws_s3_bucket_versioning.pics]', tf)



if __name__ == "__main__":
    unittest.main(verbosity=2)