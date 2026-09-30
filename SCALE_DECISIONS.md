# Scale Decisions

These are current product and implementation facts, not a certification of future capacity.

## Invite waves and attendance tiers

RSVP Society uses three formal waves: Wave 1, Wave 2 and Wave 3. Further sends are Manual / Resend. Wave 1 selects Tier 1 and sizes its target using the 80% historical attendance rate; when there are no eligible Tier 1 members, it falls back to Tier 2 for a cold start. Wave 2 expands to remaining Tier 1 and Tier 2; Wave 3 may include Tier 3.

Attendance rate remains `attendedCount / (invitedCount - timelyCancellationCount)`. A confirmed RSVP canceled at least 24 hours before local event start is excluded. Later cancellations and no-shows remain in the denominator. Existing tier boundaries are 80% (Tier 1) and 40% (Tier 2); fewer than three countable invitations remains Tier 2 unless overridden.

The shared event RSVP and wave planner use `ceil(capacity / expectedShowRate)` confirmed seats, including plus-ones. Expected show rate defaults to 60% and can be set per event; duplicating an event carries forward the last observed rate. This estimate is not a physical hard cap at venue capacity.

## Sending

Invite workers are capped at 4 recipients per 300-second invocation because they can make three provider attempts with 15-second timeouts and rate-limit backoff. Reminder and confirmed-update workers are capped at 15 recipients because each makes one 15-second-bounded provider request. Each continuation rechecks relevant consent/invite state and continues using the same job. These are timeout-safety ceilings, not Quo throughput benchmarks.

Per-recipient markers reduce duplicate sends, but they cannot make the external SMS request and DynamoDB marker atomic. A crash during an uncertain provider result can require operator reconciliation.

## City operation

Events and member locations use ZIP-derived city/state and coordinates. Automatic invitation audiences use event ZIP and radius. The current event model has one global `current` pointer, and Terraform configures one Quo sender number. This supports the stated plan of at most one event per day, run sequentially across cities. Before switching to the next event, avoid overlapping RSVP windows for the same member or add event-specific reply context: an untagged late `YES` follows the new global pointer and could confirm a new invite for that member.

## Population and cost boundary

The source has no configured 2,500-member ceiling, but no 2,500, 5,000 or 7,500 load run establishes response time or cost. On-demand DynamoDB removes fixed-capacity planning; it does not make scans, page transfer or SMS free. Import/search/finalization limitations are in `SCALE_READINESS.md`.

A credible monthly cost model requires event cadence, unique recipients per city, waves, reminders, average replies, +1 usage and current AWS/Quo billing rates. Do not infer a bill from member count alone.

Auto follow-up waves are capped at 2.5 times event capacity per wave. Manual overrides remain explicit. Event close no longer queries all event invitations to compute an unused observedShowRate.
