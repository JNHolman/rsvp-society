# RSVP Society audit handoff

The authoritative current source findings and decisions are in **CURRENT_AUDIT_UPDATE.md**. Executed checks and package boundaries are in **RELEASE_VERIFICATION.txt**. Use **VALIDATION.md** before deploying.

The full source and rebuilt Lambda, Terraform and frontend archives are included. Deploy artifacts come from `backend/lambda/export_clean.sh`; do not mix archives from separate releases.

Current verification: **312 Python tests and 22 frontend tests pass** (175 characterization/known-defect, 122 Moto integration, 15 reconciliation), plus Jade/runtime/route audits. Terraform files parse as HCL; native provider validation/plan and live checks have not run. Ruff F checks pass and the complete shell runner passes; optional coverage was skipped. Nothing was pushed or deployed.

Preserve the website reapplication/host approval flow, the tier/wave rules, manual audience overrides and nonmember guest star. No required START step, identity system or additional always-on service was added.

Read the remaining deployment checks before publishing. In particular, verify live Lambda drift, the two-stage CloudFront/WAF apply, Quo's suppression/retry behavior, Netlify response headers and SNS alert delivery. Inspect uncertain send outcomes before resending.

The delivery dashboard's event total can undercount if its summary write fails after an invite is marked delivered; see CURRENT_AUDIT_UPDATE.md. This is reporting-only and does not change RSVP or attendance state.
