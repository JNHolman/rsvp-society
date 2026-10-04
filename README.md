# RSVP Society

**A private event operations platform built to manage the work behind a curated guest experience.**

Private events become difficult to manage long before anyone reaches the door. Hosts need to build a trusted member base, decide who should receive an invitation, manage limited capacity, keep track of replies and guests, answer questions, communicate changes and know who actually attended.

RSVP Society brings that work into one system. Members request access and are approved into the community. Hosts create events and select audiences, while invitation waves prioritize who gets contacted first and automatically expand when capacity remains. Guests can RSVP, manage a +1 and ask questions by text. Confirmed attendance flows into check-in and event reporting.

## What it solves

- **Building a reusable audience:** Membership requests, approvals, profiles and location data create a community that can be used across events instead of rebuilding a guest list each time.
- **Inviting the right people first:** Tiered invitation waves prioritize stronger audiences before opening remaining capacity to additional eligible members.
- **Managing real attendance:** RSVP status, +1s, cancellations and check-in stay connected so the expected guest count can change without losing control of capacity.
- **Reducing manual communication:** Jade handles routine guest conversations while escalating requests that require a host decision.
- **Protecting private event details:** Venue information can be released according to event policy and timing rather than exposed publicly.
- **Learning from attendance:** Check-in history becomes part of the member record and can influence future invitation priority.

## How RSVP Society works

1. **Build the community.** People request membership and provide basic information such as their location. The host reviews each request before approving access.

2. **Create the event.** The host defines the event, capacity, guest rules, important details and when private information such as the venue should be released.

3. **Invite the right audience.** RSVP Society organizes invitations into waves so priority members have the first opportunity to respond before invitations expand to additional members.

4. **Manage the conversation.** Jade handles invitations, RSVPs, +1 information, cancellations, reminders and common event questions through text while the application enforces the actual event rules.

5. **Keep the guest list current.** Confirmations, cancellations and guest changes update expected attendance so the host has a reliable view of the event as it changes.

6. **Run the door.** Check-in distinguishes members from their non-member guests and records who actually attended rather than treating an RSVP as attendance.

7. **Grow the community.** Non-member guests can be identified as potential future members and directed to RSVP Society after experiencing an event.

8. **Improve future events.** Attendance history helps the host understand who consistently participates and make better decisions about future invitations.

## Meet Jade

Jade is RSVP Society's text host—the personality guests interact with from invitation through event day.

She handles conversations that normally consume a host's time: invitations, RSVPs, +1s, cancellations, reminders and event questions. Her voice is intentionally confident, concise and socially fluent. She should sound like someone who knows the event and the people coming to it—not a chatbot, customer-service agent or collection of scripted responses.

Jade also has deliberately limited authority.

She can understand a guest's message, use current event and membership context, and respond naturally. She cannot invent missing event details, override capacity, approve membership, bypass invitation rules or claim that an RSVP or guest change succeeded when the application did not save it.

That separation is fundamental to the design: **the language model owns the conversation; the application owns the truth.**

Deterministic application services remain authoritative for membership, invitation eligibility, RSVP state, +1s, cancellations, capacity, venue-release rules and host approvals. When a request requires a human decision, Jade hands it back to the host instead of improvising one.

This uses conversational AI where it adds value—the guest experience—without giving probabilistic model output control over operational state.

## Product decisions

| Decision | Business reason | Engineering consequence |
| --- | --- | --- |
| Membership before invitations | Build a reusable audience instead of starting from zero for every event | Persistent member profiles, location, tiers and attendance history |
| Staged invitation waves | Give higher-priority members an earlier opportunity while continuing to fill the event | Tiered audiences, scheduled waves, eligibility rechecks and operator controls |
| Attendance-based tiers | Actual attendance is a stronger signal than an RSVP alone | Invitation history and check-in data contribute to future audience priority |
| SMS as the guest channel | Guests can respond without installing another application | Webhooks, consent and opt-out handling, idempotency and conversational state |
| Jade for routine conversation | Host attention is more valuable on decisions and exceptions | AI handles language while deterministic services control operational state |
| Human escalation | Sections, group arrangements and exceptions require judgment | Explicit host handoff instead of model improvisation |
| Configurable venue release | Private events may require tighter control over location details | Venue visibility is enforced as event policy rather than static content |
| +1 and cancellation deadlines | Late changes affect capacity and door operations | Time-aware rules are enforced by backend services |
| One active event | The current operation does not require the complexity of a general event-management SaaS | Simpler operator workflows while historical events remain available for reporting |

