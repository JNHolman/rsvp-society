# RSVP Society

RSVP Society helps the hosts of curated R&B events build a community and run each event from the first invitation through the door.

## Meet Jade

Jade is RSVP Society's text concierge. Guests can ask her about an event, reply to an invitation, confirm or cancel, and manage a plus-one by text. She uses the event details to answer questions and shares private arrival information only with confirmed guests.

## How an event works

1. **The hosts plan a party.** They set the date, location, capacity, guest details, and which information should be shared after confirmation.
2. **The hosts choose who to invite.** Members are organized by their history with RSVP Society. Invitations prioritize the people who have shown up and supported past events.
3. **Invitations go out in waves.** Guests get time to respond before the next wave. Jade checks replies and plus-ones against the available seats; the next wave is scheduled only when more guests are needed.
4. **Jade helps guests by text.** She handles replies and common questions, shares event details at the right time, and respects opt-outs.
5. **The door team checks people in.** The guest list covers members and their confirmed plus-ones. A star marks a plus-one who is not already a member, so the host can invite them to join.
6. **The hosts learn from the night.** Attendance and cancellations help shape future invitation tiers and event planning.

The system supports one live event at a time. It is built for a growing, curated community across cities, with one event per day at most.

## What is in this repository

- The public RSVP Society website.
- The private host dashboard for members, events, invitations, check-in, and attendance.
- Jade's SMS and event workflows.
- The cloud infrastructure and deployment workflow.

## For hosts and maintainers

See [Jade's behavior](JADE.md) for how the text concierge works and [the validation checklist](VALIDATION.md) for deployment and live checks. [Current audit and release results](CURRENT_AUDIT_UPDATE.md) and [test record](RELEASE_VERIFICATION.txt) describe the reviewed source.

The current source has passed its automated checks, but it has not been validated against live AWS, SMS, or event data. The validation checklist covers what remains before production use.
