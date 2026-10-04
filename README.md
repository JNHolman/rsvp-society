# RSVP Society

**An invitation-led event platform built around what happens after someone says yes.**

RSVP Society connects membership approval, targeted invitations, text-based RSVPs, guest management and door check-in. It supports an actual event operation: different markets, changing guest lists, delayed replies and limited capacity. Jade is its SMS host, answering event questions while the application owns the guest list.

## The product

- **Membership:** A ZIP-based signup flow, host approvals and a searchable community directory. Imported contacts without ZIP codes can be assigned a market manually.
- **Event operations:** One active event, reviewed message drafts, scheduled reminders and updates to confirmed attendees. Past events remain available for reporting.
- **Audience management:** Three invitation waves with editable audiences, exclusions and automatic follow-ups. Hosts can inspect a wave before sending it or pause its automation.
- **SMS guest service:** RSVP responses, guest names and changes, event questions, and host escalation for requests requiring a decision.
- **Door and analytics:** Member and guest check-in, attendance history and event reporting. Attendance is recorded through check-in rather than edited from analytics.

The design separates the host's work by purpose: event facts and communication on the Event page, audience selection on Invites, member profiles in Members, and arrivals at Check-in.

## The engineering work

The difficult part is consistency. A member can be invited directly, named as someone else's guest, reply days later, change their guest or cancel. Meanwhile, a scheduled wave may be preparing to send.

DynamoDB conditional writes and transactions protect the relevant RSVP, guest reservation and headcount changes. A member attached as a guest is excluded from later invitation waves. Saved preview audiences establish what the operator reviewed; send-time checks still enforce current eligibility and consent. Receipt records and send claims reduce duplicate processing when webhooks or scheduled jobs retry.

Jade's voice is deliberately separate from authority. The model receives event context and saved status to answer questions; application code controls confirmations, guest updates, release timing and approval decisions. A confident sentence cannot substitute for a successful database update.

## Architecture and decisions

| Component | Responsibility | Trade-off |
| --- | --- | --- |
| Static JavaScript frontend | Public signup and private operations dashboard | Small deployment surface; client state must be refreshed against the API |
| API Gateway and Python Lambda | Signup, member administration, invitations, SMS and reminders | Scales with use; retries and partial failures need explicit handling |
| DynamoDB | Members, events, invitations, check-ins and operational records | Conditional updates suit concurrent workflows; some name searches still require scans |
| EventBridge Scheduler | Event reminders and subsequent invitation waves | Avoids an always-running worker; edited events require schedule reconciliation |
| SMS provider and language model | Guest communication and contextual answers | External delivery and model behavior require realistic end-to-end testing |
| Terraform and GitHub Actions | Infrastructure definition, regression checks and deliberate deployment | Repeatable changes; production configuration and rollout still need review |

A single active event keeps operator workflows straightforward. Historical event records support reporting without making the dashboard a full multi-event booking system. Shared-secret admin authentication matches the current single-operator setup.

## Cost-conscious design

Lambda and on-demand DynamoDB match an intermittent event workload without maintaining an application server. One-time schedules avoid continuous polling. Operational replies are handled directly where possible; the language model is reserved for drafting and conversational answers, with bounded inputs and outputs and prompt-cache hints. Reviewed invitation copy can be reused across an entire wave.

Duplicate suppression and consent checks also avoid unnecessary SMS. CloudWatch log retention is bounded, and image delivery uses edge caching and storage lifecycle rules.

These are implemented optimization mechanisms, not a claim of a particular savings percentage. A before-and-after cost figure needs comparable billing periods, traffic volumes and provider usage; those measurements are not included in this repository.

## Security approach

The code includes authenticated administrative routes, webhook signature verification, consent and opt-out handling, sensitive configuration through AWS Secrets Manager, scoped function permissions, and CloudFront/WAF origin protection. Pending requests and operational records have bounded retention, while event and member history remains available for reporting. Host decisions use the configured approval channel with expiring request records. Venue details follow the event's release policy.

These controls are part of the implementation, not a certification. Production validation, credential rotation, operator access controls and ongoing dependency review remain operational responsibilities. Never commit credentials, Terraform state, member exports or private audit evidence.

## Working with the project

- `frontend/`: public experience and admin dashboard.
- `backend/lambda/`: Python services and regression tests.
- `backend/terraform/`: infrastructure definitions.
- `JADE.md`: concise voice and behavior guide.

For local checks, install `backend/lambda/requirements-dev.txt`, then run `bash backend/lambda/run_step1_tests.sh`. Tests use mocks and an emulated AWS environment; they do not establish live SMS delivery or production readiness.

Build deployment bundles with `bash backend/lambda/export_clean.sh`. Infrastructure deployment is an explicit GitHub Actions workflow action. Review the plan and environment configuration before deploying; static frontend deployment is separate.

## About the project

RSVP Society is a portfolio and production project grounded in real event operations. Its engineering focus is reliable coordination between human decisions, asynchronous messages and a changing guest list. Product and architecture questions are welcome through GitHub Issues; keep guest information and private operational details out of public discussions.
