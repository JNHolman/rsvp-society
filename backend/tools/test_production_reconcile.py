#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import production_reconcile as pr

NOW = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)


def clean_snapshot():
    return {
        "members": [
            {"phone": "+15025551212", "name": "Jordan", "lastName": "Smith", "zipCode": "40205",
             "status": "APPROVED", "smsOptIn": True, "invitedCount": 1, "confirmedCount": 1,
             "attendedCount": 1, "noShowCount": 0},
        ],
        "events": [
            {"eventId": "current", "activeEventSlug": "party", "active": True},
            {"eventId": "party", "event_status": "LIVE", "active": True, "confirmedHeadcount": 2,
             "deliveredCount": 1},
        ],
        "invites": [
            {"eventId": "party", "phone": "+15025551212", "status": "ATTENDED", "attendedAt": "2026-09-28T15:00:00+00:00",
             "plusOneName": "Taylor", "plusOneAttendedAt": "2026-09-28T15:05:00+00:00", "deliveredAt": "2026-09-27T12:00:00+00:00"},
        ],
        "jobs": [],
        "checkins": [
            {"eventId": "party", "phone": "+15025551212", "checkedInAt": "2026-09-28T15:00:00+00:00"},
            {"eventId": "party", "phone": "PLUSONE#+15025551212", "sponsorPhone": "+15025551212", "guestType": "PLUS_ONE", "checkedInAt": "2026-09-28T15:05:00+00:00"},
        ],
        "pending_approvals": [],
    }


