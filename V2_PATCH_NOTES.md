> Historical notes: superseded by CURRENT_AUDIT_UPDATE.md and RELEASE_VERIFICATION.txt for current behavior and test results.

# RSVP Society — Jade v2 + Fix Batch (patch notes)

All changes verified: every module compiles; 41/41 integration tests pass; 5/5 guardrail +
contract audits pass; Terraform fmt clean. The deterministic safety cage (gates, RSVP state
writes, plus-one dedup, privacy firewall) is unchanged in responsibility — only phrasing,
field plumbing, and the flagged bugs changed.

## Phase 0 — Field collapse (single Event Intelligence source)
- `description` is now the one Event Intelligence field. The duplicate `jadeNotes` write is
  retired in the admin save (`admin_event_routes.py`) and the admin UI (`event.js` saves the
  one box to `description` only; loads from `description` with legacy fallback).
- The Jade context builder and the logistics branch read `description`; legacy `jadeNotes`
  records are folded into `description` automatically on read (nothing lost).
- PRIVACY SYNC: `private_prefixes` keeps `description` (and defensively `jade_notes`) so the
  leak firewall still strips Event Intelligence for unconfirmed members. Proven by
  `jade_v2_contract_tests` (INVITED context contains none of the private fields; legacy
  jadeNotes-only event is also stripped; CONFIRMED sees the facts).

## Phase 0.5 — Wave sizing and RSVP math
- WAVE-A: wave sizing now uses the event's REAL show-rate (`analytics["showRate"]`) instead
  of a frozen 0.60. Lower real show-rate -> more invites, as it should.
- WAVE-B: confirm-rate now has a minimum-sample floor (`CONFIRM_RATE_MIN_SAMPLE = 20`); thin
  early data falls back to the 30% default so a 3/200 sample can't trigger a massive over-send.
- WAVE-C (historical; superseded 2026-09-29): Wave 1 previously used `capacity * 2.5`.
  Wave 1 selects Tier 1 only and sizes invitations from its 80% attendance cutoff.
  Wave 2 expands to remaining Tier 1 and Tier 2; Wave 3 may include Tier 3. Later
  wave math counts confirmed plus-ones as occupied seats and uses observed RSVP data.

## Phase 1 — Name-intent capture
- The plus-one name validator blocklist is hardened: casual/acknowledgment/correction/question
  words ("ok ok", "yeah sure", "your wrong", "idk lol", etc.) are no longer captured as guest
  names. Real names still pass. (Correction accusations were already caught upstream.)

## Phase 2 — Jade voice (cage intact, phrasing rewritten)
- Persona prompt rewritten: match-the-question energy (clipped by default, open up only for
  open questions); banned machine-phrasing ("...posted yet", "I have X down", "the dress code
  for this event is"); deflection = withholding-with-intent, not an empty shrug; the rope
  (warm but never plays girlfriend / fakes availability); `description` named as the single
  fact+feel source.
- Temperature 0.3 -> 0.6 (0.3 was flattening her).
- Removed the ambiguous-mode two-canned-phrase straitjacket.
- Deterministic cage strings voice-rewritten (confirmation, decline, capacity, ticket, plus-one,
  logistics deflections) so the cage no longer sounds robotic.
- NOTE (deferred, by design): fully routing live logistics phrasing through Claude is the
  staged next step to test on your numbers. It is intentionally NOT in this pass because it
  can't be verified without live Claude/DynamoDB/Quo. The cage + privacy gate make it safe to
  flip later; until then logistics answers are deterministic but now on-voice.

## Production bug fixes
- BUG-1 (High): wave send no longer strands a member at INVITED on a non-rate-limit SMS error.
  The for/else was replaced with a post-loop check so both exhausted-retries AND non-retryable
  errors mark the row FAILED (retryable) — the member is recoverable, never silently dropped.
- BUG-2: manual-blast `{name}` replacement no longer crashes on a blank-name member.
- BUG-4: public signup no longer silently drops a legitimate same-name signup within 72h; it
  flags for review and continues. (Same-phone dedupe still handled by upsert.)
- FIX-5: approval codes now use `secrets`, not `random`.

## Test debt repaid (suite is now honest)
- Fixed the test-only double-increment in `_record_attendance_non_transactional_for_tests`
  (counters now count once, matching production).
- Repaired stale fixtures: negative-capacity (missing eventSlug), delete (missing confirmPhone
  safety field), two-signups (missing smsOptIn consent), dual-host (missing dev webhook flag).
- Re-pinned the event-history test to the ACTUAL contract (overwrite-in-place); snapshot-on-save
  is flagged below as an open product decision, not silently built.
- Rewrote `jade_behavior_audit` to pin v2 contracts (field collapse, leak firewall, name guards,
  voice) instead of retired strings. Added `jade_v2_contract_tests` (name-intent, leak firewall,
  wave math) and wired all audits into CI.

## Dead/stale code removed
- `_jade_notes_mentions_topic` (0 callers after field collapse).
- `_lookup_plus_one_is_member` (legacy wrapper, 0 callers).
- The explicit `if False:` dead branch in dual-host cleanup.

## Open product decision (NOT a bug — your call)
- Event history is written only on explicit archive, not on every save. "Keep a snapshot of
  every prior version on each save" was never built. It adds write-amplification on every edit;
  decide if you want it before it's added.

## Still recommended (not in this batch; threshold-triggered)
- Public-endpoint rate limiting / abuse controls — only when signup is publicly linked.
- Manual-blast double-tap guard (client-side confirm) — operational, in the admin UI.
- Scans (dup-name, admin search) — revisit ~10k members, verify via CloudWatch at ~3-4k.

---

# v2.1 — Post-deploy fix round (the confirm-loop cascade)

Live testing exposed a real bug my v2 voice changes amplified. Diagnosed, fixed, re-audited.

## Root cause
Confirm/decline detection was **exact-match only**, so natural RSVPs ("yes!", "Yes I'm in!",
"count me in!", "can't make it") missed the deterministic branch and fell through to Claude.
My v2 prompt then *told Claude how to confirm and capture plus-ones* — so she faked the whole
RSVP in words without ever writing the CONFIRMED status. That single pincer caused: the confirm
loop, plus-one names being greeted instead of captured ("Hey Raven"), logistics stuck on "once
you're confirmed", and the revealVenue checkbox doing nothing (its code lives in the
deterministic confirmation Claude was bypassing).

