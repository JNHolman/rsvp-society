# RSVP Society Platform

Latest source fixes: [CURRENT_AUDIT_UPDATE.md](CURRENT_AUDIT_UPDATE.md).

Current continuation verification and remaining live checks: [AUDIT_HANDOFF.md](AUDIT_HANDOFF.md).
Audit findings, scale findings, and pre-release limits: [AUDIT_FINDINGS.md](AUDIT_FINDINGS.md).

RSVP Society is an invite-only event operations platform for curated R&B experiences. It handles member access requests, admin approvals, event setup, SMS invites, RSVP replies, plus-one handling, door check-in, and event analytics.

The platform is intentionally small and operational: static frontend, serverless backend, DynamoDB state, SMS through Quo, and Jade as the SMS concierge.

## Current production rules

- Public signup requires the opt-in checkbox before submission.
- New CSV imports require explicit consent attestation to enable SMS. Host approval alone does not supply missing consent. Existing legacy eligibility remains subject to STOP/opt-out.
- A person who texted STOP can reapply on the website. They return to Pending and remain opted out until host approval of that new request.
- Member gender, tier, and status are edited from the Members page, not the Invite page.
- New public signups require a valid U.S. ZIP code; ZIP resolves canonical city/state and coordinates. Phone area code is never used as residence or market.
- Each Live event requires an Event ZIP and per-event promotion radius. Automatic invite audiences are limited by ZIP-derived distance before optional market/tier/gender/search filters are applied. Legacy members without coordinates remain Location: Unknown and do not qualify for radius-limited audiences.
- The Invite page can additionally filter the radius-qualified pool by market, tier, gender, invite status, and search.
- One event can be Live at a time. Draft and archived events remain private in admin.
- Invite waves are limited to **Wave 1**, **Wave 2**, and **Wave 3**. Anything after that is handled as **Manual / Resend**.
- Invite preview creates a locked preview session. Send uses the locked audience/wave, not a recalculated audience.
- Manual invite override is send-only and must still obey gated-info rules.
- Venue/address/ticket URL/logistics are gated until the member is confirmed.
- Check-in supports confirmed members and confirmed plus-ones.
- Analytics are headcount-based, not just member-count based.

## Repository structure

```text
rsvp-society/
├── frontend/
│   ├── index.html                 # Public signup page
│   ├── terms.html                 # SMS terms / privacy language
│   ├── pics.html                  # Event photo gallery
│   └── admin/
│       ├── index.html             # Admin panel shell
│       ├── checkin.html           # Door check-in page
│       ├── admin-app.js           # Admin bootstrap/routing
│       ├── api.js                 # Shared fetch/API-token layer
│       ├── members.js             # Members workflow
│       ├── event.js               # Event workflow
│       ├── invite.js              # Invite preview/send/waves
│       ├── analytics.js           # Analytics dashboard
│       ├── attendance.js          # Attendance admin view
│       ├── checkin.js             # Door check-in logic
│       ├── base.css               # Shared admin foundation
│       ├── shell.css              # Shared admin layout
│       ├── checkin.css            # Check-in-specific styling
│       ├── event.css
│       ├── invite.css
│       ├── analytics.css
│       └── attendance.css
│
├── backend/
│   ├── lambda/
│   │   ├── access_request.py       # Public signup handler
│   │   ├── sms_handler.py          # Jade + SMS reply state machine
│   │   ├── sms_adapter.py          # Quo outbound SMS adapter
│   │   ├── admin_handler.py        # Admin API router
│   │   ├── admin_event_routes.py   # Event CRUD + analytics
│   │   ├── admin_member_routes.py  # Members, search, attendance, check-in
│   │   ├── invite_handler.py       # Invite preview/send/wave logic
│   │   ├── member_store.py         # DynamoDB member/invite access layer
│   │   ├── reminder_handler.py     # Scheduled reminder sender
│   │   ├── audit_log.py            # Admin action audit log
│   │   ├── integration_tests.py
│   │   ├── route_contract_audit.py
│   │   ├── runtime_integration_check.py
│   │   └── jade_behavior_audit.py
│   └── terraform/
│       ├── main.tf
│       ├── iam_per_function.tf
│       ├── invite_jobs.tf
│       ├── checkins.tf
│       ├── events_endpoint.tf
│       ├── analytics_endpoint.tf
│       ├── confirmed_endpoint.tf
│       ├── eventbridge.tf
│       ├── audit_log.tf
│       ├── cloudfront_api.tf
│       ├── cloudwatch_dashboard.tf
│       └── storage.tf
│
├── JADE.md                         # Jade behavior and data-gating rules
├── VALIDATION.md                   # Terraform/browser/live verification checklist
└── README.md
```

## System architecture

