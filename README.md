# RSVP Society — Project Runbook

**Private R&B rooms. Invite only. No flyers.**

This document is the single source of truth for the project. If it ever needs to be rebuilt from scratch — on a new machine, in a new session, or by someone new — everything needed to understand it, recreate it, and continue it is right here. Security posture and hardening notes are at the bottom.

---

## What This Is

RSVP Society is an invitation-only R&B event brand. The website is the front door. The backend is the operation. The AI is the staff.

The system has three jobs:

1. **Collect** — people submit their name and phone number on the website to request access
2. **Filter** — an admin approves or denies members based on vibe, gender ratio, and fit
3. **Operate** — approved members get invited via SMS blast, RSVP YES or NO, and receive event details and reminders from an AI concierge named Jade

Everything runs on AWS. The frontend runs on Netlify. SMS runs through Quo (pending carrier approval). The AI runs on Anthropic Claude.

---

## Project Structure

```
rsvp-society/
├── frontend/
│   ├── index.html              # Main website + member signup form
│   ├── apple-touch-icon.png    # iOS home screen / share icon
│   ├── favicon-32.png          # Browser tab favicon
│   ├── pics.html               # Photo gallery (post-event, pulls from S3/CloudFront)
│   ├── terms.html              # SMS Terms & Privacy Policy (Quo requirement)
│   └── admin/
│       ├── index.html          # Admin panel (token protected)
│       └── checkin.html        # Mobile door check-in (tablet, staff use)
│
├── backend/
│   └── lambda/
│       ├── access_request.py   # Handles form submissions from website
│       ├── sms_handler.py      # Jade AI concierge + YES/NO RSVP + STOP opt-out
│       ├── sms_adapter.py      # SMS provider wrapper (stub — wire HTTP call before go-live)
│       ├── admin_handler.py    # All admin API endpoints
│       ├── member_store.py     # DynamoDB read/write layer (GSI queries)
│       ├── invite_handler.py   # Invite math, blast preview, send
│       ├── reminder_handler.py # EventBridge reminder blasts + manual trigger
│       └── audit_log.py        # Immutable admin action audit trail
│
└── terraform/
    ├── main.tf                 # API Gateway, Lambda, DynamoDB, WAF, IAM, CloudWatch
    ├── iam_per_function.tf     # Per-function IAM roles (least privilege)
    ├── eventbridge.tf          # reminder_handler Lambda + scheduled rules
    ├── checkins.tf             # rsvp-checkins dedup table
    ├── audit_log.tf            # rsvp-audit-log table + IAM write policy
    └── cloudfront_api.tf       # CloudFront distribution → api.rsvpsociety.com
```

---

## Tech Stack & Why

### Frontend — Netlify
Static HTML/CSS/JS. No framework. **Manual deploys only** — drag the `frontend/` folder into Netlify dashboard. GitHub auto-deploy is disabled to avoid conflicts with backend CI/CD. Custom domain `rsvpsociety.com` managed via Netlify DNS.

### Backend — AWS Lambda (Python)
Serverless functions. Each Lambda handles one responsibility. No always-on server costs. Scales automatically. Chosen because the event business is bursty — quiet for weeks, then 200 SMS messages go out in an hour.

### Database — AWS DynamoDB
NoSQL. Five tables:
- `rsvp-members` — every person who has ever submitted their number. Has `status-index` GSI.
- `rsvp-event-invites` — who was invited to which event, their RSVP status, and attendance
- `rsvp-events` — current event details (always stored as `eventId: "current"`)
- `rsvp-checkins` — per-event check-in deduplication (conditional write guard)
- `rsvp-audit-log` — immutable admin action log, 1-year TTL, PITR enabled

### API — AWS API Gateway + CloudFront
REST API routes requests from the frontend and Quo webhooks to the right Lambda. CloudFront sits in front at `api.rsvpsociety.com` — faster globally, protects against traffic spikes, handles SSL termination.

### SMS — Quo
Carrier-compliant SMS platform. Required for 10DLC registration. Quo webhooks hit the `sms_handler` Lambda when members reply. Status: **pending carrier approval**. Compliance form lives at `https://rsvpsociety.com/#access`. Flip `SMS_ENABLED=true` in Lambda env vars once approved. Wire the real HTTP call in `sms_adapter.send_sms()` at the same time.

