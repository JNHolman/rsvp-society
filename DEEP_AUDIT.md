> Historical notes: superseded by CURRENT_AUDIT_UPDATE.md and RELEASE_VERIFICATION.txt for current behavior and test results.

# RSVP Society: code, security, cost and regression audit

Date: 2026-09-29. Scope: the current local source and reviewed release archive.
Baseline archive SHA-256: `ead5bbffbb87ae1353ebf2224dc7a79d1b55a3dfe28ddfd57b62e16a00de71bc`.

Latest changes and evidence: `CURRENT_AUDIT_UPDATE.md`. Treat that file as current for fresh-city Wave 1, expected show rate, headcount drift repair, event closing, webhook deduplication and signature alarms; the findings below preserve the earlier audit snapshot for context.

## Result

This pass found and fixed eight runtime/UI defects and added CI safeguards. It does
not establish production readiness. The existing open issues in AUDIT_FINDINGS.md
and the live checks in VALIDATION.md still apply. Nothing was pushed or deployed.

**Tier-policy update:** tiers still use attendance rate and the existing 80% / 40% thresholds. A confirmed RSVP cancelled at least 24 hours before the event's scheduled local start is excluded from the rate denominator. Late cancellations and no-shows remain in it. Reconfirming removes the cancellation exemption. Fewer than three countable invitations still places a member in Tier 2 unless an admin override applies. The cancellation classification and rate credit are covered by DynamoDB transaction tests; live AWS transaction behavior remains unverified.

## Fixed findings

| ID | Priority | Defect and effect | Fix and evidence |
|---|---|---|---|
| F1 | High | `invite_capacity.transition_confirmed_invite` supplied an unused `:phone` value and an empty attribute-name map when there was no +1. The request was not valid for that branch. | Include only referenced values and omit the optional map. A strict request-shape assertion failed before the fix; a Moto transaction now releases one seat exactly once. |
| F2 | High | In `sms_handler.handler`, +1 name collection ran before RSVP decline handling. A confirmed guest replying NO could be asked for a last name instead of having the RSVP cancelled. | Explicit RSVP actions bypass both name-collection branches; cancellation clears pending guest-name fields. Both pending-name cases failed before the fix and pass now. |
| F3 | Medium | The seat-release helper duplicated the reservation-name normalization with different Unicode behavior. Cancelling a guest named José could leave the original reservation key behind. | Removed the duplicate helper and use `sms_plus_one.plus_one_reservation_key`. An accented-name reservation test failed before the change and now clears the original key. |
| F4 | High | CSV import used unconditional BatchWriteItem puts after checking for existing members. A signup or STOP recorded between the read and write could be completely replaced. | New records use conditional create-only PutItem, with at most 10 concurrent writes through a low-level client. A simulated concurrent STOP previously disappeared; it now survives and the import reports that row as skipped. |
| F5 | High | Import could change a DELETED member back to APPROVED and restore profile/consent fields; existing-row updates also lacked a guard against concurrent deletion or opt-out. | Deleted members are excluded. Existing updates compare the read status, optOut and smsOptIn values atomically. Tests cover existing tombstones, concurrent deletion and concurrent STOP. |
| F6 | Medium | Re-importing an already opted-in member without consent attestation rewrote the consent timestamp and source as if fresh CSV consent had been confirmed. | Imports without attestation preserve the existing consent fields and provenance. A Moto test failed before the fix and preserves the original values afterward. |
| F7 | Medium | Selecting an empty or malformed CSV after a valid file could leave the old rows available to import. | Clear the prior selection and disable Import before parsing the replacement. Frontend regression covers an empty replacement file. |
| F8 | High | After a check-in row expired, an ATTENDED invite could be checked in again and increment the lifetime attendance counter, incorrectly improving the tier. | An already ATTENDED invite cannot transition into ATTENDED again. NO_SHOW-to-ATTENDED corrections remain allowed. The expiry scenario previously succeeded and now returns ALREADY_CHECKED_IN without incrementing the count. |

CI changes: regression tests now run on pull requests to main, in addition to main
pushes and manual runs. Production apply remains manual. Production deployment jobs
are serialized, with cancellation disabled, to avoid interleaving the two-stage WAF
rollout. GitHub token permissions are explicitly limited to contents:read.

## Remaining findings, in priority order