| Layer | Service | Purpose |
|---|---|---|
| Public site | Netlify static hosting | Signup, terms, gallery |
| Admin | Static HTML/CSS/JS | Members, events, invites, analytics, check-in |
| API | API Gateway + CloudFront | Public, admin, SMS webhook routes |
| Compute | AWS Lambda / Python | Signup, Jade, admin API, invites, reminders |
| Database | DynamoDB | Members, events, invites, check-ins, jobs, audit |
| SMS | Quo | Outbound invite/reminder/conversation SMS and webhooks |
| AI | Anthropic Claude | Jade SMS concierge responses |
| Scheduling | EventBridge Scheduler | One-time reminders and invite-wave follow-ups |
| Secrets | AWS Secrets Manager | Admin token, Quo API key, webhook secrets, Anthropic key |
| IaC | Terraform | AWS infrastructure |

## DynamoDB tables

| Table | Key | Purpose |
|---|---|---|
| `rsvp-members` | `phone` | Member records, approval status, gender, tier, opt-in/opt-out state, attendance and timely-cancellation counts |
| `rsvp-event-invites` | `eventId` + `phone` | Per-event invite, RSVP, plus-one, delivery, and attendance state |
| `rsvp-events` | `eventId` | Saved event records plus the Live-event pointer |
| `rsvp-event-history` | `historyPk` + `eventKey` | Event history snapshots |
| `rsvp-checkins` | `eventId` + `phone` | Check-in deduplication records with TTL |
| `rsvp-invite-jobs` | `jobId` | Invite preview locks and async send-job status with TTL |
| `rsvp-audit-log` | `actionId` | Admin audit trail with TTL/PITR |
| `rsvp-pending-approvals` | `hostPhone` + `memberPhone` | Short-lived host approval queue with TTL |

## Core workflows

### 1. Public access request

1. Visitor submits the public form.
2. Opt-in checkbox is required on the frontend.
3. Backend creates or updates a `PENDING` member record.
4. Admin reviews the member.
5. Approved members become eligible for invite waves unless opted out.

CSV imports record consent only when the host explicitly attests it. Import does not clear STOP. After STOP, website reapplication records a new request; host approval is required before texting is enabled again. A newer STOP invalidates that request.

### 2. Member management

Members page supports:

- pending / approved / denied views
- approve and deny actions
- member detail modal
- gender editing
- tier override editing
- search
- backend cursor pagination
- per-event invite/confirmation/attendance history

Denied members remain in DynamoDB for audit history. They should not appear in the normal approved invite pool.

### 3. Event management

Event page supports:

- create event
- save draft
- save and make live
- duplicate event
- archive event
- edit event details
- edit event invite/reminder copy
- edit Event Intelligence

Only one event is Live. Draft events are not public and do not receive SMS behavior until made Live.

### 4. Event Intelligence

The event form has **one** field: **Event Intelligence** (stored as `description`). It is the
single source for both the night's facts and its feel. Structured fields (time, venue,
dresscode, sectionInfo, parkingInfo, ticketUrl) hold their own values; everything else —
food, drinks, hookah, pool rules, cost, and the vibe — goes in this one field. The old
separate "Jade Answers" field is retired; legacy events that still carry it are folded into
Event Intelligence automatically on read.

Write it like the invite you'd actually send: the real details and the feel, in plain language.

```text
Event Intelligence:
Poolside aquatic session built around R&B, water, and the right people. No hookah.
Food by Las Mamas. Signature drink is the RSVP. Cash bar. 21+. Venue drops once you're confirmed.
```

Jade answers closed questions with just the fact (pulled from Event Intelligence or a
structured field), and opens up only for open questions ("what's the vibe?"). If the answer
isn't there, she withholds with intent rather than inventing — she never treats vibe language
as proof of a fact. Private details (venue, address, sections, parking, tickets, and the
Event Intelligence text itself) are stripped from her context in code until a member is
confirmed — the privacy gate does not rely on prompt instructions alone.

### 5. Invite waves

Formal invite waves are limited to:

- Wave 1
- Wave 2
- Wave 3

Anything after Wave 3 is grouped as **Manual / Resend**. The dashboard should not invent Wave 4+.

Invite flow:

1. Admin selects event and filters.
2. Admin clicks Preview Next Wave.
3. Backend calculates the eligible audience, wave number, capacity gap, +1 exposure, and recommendation reason.
4. Backend creates a locked preview session in `rsvp-invite-jobs`.
5. Admin sends the locked preview or a selected subset.
6. Backend sends using the locked session. It does not recalculate the audience during send.