### AI Concierge — Anthropic Claude (Jade)
Jade is an SMS-based AI assistant for RSVP Society members. She handles RSVPs, event questions, and reminders. Built on `claude-haiku-4-5-20251001` with prompt caching (~90% token savings). Jade only responds to approved, opted-in members.

### Scheduled Reminders — AWS EventBridge
Two daily CloudWatch Event Rules fire the `reminder_handler` Lambda:
- **Day before at 6:00 PM EST** — `cron(0 23 * * ? *)` UTC
- **Day of at 11:00 AM EST** — `cron(0 16 * * ? *)` UTC

Lambda validates the event date and `reminderTiming` field before sending — it will not blast on the wrong day. Manual blast available from the Invite tab.

### Photo Storage — AWS S3 + CloudFront
Event photos upload to S3 bucket `rsvp-society-pics-prod`. CloudFront serves them at `https://d31o74npegx00h.cloudfront.net`. Gallery in `pics.html` references this URL. No photos in the git repo ever.

### Secrets — AWS Secrets Manager
Three secrets: admin token, Quo API key, Anthropic API key. Stored in Secrets Manager, not env vars. Lambda calls Secrets Manager at runtime. Rotating a key is a one-step process and nothing is ever in plaintext.

### Infrastructure as Code — Terraform
All AWS infrastructure defined in `.tf` files. Remote state: `s3://rsvp-society-terraform-state/prod/terraform.tfstate`. To recreate: `terraform init && terraform apply`.

### CI/CD — GitHub Actions
Backend deploys automatically on push to `main`. Builds Lambda zip, deploys all 6 functions. Frontend is manual via Netlify.

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
| `dayBeforeReminderSentAt` | String | Dedup guard — prevents double-sending day-before reminder |
| `dayOfReminderSentAt` | String | Dedup guard — prevents double-sending day-of reminder |

### Event Record Fields

| Field | Type | Notes |
|---|---|---|
| `eventId` | String | Always `"current"` — one active event at a time |
| `eventSlug` | String | Human ID used as the key in invite/checkin queries — e.g. `"2026-03-march"` |
| `date` | String | e.g. "Saturday March 15" |
| `startTime` | String | e.g. "9:00 PM" — used in reminder SMS |
| `city` | String | e.g. "Louisville, KY" |
| `venue` | String | Venue name |
| `address` | String | Full address — sent to confirmed members |
| `dresscode` | String | e.g. "All Black" |
| `revealVenue` | Boolean | If true, venue name appears in confirmation SMS |
| `description` | String | Brief for Jade — context she uses when building messages |
| `vibe_tag` | String | Curated vibe tag (e.g. "suits + shots") — appears in invite SMS |
| `event_label` | String | Short event label for SMS (e.g. "Pool Party") |
| `event_type` | String | Internal type — swim_party, rooftop, etc. |
| `capacity` | Number | Target headcount — drives wave math and confirmation cap |
| `reminderTiming` | String | `day_before`, `day_of`, or `manual` |
| `revealVenue` | Boolean | Controls whether venue/address appear in confirmation reply |
| `updatedAt` | String | ISO timestamp |

### API Endpoints

Base URL: `https://api.rsvpsociety.com`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/access` | None | Submit access request from website |
| POST | `/sms` | Quo signature | Inbound SMS from members |
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
| POST | `/admin/invite/preview` | Token | Preview invite list without sending |
| POST | `/admin/invite/send` | Token | Execute SMS invite blast |
| POST | `/admin/invite/reminder` | Token | Manual reminder blast to confirmed members |

---

## Member Lifecycle

```
Website form submit
       ↓
DynamoDB: status = PENDING
       ↓
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

**Event** — set event name, slug, date, time, venue, address, vibe tag, Jade brief, reveal venue toggle, reminder timing, capacity. Save overwrites the single current event record in DynamoDB. The `eventSlug` field is critical — it must match the event ID used in the Invite tab. No SMS goes out on save.

**Invite** — auto-populates from saved event. Set capacity, female %, ghost buffer. Preview invite list with state-based market filter pills (all 50 states + DC covered by area code). Remove individuals before sending. Send blast or trigger manual reminder blast. Next wave automatically excludes already-invited members — safe to run multiple times for the same event.

**Attendance** — loads confirmed invitees for a specific event by slug. Mark attended or no-show. "Attended" increments the member's `attendedCount` and writes `attendedAt` to their invite record. "No Show" stamps `noShowAt` only — does not consume the check-in dedup guard and does not touch counters, so the person can still be checked in at the door if they show up late.

