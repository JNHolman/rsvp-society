# RSVP Society

**A private event operations platform for managing a curated guest experience from membership through attendance.**

Private events become difficult to manage long before anyone reaches the door. Hosts need to build a trusted audience, decide who should be invited first, manage limited capacity, track replies and guests, communicate changes and know who actually attended.

RSVP Society brings that work into one system. Members request access and join a reusable community after approval. Hosts create events and select audiences, invitation waves expand as capacity remains, guests RSVP and manage +1s by text, and check-in records actual attendance.

## What it solves

- **Reusable membership:** Build an approved community instead of rebuilding a guest list for every event.
- **Prioritized invitations:** Give stronger audiences the first opportunity before expanding invitations to additional eligible members.
- **Live guest management:** Keep RSVPs, +1s, cancellations and expected attendance connected as the event changes.
- **Less manual texting:** Let Jade handle routine guest communication while the host retains decisions and exceptions.
- **Private event control:** Release venue details according to event policy instead of exposing them publicly.
- **Attendance intelligence:** Use check-in history to inform future invitation priority.

## How RSVP Society works

1. **Build the community.** People request membership and provide basic information such as location. The host reviews each request before approving access.
2. **Create the event.** The host defines capacity, guest rules, event details and when private information such as the venue should be released.
3. **Invite the right audience.** Invitation waves give priority members the first opportunity to respond before expanding to additional members.
4. **Manage the conversation.** Jade handles invitations, RSVPs, +1s, cancellations, reminders and common questions through text while the application enforces event rules.
5. **Keep the guest list current.** Confirmations, cancellations and guest changes update expected attendance as the event changes.
6. **Run the door.** Check-in distinguishes members from non-member guests and records who actually attended.
7. **Grow the community.** Non-member guests can be identified as potential future members after experiencing an event.
8. **Improve future events.** Attendance history helps the host make better decisions about future invitations.

## Meet Jade

Jade is RSVP Society's text host—the personality guests interact with from invitation through event day. She is designed to be confident, concise and socially fluent: someone who knows the event and the people coming to it, not a chatbot or scripted customer-service agent.

She handles invitations, RSVPs, +1s, cancellations, reminders and event questions, but her authority is deliberately limited. Jade can interpret a message and use current event and member context; she cannot invent event details, override capacity, approve membership, bypass invitation rules or claim a change succeeded when the application did not save it.

**The language model owns the conversation; the application owns the truth.**

Deterministic services remain authoritative for membership, invitation eligibility, RSVP state, guests, cancellations, capacity, venue-release rules and host approvals. Requests requiring judgment are handed back to the host instead of being improvised by the model.

## Product decisions

| Decision | Business reason | Engineering consequence |
| --- | --- | --- |
| Membership before invitations | Build a reusable audience across events | Persistent member profiles, location, tiers and attendance history |
| Staged invitation waves | Prioritize stronger audiences while continuing to fill capacity | Scheduled waves, eligibility rechecks and operator controls |
| Attendance-based tiers | Actual attendance is a stronger signal than an RSVP alone | Check-in and invitation history contribute to future priority |
| SMS as the guest channel | Guests can respond without another app | Webhooks, consent controls, idempotency and conversational state |
| Jade for routine conversation | Keep host attention on decisions and exceptions | AI handles language; deterministic services control state |
| Human escalation | Group arrangements and exceptions require judgment | Explicit host handoff rather than model improvisation |
| Configurable venue release | Private events may require tighter information control | Venue visibility becomes an enforced event policy |
| +1 and cancellation deadlines | Late changes affect capacity and door operations | Time-aware backend rules |
| One active event | The current operation does not need general event-SaaS complexity | Simpler operations with historical reporting retained |

## Engineering the hard parts

The challenge is not collecting an RSVP. It is keeping the system correct while people and automated processes change the same event at different times.

A member can confirm, change a +1, cancel before the deadline or already be attending as another member's guest while another invitation wave is preparing to run. Each action can affect capacity and who should be contacted next.

RSVP Society treats these as shared-state problems. DynamoDB conditional writes and transactions protect RSVP, guest and capacity changes. Confirmed headcount uses revision-aware reconciliation so stale work cannot overwrite a concurrent change. Members already attending as guests are excluded from later invitation waves. Reviewed audiences are revalidated at send time against current eligibility and consent. Claims and receipt records reduce duplicate processing when webhooks, scheduled work or Lambda invocations retry.

Automation is bounded rather than absolute: waves and reminders can progress without constant operator involvement, while the host can inspect audiences, pause automation and retain decisions that should not be delegated.

## Architecture and engineering decisions

| Component | Role | Why this choice |
| --- | --- | --- |
| JavaScript frontend | Membership signup and operations dashboard | Lightweight client with authoritative backend APIs |
| API Gateway + Python Lambda | Membership, events, invitations, SMS, reminders and check-in | Serverless compute fits intermittent event traffic |
| DynamoDB | Members, events, invitations, RSVP state and attendance | Conditional writes and transactions support concurrent workflows without database servers |
| EventBridge Scheduler | Invitation waves, follow-ups and reminders | Runs work when needed instead of continuously polling |
| SMS provider + language model | Guest communication and contextual responses | Familiar guest channel with business rules kept in application code |
| CloudFront + AWS WAF | Public delivery and API-edge protection | Caching, TLS, origin controls and request filtering without application servers |
| Terraform + GitHub Actions | Infrastructure and delivery | Reproducible infrastructure with automated application and IaC validation |

## Efficient by design

The workload is bursty, so the infrastructure is designed not to run like a continuously busy application. Lambda and on-demand DynamoDB scale with activity, EventBridge schedules work only when needed, and CloudFront caches static delivery. Storage lifecycle policies and bounded log retention limit unnecessary persistence.

The communication path follows the same principle: deterministic code handles routine state changes and duplicate suppression, while the language model is reserved for interactions where conversational understanding adds value.

## Security and trust

Private events and member information require explicit access and communication boundaries. Administrative routes require authentication, inbound SMS webhooks use timestamped HMAC verification, credentials are stored in AWS Secrets Manager, and Lambda functions use responsibility-specific IAM roles. CloudFront and AWS WAF protect the public API path, while consent and opt-out controls govern outbound messaging.

Operational data uses retention controls where appropriate. The audit trail has point-in-time recovery, and the private photo store blocks public S3 access in favor of CloudFront Origin Access Control. CI audits Python dependencies, validates application code and Terraform, and builds the Lambda artifact; production infrastructure deployment remains an explicit workflow action.

## Why I built it

RSVP Society grew out of firsthand experience with event promotion and operations: deciding who to invite, balancing capacity, tracking responses, communicating with guests and keeping the door organized when plans change.

I built the platform to turn those operational problems into an engineering system combining product design, Python, event-driven AWS services, infrastructure as code, automation, security controls and conversational AI. Each technical decision supports a real workflow rather than existing simply to demonstrate a cloud service.
