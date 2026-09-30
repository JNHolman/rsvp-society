# Scale readiness review

Reviewed against the planned 2,500-member start and 5,000–7,500 members across three or more cities. This is a source review, not a load test or production certification.

## What the code already does

- Members, events and invite history use DynamoDB tables with on-demand billing. The code does not set a fixed 2,500-member limit.
- Invite waves and reminders use continuation jobs instead of sending an entire audience in one Lambda request. Invite batches are now capped at 4 addresses; reminders and confirmed-guest updates at 15, based on Quo's 15-second request timeout and the 300-second worker timeout.
- Check-in/no-show status and the member counter that affects tiers now change in one transaction.

## Remaining scale and operating risks

- One global `current` event fits the stated limit of one event per day and supports sequential city changes; simultaneous-city routing is not required for this operating plan. However, untagged SMS replies always follow the current pointer. After switching events, a late `YES` from the prior event could be applied to the new event if that person also has an open invite there. To remove the ambiguity, do not overlap RSVP windows for the same member, or add an event-specific reply code/context; simply switching pointers cannot identify what an untagged reply meant.
- Closing an event is resumable in 100-invite pages and locks further check-in while closing. The path has Moto coverage but still needs an AWS/API-timeout recovery check at target event sizes.
- The admin CSV importer now sends sequential batches of 25 rows; a file can contain more than 100 distinct ZIPs. A synthetic 7,500-row frontend test verifies batching and duplicate suppression, not real AWS/ZIP-provider throughput. Direct API calls are capped at 100 rows. Failed/uncertain batches stop the upload and require reviewing saved members before retrying.
- Search scans records, while analytics and attendance pages fetch every result page into the browser. Both grow with the member list.
- RSVP acceptance and wave planning share the event's expected show rate (60% default). Duplicating an event keeps the prior configured estimate; measured show rate is retained only in the completed event’s analytics. This estimate can still overfill a venue if attendance is higher; it is not a hard capacity cap.
- No live AWS/Quo test or 2,500/5,000/7,500 load run was performed. The current archive has no matching AWS usage export, provider message prices, event cadence or send volumes for a defensible dollar forecast.

The local suite validates logic and request shapes; it does not measure provider throughput, API latency, daily city transitions, monthly cost or real AWS transaction behavior. Terraform now includes an invalid-signature log alarm; no AWS Budget amount is configured because the archive has no verified bill or approved threshold.
