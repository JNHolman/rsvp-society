# RSVP Society — Project README

**Private R&B rooms. Invite only. No flyers.**

This document exists so that if this project ever needs to be rebuilt from scratch — on a new machine, in a new session, or by someone new — everything needed to understand it, recreate it, and continue it is right here.

---

## What This Is

RSVP Society is an invitation-only R&B event brand. The website is the front door. The backend is the operation. The AI is the staff.

The system has three jobs:

1. **Collect** — people submit their name and phone number on the website to request access
2. **Filter** — an admin approves or denies members based on vibe, gender ratio, and fit
3. **Operate** — approved members get invited via SMS blast, RSVP YES or NO, and receive event details and reminders from an AI concierge named Jade

Everything runs on AWS. The frontend runs on Netlify. SMS runs through Quo (pending approval). The AI runs on Anthropic Claude.

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
│       ├── sms_adapter.py      # Quo SMS API wrapper
│       ├── admin_handler.py    # All admin API endpoints
│       ├── member_store.py     # DynamoDB read/write layer
│       ├── invite_handler.py   # Invite math, blast preview, send
│       └── reminder_handler.py # EventBridge reminder blasts + manual trigger
│
└── terraform/
    ├── main.tf                 # API Gateway, Lambda, DynamoDB, IAM, CloudWatch
    ├── cloudfront_api.tf       # CloudFront distribution → api.rsvpsociety.com
    ├── eventbridge.tf          # Scheduled reminder rules
    └── confirmed_endpoint.tf   # /admin/members/confirmed for check-in page
```

---

## Tech Stack & Why

### Frontend — Netlify
Static HTML/CSS/JS. No framework. **Manual deploys only** — drag the `frontend/` folder into Netlify dashboard. GitHub auto-deploy is disabled to avoid conflicts with backend CI/CD. Custom domain `rsvpsociety.com` managed via Netlify DNS.

### Backend — AWS Lambda (Python)
Serverless functions. Each Lambda handles one responsibility. No always-on server costs. Scales automatically. Chosen because the event business is bursty — quiet for weeks, then 200 SMS messages go out in an hour.

### Database — AWS DynamoDB
NoSQL. Three tables:
- `rsvp-members` — every person who has ever submitted their number
- `rsvp-event-invites` — who was invited to which event, their RSVP status, and attendance
- `rsvp-events` — current event details (always stored as `eventId: "current"`)

Chosen for speed, low cost at this scale, and native AWS integration with Lambda.

### API — AWS API Gateway + CloudFront
REST API routes requests from the frontend and Quo webhooks to the right Lambda. CloudFront sits in front at `api.rsvpsociety.com` — faster globally, protects against traffic spikes, handles SSL termination.

### SMS — Quo
Carrier-compliant SMS platform. Required for 10DLC registration. Quo webhooks hit the `sms_handler` Lambda when members reply. Status: **pending approval**. Carrier compliance form lives at `https://rsvpsociety.com/#access`. Flip `SEND_WELCOME_SMS=true` in Lambda env vars once approved.

### AI Concierge — Anthropic Claude (Jade)
Jade is an SMS-based AI assistant for RSVP Society members. She handles RSVPs, event questions, and reminders. Built on `claude-haiku-4-5-20251001` with prompt caching (~90% token savings). Jade only responds to approved, opted-in members.

### Scheduled Reminders — AWS EventBridge
Two daily CloudWatch Event Rules fire the `reminder_handler` Lambda:
- **Day before at 6PM EST** — `cron(0 23 * * ? *)` UTC
- **Day of at 11AM EST** — `cron(0 16 * * ? *)` UTC

Lambda checks the event's `reminderTiming` field and only sends if it matches. One reminder per event. Manual blast available from the Invite tab.

### Photo Storage — AWS S3 + CloudFront
Event photos upload to S3 bucket `rsvp-society-pics-prod`. CloudFront serves them at `https://d31o74npegx00h.cloudfront.net`. Gallery in `pics.html` references this URL. No photos in the git repo ever.

### Secrets — AWS Secrets Manager
API keys (Anthropic, Quo) stored in Secrets Manager, not env vars. Lambda calls Secrets Manager at runtime. Rotating a key is a one-step process and nothing is ever in plaintext.

### Infrastructure as Code — Terraform
All AWS infrastructure defined in `.tf` files. Remote state: `s3://rsvp-society-terraform-state/prod/terraform.tfstate`. To recreate: `terraform init && terraform apply`.