### Market Filter Pills
Preview groups confirmed members by state from area code. Every US area code is mapped. Unknown area codes show as the raw area code. As the member base grows into new cities the pills appear automatically — no code changes needed.

### Invite Wave Logic
The system supports multiple invite waves for the same event. Each blast automatically excludes anyone already in the EventInvites table for that event (confirmed, declined, or pending). Wave 1 invites at 2.5× capacity to seed confirmations. Wave 2+ uses live confirmation rate math to calculate exactly how many more invites are needed to close the gap to the show-rate-adjusted target. Use the same event slug consistently across waves.

### Confirmation Cap
Once confirmed RSVPs reach `ceil(capacity / 0.60)` (the show-rate-adjusted target), new YES replies get a capacity message instead of a confirmation. The cap prevents over-confirming on the SMS side even if a wave blast goes wide. Configurable by updating the event capacity field.

### CSV Import
Supports Superphone and Eventbrite exports. Captures: first name, last name, phone, email, tags, instagram. All imported members set to APPROVED. `smsOptIn` values of `"false"`, `"0"`, `"no"`, `"n"`, or empty string are correctly parsed as false — not coerced to true.

---

## The Check-In Page

URL: `https://rsvpsociety.com/admin/checkin.html`

Mobile-first, tablet-optimized page for door staff. Same admin token.

- Token persists for **8 hours** via sessionStorage with expiry — staff don't re-enter between tabs, but the session expires automatically overnight
- On login: loads current event first, then fetches the confirmed guest list scoped to that event's slug (not the literal `"current"` key) — so the door list always matches what was actually invited
- Checked-in state seeds from server data on page refresh — a refresh mid-event does not wipe the green rows
- Live counter: X / Y Checked In with green progress bar
- Full alphabetical list of confirmed members only (people who replied YES)
- A–Z quick-jump bar; search filters the list in real time
- One tap to check in — row turns green, counter updates instantly, `eventId` is passed explicitly in the POST body
- Idempotent — duplicate taps never double-count attendance (conditional DynamoDB write on `rsvp-checkins` keyed on `(eventId, phone)`)

---

## Jade — SMS AI Concierge

Jade is the member-facing AI. Lives in `sms_handler.py`.

She handles:
- Inbound questions about RSVP Society (dress code, event details, vibe)
- YES/NO RSVP replies — updates DynamoDB, sends confirmation SMS
- STOP opt-out — writes `optOut: true`, member never messaged again
- Ignores anyone not approved, opted-in, and with an active invite

**Capacity protection:** When a YES reply comes in and the event has a capacity set, Jade checks current confirmed count before updating status. If the show-rate-adjusted confirmation target is already met, she replies with a polite capacity message instead of confirming.

**Webhook security:** Inbound Quo webhooks are signature-verified using HMAC-SHA256 before any processing. Configure `WEBHOOK_SECRET_ID` in Lambda env vars with the Secrets Manager ID holding Quo's signing key once the account is live.

**Two-text rule per event:**
1. The invite blast ("You're on the list. Reply YES.")
2. One reminder (day before at 6PM EST or day of at 11AM EST — set per event in the Event tab)

No exceptions. Keeps texts out of spam folders.

Model: `claude-haiku-4-5-20251001`
Prompt caching: enabled (~90% token savings after first call)
System prompt: `sms_handler.py` → `JADE_SYSTEM_PROMPT`
Status: **SMS disabled** (`SMS_ENABLED=false`). Flip to `true` once Quo approved and `send_sms()` HTTP call is wired.

---

## EventBridge Reminder Schedule

| Rule | Cron (UTC) | EST | Sends if |
|---|---|---|---|
| `rsvp-reminder-day-before` | `cron(0 23 * * ? *)` | 6:00 PM | Event is tomorrow + `reminderTiming=day_before` |
| `rsvp-reminder-day-of` | `cron(0 16 * * ? *)` | 11:00 AM | Event is today + `reminderTiming=day_of` |

Lambda validates the event date before executing either path — it will not fire if the date doesn't match, `reminderTiming` is `manual`, or the event date is unparseable. Dedup sentinels (`dayBeforeReminderSentAt` / `dayOfReminderSentAt`) on each invite record prevent double-sending even if the rule fires twice. Manual override: **Send Reminder Blast** button on Invite tab fires immediately to all confirmed members for the current event.

