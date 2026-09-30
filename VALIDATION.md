# Production Validation Checklist

Use this while running Terraform and verifying the deployed RSVP Society platform.

## 1. Terraform

Run from `backend/terraform`:

```bash
terraform fmt -recursive
terraform validate
terraform plan -input=false
```

Apply through the manual GitHub deploy workflow after reviewing the plan. The
workflow stages CloudFront before WAF so the origin rule cannot block traffic
during CloudFront propagation.

Before deployment, create the GitHub Actions secret
`CLOUDFRONT_ORIGIN_VERIFY_HEADER` with at least 32 random characters. The deploy
workflow applies the CloudFront origin header and viewer-IP function first,
waits for the distribution to finish deploying, and only then applies the WAF
origin-block rule. A missing/short secret must fail the workflow before plan or
apply. Confirm Quo's webhook URL is `https://api.rsvpsociety.com/...`, never the
raw `execute-api` hostname.

For secret rotation, keep availability during propagation: first set the
previous-header secret to the new value while the current secret remains old,
then deploy so WAF accepts both; next swap current to new and previous to old
and deploy; after CloudFront is fully deployed, clear the previous-header
secret and deploy once more. Never rotate by changing only the current secret.

Confirm Terraform manages:

- `rsvp-members`
- `rsvp-event-invites`
- `rsvp-events`
- `rsvp-event-history`
- `rsvp-checkins` with TTL enabled
- `rsvp-invite-jobs` with TTL enabled
- `rsvp-audit-log`
- API routes
- Lambda permissions
- EventBridge reminder rules
- CloudFront/API domain config

Verify the CloudFront API distribution removes viewer-supplied
`X-Forwarded-For`, CloudFront forwards the resulting viewer address, and the
regional API WAF rate-limits by that address. Inspect WAF sampled requests to
confirm the rate rules see the real viewer IP. A direct request to the API
Gateway execute-api hostname without the CloudFront origin secret must receive
a WAF block response. Verify Quo webhook delivery through the custom domain.

## 2. Browser smoke test

Open the deployed frontend and check the browser console.

### Public site

- Public form loads.
- Form cannot submit unless opt-in checkbox is checked.
- Successful signup creates a pending member.

### Admin login

- `/admin/` loads.
- Login visually matches the admin system.
- Invalid token shows a clean error.
- Valid token opens the admin dashboard.

### Members page

- Pending members show.
- Approve works.
- Deny works.
- Wrong approval code/error path is clean if applicable.
- Double-click/details modal works.
- Gender edit works on Members page.
- Tier edit works on Members page.
- Denied members do not appear in the normal approved invite pool.
- Search and pagination work.
- Per-event history displays invite/confirmation/attendance history.

### Event page

- Time zone sits near start/end time.
- Capacity appears after time/timezone.
- Venue/address/location fields are clear.
- Reveal venue checkbox is not buried.
- Plus-one checkbox sits near access/ticket controls.
- Reminder controls are near time/status.
- Save draft works.
- Save and make live works.
- Duplicate works.
- Archive works.
- Event messages save.
- Event Intelligence saves as the single event description/intelligence field.

### Invite page

- Search works.
- Market filter works.
- Tier filter works.
- Gender filter works.
- Invite status filter works.
- Preview Next Wave works.
- Preview creates a locked preview session.
- Send uses the locked preview.
- Wave display only shows Wave 1, Wave 2, Wave 3, and Manual / Resend.
- Already-invited members are excluded from normal next-wave preview.
- Manual override is clearly send-only and does not bypass gating.
- Send modal refreshes job status cleanly.
- No `Decimal is not JSON serializable` error appears.

### Analytics page

Verify the page renders backend totals directly:

- members invited
- members confirmed
- members attended
- +1 confirmed
- +1 attended
- +1 no-show
- expected headcount
- checked-in headcount
- no-show headcount
- show rate
- ghost rate
- wave-level show/ghost rates

Refresh should reload the selected event without resetting to the wrong event.

### Check-in page

- `/admin/checkin.html` loads.
- Login visually matches the admin system.
- Event selector/list loads.
- Confirmed member check-in works.
- Confirmed plus-one check-in works.
- Duplicate member check-in does not double-count.
- Duplicate plus-one check-in does not double-count.
- Unconfirmed person is blocked.
- Wrong event does not check in.
- Analytics updates after check-in.

## 3. Live SMS / DynamoDB test

Use test phone numbers and a test event.

### Signup and approval

1. Submit public form with opt-in checked.
2. Confirm member appears as `PENDING` in DynamoDB/admin.
3. Approve member.
4. Confirm welcome SMS sends.

### Event and invite

1. Create test event.
2. Add Event Intelligence.
3. Save draft.
4. Make event Live.
5. Preview Wave 1.
6. Confirm `rsvp-invite-jobs` has a locked preview session with TTL.
7. Send invite.
8. Confirm invite row is written to `rsvp-event-invites`.
9. Confirm SMS is received.