## Engineering the hard parts

The challenge is not collecting an RSVP. It is keeping the system correct while people and automated processes change the same event at different times.

A member might confirm an invitation, later change their +1, cancel before the deadline or already be attending as another member's guest. At the same time, another invitation wave may be scheduled to run automatically. Each action can affect capacity and who should be contacted next.

RSVP Society handles those workflows as shared state rather than isolated features. DynamoDB conditional writes and transactions protect RSVP, guest and capacity changes from conflicting updates. Confirmed headcount uses revision-aware reconciliation so a stale repair cannot overwrite a concurrent change. Members already attending as guests are excluded from later invitation waves. Audiences can be reviewed before a wave is sent, while send-time validation checks current eligibility and consent. Claims and receipt records reduce duplicate processing when SMS webhooks, scheduled work or Lambda invocations retry.

Automation is intentionally bounded. Invitation waves and reminders can progress without constant operator involvement, but the host can inspect audiences, pause automation and retain control over decisions that should not be delegated.

Jade follows the same boundary. Event context and current member state give the model enough information to answer naturally, but generated language cannot alter attendance, capacity or guest records unless the underlying application successfully performs that operation.

## Architecture and engineering decisions

| Component | Role | Why this choice |
| --- | --- | --- |
| JavaScript frontend | Membership signup and operations dashboard | Keeps the client lightweight while backend APIs remain authoritative |
| API Gateway + Python Lambda | Membership, events, invitations, SMS, reminders and check-in workflows | Event traffic is intermittent, making serverless compute a better fit than continuously running application servers |
| DynamoDB | Members, events, invitations, RSVP state, attendance and operational records | Conditional writes and transactions support concurrent workflows without managing database infrastructure |
| EventBridge Scheduler | Invitation waves, follow-ups and timed reminders | Schedules work for the time it is needed instead of continuously polling |
| SMS provider + language model | Guest communication and contextual responses | Combines a familiar communication channel with conversational interaction while business rules remain in application code |
| CloudFront + AWS WAF | Public delivery and API-edge protection | Adds caching, TLS, origin controls and request filtering without adding application servers |
| Terraform + GitHub Actions | Infrastructure and delivery | Makes infrastructure reproducible and gives application and infrastructure changes an automated validation path |

## Built for efficient operation

RSVP Society is designed around an event business whose workload comes in bursts rather than remaining constant.

AWS Lambda and on-demand DynamoDB allow the application to scale with activity without maintaining continuously running application servers. EventBridge schedules invitation waves and reminders only when work needs to occur. CloudFront handles cached delivery of static content, while storage lifecycle policies and bounded log retention limit unnecessary long-term storage.

The communication path is also designed to control usage. Deterministic application logic handles routine state changes, duplicate processing is suppressed, and the language model is reserved for interactions where conversational understanding adds value.

The result is an architecture designed to remain lightweight between events while still supporting periods of concentrated invitation, RSVP and check-in activity.

## Security and trust

RSVP Society handles private events and member information, so access and communication boundaries are part of the application design.

Administrative routes require authentication, inbound SMS webhooks use timestamped HMAC signature verification, credentials are stored through AWS Secrets Manager, and Lambda functions use responsibility-specific IAM roles. CloudFront and AWS WAF protect the public API path, while consent and opt-out controls govern outbound messaging.

Private event information follows release rules rather than being exposed by default. Operational records use retention controls where appropriate, the audit trail uses point-in-time recovery, and the private photo store blocks public S3 access in favor of CloudFront Origin Access Control.

CI validates the application and infrastructure before deployment, audits Python dependencies, builds the Lambda artifact, and validates Terraform. Production infrastructure deployment is an explicit workflow action rather than an automatic side effect of every push to main.

## Why I built it

RSVP Society grew out of firsthand experience with event promotion and operations. The technology changed, but the underlying problems were familiar: deciding who to invite, balancing capacity, tracking responses, communicating with guests and keeping the door organized when plans inevitably change.

I built the platform to turn those operational problems into an engineering system—combining product design, Python application development, event-driven AWS services, infrastructure as code, automation, security controls and conversational AI.

The result is not a collection of disconnected cloud services built for a demo. Each technical decision supports a real workflow and a specific operational problem.
