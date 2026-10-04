"""Behavioral regression tests for the operator and Jade workflow revision."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from integration_tests import mock_aws, _create_tables, _reload_lambda_modules, _seed_active_event
import boto3


class TestWaveSendRace(unittest.TestCase):
    def test_manual_and_automatic_wave_share_one_job_claim(self):
        from botocore.exceptions import ClientError
        import invite_handler as inv
        conflict = ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem')
        body = {'confirmSend': True, 'eventId': 'revision', 'waveNumber': 2, 'autoWave': False, 'lockedWave': True, 'phones': ['+15025550101']}
        jobs, aws = Mock(), Mock()
        jobs.get_item.return_value = {'Item': {'status': 'QUEUED', 'recipientCount': 1}}
        with patch.object(inv, '_resolve_active_invitable_event', return_value={'eventSlug': 'revision'}), patch.object(inv, '_validate_initial_invite_text'), patch.object(inv, '_get_next_wave_number', return_value=2), patch.object(inv, '_write_job', side_effect=[None, conflict]) as write, patch.object(inv, '_invite_jobs_table', return_value=jobs), patch.object(inv.boto3, 'client', return_value=aws):
            first = inv.handle_send(body, '', 'token')
            second = inv.handle_send(body, '', 'token')
        self.assertEqual(first['statusCode'], 202)
        self.assertTrue(json.loads(second['body'])['duplicate'])
        self.assertEqual(write.call_args_list[0].args[0], inv._auto_wave_job_id('revision', 2))
        self.assertEqual(write.call_args_list[0].args[0], write.call_args_list[1].args[0])
        aws.invoke.assert_called_once()


class TestEventOperations(unittest.TestCase):
    phone, guest, host = '+15025550101', '+15025550102', '+15025550199'

    def setUp(self):
        aws = mock_aws(); aws.start(); self.addCleanup(aws.stop)
        _create_tables(); _reload_lambda_modules()
        import sms_handler
        self.sms = sms_handler
        db = boto3.resource('dynamodb')
        self.events = db.Table(os.environ['EVENTS_TABLE_NAME'])
        self.invites = db.Table(os.environ['INVITES_TABLE_NAME'])
        self.members = db.Table(os.environ['MEMBERS_TABLE_NAME'])
        date = (datetime.now(timezone.utc) + timedelta(days=10)).strftime('%Y-%m-%d')
        self.event = _seed_active_event('revision', date=date, startTime='19:00', endTime='23:00', event_timezone='UTC', capacity=100, allowPlusOnes=True, venueReleaseMode='confirmation', confirmedHeadcount=1, venue='Test Venue', address='123 Test Street')
        self.members.put_item(Item={'phone': self.phone, 'name': 'Jordan', 'lastName': 'Smith', 'status': 'APPROVED', 'smsOptIn': True})
        self.invites.put_item(Item={'eventId': 'revision', 'phone': self.phone, 'status': 'CONFIRMED', 'awaitingPlusOneName': True})

    def row(self):
        return self.invites.get_item(Key={'eventId': 'revision', 'phone': self.phone}, ConsistentRead=True)['Item']

    def guest_member(self):
        self.members.put_item(Item={'phone': self.guest, 'name': 'Taylor', 'lastName': 'Jones', 'status': 'APPROVED', 'smsOptIn': True})

    def guest_text(self, text, request=None):
        from sms_guest_flow import handle_guest
        return handle_guest(self.phone, text, text.upper(), self.row(), self.event, deps={**self.sms._request_deps(), 'request': request or Mock(), 'set_awaiting': self.sms._set_awaiting_plus_one, 'invites_table': self.sms._invites_table})

    def test_member_guest_is_reserved_against_later_waves(self):
        import invite_handler
        self.guest_member()
        self.assertIn('Got it', self.guest_text('Taylor Jones'))
        self.assertEqual(self.row()['plusOneMemberPhone'], self.guest)
        self.assertIn(self.guest, invite_handler._get_existing_invited_phones('revision'))
        self.assertEqual(self.events.get_item(Key={'eventId': 'revision'})['Item']['confirmedHeadcount'], 2)

    def test_duplicate_check_precedes_late_approval_request(self):
        self.guest_member()
        self.invites.put_item(Item={'eventId': 'revision', 'phone': self.guest, 'status': 'INVITED'})
        request = Mock()
        with patch('sms_guest_flow.cutoff_reached', return_value=True):
            reply = self.guest_text('Taylor Jones', request)
        self.assertIn('already have them', reply)
        request.assert_not_called()
        self.assertNotIn('plusOneName', self.row())

    def test_change_releases_old_member_guest_without_extra_seat(self):
        self.guest_member(); self.guest_text('Taylor Jones')
        self.guest_text("I'm changing my plus 1 to Raven Gillespie")
        self.assertEqual(self.row()['plusOneName'], 'Raven Gillespie')
        self.assertFalse(self.row()['plusOneIsMember'])
        self.assertNotIn('Item', self.invites.get_item(Key={'eventId': 'revision', 'phone': self.guest}))
        self.assertEqual(self.events.get_item(Key={'eventId': 'revision'})['Item']['confirmedHeadcount'], 2)

    def test_no_to_guest_question_keeps_member_confirmed(self):
        self.assertIn('Just you', self.guest_text('No'))
        self.assertEqual(self.row()['status'], 'CONFIRMED')
        self.assertNotIn('awaitingPlusOneName', self.row())

    def test_remove_guest_releases_seat_and_reservation(self):
        self.guest_member(); self.guest_text('Taylor Jones'); self.guest_text('Remove my plus one')
        self.assertNotIn('plusOneName', self.row())
        self.assertNotIn('Item', self.invites.get_item(Key={'eventId': 'revision', 'phone': self.guest}))
        event = self.events.get_item(Key={'eventId': 'revision'})['Item']
        self.assertEqual(event['confirmedHeadcount'], 1)
        self.assertEqual(event['plusOneReservations'], {})

    def test_cancel_releases_member_guest(self):
        self.guest_member(); self.guest_text('Taylor Jones')
        self.assertTrue(self.sms._cancel_confirmed_invite('revision', self.phone))
        self.assertNotIn('Item', self.invites.get_item(Key={'eventId': 'revision', 'phone': self.guest}))
        self.assertEqual(self.events.get_item(Key={'eventId': 'revision'})['Item']['confirmedHeadcount'], 0)

    def test_late_change_keeps_old_name_until_host_decides_once(self):
        from host_requests import queue_request, decide_request
        self.guest_text('Raven Gillespie')
        request = Mock()
        with patch('sms_guest_flow.cutoff_reached', return_value=True):
            self.guest_text('My guest is Ericka Jackson', request)
        request.assert_called_once_with('GUEST_CHANGE', 'Ericka Jackson')
        self.assertEqual(self.row()['plusOneName'], 'Raven Gillespie')
        send = Mock()
        pending = queue_request(self.event, self.phone, 'Jordan Smith', 'GUEST_CHANGE', 'Ericka Jackson', hosts=[self.host], send_sms=send)
        deps = {**self.sms._request_deps(), 'send_sms': send}
        self.assertEqual(decide_request(pending, True, deps=deps), 'Done.')
        self.assertEqual(self.row()['plusOneName'], 'Ericka Jackson')
        calls = send.call_count
        self.assertIn('already', decide_request(pending, True, deps=deps))
        self.assertEqual(send.call_count, calls)

    def test_stale_host_request_does_not_replace_newer_guest(self):
        from host_requests import queue_request, decide_request
        self.guest_text('Raven Gillespie')
        send = Mock()
        pending = queue_request(self.event, self.phone, 'Jordan', 'GUEST_CHANGE', 'Ericka Jackson', hosts=[self.host], send_sms=send, current_guest='Raven Gillespie')
        self.guest_text('My guest is Taylor Jones')
        decide_request(pending, True, deps={**self.sms._request_deps(), 'send_sms': send})
        self.assertEqual(self.row()['plusOneName'], 'Taylor Jones')

    def test_host_response_respects_new_opt_out(self):
        from host_requests import queue_request, decide_request
        send = Mock()
        pending = queue_request(self.event, self.phone, 'Jordan', 'GUEST_CHANGE', 'Ericka Jackson', hosts=[self.host], send_sms=send)
        self.members.update_item(Key={'phone': self.phone}, UpdateExpression='SET optOut = :yes', ExpressionAttributeValues={':yes': True})
        send.reset_mock()
        decide_request(pending, False, deps={**self.sms._request_deps(), 'send_sms': send})
        send.assert_not_called()

    def test_deleted_member_is_not_reintroduced_as_nonmember_guest(self):
        self.guest_member()
        self.members.update_item(Key={'phone': self.guest}, UpdateExpression='SET #s = :deleted', ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':deleted': 'DELETED'})
        self.guest_text('Taylor Jones')
        self.assertNotIn('plusOneName', self.row())

    def test_natural_change_question_requests_name(self):
        self.guest_text('Raven Gillespie')
        self.assertEqual(self.guest_text('Can I change my plus one?'), 'Send their first and last name.')
        self.guest_text('Ericka Jackson')
        self.assertEqual(self.row()['plusOneName'], 'Ericka Jackson')

    def test_failed_lookup_cannot_save_guest(self):
        with patch.object(self.sms, 'search_members', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError): self.guest_text('Taylor Jones')
        self.assertNotIn('plusOneName', self.row())

    def test_csv_no_zip_without_implied_consent(self):
        import admin_member_routes
        result = admin_member_routes.import_members({'body': json.dumps({'members': [{'phone': self.guest, 'name': 'Taylor', 'lastName': 'Jones', 'market': 'Louisville'}]})}, {}, 'token')
        self.assertEqual(json.loads(result['body'])['imported'], 1, result)
        row = self.members.get_item(Key={'phone': self.guest})['Item']
        self.assertEqual(row['market'], 'Louisville')
        self.assertFalse(row['smsOptIn'])

    def test_profile_edit_protects_name(self):
        import admin_member_routes
        def update(fields):
            return admin_member_routes.update_member_profile({'body': json.dumps({'phone': self.phone, **fields})}, {}, 'token')
        self.assertEqual(update({'market': 'Nashville'})['statusCode'], 200)
        self.assertEqual(self.members.get_item(Key={'phone': self.phone})['Item']['market'], 'Nashville')
        self.assertEqual(update({'name': 'Other'})['statusCode'], 400)

    def test_followup_only_once_to_unconfirmed(self):
        from followups import handle
        self.guest_member()
        self.invites.put_item(Item={'eventId': 'revision', 'phone': self.guest, 'status': 'INVITED', 'waveNumber': 1})
        payload = {'eventId': 'revision', 'kind': 'wave', 'identity': '1'}
        send = Mock()
        with patch.dict(os.environ, {'SMS_ENABLED': 'true'}):
            handle(payload, send_sms=send); handle(payload, send_sms=send)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], self.guest)


class TestTiming(unittest.TestCase):
    def test_wave_delays(self):
        from invite_wave_schedule import next_wave_schedule_spec
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        event = {'eventSlug': 'test', 'event_status': 'LIVE', 'date': '2026-10-20', 'startTime': '19:00', 'event_timezone': 'UTC'}
        for wave, hours in ((1, 48), (2, 72)):
            self.assertEqual(next_wave_schedule_spec(event, wave, now=now)['fireAt'], now + timedelta(hours=hours))

    def test_release_48h_then_day_of_without_24h(self):
        from reminder_schedule import desired_schedule_specs
        event = {'eventSlug': 'test', 'event_status': 'LIVE', 'date': '2026-10-20', 'startTime': '15:00', 'event_timezone': 'America/New_York', 'reminderTiming': 'both', 'venueReleaseMode': '48_hours', 'day_of_send_time': '11:00'}
        specs = desired_schedule_specs(event, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual([x['scheduleExpression'] for x in specs], ['at(2026-10-18T15:00:00)', 'at(2026-10-20T11:00:00)'])

    def test_cutoffs_across_dst(self):
        from event_policy import cutoff_reached, event_start
        event = {'date': '2026-11-01', 'startTime': '18:00', 'event_timezone': 'America/New_York'}
        for hours in (12, 24):
            cutoff = event_start(event).astimezone(timezone.utc) - timedelta(hours=hours)
            self.assertTrue(cutoff_reached(event, hours, now=cutoff))
            self.assertFalse(cutoff_reached(event, hours, now=cutoff - timedelta(seconds=1)))

    def test_statements_are_not_guest_names(self):
        from sms_intent import _looks_like_person_name
        for text in ('Terrible day', 'I love you', 'Lawn work', 'Already confirmed', 'Thank you'):
            self.assertFalse(_looks_like_person_name(text), text)
        self.assertTrue(_looks_like_person_name('Ericka Jackson'))

    def test_venue_in_invitation_only_when_selected(self):
        from invite_logic import _build_sms_message
        event = {'event_label': 'The Deep End', 'date': '2026-10-20', 'startTime': '15:00', 'venue': 'Test Venue', 'address': '123 Test Street'}
        self.assertIn('123 Test Street', _build_sms_message({'name': 'Josh'}, {**event, 'venueReleaseMode': 'invite'}))
        self.assertNotIn('123 Test Street', _build_sms_message({'name': 'Josh'}, {**event, 'venueReleaseMode': 'confirmation'}))
