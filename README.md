# RSVP Society — Invite-Only Event Platform

**Invitation-only R&B experiences. No flyers. No walk-ins.**

This document is the single source of truth for the project. If it ever needs to be rebuilt from scratch — on a new machine, in a new session, or by someone new — everything needed to understand it, recreate it, and continue it is right here. For everything about Jade, see `JADE.md`.

---

## What This Is

RSVP Society is an invitation-only R&B event brand. The website is the front door. The backend is the operation. The AI is the staff.

The system has three jobs:

1. **Collect** — people submit their name and phone number on the website to request access
2. **Filter** — an admin approves or denies members based on vibe, gender ratio, and fit
3. **Operate** — approved members get invited via SMS blast, RSVP YES or NO, and receive event details and reminders from an AI concierge named Jade

Everything runs on AWS. The frontend runs on Netlify. SMS runs through Quo. The AI runs on Anthropic Claude.

---

## Project Structure

```
rsvp-society/
├── frontend/
│   ├── index.html              # Main website + member signup form
│   ├── pics.html               # Photo gallery (post-event, pulls from S3/CloudFront)
│   ├── terms.html              # SMS Terms & Privacy Policy (Quo requirement)
│   └── admin/
│       ├── index.html          # Admin shell (token protected)
│       ├── checkin.html        # Mobile door check-in (tablet, staff use)
│       ├── admin-app.js        # App bootstrap + tab routing
│       ├── api.js              # Shared fetch layer + token/API base handling
│       ├── state.js            # Shared client state
│       ├── constants.js        # Route map + market/area-code constants
│       ├── ui.js               # Reusable DOM helpers
│       ├── members.js          # Members tab
│       ├── event.js            # Event tab
│       ├── invite.js           # Invite tab + reminder blast controls
│       ├── analytics.js        # Analytics tab
│       ├── attendance.js       # Attendance tab
│       ├── checkin.js          # Door check-in logic (shares api.js/state.js)
│       ├── base.css            # Shared admin typography + utility styles
│       ├── shell.css           # Admin shell layout
│       ├── event.css           # Event tab styles
│       ├── invite.css          # Invite tab styles
│       ├── attendance.css      # Attendance/check-in styles
│       └── analytics.css       # Analytics styles
│
├── backend/
│   └── lambda/
│       ├── access_request.py   # Handles form submissions from website
│       ├── sms_handler.py      # Jade AI concierge + YES/NO RSVP + STOP opt-out
│       ├── sms_adapter.py      # Quo outbound SMS adapter (live API send path)
│       ├── admin_handler.py    # Admin API router + public current-event endpoints
│       ├── admin_shared.py     # Shared helpers (CORS, body parsing, event normalization)
│       ├── admin_event_routes.py # Event CRUD, analytics, event history
│       ├── admin_member_routes.py # Member CRUD, import, attendance, search
│       ├── member_store.py     # DynamoDB read/write layer (status-index GSI + scan for search)
│       ├── invite_handler.py   # Invite math, blast preview, send
│       ├── reminder_handler.py # EventBridge reminders + manual trigger
│       ├── audit_log.py        # Immutable admin action audit trail
│       ├── integration_tests.py # Local moto-based integration tests
│       ├── route_contract_audit.py # Frontend/backend/Terraform contract check
│       └── runtime_integration_check.py # Quick structural assertions
│
└── terraform/
    ├── main.tf                 # API Gateway, Lambda, DynamoDB, IAM, CORS, CloudWatch
    ├── eventbridge.tf          # reminder_handler Lambda + hourly reminder rules
    ├── iam_per_function.tf     # Per-function IAM roles (least privilege)
    ├── analytics_endpoint.tf   # Analytics route wiring
    ├── confirmed_endpoint.tf   # Confirmed-members route wiring
    ├── events_endpoint.tf      # Event history route wiring
    ├── checkins.tf             # rsvp-checkins dedup table
    ├── audit_log.tf            # rsvp-audit-log table + IAM write policy
    ├── cloudfront_api.tf       # CloudFront distribution -> api.rsvpsociety.com
    ├── cloudwatch_dashboard.tf # Ops dashboard widgets
    └── storage.tf              # Buckets / storage helpers
```