### Jade gating

Before confirmation, text Jade:

- `Where is it?`
- `What's the address?`
- `Is there parking?`
- `Send the ticket link.`

Expected: no venue/address/ticket/private logistics before confirmation.

After confirmation, verify allowed logistics only when they exist in structured event fields or Event Intelligence.

### Plus-one behavior

1. Confirm member by SMS.
2. Add a plus-one full name.
3. Confirm plus-one name stores on the invite row.
4. Use another confirmed member to submit the same plus-one name.
5. Confirm Jade blocks the duplicate and asks for someone else.

### Analytics headcount test

Scenario A — member checked in, +1 no-shows:

1. Member confirms with +1.
2. Check in member only.
3. Verify analytics:
   - expected headcount = 2
   - checked-in headcount = 1
   - +1 no-show = 1
   - no-show headcount = 1
   - show rate = 50%
   - ghost rate = 50%

Scenario B — member and +1 both check in:

1. Check in +1.
2. Verify analytics:
   - expected headcount = 2
   - checked-in headcount = 2
   - +1 no-show = 0
   - no-show headcount = 0
   - show rate = 100%
   - ghost rate = 0%

Scenario C — member and +1 both ghost:

1. Confirm another member with +1.
2. Do not check either in.
3. Verify analytics:
   - expected headcount = 2
   - checked-in headcount = 0
   - +1 no-show = 1
   - no-show headcount = 2
   - show rate = 0%
   - ghost rate = 100%

## 4. Final release criteria

Do not call the build production-clean until all are true:

- Terraform validates and applies cleanly.
- Admin browser console has no runtime errors.
- Invite send creates locked preview/session/job rows.
- Live SMS invite and reply flow works.
- Jade gates private details correctly.
- Plus-one duplicate handling works.
- Member and plus-one check-in work.
- Analytics math matches DynamoDB state.
- Admin login and check-in UI match the rest of the admin system.

## Jade scenario tests

Run the dependency-light Jade scenario guardrail suite after backend changes:

```bash
cd backend/lambda
AWS_EC2_METADATA_DISABLED=true AWS_DEFAULT_REGION=us-east-1 python3 jade_scenario_tests.py
```

This checks the key Jade behavior scenarios that should not regress: identity/mystery framing, correction handling, multi-question topic detection, unknown logistics fallbacks, plus-one capture safety, and private-event gates.

## Deep-audit regression smoke checks

- Confirmed guest replies NO while Jade awaits a guest first or last name: cancellation releases seats and clears name prompts.
- Cancel a member with no +1 and a member whose +1 has an accented name; verify counter and reservation map.
- Import must preserve a concurrently recorded STOP or deletion and leave original consent provenance intact without a new attestation.
- Replace a valid CSV with an empty/malformed file: no previous rows remain importable.
- Recheck an ATTENDED invitation after its check-in record has expired: lifetime attendance must not increment again.
- Verify the tier rate uses `attendedCount / (invitedCount - timelyCancellationCount)` and retains the existing 80% / 40% thresholds.
- Confirm a cancellation exactly 24 hours before local event start is exempt, one second later is not, and an unknown event time does not grant an exemption.
- Confirm reconfirmation removes a timely-cancellation exemption once, retries cannot double-credit it, and a NO_SHOW cannot be changed into an exempt cancellation.
- Confirm a +1 can be replaced 12 hours before event start without changing the total reserved headcount.


## Checks for the current audit fixes

- Run Wave 2 and Wave 3 with Auto sizing in a controlled test event; preview and send must use the same configured expected show rate.
- Confirm Netlify deployed `_headers`: admin HTML must return `X-Frame-Options: DENY` and a CSP header containing `frame-ancestors 'none'`. Meta tags are insufficient.
- Confirm the public `/event` and `/event/current` return an empty event object.
- Verify Quo's actual non-2xx redelivery behavior using a controlled failed inbound request; do not assume the local lease test proves provider retries. Confirm STOP/STOP ALL, HELP, and website reapplication/host approval against Quo's own suppression state.
- For an interrupted inbound message, verify a retry can reclaim the processing lease after 60 seconds and that completed redelivery does not produce another reply.
- Confirm the invite failure alarm reaches the existing SNS subscribers, and trigger a controlled worker error/timeout. Inspect recipients and provider delivery records before resending; an ambiguous provider timeout is not proof no text was sent.
- Verify future attendance rates display Pending; close the event and verify settled rates. Test Jade around local midnight and an overnight end time.
- Review copied date, venue, address, description, ticket link and expected show rate before publishing a duplicate.
- Review the updated privacy wording against actual account retention settings. It describes application behavior and does not certify legal or carrier compliance.
