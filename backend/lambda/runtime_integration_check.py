#!/usr/bin/env python3
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend' / 'lambda'))

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('MEMBERS_TABLE_NAME', 'rsvp-members')

from admin_shared import coerce_bool

assert coerce_bool(True) is True
assert coerce_bool(False) is False
assert coerce_bool('true') is True
assert coerce_bool('false') is False
assert coerce_bool('0') is False
assert coerce_bool('1') is True

index_text = (ROOT / 'frontend' / 'index.html').read_text()
assert 'firstNameInput' in index_text
assert 'lastNameInput' in index_text
assert 'smsOptIn' in index_text

checkin_text = (ROOT / 'frontend' / 'admin' / 'checkin.js').read_text()
assert 'checkedIn' in checkin_text
assert 'checkedInPlusOnes = new Set();' in checkin_text
assert 'guest-name' in checkin_text
assert 'displayName' in checkin_text
assert 'lastName' in checkin_text
assert "(m.phone || '').includes(q)" in checkin_text
assert "className: 'guest-meta', text: m.phone" not in checkin_text
assert 'function escHtml' not in checkin_text

admin_index_text = (ROOT / 'frontend' / 'admin' / 'index.html').read_text()
assert 'ev-remind-day-before-time' in admin_index_text
assert 'ev-remind-day-of-time' in admin_index_text

invite_text = (ROOT / 'backend' / 'lambda' / 'invite_handler.py').read_text()
assert '"name"' in invite_text

member_routes_text = (ROOT / 'backend' / 'lambda' / 'admin_member_routes.py').read_text()
assert 'status == "APPROVED" and prev_status != "APPROVED"' in member_routes_text
assert 'coerce_bool(data.get("attended", False))' in member_routes_text
assert 'checkin_open = start - timedelta(hours=3)' in member_routes_text
assert 'coerce_bool(invite.get("plusOneIsMember", False))' in member_routes_text
assert 'member_name_keys' not in member_routes_text

admin_app_text = (ROOT / 'frontend' / 'admin' / 'admin-app.js').read_text()
assert "'event-lock-btn'" not in admin_app_text

access_text = (ROOT / 'backend' / 'lambda' / 'access_request.py').read_text()
assert 'MAX_ACCESS_REQUEST_BYTES = 16 * 1024' in access_text
assert 'base64.b64decode(raw_body, validate=True)' in access_text

admin_text = (ROOT / 'backend' / 'lambda' / 'admin_handler.py').read_text()
assert 'ROUTES =' in admin_text
assert 'get_public_event' in admin_text
assert 'record_member_attendance' in admin_text

print('runtime checks passed')