---

## Tech Stack & Why

### Frontend — Netlify
Static HTML/CSS/JS. No framework. The public site stays simple. The admin is modularized into small Vanilla JS/CSS files with a shared request/config layer (`api.js`, `state.js`, `constants.js`) so routes, token handling, and API base logic stay consistent across tabs and the check-in page. **Manual deploys only** — drag the `frontend/` folder into Netlify dashboard. GitHub auto-deploy is disabled to avoid conflicts with backend CI/CD. Custom domain `rsvpsociety.com` managed via Netlify DNS.

### Backend — AWS Lambda (Python)
Serverless functions. Each Lambda handles one responsibility. No always-on server costs. Scales automatically. Chosen because the event business is bursty — quiet for weeks, then 200 SMS messages go out in an hour.

### Database — AWS DynamoDB
NoSQL. Six tables:
- `rsvp-members` — every person who has ever submitted their number. Has `status-index` GSI.
- `rsvp-event-invites` — who was invited to which event, their RSVP status, and attendance
- `rsvp-events` — current event details (always stored as `eventId: "current"`)
- `rsvp-event-history` — archived snapshots of past events (written on every event save)
- `rsvp-checkins` — per-event check-in deduplication (conditional write guard)
- `rsvp-audit-log` — immutable admin action log, 1-year TTL, PITR enabled

### API — AWS API Gateway + CloudFront
REST API routes requests from the frontend and Quo webhooks to the right Lambda. CloudFront sits in front at `api.rsvpsociety.com` — faster globally, protects against traffic spikes, handles SSL termination.

### SMS — Quo
Carrier-compliant SMS platform. Required for 10DLC registration. Quo webhooks hit the `sms_handler` Lambda when members reply. Compliance form lives at `https://rsvpsociety.com/#access`. Outbound sends use Quo's `POST /v1/messages` API. The Terraform default already points at Jade (`PNqC0tQSaI`). Override `TF_VAR_quo_phone_number_id` only if you change numbers, keep the webhook signing secret in Secrets Manager, and deploy.

### AI Concierge — Anthropic Claude (Jade)
Jade is an SMS-based AI concierge for RSVP Society members. She handles RSVPs, event questions, plus-ones, and reminders entirely over SMS. Built on `claude-haiku-4-5-20251001` with prompt caching (~90% token savings). See `JADE.md` for full documentation.

### Scheduled Reminders — AWS EventBridge
Two hourly EventBridge rules fire the `reminder_handler` Lambda:
- **Day before rule** — checks every hour and sends at **6:00 PM local event time** when the event is tomorrow
- **Day of rule** — checks every hour and sends at **11:00 AM local event time** when the event is today

The Lambda reads the saved `event_timezone`, validates the local event date, checks the local hour, and respects the event's `reminderTiming` setting (`day_before`, `day_of`, `both`, or `manual`) before sending. Manual blast is still available from the Invite tab.

### Photo Storage — AWS S3 + CloudFront
Event photos upload to S3 bucket `rsvp-society-pics-prod`. CloudFront serves them at `https://d31o74npegx00h.cloudfront.net`. Gallery in `pics.html` references this URL. No photos in the git repo ever.

### Secrets — AWS Secrets Manager
Four secrets: admin token, Quo API key, Quo webhook signing secret, and Anthropic API key. Stored in Secrets Manager, not env vars. Lambda calls Secrets Manager at runtime. Rotating a key is a one-step process and nothing is ever in plaintext.

### Infrastructure as Code — Terraform
All AWS infrastructure defined in `.tf` files. Remote state: `s3://rsvp-society-terraform-state/prod/terraform.tfstate`. To recreate: `terraform init && terraform apply`.

### CI/CD — GitHub Actions
Backend deploys automatically on push to `main`. Builds the Lambda bundle and deploys all 5 live functions. Frontend is manual via Netlify.

---

## AWS Infrastructure

### Lambda Functions

