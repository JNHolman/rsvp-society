#!/usr/bin/env python3
"""Static Jade behavior guardrails for RSVP Society (Jade v2).

Pins CONTRACTS, not exact strings: the field-collapse to a single `description`
source, the privacy/leak firewall, the name-capture guards, and the v2 voice rules
(no banned machine-phrasing). Rewritten alongside the v2 refactor — the old asserts
encoded retired behavior (jadeNotes, "has not been posted yet").
"""
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SMS = "\n".join((PROJECT / "backend" / "lambda" / name).read_text() for name in ("sms_handler.py", "jade_prompt.py", "sms_intent.py", "jade_service.py"))
ADMIN_HTML = (PROJECT / "frontend" / "admin" / "index.html").read_text()
ADMIN_JS = (PROJECT / "frontend" / "admin" / "event.js").read_text()
EVENT_ROUTES = (PROJECT / "backend" / "lambda" / "admin_event_routes.py").read_text()

required = {
    # ── Field collapse: description is the single Event Intelligence source ──
    "Event Intelligence box exists in admin HTML": "ev-jade-notes" in ADMIN_HTML,
    "Admin JS saves to description (single source)": "description: ($('ev-jade-notes')" in ADMIN_JS,
    "Admin JS no longer writes duplicate jadeNotes": "jadeNotes: (" not in ADMIN_JS,
    "Event routes persist description": '"description":' in EVENT_ROUTES,
    "Event routes no longer persist duplicate jadeNotes field": '"jadeNotes":' not in EVENT_ROUTES,
    "Jade context emits description, not jade_notes": '("description",    "description"),' in SMS
        and '("jadeNotes",      "jade_notes"),' not in SMS,
    "Legacy jadeNotes folded into description on read": 'ev = {**ev, "description"' in SMS,

    # ── Privacy / leak firewall (must survive the field rename) ──
    "Privacy gate strips by prefix list": "private_prefixes" in SMS,
    "Description is in the private strip list": '"description"' in SMS and "private_prefixes" in SMS,
    "Legacy jade_notes still defensively stripped": '"jade_notes"' in SMS,
    "Tier 0 (not in wave) strips teaser fields too": "teaser_prefixes" in SMS and "if not in_wave:" in SMS,
    "Tier 1 (invited unconfirmed) strips private only": "elif not confirmed:" in SMS,
    "Date/time are gated from non-members": '"date_text"' in SMS and "teaser_prefixes" in SMS,
    "Eligibility gated on confirmed statuses": "LOGISTICS_ELIGIBLE_STATUSES" in SMS,

    # ── Name-capture guards (Phase 1) ──
    "Question detection before plus-one capture": "_is_question_like_text" in SMS,
    "Plus-one question detector uses whole-word tokens":
        "_normalized_tokens(norm) & QUESTION_LIKE_PLUS_ONE_TERMS" in SMS,
    "Likely-name guard before plus-one save": "_looks_like_person_name" in SMS,
    "Single-name token guard exists": "def _looks_like_name_token" in SMS,
    "Name blocklist hardened (rejects casual replies)": '"OK"' in SMS and '"YEAH"' in SMS and '"WRONG"' in SMS,
    "Correction phrases catch hallucination accusations": "YOUR HALLUCINATING" in SMS,

    # ── v2 voice: banned machine-phrasing removed from deterministic replies ──
    "No 'posted yet' in emitted replies":
        'detail_parts.append("' not in SMS or "posted yet\")" not in SMS,
    "No robotic 'I have X down' confirmation": 'f"I have {plus_one_name} down."' not in SMS,
    "Plus-one idk line is the warm withholding version": "No rush. Lock it in when you know." in SMS,
    "Temperature set to 0.6 (not the flattened 0.3)": '"temperature": 0.6,' in SMS,
    "Ambiguous two-canned-phrase straitjacket removed":
        "Respond with exactly one of these two options only" not in SMS,

    # ── Firewall / hard rules still present in the persona prompt ──
    "Prompt forbids inventing facts": "Never invent event details" in SMS,
    "Prompt: venue hidden until confirmed": "Never reveal the venue until a member is confirmed" in SMS,
    "Prompt has the rope / parasocial boundary": "never play girlfriend" in SMS,
    "Prompt: match the question's energy": "match the question's energy" in SMS,
    "Prompt: description is the single fact+feel source": "single source for the night's facts" in SMS,

    # ── Cage invariants ──
    "LIVE only confirmable":
        'CONFIRMABLE_EVENT_STATES = frozenset({"LIVE"})'
        in (PROJECT / "backend" / "lambda" / "member_store.py").read_text(),
    "Time formatting helper exists": "def _display_time" in SMS,
}

failed = [name for name, ok in required.items() if not ok]
if failed:
    raise SystemExit("Jade behavior audit failed:\n- " + "\n- ".join(failed))
print("jade behavior audit passed")
