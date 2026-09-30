> Historical notes: superseded by CURRENT_AUDIT_UPDATE.md and RELEASE_VERIFICATION.txt for current behavior and test results.

# Live Path Patch Notes

This patch keeps the latest national/live-path build as the base and applies the remaining code-level fixes before live checks.

## Hardening pass (audit follow-up)

Reliability
- `sms_handler` Lambda timeout raised 15s → 25s, and the Claude/Jade API call capped at 10s (was 15s). Previously a slow model call could consume the entire Lambda budget and the SMS reply would never send. Now a slow call fails fast with budget left to respond.

Privacy / logging
- Inbound SMS logs no longer write full phone numbers or message content. Sender phone is logged as last-4 only, message as a length, and host numbers as a count. (`sms_handler.py`, `admin_member_routes.py` import-error line.)

Jade
- Confirmation copy no longer refers to "Jade" in the third person — Jade speaks in first person ("Location comes once it's released.").
- `_claude()` now sets `temperature: 0.3` for a more consistent persona (was defaulting to 1.0).

Robustness
- `_get_pending_invite` / `_get_confirmed_invite` fall back to `activeEventSlug` when a record lacks `eventSlug`, so legacy events can't silently block confirmations.

Bundle / deploy hygiene
- `jade_behavior_audit.py`, `jade_scenario_tests.py`, and `smoke_test.py` are now excluded from the Lambda bundle in all three places (Terraform `archive_file`, `build_lambda.sh`, `deploy.yml`). They were dead weight in the package.
- Removed dead `_is_host_yn` / `_host_phones` code and its misleading comment about bypassing signature verification.
- Dependencies split: `requirements.txt` (runtime, pinned) vs `requirements-dev.txt` (moto/pytest, pinned, never bundled). CI installs the pinned dev set.
- Documented the `capacity / 0.60` overbooking factor inline.

Tests
- Fixed an invalid test fixture: the `rsvp-event-invites-test` table defined a `jobId-index` GSI without declaring the `jobId` attribute (real DynamoDB and moto 5 both reject this). Now matches the production table.

Validated (this pass)
- Python compile: pass · JS syntax: pass · route contract audit: pass · Jade behavior audit: pass · Jade scenario tests: pass · runtime integration check: pass · `terraform fmt`/`validate`: pass.
- Known pre-existing: 8 `integration_tests` failures remain, unrelated to this pass (confirmed identical on the pre-patch build). They stem from moto 4→5 mock-behavior drift and brittle test expectations, not production regressions. Tracked for a separate moto-5 test-maintenance pass.

## Fixed

- Check-in login now explicitly removes the `hidden` attribute before showing the app container.
- Logout restores the `hidden` attribute intentionally.
- Denied member rows now show a `Pending` action, not `Move to Pending`.
- Invite market filters and table cells display mapped state names for area-code-like market values.
- Jade confirmation message now separates dress/ticket details into their own paragraph so the plus-one prompt does not run into event copy.
- Frontend deploy export no longer excludes active `attendance.js` and `attendance.css` assets.

## Validated

- Python compile: pass
- JS syntax: pass
- Route contract audit: pass
- Runtime integration check: pass
- Jade behavior audit: pass
- Jade scenario tests: pass

## Still requires live checks

- Approval SMS delivery and host approval code
- Quo inbound webhook delivery
- Analytics refresh in browser console
- Attendance count after live check-in
