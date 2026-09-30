# RSVP Society

RSVP Society is an invite-only event operations platform built to manage a curated community's guest journey—from access request through event attendance and follow-up.

## The product

Hosts use a web dashboard to review member requests, plan events, coordinate invitations, track responses, and manage check-in. Guests can interact by text with Jade, the event concierge, to ask event questions, respond to invitations, update a plus-one, or opt out.

The platform brings these activities into one operating flow so hosts can manage guest communication and attendance consistently across events.

## Guest and host journey

1. A guest requests access and waits for host approval.
2. Hosts set up an event and choose the invitation audience.
3. Invitations and reminders go out in paced waves.
4. Guests respond by text; confirmations and plus-ones are tracked against event capacity.
5. The door team checks in confirmed guests and their plus-ones.
6. Attendance data supports post-event reporting and future planning.

## Platform at a glance

- **Guest experience:** Public website for access requests and event information.
- **Host experience:** Private dashboard for members, events, invitations, attendance, and reporting.
- **Application services:** Serverless backend, managed data storage, SMS messaging, and an AI-assisted text concierge.
- **Infrastructure and delivery:** AWS resources defined with Terraform and managed through GitHub Actions.

## Engineering focus

The project brings together privacy-aware event information, invitation pacing, RSVP and plus-one capacity tracking, opt-out handling, and reliable attendance records. Those constraints make it a practical example of designing a cloud application around real operational workflows.

**Technology:** JavaScript · Python · AWS · Terraform · GitHub Actions · SMS integration

## Project discussion

Questions about the product workflow, architecture, or engineering trade-offs are welcome through GitHub Issues.