| Priority | Finding and source | Impact / next work |
|---|---|---|
| Release gate | Live code/configuration differs may exist; settlement-epoch backfill was not located in this package. | Compare live Lambda artifacts and configuration before release. Native Terraform validate/plan, AWS IAM/WAF behavior and real Quo delivery remain unverified. |
| High | `sms_handler.handler` has no general inbound message-ID deduplication and returns HTTP 200 after internal failures. Signature timestamps reject old signed requests but do not deduplicate accepted deliveries. | Provider redeliveries can repeat Jade calls and paid SMS; transient database failures can lose an action without provider retry. Design durable receipt/outcome tracking and retry recovery together. Do not simply enable retries without deduplication. |
| High | Sending and saving delivery state are separate operations. Invite reservations or confirmed-update claims can survive a crash before send completion. | Some recipients may remain unresolved; uncertain provider outcomes can also be retried by later operator actions. Add a visible recovery/reconciliation flow. Exactly-once provider delivery was not established. |
| High | Reminder/update workers do not inspect Lambda remaining time. | Fixed safe ceilings now limit invite workers to 4 recipients (three 15-second attempts plus up to 9 seconds of retry backoff) and reminder/update workers to 15 recipients (one 15-second call each) within 300-second workers. This is a conservative timeout bound, not a provider load test; monitor execution duration and throttling in AWS. |
| Resolved locally | Check-in/no-show state and the lifetime member counter previously committed separately. | Attendance/no-show counter updates now join their invite/check-in DynamoDB transactions. Moto verifies successful increments, duplicate suppression and all-or-nothing behavior when the member record is missing. Production IAM and AWS transaction behavior still require validation. |
| Product decision | New members start Tier 2; Wave 1 selects only Tier 1. | A new city can have no Tier 1 members and an empty Wave 1. Set a deliberate first-wave override until attendance history develops. |
| High | RSVP and +1 capacity reservation still use 0.60 while later-wave planning can use observed rates. | Unify the event-level attendance assumption across planning and reservation before trusting capacity predictions. |
| Medium | CSV requests remain synchronous, but the browser now sends 25-row batches and stops on failure. | Provider/DynamoDB delays can still produce an uncertain final batch. Use bounded batches now; implement a resumable import job with progress before relying on large imports. The 100-unique-ZIP limit does not guarantee completion within the request timeout. |
| Medium | `member_store.search_members_page` scans the table until enough matches are found; it can return more than its advertised page size. Analytics pages still fetch and accumulate all pages. | Extra read/transfer work grows with the list and search frequency. Measure at the 2,500-member target before adding infrastructure. |
| High operational | No immediate operator send kill switch, no AWS Budget alert and no invalid-signature log alarm are configured. | Add explicit controls and alerts. A deployment environment flag alone is not an immediate stop mechanism. Threshold and notification recipient require actual deployment inputs. |
| Product/security boundary | Manual lists bypass geography/tier/market. Inbound texts use the active event. Sender number and timezone/city policy remain operator decisions. | Confirm these rules before a city switch; expose the chosen audience/event clearly. See the earlier audit for radius, timezone and privacy-policy work. |

## Scale review: 2,500 first, 5,000–7,500 across 3+ cities