### CI/CD — GitHub Actions
Backend deploys automatically on push to `main`. Builds Lambda zip, deploys all 5 functions. Frontend is manual via Netlify.

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
| `rsvp-members` | `phone` | All members, status, gender, tier, opt-in |
| `rsvp-event-invites` | `eventId` + `phone` | Per-event invite/RSVP/attendance tracking |
| `rsvp-events` | `eventId` | Current event — always `eventId: "current"` |

### Member Record Fields

| Field | Type | Notes |
|---|---|---|
| `phone` | String | E.164 format — partition key |
| `name` | String | First name |
| `lastName` | String | Last name (from CSV import) |
| `email` | String | Optional |
| `tags` | String | From Superphone export (e.g. "Pool Party List") |
| `instagram` | String | Handle without @ |
| `status` | String | PENDING, APPROVED, DENIED |
| `gender` | String | M, F, O — set by admin |
| `smsOptIn` | Boolean | True = eligible for SMS blasts |
| `optOut` | Boolean | True = texted STOP, never message again |
| `source` | String | web, import |
| `tierOverride` | Number | 1, 2, 3 — overrides auto-calculation |
| `attendedCount` | Number | Events they showed up to |
| `invitedCount` | Number | Times they were invited |
| `createdAt` | String | ISO timestamp |
| `lastSeenAt` | String | ISO timestamp |

### Event Record Fields

| Field | Type | Notes |
|---|---|---|
| `eventId` | String | Always `"current"` — one active event at a time |
| `eventSlug` | String | Human name e.g. "Swim Test" — used as label in invite SMS |
| `date` | String | e.g. "Saturday March 15" |
| `startTime` | String | e.g. "9:00 PM" — used in reminder SMS |
| `city` | String | e.g. "Louisville, KY" |
| `venue` | String | Venue name |
| `address` | String | Full address — sent to confirmed members |
| `dresscode` | String | e.g. "All Black" |
| `revealVenue` | Boolean | If true, venue name appears in invite SMS |
| `notes` | String | Vibe description — Jade references this |
| `reminderTiming` | String | `day_before`, `day_of`, or `manual` |
| `capacity` | Number | Target headcount |
| `updatedAt` | String | ISO timestamp |

### API Endpoints

Base URL: `https://api.rsvpsociety.com`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/access` | None | Submit access request from website |
| POST | `/sms` | Quo signature | Inbound SMS from members |
| GET | `/admin/members` | Token | List members by status |
| DELETE | `/admin/members` | Token | Delete a member permanently |
| POST | `/admin/members/status` | Token | Set member status |
| POST | `/admin/members/gender` | Token | Set member gender |
| POST | `/admin/members/tier` | Token | Set invite tier override |
| POST | `/admin/members/attendance` | Token | Mark attendance (used by checkin.html) |
| POST | `/admin/members/import` | Token | Bulk CSV import |
| GET | `/admin/members/search` | Token | Search by name or phone |
| GET | `/admin/members/confirmed` | Token | Confirmed invitees for current event (check-in page) |
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
NO  → status = DECLINED → skipped this round, eligible next event
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

Login: admin token (stored securely — not in this file).

**Tabs:**

**Members** — approve, deny, delete, set gender/tier. Search, paginate, bulk CSV import. 50 per page, alphabetical. Import modal supports Superphone and Eventbrite exports with SMS opt-in checkbox.

**Event** — set event name, date, time, venue, address, dresscode, vibe/notes, reveal venue toggle, reminder timing. Save overwrites the single current event record in DynamoDB. No separate edit button needed — the form pre-populates from the saved event every time you open the tab. Change what you need, hit Save. No SMS goes out on save.

**Invite** — auto-populates from saved event. Set capacity, female %, ghost buffer. Preview invite list with state-based market filter pills (all 50 states + DC covered by area code). Remove individuals before sending. Send blast or trigger manual reminder blast. Next wave automatically excludes already-invited members — safe to run multiple times for the same event.

### Market Filter Pills
Preview groups confirmed members by state from area code. Every US area code is mapped. Unknown area codes show as the raw area code. As the member base grows into new cities the pills appear automatically — no code changes needed.

### Invite Wave Logic
The system supports multiple invite waves for the same event. Each blast automatically excludes anyone already in the EventInvites table for that event (confirmed, declined, or pending). Use the same event name/slug consistently across waves.

### CSV Import
Supports Superphone and Eventbrite exports. Captures: first name, last name, phone, email, tags, instagram. All imported members set to APPROVED. Checkbox to mark all as SMS opted-in.

---

## The Check-In Page

URL: `https://rsvpsociety.com/admin/checkin.html`