After a formal Wave 1 or Wave 2 send completes, the next wave is scheduled automatically. The system waits 48 hours when the event date allows; otherwise it shortens the wait so the next wave still goes out at least 24 hours before the event. If that minimum window has already passed, no automatic blast is sent and the host must handle it manually. At send time the system rechecks the active event, RSVP/+1 headcount, eligible audience, gender mix, and tier order. A full headcount means no follow-up text. The next wave reuses the first wave's audience filters and gender percentage. The send status shows whether the follow-up was scheduled or needs manual handling. Wave 3 ends automatic progression.

Tier pacing is staged: Wave 1 selects Tier 1 and estimates its invitation target using the 80% attendance cutoff; if the eligible audience has no Tier 1 members, it uses Tier 2 as a cold-start fallback. Wave 2 expands to not-yet-invited Tier 1 and Tier 2 members; Wave 3 can include Tier 3 if capacity still needs guests. Each new preview uses current RSVP/show results, counts confirmed plus-ones as seats, and excludes people already invited to that event. Newer members with fewer than three countable invite results remain Tier 2 until their attendance history is meaningful.

RSVP confirmations, +1 reservations, and wave planning use the event's expected show rate (60% default). An event can set a different rate; duplicating an event keeps its configured estimate, not the measured attendance from the last event. The estimate is not a hard venue-capacity limit.

Auto Waves 2 and 3 cap each send at 2.5× event capacity, while response data can reduce the recommendation to zero. A 200-seat event therefore suggests at most 500 invites per follow-up wave. The raw fill estimate is retained in preview metadata as `uncappedInviteEstimate`; it is not the next-wave send count. Explicit manual wave-size overrides remain available. This conservative pacing limit is not calibrated from production attendance.

The attendance rate is `attendedCount / (invitedCount - timelyCancellationCount)`. A confirmed guest who cancels at least 24 hours before the scheduled event start in the event's time zone is excluded from that rate; exactly 24 hours qualifies. A later cancellation and a no-show remain in the denominator. If a guest cancels on time and then confirms again, that cancellation exemption is removed. The existing tier cutoffs remain 80% for Tier 1 and 40% for Tier 2; explicit admin tier overrides still take precedence.

There is no 12-hour or 24-hour cutoff for changing a +1 while the member is confirmed and the event remains Live. Changes are accepted within 12 hours of start. A swap preserves one occupied guest seat; it does not open a second seat.

The normal flow excludes already-invited members. Review/resend behavior is handled separately.

### 6. Manual invite override

Manual override exists for one-off send copy adjustments. It is not the main event message store.

Rules:

- send-only unless explicitly saved elsewhere
- logs the message version used
- blocks obvious gated info before confirmation
- does not replace Jade's core rules
- does not bypass venue/address/ticket gating

### 7. Jade SMS handling

Jade handles:

- event invites
- YES / NO RSVP replies
- plus-one collection and changes
- duplicate plus-one name detection
- member event questions
- confirmation-gated logistics
- STOP / opt-out
- unknown/unapproved number handling

Full behavior rules are in [`JADE.md`](JADE.md).

### 8. Check-in

Check-in supports confirmed members and confirmed plus-ones.

Rules:

- confirmed-only check-in
- duplicate check-in protection
- member check-in is idempotent
- plus-one check-in is transactional
- check-in rows include TTL
- analytics refresh reflects check-ins
- page shares the admin visual system

### 9. Analytics

Analytics are calculated by backend and rendered by frontend. The frontend should not recalculate stale show-rate math.

Required event totals:

| Metric | Formula |
|---|---|
| Members invited | count of event invite rows sent |
| Members confirmed | count of confirmed invite rows |
| Members attended | count of confirmed members checked in |
| +1 confirmed | count of confirmed plus-one names |
| +1 attended | count of plus-ones checked in |
| +1 no-show | `plus_one_confirmed - plus_one_attended` |
| Expected headcount | `members_confirmed + plus_one_confirmed` |
| Checked-in headcount | `members_attended + plus_one_attended` |
| No-show headcount | `expected_headcount - checked_in_headcount` |
| Show rate | `checked_in_headcount / expected_headcount` |
| Ghost rate | `no_show_headcount / expected_headcount` |

Wave analytics use the same headcount math for Wave 1, Wave 2, Wave 3, and Manual / Resend.

## API endpoints