| Function | Purpose |
|---|---|
| `rsvp-access-request` | Writes new member to DynamoDB when website form is submitted |
| `rsvp-sms-handler` | Jade AI + YES/NO RSVP handler + STOP opt-out |
| `rsvp-admin-handler` | All admin API endpoints |
| `rsvp-invite-handler` | Invite math, preview, and SMS blast |
| `rsvp-reminder-handler` | EventBridge reminders + manual blast endpoint |

### DynamoDB Tables

| Table | Primary Key | Purpose |
|---|---|---|
| `rsvp-members` | `phone` | All members, status, gender, tier, opt-in. Has `status-index` GSI. |
| `rsvp-event-invites` | `eventId` + `phone` | Per-event invite/RSVP/attendance tracking |
| `rsvp-events` | `eventId` | Current event — always `eventId: "current"` |
| `rsvp-event-history` | `historyPk` + `eventKey` | Archived snapshots of past events — written on every event save |
| `rsvp-checkins` | `eventId` + `phone` | Check-in dedup — conditional write prevents double-count |
| `rsvp-audit-log` | `actionId` (uuid) | Immutable admin action log. TTL 1 year. PITR enabled. |

### Member Record Fields

| Field | Type | Notes |
|---|---|---|
| `phone` | String | E.164 format — partition key |
| `name` | String | First name |
| `lastName` | String | Last name (from CSV import) |
| `email` | String | Optional |
| `tags` | String | From Superphone export (e.g. "Pool Party List") |
| `instagram` | String | Handle without @ |
| `status` | String | PENDING, APPROVED, DENIED — indexed by `status-index` GSI |
| `gender` | String | M, F, O — set by admin |
| `smsOptIn` | Boolean | True = eligible for SMS blasts |
| `optOut` | Boolean | True = texted STOP, never message again |
| `source` | String | web, import |
| `tierOverride` | Number | 1, 2, 3 — overrides auto-calculation |
| `attendedCount` | Number | Events they physically showed up to |
| `invitedCount` | Number | Times they were invited |
| `createdAt` | String | ISO timestamp |
| `lastSeenAt` | String | ISO timestamp |
| `submittedAt` | String | ISO timestamp — updated on every form submission (used by duplicate name guard) |
| `smsOptInAt` | String | ISO timestamp — first opt-in date for TCPA compliance |

### Invite Record Fields

| Field | Type | Notes |
|---|---|---|
| `eventId` | String | Matches the `eventSlug` used when the blast was sent — **not** `"current"` |
| `phone` | String | E.164 |
| `status` | String | INVITED, CONFIRMED, DECLINED, DELETED |
| `gender` | String | Copied from member at blast time |
| `tier` | Number | Copied from member at blast time |
| `invitedAt` | String | ISO timestamp |
| `confirmedAt` | String | ISO timestamp — set on YES reply |
| `declinedAt` | String | ISO timestamp — set on NO reply |
| `attendedAt` | String | ISO timestamp — set when physically checked in |
| `noShowAt` | String | ISO timestamp — set when marked No Show in admin |
| `waveNumber` | Number | Which blast wave this invite came from |
| `plusOneName` | String | Full name of plus-one — set by Jade via SMS |
| `dayBeforeReminderSentAt` | String | Dedup guard — prevents double-sending day-before reminder |
| `dayOfReminderSentAt` | String | Dedup guard — prevents double-sending day-of reminder |
| `quoMessageId` | String | Quo message ID from send response — used to match `message.delivered` webhooks |
| `deliveredAt` | String | ISO timestamp — set when carrier confirms delivery via Quo webhook |

### Event Record Fields

