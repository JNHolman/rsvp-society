#!/usr/bin/env python3
"""Small structural checks; behavioral coverage lives in the regression suites."""
from pathlib import Path
from jade_prompt import JADE_SYSTEM_PROMPT

root = Path(__file__).resolve().parents[2]
html = (root / 'frontend/admin/index.html').read_text()
event_js = (root / 'frontend/admin/event.js').read_text()
context = (root / 'backend/lambda/jade_service.py').read_text()

checks = {
    'event notes have one editor': 'ev-jade-notes' in html and 'jadeNotes: (' not in event_js,
    'context gates venue through shared policy': 'venue_available(ev, confirmed=confirmed)' in context,
    'context excludes event facts outside invitation audience': 'if not in_wave:' in context,
    'state claims require persisted results': 'Never claim that a name' in JADE_SYSTEM_PROMPT,
    'off-topic silence has an explicit protocol': '[NO_REPLY]' in JADE_SYSTEM_PROMPT,
    'handoff has an explicit protocol': '[HANDOFF]' in JADE_SYSTEM_PROMPT,
    'feedback has an explicit protocol': '[FEEDBACK]' in JADE_SYSTEM_PROMPT,
    'crisis is not treated as ordinary off-topic chat': '988' in JADE_SYSTEM_PROMPT,
}
failed = [name for name, passed in checks.items() if not passed]
if failed:
    raise SystemExit('Jade structure checks failed: ' + ', '.join(failed))
print('Jade structure checks passed')
