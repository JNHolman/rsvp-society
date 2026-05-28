#!/usr/bin/env bash
# export_clean.sh — production-clean export
# Produces:
#   frontend-deploy.zip   — frontend/admin files only
#   lambda-bundle.zip     — Lambda Python files only (no test/audit/dev files)
#   terraform-source.zip  — Terraform configs only
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
FRONTEND_DIR="$REPO_ROOT/frontend/admin"
OUT="$REPO_ROOT/dist"
mkdir -p "$OUT"

echo "Building clean production artifacts..."
echo "  Repo root: $REPO_ROOT"
echo "  Output:    $OUT"
echo ""

# ── Lambda bundle (no test/audit/dev files) ──────────────────────────────────
LAMBDA_OUT="$OUT/lambda-bundle.zip"
STAGING_LAMBDA="$(mktemp -d)"
trap 'rm -rf "$STAGING_LAMBDA"' EXIT

EXCLUDE_LAMBDA=(
  "integration_tests.py"
  "route_contract_audit.py"
  "runtime_integration_check.py"
  "smoke_test.py"
)

find "$LAMBDA_DIR" -maxdepth 1 -type f -name "*.py" | while read -r f; do
  fname="$(basename "$f")"
  skip=0
  for ex in "${EXCLUDE_LAMBDA[@]}"; do
    [[ "$fname" == "$ex" ]] && skip=1 && break
  done
  [[ $skip -eq 0 ]] && cp "$f" "$STAGING_LAMBDA/"
done

rm -f "$LAMBDA_OUT"
(cd "$STAGING_LAMBDA" && zip -q "$LAMBDA_OUT" *.py)
echo "  ✓ lambda-bundle.zip ($(du -sh "$LAMBDA_OUT" | cut -f1))"

# ── Terraform source ─────────────────────────────────────────────────────────
TERRAFORM_OUT="$OUT/terraform-source.zip"
rm -f "$TERRAFORM_OUT"
if [[ -f "$TERRAFORM_DIR/.terraform.lock.hcl" ]]; then
  (cd "$TERRAFORM_DIR" && zip -q "$TERRAFORM_OUT" *.tf .terraform.lock.hcl)
else
  (cd "$TERRAFORM_DIR" && zip -q "$TERRAFORM_OUT" *.tf)
fi
echo "  ✓ terraform-source.zip ($(du -sh "$TERRAFORM_OUT" | cut -f1))"

# ── Frontend deploy ──────────────────────────────────────────────────────────
FRONTEND_OUT="$OUT/frontend-deploy.zip"
if [[ -d "$FRONTEND_DIR" ]]; then
  rm -f "$FRONTEND_OUT"
  (cd "$FRONTEND_DIR" && zip -q -r "$FRONTEND_OUT" . \
    --exclude "*.DS_Store" --exclude "*__MACOSX*" \
    --exclude "*.pyc" --exclude "attendance.js" --exclude "attendance.css")
  echo "  ✓ frontend-deploy.zip ($(du -sh "$FRONTEND_OUT" | cut -f1))"
else
  echo "  ⚠ frontend dir not found at $FRONTEND_DIR — skipping"
fi

echo ""
echo "Deploy order:"
echo "  1. cd backend/terraform && terraform apply"
echo "  2. Deploy dist/lambda-bundle.zip to Lambda or via Terraform-controlled release"
echo "  3. Deploy dist/frontend-deploy.zip to frontend host"
echo "  4. python3 backend/lambda/smoke_test.py --api https://api.rsvpsociety.com"
