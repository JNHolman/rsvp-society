#!/usr/bin/env python3
"""Regression contracts for RSVP Society defects that have been corrected.

Every test states the documented/correct rule and must pass normally.

Run without AWS/moto:
    python -m unittest known_defect_contract_tests -v
"""
from __future__ import annotations

import json
from decimal import Decimal
import os
import unittest
from unittest.mock import MagicMock, patch

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
os.environ.setdefault("SMS_ENABLED", "false")

import access_request
import attendance_store
import admin_event_routes
import admin_member_routes
import invite_handler
import member_store
import reminder_handler
import sms_host_approval



class TestKnownDefectContracts(unittest.TestCase):
    def test_host_approval_storage_error_is_retryable(self):
        deps = {
            "get_pending_approval": MagicMock(side_effect=RuntimeError("temporary DDB error")),
            "send_sms": MagicMock(),
            "logger": MagicMock(),
        }
        result = sms_host_approval.handle_host_approval(
            "+15025550001", "Y 4821", True, ["+15025550001"], deps=deps,
        )
        self.assertEqual(result["statusCode"], 503)
        deps["send_sms"].assert_not_called()

    def test_retired_inviting_state_cannot_be_entered(self):
        """Current lifecycle is DRAFT -> LIVE -> ARCHIVED; INVITING is retired."""
        self.assertFalse(admin_event_routes._can_transition("DRAFT", "INVITING"))

    def test_denied_member_reapplication_returns_to_pending(self):
        """A genuine new access request must become reviewable again."""
        table = MagicMock()
        table.get_item.side_effect = [
            {"Item": {"phone": "+15025551212", "status": "DENIED", "name": "Jordan"}},
            {"Item": {"phone": "+15025551212", "status": "DENIED", "name": "Jordan"}},
        ]
        with patch.object(member_store, "_table", return_value=table):
            member_store.upsert_member(
                phone="+15025551212",
                name="Jordan",
                last_name="Smith",
                sms_opt_in=True,
            )
        update_expression = table.update_item.call_args.kwargs["UpdateExpression"]
        self.assertIn("#s = :pending", update_expression)

    def test_import_aborts_when_existing_member_lookup_fails(self):
        """A failed safety lookup must not be treated as an empty member table."""
        table = MagicMock()
        table.name = "rsvp-members-test"
        table.meta.client.batch_write_item.return_value = {"UnprocessedItems": {}}
        ddb = MagicMock()
        ddb.Table.return_value = table
        event = {"body": json.dumps({
            "members": [{"phone": "5025551212", "firstName": "Jordan", "zipCode": "40205"}],
            "status": "APPROVED",
        })}
        with patch.object(admin_member_routes.boto3, "resource", return_value=ddb), \
             patch.object(admin_member_routes, "resolve_us_zip", return_value={"zipCode": "40205", "city": "Louisville", "state": "KY", "latitude": 38.223, "longitude": -85.683}), \
             patch.object(admin_member_routes, "_batch_get_existing", side_effect=RuntimeError("ddb read failed")), \
             patch.object(admin_member_routes, "log_action"):
            response = admin_member_routes.import_members(event, {}, "token")
        self.assertGreaterEqual(response["statusCode"], 500)

    def test_batch_get_raises_when_keys_remain_unprocessed(self):
        """Unresolved keys cannot silently become 'new member' records."""
        table = MagicMock()
        table.name = "rsvp-members-test"
        table.meta.client.batch_get_item.return_value = {
            "Responses": {table.name: []},
            "UnprocessedKeys": {table.name: {"Keys": [{"phone": "+15025551212"}]}},
        }
        with self.assertRaises(RuntimeError):
            admin_member_routes._batch_get_existing(table, ["+15025551212"])

    def test_confirmed_update_fails_closed_when_member_batch_read_is_incomplete(self):
        invite_table = MagicMock()
        invite_table.name = "rsvp-event-invites-test"
        member_table = MagicMock()
        member_table.name = "rsvp-members-test"
        invite_table.meta.client.batch_get_item.return_value = {
            "Responses": {invite_table.name: [{
                "eventId": "event-1", "phone": "+15025551212", "status": "CONFIRMED",
            }]},
        }
        member_table.meta.client.batch_get_item.return_value = {
            "Responses": {member_table.name: []},
            "UnprocessedKeys": {member_table.name: {"Keys": [{"phone": "+15025551212"}]}},
        }
        with patch.object(invite_handler, "_invites_table", return_value=invite_table), \
             patch.object(invite_handler, "members_table", return_value=member_table):
            with self.assertRaisesRegex(RuntimeError, "member batch read left unprocessed keys"):
                invite_handler._confirmed_update_recipients("event-1", ["+15025551212"])

    def test_confirmed_update_fails_closed_when_invite_batch_read_throws(self):
        invite_table = MagicMock()
        invite_table.name = "rsvp-event-invites-test"
        invite_table.meta.client.batch_get_item.side_effect = RuntimeError("read failed")
        with patch.object(invite_handler, "_invites_table", return_value=invite_table):
            with self.assertRaisesRegex(RuntimeError, "invite batch read failed"):
                invite_handler._confirmed_update_recipients("event-1", ["+15025551212"])

    def test_legacy_approved_member_without_smsoptin_still_receives_reminder(self):
        """README: approved legacy/imported members are eligible unless explicitly opted out."""
        invite = {
            "eventId": "rooftop-sept2026",
            "phone": "+15025551212",
            "name": "Jordan Smith",
            "status": "CONFIRMED",
        }
        invites_table = MagicMock()
        event = {
            "eventSlug": "rooftop-sept2026",
            "event_label": "RSVP Society",
            "day_of_template": "{name}, tonight.",
        }
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(reminder_handler, "_get_confirmed_invites", return_value=[invite]), \
             patch.object(reminder_handler, "_batch_get_members", return_value={
                 "+15025551212": {"phone": "+15025551212", "name": "Jordan", "status": "APPROVED"}
             }), \
             patch.object(reminder_handler, "_invites_table", return_value=invites_table), \
             patch.object(reminder_handler, "send_sms") as send_sms, \
             patch.object(reminder_handler.time, "sleep"), \
             patch.object(reminder_handler, "log_action"):
            result = reminder_handler.send_reminders(event, True)
        self.assertEqual(result["sent"], 1)
        send_sms.assert_called_once()

    def test_repeat_pending_signup_does_not_resend_host_approval_sms(self):
        """Resubmitting an already-pending request must not generate another paid host SMS."""
        fake_scan_table = MagicMock()
        fake_scan_table.scan.return_value = {"Items": []}
        fake_approval_table = MagicMock()
        fake_ddb = MagicMock()
        fake_ddb.Table.side_effect = [fake_scan_table, fake_approval_table]
        existing_pending = {
            "phone": "+15025551212",
            "name": "Jordan",
            "lastName": "Smith",
            "status": "PENDING",
            "smsOptIn": True,
            "createdAt": "2026-09-01T00:00:00+00:00",
        }
        event = {"httpMethod": "POST", "headers": {}, "body": json.dumps({
            "firstName": "Jordan",
            "lastName": "Smith",
            "phone": "5025551212",
            "zipCode": "40205",
            "smsOptIn": True,
        })}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(access_request.boto3, "resource", return_value=fake_ddb), \
             patch.object(access_request, "get_member", return_value=existing_pending), \
             patch.object(access_request, "resolve_us_zip", return_value={"zipCode":"40205","city":"Louisville","state":"KY","latitude":38.223,"longitude":-85.683}), \
             patch.object(access_request, "upsert_member", return_value=existing_pending), \
             patch.object(access_request, "get_host_phones", return_value=["+15025550000"]), \
             patch.object(access_request, "send_sms") as send_sms:
            response = access_request.handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        send_sms.assert_not_called()

    @patch("invite_sender.reserve_invite", new=lambda invites, members, item, expected_status=None: invites.put_item(Item=item))
    def test_locked_preview_member_is_not_dropped_when_market_data_changes_before_send(self):
        """A locked preview is authoritative; mutable audience fields cannot silently remove its recipient."""
        phone = "+15025551212"
        # Preview selected this person while Louisville; record now says Lexington.
        member = {
            "phone": phone,
            "name": "Jordan",
            "status": "APPROVED",
            "smsOptIn": True,
            "market": "Lexington",
            "gender": "F",
            "tierOverride": 1,
            "_tier": 1,
        }
        invites = MagicMock()
        members = MagicMock()
        events = MagicMock()
        ddb = MagicMock(); ddb.Table.return_value = events
        body = {
            "eventId": "rooftop-sept2026",
            "capacity": 100,
            "waveNumber": 1,
            "phones": [phone],
            "lockedWave": True,
            "audienceFilters": {"market": "Louisville"},
        }
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
            invite_handler._execute_send(body, "", "token", "job-lock")
        invites.put_item.assert_called_once()

    @patch("invite_sender.reserve_invite", new=lambda invites, members, item, expected_status=None: invites.put_item(Item=item))
    def test_successful_sms_is_not_erased_when_member_counter_update_fails(self):
        """After Quo accepts an SMS, a secondary counter failure must not delete the invite row."""
        phone = "+15025551212"
        member = {"phone": phone, "name": "Jordan", "status": "APPROVED", "smsOptIn": True, "gender": "F", "tierOverride": 1, "_tier": 1}
        invites = MagicMock()
        members = MagicMock()
        members.update_item.side_effect = RuntimeError("counter write failed")
        events = MagicMock()
        ddb = MagicMock(); ddb.Table.return_value = events
        body = {"eventId": "rooftop-sept2026", "capacity": 100, "waveNumber": 1, "phones": [phone]}
        event = {"eventSlug": "rooftop-sept2026", "event_status": "LIVE", "invite_template": "{name}. RSVP Society."}
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(invite_handler, "_resolve_active_invitable_event", return_value=event), \
             patch.object(invite_handler, "_assert_formal_wave_available"), \
             patch.object(invite_handler, "_get_existing_invited_phones", return_value=set()), \
             patch.object(invite_handler, "_get_approved_members", return_value=[member]), \
             patch.object(invite_handler, "_invites_table", return_value=invites), \
             patch.object(invite_handler, "members_table", return_value=members), \
             patch.object(invite_handler, "send_sms", return_value="quo-msg-1"), \
             patch.object(invite_handler.time, "sleep"), \
             patch.object(invite_handler.boto3, "resource", return_value=ddb), \
             patch.object(invite_handler, "_update_job"), \
             patch.object(invite_handler, "log_action"):
            invite_handler._execute_send(body, "", "token", "job-send")
        invites.delete_item.assert_not_called()

    def test_finalization_reports_failure_if_member_no_show_write_fails(self):
        """An event cannot be declared finalized while a required attendee settlement failed."""
        invites = MagicMock()
        invites.query.return_value = {"Items": [{
            "eventId": "rooftop-sept2026",
            "phone": "+15025551212",
            "status": "CONFIRMED",
        }]}
        with patch.object(attendance_store, "_invites_table", return_value=invites), \
             patch.object(attendance_store, "record_attendance", return_value={"ok": False, "reason": "ddb failed"}):
            result = attendance_store.finalize_event_attendance({"eventSlug": "rooftop-sept2026"})
        self.assertFalse(result["ok"])

    def test_stop_reapplication_waits_for_host_review(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"phone": "+15025551212", "status": "APPROVED", "optOut": True, "optOutAt": "old", "smsOptIn": False}}
        with patch.object(member_store, "_table", return_value=table):
            member_store.upsert_member(phone="+15025551212", name="Jordan", sms_opt_in=True)
        call = table.update_item.call_args.kwargs
        self.assertIn("pendingSmsConsentAt", call["UpdateExpression"])
        self.assertIn("#s = :pending", call["UpdateExpression"])
        self.assertFalse(call["ExpressionAttributeValues"][":soi"])
        self.assertNotIn("optOut", call["UpdateExpression"])


