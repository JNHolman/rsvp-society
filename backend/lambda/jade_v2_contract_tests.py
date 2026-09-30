#!/usr/bin/env python3
"""Jade v2 + wave-math contract tests. New behavior locked by the fix batch.

Dependency-light; no live SMS/DynamoDB. Pins: name-intent guards (casual replies are
not captured as plus-one names), the leak firewall against the collapsed `description`
field, and the wave-math correctness fixes (real show-rate, confirm-rate sample floor).
"""
import os, sys
from pathlib import Path
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("MEMBERS_TABLE_NAME", "rsvp-members-test")
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import sms_handler as sms
import invite_handler as inv

failures = []
def check(name, cond):
    if not cond: failures.append(name)

# ── Phase 1: name-intent — casual/correction/question replies are NOT names ──
for not_a_name in ["ok ok", "yeah sure", "your wrong", "wait what", "idk lol",
                   "stop it", "thanks girl", "who is this", "lol ok"]:
    check(f"not captured as name: {not_a_name!r}", not sms._looks_like_person_name(not_a_name))

# Real names still pass.
for real in ["Mike Johnson", "Raven Gillespie", "Jordan Banks Jr"]:
    check(f"real name accepted: {real!r}", sms._looks_like_person_name(real))

# Correction accusations are detected (not filed as names).
for corr in ["you're hallucinating", "your hallucinating", "that's wrong", "you made that up"]:
    check(f"correction detected: {corr!r}", sms._is_correction_text(corr))

# ── Phase 0: leak firewall against the collapsed description field ──
class _FakeInv:
    def __init__(self, status): self.status = status
    def get_item(self, Key): return {"Item": {"status": self.status}}

def _ctx(status, ev_extra):
    oe, oi, op = sms._get_current_event, sms._invites_table, sms._get_pending_invite
    try:
        sms._get_pending_invite = lambda phone: {"eventId": "leak-test", "phone": phone, "status": status}
        sms._get_current_event = lambda: {
            "eventSlug": "leak-test", "event_status": "LIVE", "event_label": "Night",
            "date": "2099-12-31", "startTime": "21:00", "venue": "Secret Lounge",
            "address": "123 Hidden Way", "ticketUrl": "https://t.example.com",
            "sectionInfo": "VIP only", "parkingInfo": "Out back", **ev_extra,
        }
        sms._invites_table = lambda: _FakeInv(status)
        return sms._build_event_context({"phone": "+15555550123"})
    finally:
        sms._get_current_event, sms._invites_table, sms._get_pending_invite = oe, oi, op

# Unconfirmed (INVITED): description + venue/address private fields stripped,
# but section pricing IS visible (teaser-tier upsell).
ctx_invited = _ctx("INVITED", {"description": "Back room. Side door code 4421."})
for token in ["Secret Lounge", "123 Hidden Way", "Side door", "Out back",
              "t.example.com", "description:", "venue_name:", "address_text:"]:
    check(f"INVITED context strips {token!r}", token not in ctx_invited)
check("INVITED context KEEPS section pricing (teaser)", "VIP" in ctx_invited)

# Legacy jadeNotes-only event: folded into description, still stripped for unconfirmed.
ctx_legacy = _ctx("INVITED", {"jadeNotes": "Side door code 4421."})
check("legacy jadeNotes folded + stripped for unconfirmed", "4421" not in ctx_legacy)

# Confirmed: description is present (facts available).
ctx_conf = _ctx("CONFIRMED", {"description": "Rooftop. Food from Las Mamas."})
check("CONFIRMED context includes description", "Las Mamas" in ctx_conf)

