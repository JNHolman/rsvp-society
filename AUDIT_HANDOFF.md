# RSVP Society audit handoff

The authoritative current source findings and decisions are in **CURRENT_AUDIT_UPDATE.md**. Executed checks and package boundaries are in **RELEASE_VERIFICATION.txt**. Use **VALIDATION.md** before deploying.

The full source and rebuilt Lambda, Terraform and frontend archives are included. Deploy artifacts come from `backend/lambda/export_clean.sh`; do not mix archives from separate releases.

Current verification counts and the automatic-wave tests are in **RELEASE_VERIFICATION.txt**. Terraform files parse as HCL; native provider validation/plan and live checks have not run. Ruff F checks pass. This release update is source-only; AWS has not been changed.

Preserve the website reapplication/host approval flow, the tier/wave rules, manual audience overrides and nonmember guest star. The timed follow-up uses one-time EventBridge schedules and the existing invite Lambda; no new always-on service was added.

Read the remaining deployment checks before publishing. In particular, verify live Lambda drift, the two-stage CloudFront/WAF apply, Quo's suppression/retry behavior, Netlify response headers and SNS alert delivery. Inspect uncertain send outcomes before resending.

The delivery dashboard's event total can undercount if its summary write fails after an invite is marked delivered; see CURRENT_AUDIT_UPDATE.md. This is reporting-only and does not change RSVP or attendance state.
