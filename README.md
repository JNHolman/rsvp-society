# RSVP Society — Project README

**Private R&B rooms. Invite only. No flyers.**

This document exists so that if this project ever needs to be rebuilt from scratch — on a new machine, in a new session, or by someone new — everything needed to understand it, recreate it, and continue it is right here.

---

## What This Is

RSVP Society is an invitation-only R&B event brand. The website is the front door. The backend is the operation. The AI is the staff.

The system has three jobs:

1. **Collect** — people submit their name and phone number on the website to request access
2. **Filter** — an admin approves or denies members based on vibe, gender ratio, and fit
3. **Operate** — approved members get invited via SMS blast, RSVP YES or NO, and receive event details from an AI concierge named Jade

Everything runs on AWS. The frontend runs on Netlify. SMS runs through Quo (pending approval). The AI runs on Anthropic Claude.

---

## Project Structure

```
rsvp-society/
├── frontend/
│   ├── index.html          # Main website
│   ├── checkin.html        # Mobile door check-in page (staff use)
│   ├── admin/
│   │   └── index.html      # Admin panel (token protected)
│   ├── pics.html           # Photo gallery (post-event)
│   └── terms.html          # SMS Terms & Privacy Policy (Quo requirement)
│
├── backend/
│   ├── access_request.py   # Handles form submissions from website
│   ├── sms_handler.py      # Jade AI concierge + YES/NO RSVP + STOP opt-out
│   ├── sms_adapter.py      # Quo SMS API wrapper
│   ├── admin_handler.py    # All admin API endpoints
│   ├── member_store.py     # DynamoDB read/write layer
│   └── invite_handler.py   # Invite math, blast preview, send
│
└── terraform/
    ├── main.tf             # API Gateway, Lambda, CloudWatch
    └── storage.tf          # DynamoDB tables, S3, CloudFront
```

---

## Tech Stack & Why

### Frontend — Netlify
Static HTML/CSS/JS. No framework. Deploys on git push. Custom domain via Netlify DNS. Chosen because it's instant, free, and requires zero server maintenance.

### Backend — AWS Lambda (Python)
Serverless functions. Each Lambda handles one responsibility. No always-on server costs. Scales automatically. Chosen because the event business is bursty — quiet for weeks, then 200 SMS messages go out in an hour.

### Database — AWS DynamoDB
NoSQL. Two main tables:
- `rsvp-members` — every person who has ever submitted their number
- `rsvp-event-invites` — who was invited to which event, their RSVP status, and attendance

Chosen for its speed, low cost at this scale, and native AWS integration with Lambda.

### API — AWS API Gateway
REST API that routes requests from the frontend and Quo webhooks to the right Lambda function. All endpoints either require an `x-admin-token` header or are public read-only.

### SMS — Quo
Carrier-compliant SMS platform. Required for 10DLC registration (the process that lets you send mass texts from a business number without getting flagged as spam). Quo webhooks hit the `sms_handler` Lambda when members reply to texts. Status: pending approval. Carrier compliance form at `https://rsvpsociety.com/#access`.

### AI Concierge — Anthropic Claude (Jade)
Jade is an SMS-based AI assistant for RSVP Society members. She answers questions about events, dress codes, and details via text. Built on `claude-haiku-4-5-20251001` with prompt caching enabled to reduce token costs. Jade only responds to approved, opted-in members. She ignores strangers.

### Photo Storage — AWS S3 + CloudFront
Event photos upload to S3 bucket `rsvp-society-pics-prod`. CloudFront serves them globally at `https://d31o74npegx00h.cloudfront.net`. The `pics.html` gallery page references this URL. No photos in the git repo — ever.

### Secrets — AWS Secrets Manager
API keys (Anthropic, Quo) are stored in Secrets Manager, not environment variables. Lambda functions call Secrets Manager at runtime. This means rotating a key is a one-step process and nothing is ever in plaintext.

### Infrastructure as Code — Terraform
All AWS infrastructure is defined in `main.tf` and `storage.tf`. Nothing was clicked together in the console. To recreate the entire backend: `terraform init && terraform apply`.

---

## AWS Infrastructure

### Lambda Functions

| Function | Purpose |
|---|---|
| `access-request` | Writes new member to DynamoDB when form is submitted |
| `sms-handler` | Jade AI + YES/NO RSVP handler + STOP opt-out |
| `admin-handler` | Member management, event editor, attendance, search |
| `invite-handler` | Invite math and SMS blast |