class TestFinalAuditRegressionContracts(unittest.TestCase):
    def _attendance_event(self, now_value, *, start="21:00", end="02:00"):
        event_row = {
            "eventId": "deep-end",
            "eventSlug": "deep-end",
            "date": "2026-10-04",
            "startTime": start,
            "endTime": end,
            "event_timezone": "America/New_York",
            "event_status": "LIVE",
        }
        event = {"body": json.dumps({
            "eventId": "deep-end",
            "phone": "+15025551212",
            "attended": True,
        })}
        event_table = MagicMock()
        event_table.get_item.return_value = {"Item": event_row}
        ddb = MagicMock()
        ddb.Table.return_value = event_table
        fake_datetime = MagicMock(wraps=admin_member_routes.datetime)
        fake_datetime.now.return_value = now_value
        fake_datetime.combine.side_effect = admin_member_routes.datetime.combine
        fake_datetime.strptime.side_effect = admin_member_routes.datetime.strptime
        return event, ddb, fake_datetime

    def test_checkin_window_boundaries_and_overnight_end(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("America/New_York")
        cases = [
            (datetime(2026, 10, 4, 17, 59, 59, tzinfo=zone), 409),
            (datetime(2026, 10, 4, 18, 0, 0, tzinfo=zone), 200),
            (datetime(2026, 10, 4, 20, 0, 0, tzinfo=zone), 200),
            (datetime(2026, 10, 4, 23, 30, 0, tzinfo=zone), 200),
            (datetime(2026, 10, 5, 1, 30, 0, tzinfo=zone), 200),
            (datetime(2026, 10, 5, 2, 0, 0, tzinfo=zone), 200),
            (datetime(2026, 10, 5, 2, 0, 1, tzinfo=zone), 409),
        ]
        for now_value, expected_status in cases:
            with self.subTest(now=now_value.isoformat()):
                event, ddb, fake_datetime = self._attendance_event(now_value)
                with patch.object(admin_member_routes.boto3, "resource", return_value=ddb), \
                     patch.object(admin_member_routes, "datetime", fake_datetime), \
                     patch.object(admin_member_routes, "record_attendance", return_value={"ok": True, "result": "ATTENDANCE_OK", "reason": ""}), \
                     patch.object(admin_member_routes, "log_action"):
                    response = admin_member_routes.record_member_attendance(event, {}, "token")
                self.assertEqual(response["statusCode"], expected_status)

    def test_checkin_uses_persisted_plus_one_membership_without_member_scan(self):
        invite_table = MagicMock()
        invite_table.query.return_value = {"Items": [{
            "eventId": "deep-end",
            "phone": "+15025551212",
            "status": "CONFIRMED",
            "name": "Jordan",
            "lastName": "Smith",
            "plusOneName": "Taylor Jones",
            "plusOneIsMember": True,
        }]}
        member_table = MagicMock()
        member_table.name = "rsvp-members-test"
        member_table.meta.client.batch_get_item.return_value = {
            "Responses": {"rsvp-members-test": [{
                "phone": "+15025551212", "name": "Jordan", "lastName": "Smith"
            }]}
        }
        ddb = MagicMock()
        ddb.Table.side_effect = [invite_table, member_table]
        event = {"queryStringParameters": {"eventId": "deep-end"}}

        with patch.object(admin_member_routes.boto3, "resource", return_value=ddb):
            response = admin_member_routes.get_confirmed(event, {}, "token")

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])
        self.assertTrue(payload["members"][0]["plusOneIsMember"])
        member_table.scan.assert_not_called()

    def test_public_signup_rejects_oversized_plain_body_before_processing(self):
        event = {
            "httpMethod": "POST",
            "headers": {},
            "body": "x" * (access_request.MAX_ACCESS_REQUEST_BYTES + 1),
        }
        with patch.object(access_request, "upsert_member") as upsert:
            response = access_request.handler(event, None)
        self.assertEqual(response["statusCode"], 413)
        upsert.assert_not_called()

    def test_base64_payload_size_is_measured_after_decoding(self):
        import base64

        # Base64 expansion itself must not trigger the application body limit.
        # This decoded JSON is valid and below the limit, while its encoded form
        # is larger than MAX_ACCESS_REQUEST_BYTES.
        valid = json.dumps({"firstName": "A", "lastName": "B", "phone": "1", "smsOptIn": True, "zipCode": "0", "pad": "x" * 12200}).encode("utf-8")
        self.assertLessEqual(len(valid), access_request.MAX_ACCESS_REQUEST_BYTES)
        self.assertGreater(len(base64.b64encode(valid)), access_request.MAX_ACCESS_REQUEST_BYTES)
        event = {
            "httpMethod": "POST",
            "headers": {},
            "isBase64Encoded": True,
            "body": base64.b64encode(valid).decode("ascii"),
        }
        with patch.object(access_request, "normalize_phone", side_effect=ValueError("bad phone")):
            response = access_request.handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        self.assertNotEqual(json.loads(response["body"]).get("error"), "request body too large")

    def test_public_signup_rejects_oversized_decoded_base64_body(self):
        import base64

        oversized = b"x" * (access_request.MAX_ACCESS_REQUEST_BYTES + 1)
        event = {
            "httpMethod": "POST",
            "headers": {},
            "isBase64Encoded": True,
            "body": base64.b64encode(oversized).decode("ascii"),
        }
        with patch.object(access_request, "upsert_member") as upsert:
            response = access_request.handler(event, None)
        self.assertEqual(response["statusCode"], 413)
        upsert.assert_not_called()


