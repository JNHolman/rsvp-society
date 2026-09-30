# Current source audit — September 30, 2026

This is the current audit record. Older audit/patch notes are historical. The scope is RSVP Society's existing signup, host approval, Jade, invitation waves, plus-ones, check-in and attendance flow: one event a day at most, with 2,500 initial members and 5,000–7,500 across cities later. No new queue, database, identity-verification step or send-stop interface was added.

## Fixed in this pass

| Area | Change |
| --- | --- |
| Auto waves | Wave 1/2 completion schedules the next formal wave after 24–48 hours, shortened only to preserve at least 24 hours before event start. The trigger rechecks the active event, current seat count and eligible audience, then creates a locked preview and idempotent send job. The admin send-status panel reports the scheduled wave or says when manual handling is needed. Wave 3 ends automation. CI checks Python F rules, including undefined names and unused imports/locals. |
| Inbound SMS | STOP runs before receipt storage. Receipt storage failures no longer masquerade as duplicates. In-progress receipts expire after 60 seconds; completed receipts last 30 days. Unexpected processing failures return 503 and release their lease. |
| RSVP versus opt-out | “Cancel my RSVP” cancels attendance and releases its seat through the existing transaction. STOP ALL is recognized. Standard opt-out keywords retain their meaning; SMS opt-out preserves the member profile and existing RSVP. |
| Jade timing | Context uses the event's local date/time, including overnight end times. |
| Jade facts | Food/drink/hookah notes go through Jade's full, privacy-filtered context; isolated words no longer invent availability. “Coffee” no longer triggers the fee reply. Direct AI/bot questions receive truthful prompt guidance. |
| Attendance analytics | Future/unsettled events have zero no-show headcount and Pending show/ghost rates. |
| Event duplication | Keeps the configured expected show rate instead of an unreliable measured rate; still clears operational counters. The duplicate dialog identifies copied details to review. |
| Time zones | Invalid IANA time-zone values are rejected before saving an event or scheduling reminders. |
| Public event route | Retired discovery endpoints retain a compatible empty response and disclose no event details. |
| Admin token | API destination is fixed to the configured origin; stored overrides are ignored, external URLs are rejected and fetch redirects are disabled. |
| Browser framing | Netlify `_headers` supplies response-level framing protection. Ineffective meta framing directives were removed. |
| Runtime safety | Removed the nontransactional check-in test fallback. The unsigned-webhook development flag cannot bypass signatures in AWS Lambda. |
| ZIP lookup | Rejects non-HTTPS/invalid base URLs and nonfinite/out-of-range coordinates. |
| Invite failures | Failed recipient reads abort instead of silently dropping people. Failed jobs emit a monitored log marker. Terraform alarms on a single invite worker error, async drop or recorded job failure; automatic function-error retries are disabled. |
| Cost/read work | Routine job polling reads saved counters instead of rescanning recipients. Current-event invite lookups use direct consistent reads; redundant ticket-reply lookups were removed. |
| Cleanup | Removed unused imports/locals, an unused argument, the obsolete attendance fallback and the stale ignore entry. Preview/+1 wrappers used through dependency injection remain. The empty event directory is excluded from the deploy archive. |
| Privacy wording | Describes stored location, Jade/AI processing, retention and deletion requests, and separates SMS opt-out from RSVP cancellation. This is a factual documentation update, not a legal-compliance certification. |

## Existing behavior preserved and checked

- Website signup/reapplication still requires host approval. No required START/phone-verification step was added. A newer STOP invalidates older pending consent.
- Imports preserve saved locations; new consent requires explicit host attestation. The browser batches large files in groups of 25 rows.
- Tier-based waves, the fresh-city fallback, configured show-rate planning, manual audience overrides and plus-one duplicate checks remain.
- Timely cancellation uses the existing 24-hour cutoff. Check-in and no-show counters remain transactional.
- Nonmember plus-ones retain the star at check-in. Confirmed guests can change their plus-one while the event is live, subject to the existing seat and duplicate checks.
- Unknown/unapproved numbers retain the daily website-reply limit. Private event information stays behind the invitation/confirmation gates.

## Executed verification

The complete shell runner passed using the recovered local test dependencies and Ruff executable:

- 183 characterization/known-defect tests.
- 122 Moto integration tests using the real handlers.
- 15 production-reconciliation tests.
- 23 frontend execution tests.
- Python/JavaScript syntax, Jade scenarios/contracts, route/runtime checks and canonical Lambda imports.

