import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

import admin_member_routes


class TestCheckinAuditContracts(unittest.TestCase):
    def test_checkin_opens_exactly_three_hours_before_start(self):
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 10, 10, 21, 0, tzinfo=zone)
        end = datetime(2026, 10, 11, 2, 0, tzinfo=zone)
        self.assertFalse(admin_member_routes._checkin_window_open(start - timedelta(hours=3, seconds=1), start, end))
        self.assertTrue(admin_member_routes._checkin_window_open(start - timedelta(hours=3), start, end))
        self.assertTrue(admin_member_routes._checkin_window_open(start - timedelta(minutes=1), start, end))
        self.assertTrue(admin_member_routes._checkin_window_open(start, start, end))

    def test_overnight_checkin_stays_open_until_end(self):
        zone = ZoneInfo("America/New_York")
        start = datetime(2026, 10, 10, 21, 0, tzinfo=zone)
        end = datetime(2026, 10, 11, 2, 0, tzinfo=zone)
        self.assertTrue(admin_member_routes._checkin_window_open(datetime(2026, 10, 11, 1, 59, tzinfo=zone), start, end))
        self.assertTrue(admin_member_routes._checkin_window_open(end, start, end))
        self.assertFalse(admin_member_routes._checkin_window_open(end + timedelta(seconds=1), start, end))

    def test_confirmed_guest_endpoint_uses_persisted_plus_one_membership(self):
        source = Path(admin_member_routes.__file__).read_text()
        self.assertIn('coerce_bool(invite.get("plusOneIsMember"))', source)
        self.assertNotIn("member_name_keys", source)


if __name__ == "__main__":
    unittest.main()