### DynamoDB Tables

| Table | Primary Key | Purpose |
|---|---|---|
| `rsvp-members` | `phone` | All members, status, gender, tier, opt-in |
| `rsvp-event-invites` | `eventId` + `phone` | Per-event invite/RSVP/attendance tracking |
| `rsvp-events` | `eventId` | Current event details Jade references |

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
| `source` | String | web, import |
| `tierOverride` | Number | 1, 2, 3 — overrides auto-calculation |
| `attendedCount` | Number | How many events they showed up to |
| `confirmedCount` | Number | How many times they RSVPed YES |
| `createdAt` | String | ISO timestamp |
| `lastSeenAt` | String | ISO timestamp |

### API Endpoints

All endpoints are under `https://ez2z31emm2.execute-api.us-east-1.amazonaws.com/prod`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/access` | None | Submit access request from website |
| POST | `/sms` | Quo signature | Inbound SMS from members |
| GET | `/admin/members` | Token | List members by status (paginated, alphabetical) |
| DELETE | `/admin/members` | Token | Delete a member permanently |
| POST | `/admin/members/status` | Token | Set member status |
| POST | `/admin/members/gender` | Token | Set member gender |
| POST | `/admin/members/tier` | Token | Set invite tier override |
| POST | `/admin/members/attendance` | Token | Mark post-event attendance |
| POST | `/admin/members/import` | Token | Bulk CSV import |
| GET | `/admin/members/search` | Token | Search members by name or phone |
| GET | `/admin/event` | Token | Get current event details |
| POST | `/admin/event` | Token | Set current event details |
| POST | `/admin/invite/preview` | Token | Preview invite math without sending |
| POST | `/admin/invite/send` | Token | Execute SMS invite blast |
| GET | `/event` | None | Public event details (venue hidden) |

---

## Member Lifecycle

```
Website form submit
       ↓
DynamoDB: status = PENDING
       ↓
Admin reviews in admin panel
       ↓
APPROVED or DENIED
       ↓ (if approved)
Included in next invite blast
       ↓
Member receives SMS: "You're invited. Reply YES or NO."
       ↓
YES → status = CONFIRMED → gets event details
NO  → status = DECLINED → skipped this round
       ↓
Post-event: admin marks attendance (or door staff via checkin.html)
       ↓
Attendance tracked → reliability tier calculated automatically
```

### Reliability Tiers (auto-calculated)

- **Tier 1** — 80%+ show rate after 3+ invites. Fills first. Guaranteed spots.
- **Tier 2** — 40–79% show rate. Invited with a ghost buffer (default 30% over-invite).
- **Tier 3** — Under 40% show rate. Never auto-invited. Admin can manually override.

Gender ratio and tier math happen in `invite_handler.py` → `_build_invite_list()`.

---

## The Admin Panel

URL: `https://rsvpsociety.com/admin/`

Login: enter the admin token (stored securely — not in this file).

Tabs:
- **Members** — approve, deny, delete, set gender, set tier. Search by name or phone. 50 per page, alphabetical by last name. Bulk import via CSV with SMS opt-in control.
- **Invite** — set event capacity, female %, ghost buffer, preview invite list, send blast
- **Event** — set date, venue, dresscode, city, notes (Jade reads this)
- **Attendance** — load invitees for an event, mark who showed up

### CSV Import
Supports Superphone and Eventbrite exports. Parser handles quoted fields with empty values correctly. Captures: first name, last name, phone, email, tags, instagram. All imported members are set to APPROVED. A checkbox in the import modal lets you mark all as SMS opted-in (checked by default — appropriate for Superphone/Eventbrite contacts who consented at point of collection).

---

## The Check-In Page

URL: `https://rsvpsociety.com/checkin.html`

Mobile-first page for door staff. Token-gated (same admin token). Search guests by name or phone, tap Check In to record attendance in real time. Session persists via sessionStorage so staff don't have to re-enter the token between searches.

---

## Jade — SMS AI Concierge

Jade is the member-facing AI. She lives in `sms_handler.py`.

She handles:
- General questions about RSVP Society (dress code, event details, vibe)
- YES/NO RSVP replies (updates DynamoDB automatically)
- STOP opt-out (writes `optOut: true` to member record, no future SMS)
- Ignores anyone who is not an approved, opted-in member