| Field | Type | Notes |
|---|---|---|
| `eventId` | String | Always `"current"` — one active event at a time |
| `eventSlug` | String | Human ID used as the key in invite/checkin queries — e.g. `"2026-03-march"` |
| `date` | String | Stored as a real event date string. Scheduled reminders parse this field. |
| `startTime` | String | e.g. `"9:00 PM"` — used in reminder SMS |
| `endTime` | String | e.g. `"2:00 AM"` — available to Jade if set |
| `city` | String | e.g. `"Louisville, KY"` |
| `event_timezone` | String | IANA timezone name — e.g. `"America/New_York"`, `"America/Chicago"` |
| `venue` | String | Venue name |
| `address` | String | Full address — sent to confirmed members |
| `dresscode` | String | e.g. `"All Black"` |
| `revealVenue` | Boolean | If true, venue name/address appear in confirmation + public current-event response |
| `description` | String | Jade's briefing document — parking, food, drinks, amenities. Write as a fact sheet, not marketing copy. |
| `vibe_tag` | String | Curated vibe tag (e.g. `"suits + shots"`) — appears in invite SMS |
| `event_label` | String | Short event label for SMS (e.g. `"Pool Party"`) |
| `event_type` | String | Internal type — swim, day, rooftop, karaoke, etc. |
| `capacity` | Number | Target headcount — drives wave math and confirmation cap |
| `reminderTiming` | String | `day_before`, `day_of`, `both`, or `manual` |
| `invite_template` | String | Saved Jade invite copy |
| `reminder_template` | String | Legacy shared reminder field kept for backward compatibility |
| `day_before_template` | String | Saved day-before reminder copy |
| `day_of_template` | String | Saved day-of reminder copy |
| `updatedAt` | String | ISO timestamp |
| `lastBlastAt` | String | ISO timestamp — when the last invite blast was sent |
| `lastBlastSmsSent` | Number | SMS messages sent in the last blast |
| `lastBlastFailed` | Number | Failed sends in the last blast |
| `lastBlastWave` | Number | Wave number of the last blast |
| `deliveredCount` | Number | Carrier-confirmed deliveries for the last blast (incremented by `message.delivered` webhook) |

### API Endpoints

Base URL: `https://api.rsvpsociety.com`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/access` | None | Submit access request from website |
| POST | `/sms/inbound` | Quo signature | Inbound SMS from members (Quo webhook target) |
| GET | `/event` | None | Public current-event payload |
| GET | `/event/current` | None | Public current-event payload (explicit path) |
| GET | `/admin/members` | Token | List members by status (uses `status-index` GSI) |
| DELETE | `/admin/members` | Token | Soft-delete a member (wipes PII, tombstones invites) |
| POST | `/admin/members/status` | Token | Set member status |
| POST | `/admin/members/gender` | Token | Set member gender |
| POST | `/admin/members/tier` | Token | Set invite tier override |
| POST | `/admin/members/attendance` | Token | Mark attended / no-show (used by checkin.html and admin panel) |
| POST | `/admin/members/import` | Token | Bulk CSV import |
| GET | `/admin/members/search` | Token | Search by name or phone |
| GET | `/admin/members/confirmed` | Token | List confirmed members for an event (`?eventId=` required) |
| GET | `/admin/event` | Token | Get current event |
| POST | `/admin/event` | Token | Save current event |
| GET | `/admin/event/analytics` | Token | Event analytics snapshot for the current or requested event |
| POST | `/admin/invite/preview` | Token | Preview invite list without sending |
| POST | `/admin/invite/send` | Token | Execute SMS invite blast |
| POST | `/admin/invite/reminder` | Token | Manual reminder blast to confirmed members (`timing=day_before/day_of`) |
| GET | `/health` | None | Liveness check — confirms Lambda runs and DynamoDB is reachable |

---

## Member Lifecycle

```
Website form submit
       ↓
DynamoDB: status = PENDING
Host(s) receive SMS notification: "New request: Name\nY to approve, N to deny"
       ↓
Host texts Y/N → status updated (single-slot per host, last signup wins)
— OR —
Admin reviews in admin panel (Members tab)
       ↓
APPROVED or DENIED
       ↓ (if approved)
Included in next invite blast (Invite tab → Preview → Send)
       ↓
Member receives SMS from Jade: "You're on the list. Reply YES to hold your spot."
       ↓
YES → status = CONFIRMED → gets date/venue confirmation SMS
NO  → status = DECLINED  → skipped this round, eligible next event
Ghost → no reply → skipped, tier score drops over time
       ↓
Day before or day of: EventBridge fires reminder SMS to all CONFIRMED members
       ↓
Night of: door staff uses checkin.html on tablet
       ↓