Total: 320 Python tests and 23 frontend tests (343), all passing. All 13 Terraform files parse as HCL. Five handler entrypoints import from the rebuilt Lambda package. Package/source hashes and nested deploy archives are checked during release packaging. Ruff F checks pass, including undefined names and unused imports/locals. Optional coverage reporting was skipped. See RELEASE_VERIFICATION.txt.

## Limits and deployment checks

- The source update is published to GitHub. No AWS deployment, text message or live account change occurred. No live-Lambda diff or production-state reconciliation was performed.
- Native Terraform provider validation/plan, live AWS transactions/IAM, WAF rollout/viewer IPs, GitHub secrets, Quo suppression/reapplication/redelivery and live Jade responses remain deployment checks in VALIDATION.md.
- A processing lease makes interrupted messages retryable; recovery still depends on provider redelivery. A crash after a text is accepted but before its receipt is completed can still produce an ambiguous outcome. Exactly-once outbound SMS is not claimed.
- Invite worker failures must be reviewed against job/provider delivery records before resending. The alert configuration must be applied and SNS subscribers verified to deliver alerts.
- A late untagged YES after switching events cannot identify which party the sender meant. The single active-event model is retained; do not overlap RSVP windows for the same member.
- Configured show rate is a planning assumption, not a hard physical venue-capacity guarantee. Member/analytics scans still grow with the list. Synthetic 7,500-row import coverage is not a live load test or cost forecast.
- No dependency-CVE clearance, universal bug-free claim, legal certification or live browser certification is made. Coverage reporting was skipped because the coverage executable is unavailable.

## Final focused fixes — September 30, 2026

- Auto Waves 2 and 3 use the response-based gap estimate, capped at 2.5 times event capacity per wave. For a 200-seat event with no confirmations, the next Auto wave is at most 500 invitations. Actual confirmations can reduce it to zero. Explicit manual wave sizes still override the limit, and tier order is preserved. This is a conservative pacing choice, not a forecast calibrated from real attendance.
- Cancellation processing remains retryable on a database failure. A separate expiring notice marker in the existing jobs table suppresses repeated failure SMS for the same inbound message. Without a provider message ID, failure notices are limited to one per phone per day. If marker storage is unavailable, the optional failure notice is suppressed; the cancellation still returns 503 for retry. A failed SMS after claiming a notice can leave that notice unsent; exactly-once external delivery is not claimed. IAM permits only the new marker prefix as well as existing receipt/reply prefixes.
- Invalid legacy time zones no longer discard Jade's event context. Known logistics remain available under the same privacy gates, while timing is marked unknown. New saves reject fixed-offset abbreviations such as EST and require a regional zone or UTC. Overnight behavior for correctly configured events remains covered by the existing tests.
- Removed the write-only observedShowRate calculation and its extra event-invite query at close. Individual attendance/tier settlement still runs. Removed three unused state constants. The empty public event endpoint remains a compatibility stub; preview-lock helpers remain wired into send processing.
- Host approval transient lookup failures return 503 and leave their inbound receipt retryable. The previously unrun regression test is now passing under Moto. Two errors in the earlier follow-up test wiring were corrected: the approval test now uses a received-message envelope, and the expired website-reply test retains its own assertions.
- Delivery dashboard totals can still undercount if the event summary write fails after an invite delivery marker succeeds. This is a reporting-only limitation; it does not alter RSVP, capacity or attendance. This focused pass did not change that path.

### Current executed checks

183 characterization/known-defect + 122 Moto integration + 15 reconciliation = 320 Python tests, plus 23 frontend tests (343 total), all pass. Jade scenario/behavior/v2, runtime and route audits pass. Python compilation and JavaScript syntax pass. All 13 Terraform files parse as HCL. Ruff F checks pass. No live AWS, Quo, Netlify, Terraform provider plan/apply or model-response test was performed.

## Pre-automation clean baseline

- A failed or partially completed invite-history/analytics query now propagates its error. It cannot become Wave 1 or a zero-confirmation estimate. Preview fails without locking an audience; the existing worker error handling fails the job before sends. Two failure-path regressions cover partial pagination and aborted preview.
- Removed an overwritten duplicate test helper and unused test imports/variables. Production modules and development tools/tests pass all Ruff F rules; CI now enforces that broader check.
- The full runner passes: 183 characterization/known-defect, 122 Moto integration, 15 reconciliation and 23 frontend tests (343 total), plus Jade/runtime/route, syntax and canonical packaging checks.
- Timed progression is part of this source release. Its one-time schedules and alert configuration are not active until the Terraform changes are planned and applied. Review the Terraform plan and live environment before deployment.
- Passing source checks do not certify the live deployment. The existing live-Lambda comparison, Terraform provider plan and AWS/Quo/Netlify runtime checks remain outstanding.
