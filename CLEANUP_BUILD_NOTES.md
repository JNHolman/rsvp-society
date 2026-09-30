> Historical notes: superseded by CURRENT_AUDIT_UPDATE.md and RELEASE_VERIFICATION.txt for current behavior and test results.

# Strong Cleanup Build Notes

## Changes applied

- Added fail-safe `hidden` attribute to the check-in app container so app markup cannot flash if CSS fails.
- Added Event admin UI fields for backend-supported `parkingInfo` and `privateNotes`.
- Wired `parkingInfo` and `privateNotes` through event form load, clear, and save flows.
- Changed confirmation SMS behavior so venue/address are only included when `revealVenue` is explicitly enabled.
- Added a safer fallback confirmation line when the location has not been released.
- Filled `backend/lambda/requirements.txt` with the test/runtime packages used by the repo.
- Replaced the public `current-event.json` placeholder with a schema-aligned draft event object.

## Validation run locally

- Python compile: passed.
- JavaScript syntax check: passed.
- Route contract audit: passed.
- Runtime integration check: passed.
- Jade behavior audit: passed.
- Jade scenario tests: passed.
- Static unsafe frontend handler scan: no `innerHTML`, inline `onclick`, inline `onchange`, or `eval()` findings.

## Still requires deployed validation

- `smoke_test.py` must be run against the live deployed API with a valid admin token.
- Full `integration_tests.py` requires installing `moto[dynamodb,secretsmanager]`, `boto3`, and `pytest` from `backend/lambda/requirements.txt`.

## Strong Cleanup Patch 2

- Tightened Jade logistics privacy gate: invited-but-unconfirmed members no longer receive venue, address, parking, section, ticket links, description, or Jade notes, even when `revealVenue` is true.
- Guarded missing `template-lock-status` references so Preview Jade and event form loading cannot crash if the status element is absent.
- Updated route contract audit hints to include `parkingInfo` and `privateNotes` in the admin event POST body.



## Public Event Privacy Fix

- Public `/event` no longer returns `venue`, `address`, or `revealVenue` under any condition.
- `revealVenue` remains available for confirmed/private SMS/admin flows only.
- Updated integration test expectation so public discovery cannot leak gated logistics.

## Fixed v3 — SMS Operational Logistics Gate

- Fixed deterministic SMS operational-detail branch so invited/unconfirmed members cannot receive private logistics from parkingInfo, sectionInfo, jadeNotes, ticketUrl, description, or related event fields.
- Added `_get_current_invite_status()` helper scoped to the active event.
- Added regression coverage for:
  - unconfirmed member asking parking/food receives only: `Once you're confirmed, I'll send what you need.`
  - confirmed member asking parking/food can receive event logistics.

Validated locally:
- Python compile: pass
- JS syntax: pass
- Route contract audit: pass
- Runtime integration check: pass
- Jade behavior audit: pass
- Jade scenario tests: pass


## Live Fix Batch

- Deleted/reapplied members now reset to `PENDING` so host approval-code SMS is generated again.
- Reapply clears delete/welcome tombstone fields so approval can send the member welcome once.
- Check-in hidden-app regression fixed by un-hiding the app after admin login.
- Plus-one confirmation copy now uses a separate sentence instead of being appended into the logistics line.
- Event Private Notes removed from the admin event form.
- Event editor is collapsed by default; New Event/Edit opens the builder and save closes/clears it.
- Event cards now expose Delete directly beside Edit/Archive.
- Analytics Refresh now reloads the whole analytics tab and selected event.
- Invite markets no longer infer from area codes; only explicit market/city/state values are used.
- Invite preview auto-checks the generated eligible wave while keeping manual checkbox override.