Base URL: `https://api.rsvpsociety.com`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/access` | public | Public access request |
| `POST` | `/sms/inbound` | Quo signature | Inbound SMS webhook |
| `GET` | `/event` | public | Current Live event |
| `GET` | `/event/current` | public | Current Live event |
| `GET` | `/admin/members` | admin token | List members by status |
| `GET` | `/admin/members/search` | admin token | Search members |
| `POST` | `/admin/members/status` | admin token | Approve/deny/status update |
| `POST` | `/admin/members/gender` | admin token | Edit gender |
| `POST` | `/admin/members/tier` | admin token | Edit tier override |
| `POST` | `/admin/members/attendance` | admin token | Check-in / no-show actions |
| `POST` | `/admin/members/import` | admin token | CSV import |
| `GET` | `/admin/members/confirmed` | admin token | Confirmed list for event/check-in |
| `GET` | `/admin/event` | admin token | Current/selected event |
| `POST` | `/admin/event` | admin token | Save event |
| `GET` | `/admin/event/analytics` | admin token | Analytics snapshot |
| `GET` | `/admin/events` | admin token | List events |
| `POST` | `/admin/events` | admin token | Create/update event |
| `POST` | `/admin/events/set-active` | admin token | Make event Live |
| `POST` | `/admin/events/archive` | admin token | Archive event |
| `POST` | `/admin/events/duplicate` | admin token | Duplicate event |
| `POST` | `/admin/invite/preview` | admin token | Locked invite preview |
| `POST` | `/admin/invite/send` | admin token | Send locked invite batch |
| `POST` | `/admin/invite/reminder` | admin token | Send a regular reminder or queue a custom-message reminder job; queued jobs return a job ID that can be checked at `/admin/invite/status` |
| `GET` | `/health` | public | Basic health check |

## Deployment

### Frontend

The frontend is static. Deploy the `frontend/` folder to Netlify, including `_headers` for response-level framing protection. The admin API origin is fixed in `frontend/admin/constants.js`; localStorage cannot override it.

```bash
# Manual deploy path
frontend/
```

### Backend

Backend Lambda deploys through GitHub Actions on push to `main`. The workflow packages `backend/lambda` and updates the live functions.

### Terraform

Run Terraform from `backend/terraform`:

```bash
terraform init
terraform fmt -recursive
terraform validate
terraform plan -input=false
```

Use the manual GitHub deploy workflow for apply. It deploys the CloudFront
origin header first, waits for propagation, then applies the WAF origin rule.
Follow [`VALIDATION.md`](VALIDATION.md) for first rollout, secret rotation, and
Terraform/browser/SMS checks.

## Local validation

From the repository root:

```bash
pip install -r backend/lambda/requirements-dev.txt
python -m py_compile backend/lambda/*.py
node --check frontend/admin/admin-app.js
node --check frontend/admin/members.js
node --check frontend/admin/event.js
node --check frontend/admin/invite.js
node --check frontend/admin/analytics.js
node --check frontend/admin/checkin.js
python backend/lambda/route_contract_audit.py
python backend/lambda/runtime_integration_check.py
python backend/lambda/jade_behavior_audit.py
```

## Scale decisions

These are current accepted decisions, not hidden bugs:

- RSVP Society uses exactly three formal invite waves.
- Anything after Wave 3 is Manual / Resend.
- Approved-member invite pool is read through the status index so the backend can balance tier/gender and exclude already-invited members.
- Admin partial search remains backend cursor-paginated scan for now. At much larger scale, move search to dedicated normalized lookup records or a search index.
- Public signup opt-in is required; legacy/imported eligible members do not need to re-submit the public form. STOP always wins.

## Security notes

- Admin routes require the admin token.
- The API WAF requires a secret CloudFront origin header; CloudFront strips viewer-supplied `X-Forwarded-For` before the WAF uses it for per-viewer rate limits.
- Secrets live in AWS Secrets Manager.
- Quo inbound webhooks require signature validation.
- Inbound SMS logs redact PII — sender phone as last-4, message as a length, never full content.
- STOP/opt-out blocks future SMS.
- Venue/address/ticket URL are not exposed before confirmation.
- Check-in writes are idempotent/transactional where headcount could drift.
- Jade's logistics privacy gate strips venue/address/sections/parking/tickets/Event Intelligence from the model's context (in code) until a member is confirmed.
- Wave sizing uses the event's real show-rate and confirm-rate, with a minimum-sample floor so thin early data cannot trigger an over-send. Wave 1 and Wave 2 schedule one-time follow-ups after the response window; reminders use their own one-time schedules.

## Project status

This is a locally tested review candidate, not a production certification. Complete [`VALIDATION.md`](VALIDATION.md), including Terraform plan/apply, browser smoke testing, live SMS, DynamoDB state checks, and analytics verification, before calling it production-ready.


### Current audit and verification

Use `CURRENT_AUDIT_UPDATE.md` for the current fixes and decisions, `RELEASE_VERIFICATION.txt` for executed checks, and `VALIDATION.md` for live deployment checks. Older audit notes are historical.

Inbound message receipts use a 60-second processing lease and 30-day completed-message retention. STOP bypasses receipt storage. This is retry recovery, not a guarantee of exactly-once SMS delivery. Invite workers do not automatically replay failed Lambda invocations; their existing job records and CloudWatch alarms support reviewing incomplete sends.
