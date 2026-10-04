#!/usr/bin/env bash
# export_clean.sh — production-clean export
# Produces:
#   frontend-deploy.zip   — complete public + admin frontend
#   lambda-bundle.zip     — Lambda Python files only (no test/audit/dev files)
#   terraform-source.zip  — Terraform configs + canonical Lambda bundle
#
# Works from repo root OR from backend/lambda after being applied to the repo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_repo_root() {
  local dir="$SCRIPT_DIR"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "$dir/backend/lambda" && -d "$dir/backend/terraform" ]]; then
      echo "$dir"
      return 0
    fi
    if [[ "$(basename "$dir")" == "lambda" && -d "$dir" && -d "$(dirname "$dir")/terraform" ]]; then
      cd "$dir/../.." && pwd
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

REPO_ROOT="$(find_repo_root || true)"
if [[ -z "${REPO_ROOT:-}" ]]; then
  echo "ERROR: Could not locate repo root. Apply this script inside the full repo first." >&2
  exit 1
fi

LAMBDA_DIR="$REPO_ROOT/backend/lambda"
TERRAFORM_DIR="$REPO_ROOT/backend/terraform"
FRONTEND_DIR="$REPO_ROOT/frontend"
OUT="$REPO_ROOT/dist"
mkdir -p "$OUT"

echo "Building clean production artifacts..."
echo "  Repo root: $REPO_ROOT"
echo "  Output:    $OUT"
echo ""

# ── Lambda bundle (canonical build path) ─────────────────────────────────────
# build_lambda.sh is the single source of truth for runtime package contents.
# export_clean.sh must never maintain a second exclusion/copy list.
LAMBDA_OUT="$OUT/lambda-bundle.zip"
bash "$LAMBDA_DIR/build_lambda.sh"
cp "$TERRAFORM_DIR/lambda_bundle.zip" "$LAMBDA_OUT"
echo "  ✓ lambda-bundle.zip ($(du -sh "$LAMBDA_OUT" | cut -f1))"

# ── Terraform source ─────────────────────────────────────────────────────────
TERRAFORM_OUT="$OUT/terraform-source.zip"
rm -f "$TERRAFORM_OUT"
if [[ -f "$TERRAFORM_DIR/.terraform.lock.hcl" ]]; then
  (cd "$TERRAFORM_DIR" && zip -q "$TERRAFORM_OUT" *.tf .terraform.lock.hcl lambda_bundle.zip)
else
  (cd "$TERRAFORM_DIR" && zip -q "$TERRAFORM_OUT" *.tf lambda_bundle.zip)
fi
echo "  ✓ terraform-source.zip ($(du -sh "$TERRAFORM_OUT" | cut -f1))"

# ── Frontend deploy ──────────────────────────────────────────────────────────
FRONTEND_OUT="$OUT/frontend-deploy.zip"
if [[ -d "$FRONTEND_DIR" ]]; then
  rm -f "$FRONTEND_OUT"
  (cd "$FRONTEND_DIR" && zip -q -r "$FRONTEND_OUT" . \
    --exclude "*.DS_Store" --exclude "*__MACOSX*" \
    --exclude "*.pyc")
  echo "  ✓ frontend-deploy.zip ($(du -sh "$FRONTEND_OUT" | cut -f1))"
else
  echo "  ⚠ frontend dir not found at $FRONTEND_DIR — skipping"
fi

echo ""
echo "Deploy order:"
echo "  1. Build canonical Lambda bundle: backend/lambda/build_lambda.sh"
echo "  2. Use the manual GitHub deploy workflow; it deploys CloudFront and waits before applying WAF"
echo "     Review the production plan and origin-secret configuration; do not use a one-step apply"
echo "  3. Deploy dist/frontend-deploy.zip to frontend host"
echo "  4. python3 backend/lambda/smoke_test.py --api https://api.rsvpsociety.com"