Attendance recorded → reliability tier recalculated automatically
```

### Reliability Tiers (auto-calculated)

- **Tier 1** — 80%+ show rate. Fills first. Guaranteed spots.
- **Tier 2** — 40–79% show rate. Invited with ghost buffer (default 30% over-invite).
- **Tier 3** — Under 40% show rate. Never auto-invited. Admin can manually override.

Gender ratio and tier math in `invite_handler.py` → `_build_invite_list()`.

---

## The Admin Panel

URL: `https://rsvpsociety.com/admin/`

Login: admin token (stored in Secrets Manager — not in this file).

**Members** — approve, deny, delete, set gender/tier. Search, paginate, bulk CSV import. 50 per page, alphabetical. Import modal supports Superphone and Eventbrite exports. `smsOptIn` in imported CSVs is parsed safely — string values like `"false"` and `"0"` correctly resolve to false. Imported members who are not opted-in are excluded from invite blasts at the query layer.

**Event** — set event name, slug, date, time, city, timezone, venue, address, vibe tag, Jade brief, reveal venue toggle, reminder timing, capacity, and Jade message templates (invite, day-before reminder, day-of reminder). Save overwrites the single current event record in DynamoDB. The `eventSlug` field is critical — it must match the event ID used in the Invite tab. No SMS goes out on save.

**Invite** — auto-populates from saved event. Set capacity, female %, ghost buffer. Preview invite list with state-based market filter pills (all 50 states + DC covered by area code). Remove individuals before sending. Send blast or trigger a **manual reminder blast** — type a free-form message (use `{name}` to personalize), hit Send, and it goes immediately to all confirmed members. Next wave automatically excludes already-invited members — safe to run multiple times for the same event.

**Analytics** — real-time snapshot of the current event: confirmed count, delivered count, attendance rate, wave breakdown, gender split, and tier distribution.

**Attendance** — loads confirmed invitees for a specific event by slug. Mark attended or no-show. "Attended" increments the member's `attendedCount` and writes `attendedAt` to their invite record. "No Show" stamps `noShowAt` only — does not consume the check-in dedup guard. **No Show is blocked if the member is already checked in.**

### Market Filter Pills
Preview groups confirmed members by state from area code. Every US area code is mapped. As the member base grows into new cities the pills appear automatically — no code changes needed.

### Invite Wave Logic
Wave 1 invites at 2.5× capacity. Wave 2+ uses live confirmation rate math to calculate exactly how many more invites are needed. Each wave automatically excludes anyone already in the EventInvites table.

### Confirmation Cap
Once confirmed RSVPs reach `ceil(capacity / 0.60)`, new YES replies get a capacity message. Configurable by updating the event capacity field.

### CSV Import
Supports Superphone and Eventbrite exports. Captures: first name, last name, phone, email, tags, instagram. All imported members set to APPROVED.

---

## The Check-In Page

URL: `https://rsvpsociety.com/admin/checkin.html`

Mobile-first, tablet-optimized page for door staff. Same admin token.

- Token persists for **8 hours** via sessionStorage with expiry
- Loads confirmed guest list scoped to the event slug — door list always matches what was actually invited
- Checked-in state seeds from server on page refresh — refresh mid-event does not wipe green rows
- Live counter: X / Y Checked In with green progress bar
- Full alphabetical list of confirmed members only
- ⭐ next to names of guests who are not yet RSVP Society members
- A–Z quick-jump bar; search filters in real time
- One tap to check in — row turns green, counter updates instantly
- Idempotent — duplicate taps never double-count

---

## EventBridge Reminder Schedule

| Rule | Cron (UTC) | Local send target | Sends if |
|---|---|---|---|
| `rsvp-reminder-day-before` | `cron(0/5 22-23 * * ? *)` | 6:00 PM local event time | Event is tomorrow + `reminderTiming=day_before` or `both` |
| `rsvp-reminder-day-of` | `cron(0/5 15-16 * * ? *)` | 11:00 AM local event time | Event is today + `reminderTiming=day_of` or `both` |

Dedup sentinels on each invite record prevent double-sending even if a rule fires twice.

---

## Deployment

### Backend (automatic)
Push to `main` → GitHub Actions builds the Lambda bundle → deploys all 5 live functions.

### Frontend (manual)
Drag `frontend/` folder into Netlify dashboard.