---

## Deployment

### Backend (automatic)
Push to `main` → GitHub Actions builds Lambda zip → deploys all 6 functions.

### Frontend (manual)
Drag `frontend/` folder into Netlify dashboard. Takes 4 seconds.

### Infrastructure changes
```bash
cd backend/terraform
terraform apply
```

### Full rebuild from scratch
```bash
# 1. Infrastructure
cd backend/terraform
terraform init
terraform apply

# 2. Frontend
# Drag frontend/ to Netlify, point rsvpsociety.com to Netlify

# 3. Quo
# Register, submit 10DLC, point webhook to api.rsvpsociety.com/sms
# Wire HTTP call in sms_adapter.send_sms() using the skeleton in the file
# Set WEBHOOK_SECRET_ID in sms_handler Lambda env vars (Quo signing key)
# Flip SMS_ENABLED=true in Lambda env vars once approved

# 4. Seed event
# Admin panel → Event tab → fill in details, set eventSlug carefully

# 5. Import members
# Admin panel → Members → Import CSV (Superphone export)
```

---

## Market Expansion

Current: Louisville, KY
Planned: Indianapolis, Cincinnati, Charlotte, Nashville, Atlanta, Houston — and college markets for a slightly younger, elevated experience alongside the core older crowd.

The infrastructure supports this natively:
- Member base grouped by state, filter by market in invite preview
- No code changes needed when expanding to a new city
- Tier system maintains quality control as the list grows
- One active event at a time keeps the operation clean

When expanding: get members from that market approved, filter to their state pill in invite preview, send city-specific blast.

---

## Gallery (pics.html)

Built and ready. Currently shows empty state ("Photos coming soon"). After each event:

1. Upload photos to S3: `rsvp-society-pics-prod`
2. Create a folder: e.g. `2026-03-march/`
3. Add entry to `EVENT_FOLDERS` in `pics.html`:
   ```js
   { id: '2026-03-march', label: 'March 2026' }
   ```
4. Add filenames to `PHOTO_MANIFEST`:
   ```js
   { folder: '2026-03-march', files: ['001.jpg', '002.jpg'] }
   ```
5. Drag frontend to Netlify.

CloudFront URL: `https://d31o74npegx00h.cloudfront.net`

---

## Costs (Approximate Monthly)

| Service | Estimated Cost | Notes |
|---|---|---|
| AWS Lambda | Free tier | ~1M requests/month included |
| DynamoDB | Free tier | 5 tables, current scale well within limits |
| API Gateway | ~$3.50/million API calls | |
| CloudFront (API) | < $1/month | Minimal at current volume |
| S3 + CloudFront (photos) | < $1/month | |
| Secrets Manager | ~$1.20/month | 3 secrets × $0.40/secret/month |
| WAF | ~$5/month | Web ACL + 2 rate-limit rules (`/access` + `/admin/invite`) |
| CloudWatch Logs | < $1/month | 30-day retention on all 6 Lambda log groups |
| EventBridge | Effectively $0 | 2 scheduled rules |
| Quo SMS | Per-message | Invite blast is the main cost — check current rates |
| Anthropic Claude | Very low | Haiku with caching — ~90% token savings |
| Netlify | Free tier | |

**Total: under $10/month** in AWS costs until scale changes significantly. SMS cost (Quo) scales with blast size but is marginal per message.

---

## Known Gaps / Future Work

- **`sms_adapter.send_sms()` HTTP call** — documented stub. The OpenPhone skeleton is in the file. Wire it when Quo approves and the API contract is confirmed.
- **Video in hero** — no video asset yet. Replace static hero with looping 5–10 second moody venue clip when available
- **Event photos** — gallery built and ready, waiting on first event
- **Jade system prompt tuning** — baseline personality set, refine tone as brand develops
- **`search_members()` scan** — full-table scan used for name/phone search (check-in page). Acceptable at current scale. Revisit around 5,000+ members.
- **Multi-city simultaneous events** — current architecture supports one active event at a time. Multi-city same-night would require a different `eventId` per city — doable but not needed yet
- **Admin auth upgrade** — Cognito + MFA is the long-term play. Not needed at current scale. See Security section.
- **Confirmation cap show-rate** — currently hardcoded at 60%. If your actual show rate diverges significantly after 3–5 events, update this value or pull it from the event record.

---

---

# Security Posture

---

## What Is Built and Live