This was a self-inflicted architecture violation: I had scripted Claude to perform state
transitions she cannot persist. v2.1 restores "code owns state, Claude owns words."

## Fixes
- **Robust RSVP intent detection** (`_detect_rsvp_intent`): punctuation- and phrase-tolerant,
  but conservative (only fires on short, RSVP-shaped messages — never swallows a question,
  logistics, or a name). Confirm and decline branches now use it instead of exact set membership.
- **Smarter confirm handling** (`_is_status_question`): "I'm confirmed" / "am I in?" / "did I
  confirm" are answered as status questions, never re-trigger confirmation. Kills the loop.
- **Prompt de-state-faked**: Claude is now explicitly forbidden from saying "You're in.",
  "Who are you bringing?", confirming, declining, or capturing a guest name. If a near-confirm
  reaches her, she nudges once ("Is that a yes?") instead of faking it.
- **revealVenue fixed for free**: it saves/loads correctly in the frontend; it only "didn't
  work" because Claude was bypassing the deterministic confirmation that reads it. With real
  confirms now hitting that branch, the checkbox works.
- **Plus-one idk line moved into the deterministic handler**: "No rush. Lock it in when you
  know." was coming from Claude's prompt; the cage emitted the robotic "No problem. Send their
  first and last name when you know." Now the cage emits the warm line.
- **Invite generator fixed** (`_build_sms_message`): formats the date ("Sunday May 31") and
  time ("4 PM") instead of raw ISO/24h, and no longer pastes the raw vibe-tag fragment. The
  vibe tag is now a private label/seed, not invite copy.
- **Frontend**: Vibe Tag hint corrected (it no longer claims it "goes in Jade's invite");
  Event Intelligence label/placeholder updated for the single-field model; orphaned hidden
  `ev-description` input removed.

## Tests added
- `jade_v2_contract_tests` now pins: confirm/decline intent across realistic phrasings,
  non-RSVP messages (questions/logistics/names) NOT read as actions, status-question routing,
  and the prompt guardrails (no faked confirms, nudge present). This is the regression net for
  the exact bug that shipped.

## Verify on live re-test
- Confirm with natural phrasing ("yes!", "I'm in!") → one clean "You're in" + venue if revealVenue is checked.
- Say "I'm confirmed" again → status answer, NO loop.
- After confirming, send a guest name → captured (not greeted).
- Toggle revealVenue and re-confirm a test number → venue appears/withholds accordingly.

---

# v2.2 — Attendance finalization + bare-name deflect

## Close Event (new) — attendance finalization
- **Button on the check-in page** (next to Refresh). Confirm dialog shows how many confirmed-but-unchecked guests will be marked no-show, then finalizes. Locked after — re-running is a no-op, no double-count.
- **Route:** `POST /admin/events/finalize-attendance` → `finalize_admin_event_attendance`. Flips `attendanceFinalized` only after a successful pass.
- **Finalize pass** (`finalize_event_attendance` in member_store): every CONFIRMED row with no check-in becomes NO_SHOW via the transactional writer (idempotent — already-ATTENDED/NO_SHOW rows skipped); confirmed plus-ones with no check-in get `plusOneNoShowAt` stamped. Increments `noShowCount` so tiers self-correct on next read.