class TestAdditionalKnownDefectContracts(unittest.TestCase):
    def _signup_event(self, extra=None):
        body = {
            "firstName": "Jordan",
            "lastName": "Smith",
            "phone": "5025551212",
            "smsOptIn": True,
        }
        body.update(extra or {})
        return {"httpMethod": "POST", "headers": {}, "body": json.dumps(body)}

    def test_new_public_signup_requires_zip_code(self):
        """New members need an authoritative location input; phone area code is not residence."""
        scan_table = MagicMock(); scan_table.scan.return_value = {"Items": []}
        ddb = MagicMock(); ddb.Table.return_value = scan_table
        with patch.object(access_request.boto3, "resource", return_value=ddb), \
             patch.object(access_request, "upsert_member", return_value={"phone": "+15025551212", "status": "PENDING", "smsOptIn": True}), \
             patch.object(access_request, "get_host_phones", return_value=[]), \
             patch.object(access_request, "maybe_send_welcome"):
            response = access_request.handler(self._signup_event(), None)
        self.assertEqual(response["statusCode"], 400)
        self.assertIn("ZIP", json.loads(response["body"])["error"].upper())

    def test_new_public_signup_rejects_nonexistent_or_invalid_zip(self):
        """ZIP must resolve to a real U.S. postal code before the signup is accepted."""
        scan_table = MagicMock(); scan_table.scan.return_value = {"Items": []}
        ddb = MagicMock(); ddb.Table.return_value = scan_table
        with patch.object(access_request, "resolve_us_zip", side_effect=access_request.InvalidZipError("Enter a valid U.S. ZIP code")), \
             patch.object(access_request, "upsert_member", return_value={"phone": "+15025551212", "status": "PENDING", "smsOptIn": True}), \
             patch.object(access_request, "get_host_phones", return_value=[]), \
             patch.object(access_request, "maybe_send_welcome"):
            response = access_request.handler(self._signup_event({"zipCode": "00000"}), None)
        self.assertEqual(response["statusCode"], 400)
        self.assertIn("ZIP", json.loads(response["body"])["error"].upper())

    def test_confirmed_headcount_includes_confirmed_plus_one(self):
        """Capacity is people, not sponsor rows: one confirmed member with a +1 counts as two."""
        table = MagicMock()
        table.query.return_value = {"Items": [{
            "eventId": "rooftop-sept2026",
            "phone": "+15025551212",
            "status": "CONFIRMED",
            "plusOneName": "Taylor Smith",
        }]}
        with patch("sms_handler._invites_table", return_value=table):
            import sms_handler
            self.assertEqual(sms_handler._get_confirmed_count("rooftop-sept2026"), 2)

    def test_capacity_lookup_failure_does_not_return_zero(self):
        """Unknown capacity state must fail closed rather than look like an empty event."""
        import sms_handler
        table = MagicMock(); table.query.side_effect = RuntimeError("ddb unavailable")
        with patch.object(sms_handler, "_invites_table", return_value=table):
            with self.assertRaises(RuntimeError):
                sms_handler._get_confirmed_count("rooftop-sept2026")

    def test_rsvp_status_transition_is_conditional(self):
        """Duplicate/replayed YES webhooks must not apply CONFIRMED twice."""
        import sms_handler
        invites = MagicMock(); members = MagicMock()
        with patch.object(sms_handler, "_invites_table", return_value=invites), \
             patch.object(sms_handler, "_members_table", return_value=members):
            sms_handler._update_invite_status("rooftop-sept2026", "+15025551212", "CONFIRMED")
        kwargs = invites.update_item.call_args.kwargs
        self.assertIn("ConditionExpression", kwargs)

    def test_admin_status_decision_clears_pending_sms_approval_records(self):
        """A stale Y/N host text must not be able to reverse a newer admin decision."""
        event = {"body": json.dumps({"phone": "5025551212", "status": "DENIED"})}
        approvals = MagicMock()
        approvals.scan.return_value = {
            "Items": [{"hostPhone": "+15025550000", "memberPhone": "+15025551212"}],
        }
        ddb = MagicMock(); ddb.Table.return_value = approvals
        with patch.object(admin_member_routes, "get_member", return_value={"phone": "+15025551212", "status": "PENDING"}), \
             patch.object(admin_member_routes, "set_status"), \
             patch.object(admin_member_routes, "log_action"), \
             patch.object(admin_member_routes.boto3, "resource", return_value=ddb):
            response = admin_member_routes.set_member_status(event, {}, "token")
        self.assertEqual(response["statusCode"], 200)
        self.assertGreater(approvals.delete_item.call_count, 0)

    def test_finalized_event_rejects_later_attendance_mutation(self):
        """Close Event is a lock: attendance cannot change after attendanceFinalized=true."""
        event = {"body": json.dumps({
            "phone": "5025551212",
            "eventId": "rooftop-sept2026",
            "attended": True,
        })}
        event_table = MagicMock()
        event_table.get_item.return_value = {"Item": {"eventId": "rooftop-sept2026", "attendanceFinalized": True}}
        ddb = MagicMock(); ddb.Table.return_value = event_table
        with patch.object(admin_member_routes, "record_attendance", return_value={"ok": True, "result": "ATTENDANCE_OK", "reason": ""}) as record, \
             patch.object(admin_member_routes, "log_action"), \
             patch.object(admin_member_routes.boto3, "resource", return_value=ddb):
            response = admin_member_routes.record_member_attendance(event, {}, "token")
        self.assertEqual(response["statusCode"], 409)
        record.assert_not_called()

    def test_activating_new_live_event_is_one_transaction_with_pointer_guard(self):
        """Previous event, new event, and current pointer must switch atomically."""
        table = MagicMock(); table.name = "rsvp-events-test"
        client = MagicMock()
        new = {
            "eventId": "new-event", "eventSlug": "new-event", "event_status": "LIVE", "active": False,
            "eventZipCode": "40205", "latitude": Decimal("38.2231"), "longitude": Decimal("-85.6836"),
            "promotionRadiusMiles": Decimal("60"),
        }
        with patch.object(admin_event_routes, "_get_event_by_slug", side_effect=[new, {"eventId": "old-event", "event_status": "ARCHIVED", "attendanceFinalized": True}]), \
             patch.object(admin_event_routes, "_current_pointer", return_value={"activeEventSlug": "old-event"}), \
             patch.object(admin_event_routes, "events_table", return_value=table), \
             patch.object(admin_event_routes.boto3, "client", return_value=client):
            result = admin_event_routes.set_active_event_by_slug("new-event")
        tx = client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(tx), 2)
        self.assertEqual(tx[0]["Update"]["Key"], {"eventId": {"S": "new-event"}})
        self.assertIn("activeEventSlug", tx[1]["Put"]["ExpressionAttributeNames"].values())
        self.assertTrue(result["active"])

    def test_pending_count_excludes_logically_expired_pending_members(self):
        """Dashboard Pending total must represent reviewable requests, not expired rows."""
        table = MagicMock()
        table.query.return_value = {"Count": 2, "Items": [
            {"phone": "+15025550001", "status": "PENDING", "pendingExpiresAt": "2000-01-01T00:00:00+00:00"},
            {"phone": "+15025550002", "status": "PENDING", "pendingExpiresAt": "2999-01-01T00:00:00+00:00"},
        ]}
        with patch.object(member_store, "_table", return_value=table):
            self.assertEqual(member_store.count_members_by_status("PENDING"), 1)

    def test_successful_reminder_send_keeps_dedup_claim_if_stamp_write_fails(self):
        """Once Quo accepts a reminder, bookkeeping failure must not reopen it for a duplicate send."""
        invite = {"eventId": "rooftop-sept2026", "phone": "+15025551212", "name": "Jordan", "status": "CONFIRMED"}
        table = MagicMock()
        table.update_item.side_effect = [None, RuntimeError("stamp failed"), None]
        with patch.dict(os.environ, {"SMS_ENABLED": "true"}), \
             patch.object(reminder_handler, "_get_confirmed_invites", return_value=[invite]), \
             patch.object(reminder_handler, "_batch_get_members", return_value={invite["phone"]: {"phone": invite["phone"], "smsOptIn": True, "optOut": False}}), \
             patch.object(reminder_handler, "_invites_table", return_value=table), \
             patch.object(reminder_handler, "send_sms", return_value="quo-1"), \
             patch.object(reminder_handler.time, "sleep"), \
             patch.object(reminder_handler, "log_action"):
            reminder_handler.send_reminders({"eventSlug": "rooftop-sept2026", "day_of_template": "{name}, tonight."}, True)
        clear_calls = [c for c in table.update_item.call_args_list if str(c.kwargs.get("UpdateExpression", "")).startswith("REMOVE")]
        self.assertEqual(clear_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