# ── Phase 0.5: wave-math ──
# WAVE-A: real show-rate is used (lower show-rate -> higher target -> more invites).
hi_show = inv._calc_invite_suggestion(100, 0, 50, actual_confirm_rate=0.30, show_rate=0.90, responses=50)
lo_show = inv._calc_invite_suggestion(100, 0, 50, actual_confirm_rate=0.30, show_rate=0.45, responses=50)
check("WAVE-A: lower show-rate raises the total invite estimate", lo_show["uncappedInviteEstimate"] > hi_show["uncappedInviteEstimate"])
check("Auto follow-up waves stay bounded despite a large total estimate", lo_show["suggestedInvites"] <= lo_show["autoWaveLimit"])
check("WAVE-A: show-rate source is actual when provided", hi_show["showRateSource"] == "actual")

# WAVE-B: thin sample ignores the (noisy) actual confirm-rate, uses default.
thin = inv._calc_invite_suggestion(100, 3, 3, actual_confirm_rate=0.015, responses=3)
check("WAVE-B: thin sample falls back to default confirm-rate", thin["confirmRateSource"] == "default")
check("WAVE-B: default confirm-rate is 30%", thin["assumedConfirmRate"] == 30)
big = inv._calc_invite_suggestion(100, 30, 100, actual_confirm_rate=0.50, responses=100)
check("WAVE-B: large sample trusts actual confirm-rate", big["confirmRateSource"] == "actual")

# ── Confirm/decline intent (the deploy bug: natural RSVPs must hit the deterministic
#    branch, never fall through to Claude to be faked) ──
import sms_handler as _sms
for t in ["Yes", "yes!", "Yes I'm in!", "count me in!", "definitely!", "I'll be there!",
          "im in", "sure", "ok", "I'm down"]:
    check(f"confirm detected: {t!r}", _sms._detect_rsvp_intent(t, t.upper().strip()) == "confirm")
for t in ["No", "no!", "nah", "can't make it", "not coming", "I'm out"]:
    check(f"decline detected: {t!r}", _sms._detect_rsvp_intent(t, t.upper().strip()) == "decline")
# Must NOT read questions/logistics/names as an RSVP action.
for t in ["is there parking?", "what time does it start", "Raven Gillespie",
          "where is it", "can I bring 2 people"]:
    check(f"not an RSVP action: {t!r}", _sms._detect_rsvp_intent(t, t.upper().strip()) == "")
# Status questions are answered, not re-confirmed (the loop). Plain confirms still confirm.
for t in ["I'm confirmed", "am I in?", "did I confirm"]:
    check(f"status question: {t!r}", _sms._is_status_question(t.upper().strip()))
for t in ["I'm in", "yes", "sure", "count me in"]:
    check(f"plain confirm is NOT a status question: {t!r}", not _sms._is_status_question(t.upper().strip()))
# Prompt must NOT instruct Claude to perform state transitions.
_p = _sms.JADE_SYSTEM_PROMPT
check("prompt explicitly bans the faked-confirm wording", 'Never say "You\'re in."' in _p)
check("prompt has the state-transition guardrail", "State transitions are NOT yours to perform" in _p)
check("prompt nudges instead of faking", "Is that a yes?" in _p)

# ── Bug A: bare name outside awaiting-context must deflect, never greet ──
import sms_handler as _smsA
def _bare_catch(t):
    n=t.upper().strip()
    return _smsA._is_bare_name_for_catch(t,n) and _smsA._detect_rsvp_intent(t,n)=="" and not _smsA._is_status_question(n)
for t in ["Raven","Ericka Jackson","Raven Gillespie","Mike Smith"]:
    check(f"bare name deflects: {t!r}", _bare_catch(t))
for t in ["wanna hang","Wanna hang?","are you free","Are you single","yes","no","is there parking?","where is it","I am confirmed"]:
    check(f"NOT deflected (rope/q/rsvp): {t!r}", not _bare_catch(t))
check("prompt bans greeting a bare name", "never a person greeting you" in _smsA.JADE_SYSTEM_PROMPT.lower() or "lone name" in _smsA.JADE_SYSTEM_PROMPT.lower())

if failures:
    raise SystemExit("Jade v2 contract tests FAILED:\n- " + "\n- ".join(failures))
print(f"jade v2 contract tests passed ({'all checks'})")
