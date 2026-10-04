#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$HERE"

echo '== Python names and dead imports/locals =='
python -m ruff check --select F "$HERE" "$ROOT/backend/tools"

echo '== Python syntax/compile =='
python -m compileall -q "$HERE" "$ROOT/backend/tools"

echo '== Frontend JavaScript syntax =='
while IFS= read -r -d '' file; do
  node --input-type=module --check < "$file" >/dev/null
done < <(find "$ROOT/frontend" -type f -name '*.js' -print0)

echo '== Step 1 Python characterization + known-defect contracts =='
python -m unittest characterization_tests known_defect_contract_tests -v

echo '== Step 1 frontend execution contracts =='
node --test frontend_characterization_tests.mjs

echo '== Production reconciliation tool contracts =='
python "$ROOT/backend/tools/test_production_reconcile.py"

echo '== Canonical Lambda packaging contract =='
bash "$HERE/build_lambda.sh" >/dev/null
python - <<'PY'
import zipfile
from pathlib import Path
z = Path('../terraform/lambda_bundle.zip')
with zipfile.ZipFile(z) as bundle:
    names = set(bundle.namelist())
forbidden = {
    'integration_tests.py', 'characterization_tests.py', 'known_defect_contract_tests.py',
    'route_contract_audit.py', 'runtime_integration_check.py', 'jade_behavior_audit.py',
    'jade_scenario_tests.py', 'jade_v2_contract_tests.py', 'smoke_test.py',
}
leaked = sorted(forbidden & names)
if leaked:
    raise SystemExit(f'Lambda bundle contains dev/test files: {leaked}')
required = {'admin_handler.py', 'sms_handler.py', 'invite_handler.py', 'reminder_handler.py', 'jade_service.py', 'sms_webhook.py'}
missing = sorted(required - names)
if missing:
    raise SystemExit(f'Lambda bundle missing runtime modules: {missing}')
print(f'canonical Lambda bundle clean: {len(names)} runtime modules')
PY

echo '== Existing contract/audit baseline =='
python jade_scenario_tests.py
python jade_behavior_audit.py
python jade_v2_contract_tests.py
python runtime_integration_check.py
python route_contract_audit.py >/dev/null

echo '== Existing Moto integration suite =='
if python - <<'PY' >/dev/null 2>&1
import importlib.util
raise SystemExit(0 if importlib.util.find_spec('moto') else 1)
PY
then
  python -m unittest integration_tests revision_tests -v
else
  echo 'SKIP: moto is not installed in this environment; existing integration_tests.py was not executed.'
fi

echo '== Targeted branch-aware coverage report =='
if command -v coverage >/dev/null 2>&1; then
  coverage erase
  coverage run --branch -m unittest characterization_tests known_defect_contract_tests >/dev/null 2>&1
  coverage report \
    sms_handler.py sms_intent.py sms_webhook.py sms_plus_one.py sms_delivery.py sms_host_approval.py jade_prompt.py jade_service.py location_resolver.py \
    invite_handler.py invite_logic.py invite_sender.py invite_job_store.py member_store.py attendance_store.py reminder_handler.py reminder_schedule.py \
    admin_member_routes.py admin_event_routes.py access_request.py admin_handler.py
else
  echo 'SKIP: coverage command is not installed.'
fi

echo 'Step 1 baseline checks passed.'
