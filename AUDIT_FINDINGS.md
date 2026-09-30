> Historical notes: superseded by CURRENT_AUDIT_UPDATE.md and RELEASE_VERIFICATION.txt for current behavior and test results.

# RSVP Society audit findings

Audit date: 2026-09-29

## Result

The local release candidate has been improved and retested, but is **not certified bug-free or proven at 2,500–7,500 members**. The one-event-per-day operating plan fits the current global event pointer. `DEEP_AUDIT.md` contains the source review; `SCALE_READINESS.md` gives the scale findings in plain language.

See `CURRENT_AUDIT_UPDATE.md` for the latest reapplication, import, SMS and dead-code fixes.

## Fixed and verified locally

- RSVP cancellations at least 24 hours before event start are excluded from the attendance-rate denominator. Late cancellations and no-shows remain counted. Reconfirmation removes the exemption.
- Check-in/no-show state and the matching tier counter now commit in the same DynamoDB transaction. Moto tests cover duplicate check-ins and missing-member all-or-nothing behavior.
- Invite sends are capped at 4 recipients per Lambda invocation because each may make three Quo attempts with 15-second timeouts. Reminders and confirmed updates are capped at 15 per invocation. Larger environment settings are clamped; continuation paths preserve remaining recipients.
- Previously reviewed fixes remain: atomic RSVP/capacity updates, cancellation seat release, consent-safe imports, CSV ZIP/consent handling, reminder/update idempotency markers, WAF staged deployment, deletion protections, attendance replay guards, and RSVP intent matching.

## Scale limits and unresolved logic

| Priority | Finding | What it means |
|---|---|---|
| Medium | One global active-event pointer and one configured Quo sender ID. | This fits one event per day across cities. After switching events, a late untagged reply can be handled in the new event context if that member has an open invite there; avoid overlapping RSVP windows for the same member or add event-specific reply context. |
| Improved locally | Event finalization now closes in resumable 100-invite pages and locks check-in while closing. | Moto tests cover a multi-page close; production API time limits and recovery still need verification. |
| Improved locally | CSV import now uses sequential 25-row browser batches; direct requests are capped at 100 rows. | Synthetic 7,500-row batching and failure-stop tests pass. Live latency and recovery still need checking; an uncertain batch is never automatically replayed. |
| Improved locally | RSVP, +1 and wave planning now use the event-level expected show rate (60% default); duplicated events carry the last measured rate. | It remains an estimate, not a hard venue cap. Set a rate from local history and verify before a high-demand event. |
| Medium | Inbound messages with provider IDs are claimed once for 30 days. | This suppresses duplicate processing, but a crash after claim and before processing can lose an action; there is no recovery worker. Messages without IDs cannot be deduplicated. |
| High | SMS claims and provider delivery cannot be committed atomically. | A crash around sending may leave an unresolved CLAIMED result. Automatic resend can duplicate a text; current markers choose to surface ambiguity rather than risk a duplicate. |
| Medium | Search scans the member table; analytics and attendance pages fetch all result pages into browser memory. | Work and browser memory grow with the full member list. Benchmark at 2,500 and 7,500 or move to bounded server pagination/search. |
| Release gate | No 2,500/5,000/7,500 load test or live AWS/Quo verification was run. | Moto proves request logic, not real latency, provider rate limits, IAM correctness, WAF behavior or cost. |

## Cost evidence

Core tables use DynamoDB on-demand billing, and this audit adds no always-on compute service or new table. Actual spend depends mainly on messages per event, invitation waves, event count and provider pricing. No dollar forecast is defensible without the matching AWS usage export, Quo message pricing and event/message volumes.

## Release gates still open

- Compare live Lambda code/configuration with this archive; the settlement-epoch backfill is not included here.
- Run native Terraform `validate` and review `plan`; confirm `CLOUDFRONT_ORIGIN_VERIFY_HEADER` is set before deploy.
- Verify AWS IAM transactions, WAF rollout, Quo webhook URL/STOP behavior, and provider SMS rate limits in a safe environment.
- Verify the event-transition procedure: avoid overlapping RSVP windows for the same member, or add event-specific reply context; test late `YES`/`NO` messages from members invited to successive events.
- Exercise import and event-close recovery at target event sizes; load-test the invitation/reminder chain and observe AWS costs.
- Set an AWS Budget after confirming the alert amount against the actual bill; the invalid-signature log alarm is now in Terraform. Verify the admin token without exposing it.

## Verification

Latest local run: 171 characterization/known-defect tests, 102 Moto integration tests, 15 reconciliation-tool tests and 20 frontend execution contracts passed (288 Python tests; 308 total). This is not production or load validation.