## B1 — Premature ghost rate (fixed)
- No-show now only counts once attendance is **settled**: Close Event hit, OR event end time + `NO_SHOW_GRACE_HOURS` (4) passed. Confirmed-but-not-checked-in is pending, not a ghost, during the event + grace window.
- **No end time → only an explicit Close Event finalizes.** Auto-grace never fires without an end time, so a misconfigured event can't false-ghost anyone.
- Both member and plus-one no-show gated on `attendance_is_settled` in analytics.

## B2 — Two sources of truth (fixed)
- Root cause was stale member counters: no-shows were never written, so `attendedCount`/`noShowCount` (which tiers, waves, and the member-list attendance filter read) lagged the invite rows. Finalize writes them via the transactional path, reconciling all readers to one settled truth.

## Tiers
- Stay live-computed via `calc_tier` from the now-reliable counters; self-correct on next read after close (next wave build, console load). `tierOverride` remains the manual backup.

## Bug A — Bare-name greeting (fixed)
- A lone name ("Raven", "Ericka Jackson") sent with no awaiting-plus-one context no longer reaches Claude to be greeted ("Hey Raven"). New deterministic catch (`_is_bare_name_for_catch`) deflects: confirmed members get "If that's your plus one, say so and send their first and last name."; unconfirmed get "Lock your own spot first. Reply yes."
- Single-name aware (the worst-case "Raven"), but excludes rope/verb/question/RSVP phrasing so "wanna hang", "are you free", "yes", logistics questions pass through untouched.
- Prompt rule added: never greet a bare name as a person.

## Frontend
- Vibe Tag hint removed entirely (just "Vibe Tag"). This also fixes the row alignment — the two-line label was pushing the select box below its neighbors in the flex-column grid cell.

## Tests
- New `TestFinalizeAttendance` integration test: confirmed-unattended → no-show (member + plus-one), attended guests untouched, counters incremented, re-close locked/no double-count.
- Contract tests for the bare-name catch (deflects names incl. single; passes rope/questions/RSVP).

## Verify on live re-test
- Send a bare name out of flow → deflect, not "Hey [name]".
- Check in some guests, leave others; hit Close Event → unchecked confirmed become no-shows, ghost rate becomes real, next event's Wave A favors attenders.
- Event with no end time: ghost rate stays 0 until you Close Event.

---

# v2.3 — Privacy gate leak + decline-reconfirm + name greeting

Live testing exposed a real velvet-rope leak and a sticky-decline loop. Traced the full
inbound AI flow end-to-end (handler → _build_event_context → Claude call) to confirm the
gate is the ONLY thing feeding event facts to Claude — no second leak vector.

## Bug 1 — Privacy gate leaked date/time/venue to non-members (FIXED)
Root cause: the gate had only TWO tiers. Anyone "not in wave" (uninvited, declined,
no-show, or a plus-one with no invite of their own) got the SAME strip as invited-
unconfirmed members — and that strip never included date_text/time_text/event_label, so
those leaked ("Doors at 4:00 PM" to an uninvited number).

Fix: strict THREE-tier gate.
- Tier 0 — not in the wave: NOTHING. Venue, address, date, time, label, vibe, dress code,
  description all stripped. A plus-one is not a member — they get their own invite or sign
  up at the door. The rope.
- Tier 1 — invited, unconfirmed: teaser only (label, date/time, vibe, dress code).
- Tier 2 — confirmed/attended: full logistics.

## Bug 2 — Declined member couldn't re-confirm (FIXED)
Root cause: the confirm branch used `_get_pending_invite`, which only matches INVITED
rows. A declined member's row is DECLINED, so "I changed my mind / yes" found nothing to
flip, wrote nothing, and Jade looped "you declined this one already."

Fix: new `_get_reconfirmable_invite` finds the current-event row whether INVITED or
DECLINED; the confirm branch flips DECLINED -> CONFIRMED directly (per the chosen behavior:
direct flip, not a fresh confirm flow).

## Bug 3 — Bare name greeted as a person (already fixed in v2.1/v2.2, confirmed)
The "Hey Ericka" greeting was the pre-v2.2 build. The deterministic bare-name catch
(`_is_bare_name_for_catch`) deflects a lone name out of awaiting-context. Confirmed intact
after this round's edits.

## Tests
- `TestPrivacyGateTiers`: behavioral proof across all three tiers — Tier 0 leaks nothing
  (incl. date/time), Tier 1 sees teaser but not venue, Tier 2 sees full.
- `TestDeclineReconfirm`: the reconfirmable lookup finds a DECLINED row, and proves the old
  pending-only lookup missed it.
- Behavior audit updated to assert the three-tier gate.

## Verify on live re-test
- Uninvited / declined / plus-one number asks "when's the event" -> deflect, no time/venue.
- Declined member: "I changed my mind" / "yes" -> actually re-confirmed, no loop.
- Bare name out of flow -> deflect, not "Hey [name]".