Mobile-first, tablet-optimized page for door staff. Same admin token. Shows:
- Event banner (name, date, venue, time) pulled live from current event
- Live counter: X / Y Checked In with green progress bar
- Full alphabetical list of **confirmed members only** (people who replied YES)
- A–Z quick-jump bar
- Search filters the list in real time
- One tap to check in — row turns green, counter updates instantly
- Session persists via sessionStorage so staff don't re-enter token

---

## Jade — SMS AI Concierge

Jade is the member-facing AI. Lives in `sms_handler.py`.

She handles:
- Inbound questions about RSVP Society (dress code, event details, vibe)
- YES/NO RSVP replies — updates DynamoDB, sends confirmation SMS
- STOP opt-out — writes `optOut: true`, member never messaged again
- Ignores anyone not approved and opted-in

**Two-text rule per event:**
1. The invite blast ("You're on the list. Reply YES.")
2. One reminder (day before at 6PM or day of at 11AM — set per event in the Event tab)

No exceptions. Keeps texts out of spam folders. iPhone's spam folder consolidation makes over-texting fatal for deliverability.

Model: `claude-haiku-4-5-20251001`
Prompt caching: enabled (~90% token savings after first call)
System prompt: `sms_handler.py` → `JADE_SYSTEM_PROMPT`
Status: **SMS disabled** (`SEND_WELCOME_SMS=false`). Flip to `true` once Quo approved.

---

## EventBridge Reminder Schedule

| Rule | Cron (UTC) | EST | Sends if |
|---|---|---|---|
| `rsvp-reminder-day-before` | `0 23 * * ? *` | 6:00 PM | Event is tomorrow + `reminderTiming=day_before` |
| `rsvp-reminder-day-of` | `0 16 * * ? *` | 11:00 AM | Event is today + `reminderTiming=day_of` |

Manual override: **Send Reminder Blast** button on Invite tab → fires immediately to all confirmed members for the current event.

---

## Deployment

### Backend (automatic)
Push to `main` → GitHub Actions builds Lambda zip → deploys all 5 functions.

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
# Flip SEND_WELCOME_SMS=true in Lambda env vars once approved

# 4. Seed event
# Admin panel → Event tab → fill in details

# 5. Import members
# Admin panel → Members → Import CSV (Superphone export)
# ~180 members, imports in under 1 second
```

---

## Market Expansion

Current: Louisville, KY
Planned: Indianapolis, Cincinnati, Charlotte, Nashville, Atlanta, Houston — and college markets for a slightly younger, elevated experience alongside the core older crowd.

Vision: a traveling experience brand with identity built around curation — signature drinks, dope DJs, R&B artists new and old. Not just a genre party. An experience that people plan around. The infrastructure supports this:
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

## Known Gaps / Future Work

- **Video in hero** — no video asset yet. Replace static hero with looping 5–10 second moody venue clip when available
- **Event photos** — gallery built and ready, waiting on first event
- **Jade system prompt tuning** — baseline personality set, refine tone as brand develops
- **GSI on DynamoDB** — currently full table scans. Add GSI on `status` field when member list grows past ~5,000
- **Multi-city simultaneous events** — current architecture supports one active event at a time. Multi-city same-night would require a different `eventId` per city — doable but not needed yet

---

## Costs (Approximate Monthly)

| Service | Estimated Cost |
|---|---|
| AWS Lambda | Free tier (~1M requests/month) |
| DynamoDB | Free tier at current scale |
| API Gateway | ~$3.50/million API calls |
| CloudFront (API) | Minimal at current volume |
| S3 + CloudFront (photos) | < $1/month |
| Secrets Manager | ~$0.40/secret/month |
| EventBridge | Effectively $0 |
| Quo SMS | Per-message (check current rates) |
| Anthropic Claude | Haiku with caching — very low per conversation |
| Netlify | Free tier |

**Total: under $10/month** until scale changes significantly.

---

*Built February 2026. The bones are solid. The system is production-ready. Now go build the experience.*