class ReconcileContract(unittest.TestCase):
    def test_clean_snapshot_has_no_findings(self):
        report = pr.reconcile(clean_snapshot(), now=NOW)
        self.assertEqual(report["summary"], {"errors": 0, "warnings": 0, "findings": 0, "clean": True})
        self.assertTrue(report["readOnly"])

    def test_detects_sms_consent_conflict_without_exposing_full_phone(self):
        snap = clean_snapshot()
        snap["members"][0]["optOut"] = True
        snap["members"][0]["optOutAt"] = "2026-09-20T12:00:00+00:00"
        report = pr.reconcile(snap, now=NOW)
        finding = next(f for f in report["findings"] if f["code"] == "MEMBER_CONSENT_CONFLICT")
        self.assertNotIn("+15025551212", finding["entity"])
        self.assertTrue(finding["entity"].endswith("...1212"))

    def test_detects_stale_pending_approval(self):
        snap = clean_snapshot()
        snap["pending_approvals"] = [{"hostPhone": "+15025550000", "memberPhone": "+15025551212"}]
        report = pr.reconcile(snap, now=NOW)
        self.assertIn("APPROVAL_STALE", {f["code"] for f in report["findings"]})

    def test_detects_event_pointer_and_multiple_live_drift(self):
        snap = clean_snapshot()
        snap["events"].append({"eventId": "party-2", "event_status": "LIVE", "active": True})
        snap["events"][0]["activeEventSlug"] = "missing"
        codes = {f["code"] for f in pr.reconcile(snap, now=NOW)["findings"]}
        self.assertIn("EVENT_MULTIPLE_LIVE", codes)
        self.assertIn("EVENT_MULTIPLE_ACTIVE", codes)
        self.assertIn("EVENT_POINTER_MISSING_TARGET", codes)

    def test_detects_retired_inviting_state(self):
        snap = clean_snapshot()
        snap["events"][1]["event_status"] = "INVITING"
        codes = {f["code"] for f in pr.reconcile(snap, now=NOW)["findings"]}
        self.assertIn("EVENT_RETIRED_INVITING", codes)

    def test_detects_finalized_unsettled_member_and_plus_one(self):
        snap = clean_snapshot()
        snap["events"][1]["attendanceFinalized"] = True
        snap["events"][1]["confirmedHeadcount"] = 2
        snap["invites"][0].update({"status": "CONFIRMED", "plusOneName": "Taylor"})
        snap["invites"][0].pop("attendedAt", None)
        snap["invites"][0].pop("plusOneAttendedAt", None)
        snap["checkins"] = []
        codes = {f["code"] for f in pr.reconcile(snap, now=NOW)["findings"]}
        self.assertIn("FINALIZED_UNRESOLVED_CONFIRMED", codes)
        self.assertIn("FINALIZED_UNRESOLVED_PLUS_ONE", codes)

    def test_detects_attendance_and_plus_one_contradictions(self):
        snap = clean_snapshot()
        inv = snap["invites"][0]
        inv["status"] = "NO_SHOW"
        inv["noShowAt"] = "2026-09-28T15:00:00+00:00"
        inv["plusOneNoShowAt"] = "2026-09-28T15:06:00+00:00"
        codes = {f["code"] for f in pr.reconcile(snap, now=NOW)["findings"]}
        self.assertIn("NOSHOW_ATTENDANCE_CONTRADICTION", codes)
        self.assertIn("PLUS_ONE_ATTENDANCE_CONTRADICTION", codes)
        self.assertIn("PLUS_ONE_NOSHOW_HAS_CHECKIN", codes)

    def test_detects_stuck_invite_job(self):
        snap = clean_snapshot()
        snap["jobs"] = [{"jobId": "job-123", "status": "PROCESSING", "updatedAt": "2026-09-28T14:00:00+00:00", "invitesWritten": 0, "smsSent": 0}]
        report = pr.reconcile(snap, now=NOW)
        finding = next(f for f in report["findings"] if f["code"] == "INVITE_JOB_STUCK")
        self.assertEqual(finding["details"]["ageMinutes"], 120)

    def test_detects_counter_drift(self):
        snap = clean_snapshot()
        snap["members"][0]["attendedCount"] = 0
        snap["events"][1]["confirmedHeadcount"] = 7
        codes = {f["code"] for f in pr.reconcile(snap, now=NOW)["findings"]}
        self.assertIn("MEMBER_COUNTER_UNDERCOUNT", codes)
        self.assertIn("EVENT_HEADCOUNT_DRIFT", codes)

    def test_deleted_member_is_a_valid_tombstone_status(self):
        snap = clean_snapshot()
        snap["members"][0] = {"phone": "+15025551212", "status": "DELETED"}
        snap["invites"][0]["status"] = "DELETED"
        snap["checkins"] = []
        snap["events"][1]["confirmedHeadcount"] = 0
        snap["events"][1]["deliveredCount"] = 1
        codes = {f["code"] for f in pr.reconcile(snap, now=NOW)["findings"]}
        self.assertNotIn("MEMBER_INVALID_STATUS", codes)

    def test_expired_approval_row_is_stale_even_while_member_is_pending(self):
        snap = clean_snapshot()
        snap["members"][0].update({"status": "PENDING", "pendingExpiresAt": "2026-12-01T00:00:00+00:00"})
        snap["pending_approvals"] = [{
            "hostPhone": "+15025550000", "memberPhone": "+15025551212",
            "expiresAt": int(NOW.timestamp()) - 1,
        }]
        finding = next(f for f in pr.reconcile(snap, now=NOW)["findings"] if f["code"] == "APPROVAL_STALE")
        self.assertTrue(finding["details"]["approvalExpired"])

    def test_lifetime_counter_above_surviving_history_is_not_false_positive(self):
        snap = clean_snapshot()
        snap["members"][0].update({"invitedCount": 20, "confirmedCount": 15, "attendedCount": 10, "noShowCount": 2})
        codes = {f["code"] for f in pr.reconcile(snap, now=NOW)["findings"]}
        self.assertNotIn("MEMBER_COUNTER_UNDERCOUNT", codes)

    def test_old_attendance_does_not_require_expired_90_day_checkin_row(self):
        snap = clean_snapshot()
        snap["invites"][0]["attendedAt"] = "2026-01-01T12:00:00+00:00"
        snap["invites"][0]["plusOneAttendedAt"] = "2026-01-01T12:05:00+00:00"
        snap["checkins"] = []
        codes = {f["code"] for f in pr.reconcile(snap, now=NOW)["findings"]}
        self.assertNotIn("ATTENDED_MISSING_CHECKIN", codes)
        self.assertNotIn("PLUS_ONE_ATTENDED_MISSING_CHECKIN", codes)

    def test_manual_github_workflow_is_read_only_and_does_not_block_by_default(self):
        workflow = (HERE.parents[1] / ".github" / "workflows" / "reconcile-production.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("python backend/tools/production_reconcile.py", workflow)
        self.assertIn("actions/upload-artifact@v7", workflow)
        self.assertIn("default: false", workflow)
        self.assertNotIn("terraform apply", workflow)

    def test_scan_all_paginates_and_never_calls_write_methods(self):
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [{"id": 1}], "LastEvaluatedKey": {"id": 1}},
            {"Items": [{"id": 2}]},
        ]
        self.assertEqual(pr._scan_all(table), [{"id": 1}, {"id": 2}])
        self.assertEqual(table.scan.call_count, 2)
        self.assertTrue(table.scan.call_args_list[0].kwargs["ConsistentRead"])
        table.put_item.assert_not_called()
        table.update_item.assert_not_called()
        table.delete_item.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