### 1. Per-Function IAM Roles (`iam_per_function.tf`)

Replaces the single shared `rsvp-lambda-role` with six separate roles, each scoped to only the tables and secrets that function actually needs.

| Function | Tables before | Tables after |
|---|---|---|
| access_request | All tables | members only |
| sms_handler | All tables | members, invites, events (read/update only) |
| event_handler | All tables | events only |
| reminder_handler | All tables | members (read), invites (scan), events (read) |
| invite_handler | All tables | members, invites, events (no delete, no checkins) |
| admin_handler | All tables | All (intentional — needs full access) |

**Migration status: in progress.** Per-function roles are defined and attached. The legacy `lambda_role` and `lambda_policy` remain in `main.tf` and should be removed once all functions are verified on their per-function roles. See migration checklist below.

### 2. Status-Index GSI on Members Table

The members table has a GSI on the `status` field. `list_members_by_status()` queries the GSI directly — O(matching items) instead of O(all members). Every admin panel load and every invite blast use this path. Full-table scans for status-based queries are eliminated.

### 3. Check-In Dedup Table (`rsvp-checkins`)

`record_attendance(attended=True)` uses a conditional `put_item` on `rsvp-checkins` — `ConditionalCheckFailedException` on a duplicate tap means no counter update and no double-count. Keyed on `(eventId, phone)`. Records auto-expire after 90 days via TTL. The `attended=False` (No Show) path does **not** write to this table — so a no-show mark doesn't block a real check-in if the person shows up late.

### 4. Audit Log (`rsvp-audit-log`)

Every admin action writes an immutable record: who (last 8 chars of admin token), what (action type), to whom (target phone), and when (ISO timestamp). Records auto-expire after 1 year via TTL. PITR enabled. Three functions write to the audit log: `admin_handler`, `invite_handler`, `reminder_handler`.

Actions logged:
- `MEMBER_APPROVED`, `MEMBER_DENIED`, `MEMBER_RESTORED_PENDING`
- `MEMBER_DELETED`, `MEMBER_GENDER_SET`, `MEMBER_TIER_SET`
- `ATTENDANCE_RECORDED`, `INVITE_BATCH_SENT`, `REMINDER_BLAST_SENT`
- `EVENT_UPDATED`, `MEMBER_IMPORT_COMPLETED`

### 5. WAF Rate Limiting

Two rules on the API:
- **`/access`** — 500 requests per 5 minutes per IP. Protects the public sign-up form from enumeration and spam.
- **`/admin/invite`** — 20 requests per 5 minutes per IP. Protects the SMS blast endpoints from scripted abuse even if the admin token leaks.

### 6. SMS Consent Enforcement

`smsOptIn` is enforced at the query layer, not the send layer. `_get_approved_members()` filters opted-out members before they enter the invite pool. The invite preview never shows opted-out members. `reminder_handler` applies the same check independently on every send.

### 7. Input Validation and Error Handling

- All phone numbers normalized to E.164 before any DynamoDB write
- Malformed JSON body or bad base64 encoding returns 400, not 500
- Bad phone format returns 400 with a descriptive error, not 500
- Selected phones in invite blasts are normalized individually — one bad phone skips with a warning, doesn't abort the entire send
- `smsOptIn` in CSV imports parsed safely — string `"false"` / `"0"` / `"no"` correctly resolves to false
- Internal exception details never surfaced to API callers
- Import row errors log full detail to CloudWatch, return only error type to client

### 8. Webhook Signature Verification

Inbound SMS webhooks are HMAC-SHA256 verified against the provider's signing key (fetched from Secrets Manager via `WEBHOOK_SECRET_ID`). Requests with no signature header or a mismatched digest are rejected. If `WEBHOOK_SECRET_ID` is not set, requests pass through with a warning log — configure this before going live.

---

## What Is Architectural (Decisions, Not Code)

### A. Single Static Admin Token

One bearer token in Secrets Manager controls the entire admin surface. If it leaks, everything is exposed until manually rotated.

**What elite looks like:**
- AWS Cognito user pool with MFA for admin login
- API Gateway `AWS_COGNITO_USER_POOLS` authorizer instead of `authorization = "NONE"`
- Short-lived JWT sessions — Cognito handles rotation automatically
- Separate admin users per promoter if promoters ever get access