- Core member/event/invite tables use DynamoDB on-demand billing. The source has no hard member-count ceiling at 7,500, but no production load test proves response time, throttling behavior or cost at those populations. Member count alone is not a cost model; SMS sends and event frequency dominate usage.
- SMS volume can exceed the recipient count: every wave, reminder, confirmed-guest update, RSVP reply and +1 logistics message adds provider calls. Invite retry ceilings are now conservative enough for Lambda timeouts, but 7,500-recipient throughput and Quo rate limits remain unmeasured.
- `set_active_admin_event` uses one `eventId="current"` pointer and demotes the previously active event to DRAFT. This fits the stated operating plan of at most one event per day, including sequential city changes. SMS replies and reminders resolve the global pointer, though, so a late, untagged reply after a switch can be applied in the new event context if the member also has an open invite there. Simply switching pointers cannot identify what an untagged reply meant. Avoid overlapping RSVP windows for the same member or add explicit message-to-event context.
- `finalize_event_attendance` reads the event invite set and processes each confirmed member synchronously from an admin API request. A large over-invited event can run past the request window and leave a partial close. Convert this to a resumable background job before relying on large events.
- CSV import is also synchronous. New-row writes are bounded to 10 concurrent conditional puts, but existing-member updates are sequential and ZIP resolution is capped at 100 distinct codes per request. A 7,500-row import is not proven to finish within its request window; it needs a resumable import job or a tested chunked UI workflow.
- Member search scans until it finds matches, and admin analytics/attendance views fetch all pages into browser memory. These paths grow with the total list. They may be acceptable at a few thousand records but have not been benchmarked at 7,500.
- Event RSVP capacity currently permits confirmed headcount up to `ceil(venue capacity / 0.60)` in both member and +1 paths. This is a 1.67x overbooking allowance. If actual attendance exceeds 60%, the venue can be over capacity; the rate is hard-coded for RSVP acceptance even though later-wave planning can use observed show rate. Product policy and event-level safety cap need resolution before high-demand events.
- No dollar forecast is included because the current project archive has no matching AWS usage export, Quo price/usage data, annual event cadence or send-volume assumptions. No additional always-on service has been added by this pass.

This review is static source analysis plus local regression testing. It does not certify multi-city operation, event-finalization reliability, 7,500-member performance or cost.

## Stale/dead code review

- Static import traversal from all five Lambda entrypoints reaches all 26 packaged
  runtime modules. No entire runtime module was demonstrated to be dead. This does
  not prove every branch or helper is used in production.
- The duplicated reservation-key function was a real source of behavior drift;
  it was removed and replaced with the shared implementation.
- Compatibility wrappers and test patch points are referenced by the existing
  suite. Removing them indiscriminately would create regression risk.
- The nontransactional attendance test fallback remains in the runtime source,
  disabled unless RSVP_TEST_DISABLE_DDB_TRANSACTIONS is explicitly true. Native
  transaction behavior must still be validated against AWS; the test escape hatch
  must not be enabled in deployed configuration.
- Historical patch notes remain historical. This report, AUDIT_FINDINGS.md and
  RELEASE_VERIFICATION.txt describe the current local result.

## Security and dependency checks

- Reviewed admin authentication, webhook signature verification, opt-out guards,
  per-function IAM, frontend token handling and dynamic DOM construction.
- A scan for recognizable AWS/GitHub/Anthropic keys and private-key blocks found no
  matches in the reviewed source. This is a limited pattern scan, not proof that
  no secret or personal data exists anywhere in project history.
- ZIP hygiene and canonical Lambda packaging are checked by the release workflow.
- The declared Terraform 1.16.4 release and GitHub actions v7 release pages were
  checked at their official sources. No blanket toolchain upgrade was performed.
- Python requirements use compatible version ranges, and deployed boto3 is supplied
  by Lambda. Local test versions therefore do not establish exact production SDK
  parity. No full dependency advisory scan or production software inventory was
  available; no claim of zero known vulnerabilities is made.

## Cost review

No new AWS service, queue, table or always-on worker was introduced by these fixes.
Conditional imports trade batch HTTP efficiency for protection against replacing
live member records; concurrency is bounded at ten new-member writes. ZIP lookup
is performed once per distinct ZIP within an import. Existing cost risks are
duplicate inbound handling, timeout recovery, repeated scans and bulk page loading.
Cost effectiveness is assessed from operations and failure modes here, not a bill
comparison. A $50,000-versus-$X forecast at 2,500 members cannot be established from
member count alone without event/message volumes and provider usage.

## Verification

See RELEASE_VERIFICATION.txt for the final suite counts. New tests reproduce
concurrent import changes, consent provenance, deleted-member protection,
cancellation while collecting guest names, seat-release request parameters,
accented-name reservation cleanup, stale CSV selection and expired-check-in replay.
The final archive manifest covers the reviewed source and regenerated bundles.
Browser interaction, real SMS, live DynamoDB, Terraform provider execution and
production load/cost tests were not run.

## Official references consulted

- DynamoDB conditional create semantics: https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_PutItem.html
- DynamoDB transaction request contract: https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html
- Terraform release: https://releases.hashicorp.com/terraform/1.16.4/
- Action releases: https://github.com/actions/checkout/releases , https://github.com/actions/setup-python/releases , https://github.com/actions/setup-node/releases
