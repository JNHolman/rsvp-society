#!/usr/bin/env python3
import os, sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend' / 'lambda'))

from admin_shared import coerce_bool
from admin_member_routes import handle_confirmed_members  # noqa: F401

assert coerce_bool(True) is True
assert coerce_bool(False) is False
assert coerce_bool('true') is True
assert coerce_bool('false') is False
assert coerce_bool('0') is False
assert coerce_bool('1') is True

# checkin-view.js: must export displayName and handle lastName
view_text = (ROOT / 'frontend' / 'admin' / 'checkin-view.js').read_text()
assert 'displayName' in view_text
assert 'lastName' in view_text

# index.html: intake form has name + phone + smsOptIn fields
# (first/last name split is a planned enhancement — not yet live)
index_text = (ROOT / 'frontend' / 'index.html').read_text()
assert 'nameInput' in index_text
assert 'smsOptIn' in index_text

# invite_handler.py: preview members include at minimum a name field
invite_text = (ROOT / 'backend' / 'lambda' / 'invite_handler.py').read_text()
assert '"name"' in invite_text

# After the module split, welcome guard and attended coerce_bool live in
# admin_member_routes.py, not admin_handler.py.  Both must be present.
member_routes_text = (ROOT / 'backend' / 'lambda' / 'admin_member_routes.py').read_text()
assert 'status == "APPROVED" and prev_status != "APPROVED"' in member_routes_text
assert 'coerce_bool(data.get("attended", False))' in member_routes_text

# admin_handler.py is now a thin dispatcher — it imports from the route modules.
admin_text = (ROOT / 'backend' / 'lambda' / 'admin_handler.py').read_text()
assert 'from admin_member_routes import' in admin_text

print('runtime checks passed')