**Practical steps now (without Cognito):**
- Add a CloudWatch metric filter + alarm on repeated 401 responses to admin endpoints (catches brute-force attempts on the token)
- Rotate the token before every major event via `aws secretsmanager put-secret-value`
- Consider WAF IP allowlist for `/admin/*` routes (your home IP or a specific device)

### B. API Gateway Authorization Is NONE on All Routes

Auth happens inside Lambda, not at the API Gateway layer. Every request — including clearly unauthorized ones — invokes a Lambda and consumes compute before being rejected.

Acceptable at current scale. The token check is fast (Secrets Manager cached after first read) and Lambda cold starts are rare in prod. WAF rate-limiting blocks volumetric abuse. Fix this when adding Cognito — they solve it together.

### C. `search_members()` Still Scans

`list_members_by_status()` now uses the status-index GSI and is O(matching items). `search_members()` — used by the check-in page for name/phone search — still scans the full members table. At current scale this is fine. Around 5,000+ members it will slow down and cost real read units.

Options then: accept the scan (it's a door tool used by one person at a time), or add a name-prefix index (limited flexibility).

### D. PII Exposure Scope

Admin endpoints return full member records — name, phone, email, tags, attendance history — to any caller with the admin token. **This is expected and intentional.** You're running an events business and need to see this data to operate it.

What to do: document a data retention policy for denied or inactive members, and consider field-level redaction if any third parties ever get read access.

### E. eventSlug Is the Operational Key

Invite records are stored under `eventId = eventSlug` (e.g. `"2026-03-march"`), not `"current"`. The Event tab saves the event with `eventId: "current"` for the API to retrieve, but the slug is what flows through invites, checkin queries, and attendance writes. If the slug changes mid-event (don't do this), invites sent under the old slug will be invisible to the door page.

---

## IAM Migration Checklist

To complete the per-function IAM migration and remove the legacy role:

- [ ] Verify each `aws_lambda_function.*.role` in `main.tf` references the per-function role ARN
- [ ] `terraform plan` — confirm only IAM + Lambda env changes, no table drops or recreations
- [ ] `terraform apply`
- [ ] Check CloudWatch Logs on first check-in after deploy — look for `ConditionalCheckFailedException` on a deliberate double-tap (that's the dedup guard working correctly)
- [ ] Remove `aws_iam_role.lambda_role` and `aws_iam_policy.lambda_policy` from `main.tf`

---

## Honest Bottom Line

| Layer | Status |
|---|---|
| Public endpoints (sign-up, event info) | ✓ Clean |
| DynamoDB (not internet-facing) | ✓ Solid — PITR on all 5 tables |
| Secrets management | ✓ Good — Secrets Manager, not env vars |
| Data backup / recovery | ✓ Good — PITR enabled |
| Audit trail | ✓ Built — all three handler functions write to audit log |
| Status query scans | ✓ Eliminated — status-index GSI live |
| WAF rate limiting | ✓ `/access` + `/admin/invite` both covered |
| Check-in dedup | ✓ Conditional write guard on `rsvp-checkins` |
| Check-in event scoping | ✓ Door page queries the correct event bucket by slug |
| Check-in state persistence | ✓ Checked-in state reseeds from server on refresh |
| SMS consent enforcement | ✓ Enforced at query time in both invite and reminder paths |
| Input validation | ✓ Bad phone/JSON → 400, not 500. CSV bool parsing safe. |
| Attendance integrity | ✓ No-show does not burn check-in guard or skew counters |
| Webhook signature verification | ✓ HMAC-SHA256 — configure `WEBHOOK_SECRET_ID` before go-live |
| Confirmation cap | ✓ YES replies blocked at show-rate-adjusted target |
| Reminder dedup | ✓ Per-invite sentinels prevent double-send |
| IAM least-privilege | ⚠ In progress — per-function roles defined, legacy role pending removal |
| Admin auth | ⚠ Acceptable. Not elite. One token = full access. |
| `search_members()` scan | ⚠ Acceptable now. Revisit at 5,000+ members. |
| SMS send (sms_adapter) | ⚠ Documented stub — wire HTTP call before go-live |

The system is private-facing, not public-facing. The threat model is "someone who gets hold of the admin token" and "member data staying out of places it shouldn't be." Both are addressed at an acceptable level for current scale. The path to elite is Cognito + per-function IAM fully migrated.

---

*Built February 2026. Audited and hardened March 2026. The bones are solid. The system is production-ready pending carrier approval. Now go build the experience.*
