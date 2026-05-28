#!/usr/bin/env bash
# build_lambda.sh — creates a clean Lambda deployment package
# Lives in backend/lambda/ — run from project root: ./backend/lambda/build_lambda.sh
# Output: backend/terraform/lambda_bundle.zip

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDA_DIR="$SCRIPT_DIR"
OUTPUT="$SCRIPT_DIR/../terraform/lambda_bundle.zip"
STAGING="$(mktemp -d)"

echo "Building Lambda bundle..."
echo "  Source: $LAMBDA_DIR"
echo "  Output: $OUTPUT"

if [ ! -d "$LAMBDA_DIR" ]; then
  echo "ERROR: Lambda source not found at $LAMBDA_DIR" >&2
  rm -rf "$STAGING"
  exit 1
fi

find "$LAMBDA_DIR" -maxdepth 1 -name "*.py" \
  ! -name "integration_tests.py" \
  ! -name "route_contract_audit.py" \
  ! -name "runtime_integration_check.py" \
  -exec cp {} "$STAGING/" \;

FILE_COUNT=$(find "$STAGING" -name "*.py" | wc -l | tr -d ' ')
echo "  Bundling $FILE_COUNT Python files"

rm -f "$OUTPUT"
(cd "$STAGING" && zip -q "$OUTPUT" *.py)
rm -rf "$STAGING"

echo "  Done: $OUTPUT ($(du -sh "$OUTPUT" | cut -f1))"
echo "  Next: cd backend/terraform && terraform apply"