### Manual Lambda deploy (emergency)
```bash
cd ~/Desktop/rsvp-society/backend/lambda
cp ~/Downloads/sms_handler.py .
zip -j /tmp/sms_handler.zip sms_handler.py member_store.py sms_adapter.py audit_log.py admin_shared.py
aws lambda update-function-code --function-name rsvp-sms-handler --zip-file fileb:///tmp/sms_handler.zip
aws lambda get-function --function-name rsvp-sms-handler --query 'Configuration.LastModified'
```

### Infrastructure changes
```bash
cd backend/terraform
terraform apply
```

### Full rebuild from scratch
```bash
# 1. Infrastructure
cd backend/terraform && terraform init && terraform apply

# 2. Frontend — drag frontend/ to Netlify, point rsvpsociety.com to Netlify DNS

# 3. Quo — register 10DLC, point webhook to api.rsvpsociety.com/sms/inbound
#    Store Quo API key at rsvp/quo-api-key in Secrets Manager
#    Store Quo signing secret at rsvp/webhook-secret in Secrets Manager

# 4. Seed event — Admin panel → Event tab → fill in details, set eventSlug carefully

# 5. Import members — Admin panel → Members → Import CSV
```

---

## Market Expansion

Current: Louisville, KY
Planned: Indianapolis, Cincinnati, Charlotte, Nashville, Atlanta, Houston

Infrastructure supports this natively — member base grouped by state, filter by market pill in invite preview, no code changes needed when expanding.

---

## Gallery (pics.html)

Built and ready. After each event: upload photos to S3 `rsvp-society-pics-prod`, add entry to `EVENT_FOLDERS` and `PHOTO_MANIFEST` in `pics.html`, drag frontend to Netlify.

CloudFront URL: `https://d31o74npegx00h.cloudfront.net`

---

## Costs (Approximate Monthly)

| Service | Estimated Cost | Notes |
|---|---|---|
| AWS Lambda | Free tier | ~1M requests/month included |
| DynamoDB | Free tier | 6 tables, well within free tier limits |
| API Gateway | ~$3.50/million API calls | Negligible at current volume |
| CloudFront (API) | < $1/month | Minimal at current volume |
| S3 + CloudFront (photos) | < $1/month | |
| Secrets Manager | ~$1.60/month | 4 secrets × $0.40/secret/month |
| WAF | ~$8/month | $5 Web ACL + $1/rule × 3 rules |
| CloudWatch Logs | < $1/month | 30-day retention on all 5 Lambda log groups |
| EventBridge | Effectively $0 | 2 scheduled rules |
| Quo SMS | $0.01/segment | Per API send. 160 chars = 1 segment (GSM-7/ASCII). Non-ASCII or emoji drops limit to 70 chars and doubles cost — keep Jade in plain ASCII. Blast of 200 members = ~$2.00. |
| Anthropic Claude | Very low | Haiku with prompt caching — ~90% token savings |
| Netlify | Free tier | |

**Total AWS infrastructure: ~$12/month** at current scale. Quo SMS is the only variable cost — scales with blast size.

---

## Known Gaps / Future Work

- **Video in hero** — replace static hero with looping 5–10 second moody venue clip when available
- **Event photos** — gallery built and ready, waiting on first event
- **`search_members()` scan** — full-table scan for name/phone search. Acceptable now, revisit at 5,000+ members
- **Multi-city simultaneous events** — one active event at a time. Multi-city same-night requires different `eventId` per city — doable but not needed yet
- **Admin auth upgrade** — Cognito + MFA is the long-term play. Not needed at current scale
- **Confirmation cap show-rate** — hardcoded at 60%. Update after 3–5 events if actual show rate diverges
- **Host approval single-slot** — `pending_approval:{host_phone}` is one key per host. Last signup wins if two arrive simultaneously. Acceptable at current volume

---

---

# Security Posture

---

## What Is Built and Live

### 1. Per-Function IAM Roles

Five separate roles, each scoped to only what that function needs.

| Function | Access |
|---|---|
| access_request | members, events (write pending approval) |
| sms_handler | members, invites, events (read/update only) |
| reminder_handler | members (read), invites (query/update), events (read) |
| invite_handler | members, invites, events (no delete, no checkins) |
| admin_handler | All (intentional) |