Model: `claude-haiku-4-5-20251001`
Prompt caching: enabled (saves ~90% on system prompt tokens after first call)
System prompt: defined in `sms_handler.py` → `JADE_SYSTEM_PROMPT`

To update Jade's knowledge: edit the system prompt. To give her event details: fill in the Events table via the admin panel Event tab.

---

## Mia — Web Chat AI (Summer's Calling Festival)

Separate project. Lives in `chatbot.js` on the Summer's Calling Festival website (`summerscallingfestival.com`). Deployed as a Netlify function (`chat.js`).

Model: `claude-haiku-4-5-20251001`
Prompt caching: enabled
System prompt: embedded in `chatbot.js`

Mia is specific to Summer's Calling Festival 2026 (Playa del Carmen, June 11–14). She is not connected to RSVP Society.

---

## Environment Variables (per Lambda)

| Variable | Used By | Value |
|---|---|---|
| `MEMBERS_TABLE_NAME` | all | `rsvp-members` |
| `EVENTS_TABLE_NAME` | admin_handler, sms_handler | `rsvp-events` |
| `INVITES_TABLE_NAME` | invite_handler, sms_handler | `rsvp-event-invites` |
| `ADMIN_TOKEN` | admin_handler | Secret — do not commit |
| `SEND_WELCOME_SMS` | access_request | `false` until Quo approved, then `true` |
| `ALLOWED_ORIGINS` | all | `https://rsvpsociety.com` |
| `QUO_API_KEY` | sms_adapter | Stored in Secrets Manager |
| `CLAUDE_API_KEY_SECRET` | sms_handler | Stored in Secrets Manager |

---

## How to Redeploy From Scratch

### 1. AWS Infrastructure
```bash
cd terraform
terraform init
terraform apply
```
Note the outputs: `api_base_url`, `pics_cloudfront_url`, `pics_s3_bucket`

### 2. Lambda Functions
All Python files live in the `backend/` folder. Terraform zips and deploys them automatically on `terraform apply`. If you need to update a single function manually:
```bash
zip function.zip admin_handler.py member_store.py sms_adapter.py
aws lambda update-function-code --function-name rsvp-admin-handler --zip-file fileb://function.zip
```

### 3. Frontend
Push to GitHub. Netlify auto-deploys on push. Custom domain configured in Netlify dashboard.

### 4. Quo
- Register at quo.com
- Submit 10DLC brand registration
- Point webhook to `{api_base_url}/sms`
- Set `SEND_WELCOME_SMS=true` once approved

### 5. Seed the Database
Open admin panel → Event tab → fill in the next event details so Jade has something to reference.

### 6. Import Members
Open admin panel → Members → Import CSV. Use Superphone export. Check "Mark all as SMS opted in". All 179 members import in under 1 second.

---

## Known Gaps / Future Work

- **Video in hero** — no video asset yet. When available, replace the static hero background with a looping 5–10 second moody R&B venue clip
- **Real event photos** — `pics.html` gallery is built and ready. Upload photos to S3 after first event, add folder entry to `EVENT_FOLDERS` and `PHOTO_MANIFEST` in `pics.html`
- **Waitlist position** — infrastructure supports it (count PENDING members with earlier `createdAt`). Hold until brand has enough weight that people actually care about their number
- **Post-event attendance UI** — `record_attendance()` function is built. Admin Attendance tab is built. Door staff can use `checkin.html` in real time. Just needs to be used after each event.
- **Jade system prompt expansion** — currently has baseline personality. Add specific event details, venue info, and dress codes via the admin Event tab after each event is confirmed
- **GSI on DynamoDB** — currently using full table scans with pagination for member queries. Works fine at current scale. Add a GSI on `status` when member list grows past ~5,000

---

## Costs (Approximate Monthly)

| Service | Estimated Cost |
|---|---|
| AWS Lambda | Free tier covers ~1M requests/month |
| DynamoDB | Free tier covers this scale indefinitely |
| API Gateway | ~$3.50/million API calls |
| S3 + CloudFront | < $1/month at current photo volume |
| Secrets Manager | ~$0.40/secret/month |
| Quo SMS | Per-message pricing (check current rates) |
| Anthropic Claude | Haiku with caching — very low per conversation |
| Netlify | Free tier |

Total estimated: under $10/month until the member list and event frequency scale significantly.

---

*Built February 2026. If you're reading this in another lifetime — the bones are solid. Pick up where we left off.*