Legacy `lambda_role` removed from `main.tf`. Migration complete.

### 2. Status-Index GSI
`list_members_by_status()` queries the GSI directly — O(matching items). Full-table scans for status-based queries are eliminated.

### 3. Check-In Dedup
Conditional `put_item` on `rsvp-checkins` — duplicate tap = no counter update, no double-count. 90-day TTL.

### 4. Audit Log
Every admin action: who, what, to whom, when. Immutable. 1-year TTL. PITR enabled.

Actions logged: `MEMBER_APPROVED`, `MEMBER_DENIED`, `MEMBER_RESTORED_PENDING`, `MEMBER_DELETED`, `MEMBER_GENDER_SET`, `MEMBER_TIER_SET`, `ATTENDANCE_RECORDED`, `INVITE_BATCH_SENT`, `REMINDER_BLAST_SENT`, `EVENT_UPDATED`, `MEMBER_IMPORT_COMPLETED`

### 5. WAF Rate Limiting
- `/access` — 500 req/5 min/IP
- `/admin/invite` — 20 req/5 min/IP
- `/admin/members` — 100 req/5 min/IP

### 6. SMS Consent Enforcement
`smsOptIn` and `optOut` enforced at the send layer in both `invite_handler` and `reminder_handler`. STOP wipes name/lastName/email and sets `optOut=true` permanently.

### 7. Input Validation
All phones normalized to E.164. Malformed JSON → 400, not 500. Internal exception details never surfaced to callers. CSV bool parsing safe.

### 8. Webhook Signature Verification
HMAC-SHA256 verified against Quo signing key from Secrets Manager. No secret configured = all requests hard-rejected. CloudFront forwards `openphone-signature` header. Quo timestamps in milliseconds — divided by 1000 before 5-minute age check.

---

## Architectural Notes

**Single static admin token** — one token = full admin access. Long-term fix is Cognito + MFA. Short-term: rotate before major events.

**API Gateway auth is NONE** — auth happens inside Lambda. Acceptable at current scale, fix with Cognito.

**`search_members()` scans** — name/phone search on check-in page is a full-table scan. Fine now, revisit at 5,000+ members.

**eventSlug is the operational key** — invite records stored under `eventSlug`, not `"current"`. Don't change the slug mid-event.

---

## Honest Bottom Line

| Layer | Status |
|---|---|
| Public endpoints | ✓ Clean |
| DynamoDB | ✓ PITR on all 6 tables |
| Secrets management | ✓ Secrets Manager, not env vars |
| Audit trail | ✓ All three handler functions write to audit log |
| Status query scans | ✓ Eliminated — status-index GSI live |
| WAF rate limiting | ✓ 3 rules live |
| Check-in dedup | ✓ Conditional write guard |
| Check-in event scoping | ✓ Queries correct event bucket by slug |
| Check-in state persistence | ✓ Reseeds from server on refresh |
| SMS consent enforcement | ✓ Enforced at send time — both blast and reminder |
| Input validation | ✓ Bad phone/JSON → 400, not 500 |
| Attendance integrity | ✓ No-show blocked if member already checked in |
| Webhook signature verification | ✓ Live and verified in prod |
| Confirmation cap | ✓ YES replies blocked at show-rate-adjusted target |
| Reminder dedup | ✓ Per-invite sentinels prevent double-send |
| IAM least-privilege | ✓ Per-function roles fully deployed |
| Delivery tracking | ✓ `message.delivered` webhook increments `deliveredCount` for blast invites only |
| CI integration tests | ✓ Tests must pass before Lambda ships |
| Admin auth | ⚠ Acceptable. Not elite. One token = full access. |
| `search_members()` scan | ⚠ Acceptable now. Revisit at 5,000+ members. |

---

*Built February 2026. Audited and hardened March 2026. Full audit pass March 10, 2026. Second pass March 11, 2026 — Jade brand identity block, parking/drinks rules, confirmation wording, IGNORE_KEYWORDS expanded, README split into README.md and JADE.md. WAF cost corrected to $8/month. Quo SMS confirmed at $0.01/segment. Live in production.*
