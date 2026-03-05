#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

ROUTE_CONST_RE = re.compile(r"([A-Z0-9_]+):\s*'([^']+)'")
RESOURCE_BLOCK_RE = re.compile(r'resource\s+"aws_api_gateway_resource"\s+"([^"]+)"\s*\{(.*?)\n\}', re.S)
METHOD_BLOCK_RE = re.compile(r'resource\s+"aws_api_gateway_method"\s+"([^"]+)"\s*\{(.*?)\n\}', re.S)
PATH_PART_RE = re.compile(r'path_part\s*=\s*"([^"]+)"')
PARENT_RESOURCE_RE = re.compile(r'parent_id\s*=\s*aws_api_gateway_resource\.([A-Za-z0-9_]+)\.id')
RESOURCE_ID_RE = re.compile(r'resource_id\s*=\s*aws_api_gateway_resource\.([A-Za-z0-9_]+)\.id')
ROOT_PARENT_RE = re.compile(r'parent_id\s*=\s*aws_api_gateway_rest_api\.[A-Za-z0-9_]+\.root_resource_id')
HTTP_METHOD_RE = re.compile(r'http_method\s*=\s*"([A-Z]+)"')
BACKEND_ROUTE_METHOD_RE = re.compile(r'if\s+method\s*==\s*"([A-Z]+)"\s+and\s+path\.endswith\("([^"]+)"\)')
IN_PATH_ROUTE_RE = re.compile(r'if\s+method\s*==\s*"([A-Z]+)"\s+and\s+"([^"]+)"\s+in\s+path')
HARDCODED_ROUTE_RE = re.compile(r'''['"](/admin/[^'"]+|/event(?:/current)?)['"]''')
FRONTEND_CALL_RE = re.compile(r'api(?:Json|Fetch)\(\s*(?:`[^`]*ROUTES\.([A-Z0-9_]+)[^`]*`|ROUTES\.([A-Z0-9_]+))', re.S)
METHOD_IN_WINDOW_RE = re.compile(r'method\s*:\s*["\']([A-Z]+)["\']')
INLINE_BODY_RE = re.compile(r'body\s*:\s*\{(.*?)\}', re.S)
BODY_COLON_KEY_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*:')
BODY_SHORTHAND_KEY_RE = re.compile(r'(^|[,\n])\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?=,|\n|$)')

BACKEND_REQUIRED_KEYS: Dict[str, Dict[str, List[str]]] = {
    '/admin/members': {'DELETE': ['phone']},
    '/admin/members/status': {'POST': ['phone', 'status']},
    '/admin/members/gender': {'POST': ['phone', 'gender']},
    '/admin/members/tier': {'POST': ['phone', 'tier']},
    '/admin/members/attendance': {'POST': ['phone']},
    '/admin/members/import': {'POST': ['members']},
    '/admin/event': {'POST': ['date']},
    '/admin/invite/preview': {'POST': ['eventId', 'capacity', 'femalePercent', 'tier2BufferPct', 'waveNumber', 'waveSize']},
    '/admin/invite/send': {'POST': ['eventId', 'capacity', 'femalePercent', 'tier2BufferPct', 'waveNumber', 'waveSize', 'phones', 'confirmSend', 'removedPhones']},
    '/admin/invite/reminder': {'POST': ['timing']},
}

FRONTEND_BODY_HINTS: Dict[str, Dict[str, List[str]]] = {
    'ADMIN_EVENT': {'POST': ['eventSlug', 'event_label', 'date', 'startTime', 'city', 'capacity', 'event_timezone', 'venue', 'address', 'dresscode', 'revealVenue', 'event_type', 'vibe_tag', 'description', 'invite_template', 'reminder_template', 'day_before_template', 'day_of_template', 'reminderTiming']},
    'ADMIN_INVITE_SEND': {'POST': ['eventId', 'capacity', 'femalePercent', 'tier2BufferPct', 'waveNumber', 'waveSize', 'phones', 'confirmSend', 'removedPhones']},
}

QUO_BONE_CHECKS = {
    'terraform_quo_secret_variable': ('backend/terraform/main.tf', 'variable "quo_api_key_secret_id"'),
    'terraform_quo_phone_number_variable': ('backend/terraform/main.tf', 'variable "quo_phone_number_id"'),
    'terraform_webhook_secret_variable': ('backend/terraform/main.tf', 'variable "webhook_secret_id"'),
    'terraform_sms_provider_quo': ('backend/terraform/main.tf', 'SMS_PROVIDER             = "quo"'),
    'terraform_quo_key_env': ('backend/terraform/main.tf', 'QUO_API_KEY_SECRET_ID'),
    'terraform_quo_phone_env': ('backend/terraform/main.tf', 'QUO_PHONE_NUMBER_ID'),
    'terraform_sms_enabled_default': ('backend/terraform/main.tf', 'SMS_ENABLED              = "true"'),
    'eventbridge_sms_enabled_default': ('backend/terraform/eventbridge.tf', 'SMS_ENABLED           = "true"'),
    'sms_adapter_quo_secret_lookup': ('backend/lambda/sms_adapter.py', 'QUO_API_KEY_SECRET_ID'),
    'sms_adapter_live_send': ('backend/lambda/sms_adapter.py', 'https://api.openphone.com/v1/messages'),
    'sms_handler_quo_signature': ('backend/lambda/sms_handler.py', 'openphone-signature'),
    'admin_handler_welcome_hook': ('backend/lambda/admin_handler.py', 'maybe_send_welcome'),
    'admin_handler_welcome_gate': ('backend/lambda/admin_handler.py', 'status == "APPROVED" and prev_status != "APPROVED"'),
}


def detect_root(script_path: Path, explicit_root: Optional[Path]) -> Path:
    if explicit_root:
        return explicit_root.resolve()
    candidates = [script_path.resolve().parents[2], script_path.resolve().parents[1], Path.cwd()]
    for candidate in candidates:
        if (candidate / 'frontend').exists() and (candidate / 'backend' / 'lambda').exists() and (candidate / 'backend' / 'terraform').exists():
            return candidate.resolve()
    raise SystemExit('Could not locate project root containing frontend/, backend/lambda/, and backend/terraform/.')


def load_frontend_routes(frontend_dir: Path) -> Dict[str, str]:
    text = (frontend_dir / 'admin' / 'constants.js').read_text()
    match = re.search(r"const ROUTES = Object\.freeze\(\{(.*?)\}\);", text, re.S)
    if not match:
        raise SystemExit('Could not parse ROUTES from frontend/admin/constants.js')
    return dict(ROUTE_CONST_RE.findall(match.group(1)))


def load_frontend_contracts(frontend_dir: Path, frontend_routes: Dict[str, str]) -> Dict[str, Dict[str, object]]:
    contracts: Dict[str, Dict[str, object]] = {name: {'path': path, 'methods': set(), 'body_keys': {}} for name, path in frontend_routes.items()}
    for js_path in (frontend_dir / 'admin').glob('*.js'):
        if js_path.name == 'constants.js':
            continue
        text = js_path.read_text()
        for match in FRONTEND_CALL_RE.finditer(text):
            route_name = match.group(1) or match.group(2)
            if route_name not in contracts:
                continue
            window = text[match.start():match.start() + 500]
            method_match = METHOD_IN_WINDOW_RE.search(window)
            method = method_match.group(1) if method_match else 'GET'
            contracts[route_name]['methods'].add(method)
            body_match = INLINE_BODY_RE.search(window)
            if body_match:
                body_text = body_match.group(1)
                keys = set(BODY_COLON_KEY_RE.findall(body_text))
                keys.update(m.group(2) for m in BODY_SHORTHAND_KEY_RE.finditer(body_text))
                if keys:
                    contracts[route_name]['body_keys'].setdefault(method, set()).update(keys)
    for route_name, method_map in FRONTEND_BODY_HINTS.items():
        if route_name not in contracts:
            continue
        for method, keys in method_map.items():
            contracts[route_name]['methods'].add(method)
            contracts[route_name]['body_keys'].setdefault(method, set()).update(keys)
    for spec in contracts.values():
        spec['methods'] = sorted(spec['methods'])
        spec['body_keys'] = {method: sorted(keys) for method, keys in spec['body_keys'].items()}
    return contracts


def load_backend_contracts(lambda_dir: Path) -> Dict[str, Dict[str, object]]:
    contracts: Dict[str, Dict[str, object]] = {}
    for path in lambda_dir.glob('*.py'):
        if path.name == 'route_contract_audit.py':
            continue
        text = path.read_text()
        for method, route in BACKEND_ROUTE_METHOD_RE.findall(text):
            spec = contracts.setdefault(route, {'methods': set(), 'required_body_keys': {}})
            spec['methods'].add(method)
        for method, route in IN_PATH_ROUTE_RE.findall(text):
            spec = contracts.setdefault(route, {'methods': set(), 'required_body_keys': {}})
            spec['methods'].add(method)
    for route, by_method in BACKEND_REQUIRED_KEYS.items():
        spec = contracts.setdefault(route, {'methods': set(), 'required_body_keys': {}})
        for method, keys in by_method.items():
            spec['methods'].add(method)
            spec['required_body_keys'][method] = sorted(keys)
    for spec in contracts.values():
        spec['methods'] = sorted(spec['methods'])
    return contracts


def load_frontend_hardcoded(frontend_dir: Path) -> List[Tuple[str, str]]:
    findings: List[Tuple[str, str]] = []
    files = list(frontend_dir.glob('*.html')) + list((frontend_dir / 'admin').glob('*'))
    for path in files:
        if path.suffix not in {'.js', '.html'} or path.name == 'constants.js':
            continue
        text = path.read_text()
        for route in HARDCODED_ROUTE_RE.findall(text):
            findings.append((path.relative_to(frontend_dir).as_posix(), route))
    return sorted(set(findings))


def parse_tf_resources(terraform_dir: Path) -> Dict[str, Dict[str, Optional[str]]]:
    resources: Dict[str, Dict[str, Optional[str]]] = {}
    for tf_path in terraform_dir.glob('*.tf'):
        text = tf_path.read_text()
        for name, body in RESOURCE_BLOCK_RE.findall(text):
            path_part = PATH_PART_RE.search(body)
            parent = PARENT_RESOURCE_RE.search(body)
            is_root = bool(ROOT_PARENT_RE.search(body))
            resources[name] = {
                'path_part': path_part.group(1) if path_part else None,
                'parent': parent.group(1) if parent else None,
                'is_root': is_root,
            }
    return resources


def _resource_path(name: str, resources: Dict[str, Dict[str, Optional[str]]]) -> Optional[str]:
    if name not in resources:
        return None
    parts: List[str] = []
    current = name
    seen: Set[str] = set()
    while current and current in resources and current not in seen:
        seen.add(current)
        item = resources[current]
        if item['path_part']:
            parts.append(item['path_part'])
        if item['is_root']:
            break
        current = item['parent']
    return '/' + '/'.join(reversed(parts)) if parts else None


def load_terraform_methods(terraform_dir: Path, resources: Dict[str, Dict[str, Optional[str]]]) -> Dict[str, Set[str]]:
    methods: Dict[str, Set[str]] = {}
    for tf_path in terraform_dir.glob('*.tf'):
        text = tf_path.read_text()
        for _, body in METHOD_BLOCK_RE.findall(text):
            resource_match = RESOURCE_ID_RE.search(body)
            http_match = HTTP_METHOD_RE.search(body)
            if not resource_match or not http_match:
                continue
            route = _resource_path(resource_match.group(1), resources)
            if route:
                methods.setdefault(route, set()).add(http_match.group(1))
    return methods


def check_quo_bones(root: Path) -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for name, (rel_path, needle) in QUO_BONE_CHECKS.items():
        try:
            text = (root / rel_path).read_text()
            out[name] = needle in text
        except FileNotFoundError:
            out[name] = False
    return out


def build_summary(root: Path) -> Dict[str, object]:
    frontend_dir = root / 'frontend'
    lambda_dir = root / 'backend' / 'lambda'
    terraform_dir = root / 'backend' / 'terraform'

    frontend_routes = load_frontend_routes(frontend_dir)
    frontend_contracts = load_frontend_contracts(frontend_dir, frontend_routes)
    backend_contracts = load_backend_contracts(lambda_dir)
    tf_resources = parse_tf_resources(terraform_dir)
    terraform_methods = {route: sorted(methods) for route, methods in load_terraform_methods(terraform_dir, tf_resources).items()}
    hardcoded = load_frontend_hardcoded(frontend_dir)

    missing_in_backend = {}
    missing_in_terraform = {}
    method_mismatches = {}
    body_key_gaps = {}

    for name, frontend_spec in frontend_contracts.items():
        route = frontend_spec['path']
        frontend_methods = set(frontend_spec['methods'])
        backend_spec = backend_contracts.get(route, {'methods': []})
        backend_methods = set(backend_spec['methods'])
        tf_methods = set(terraform_methods.get(route, []))
        tf_methods_no_options = {m for m in tf_methods if m != 'OPTIONS'}

        missing_backend_methods = sorted(frontend_methods - backend_methods)
        missing_tf_methods = sorted(frontend_methods - tf_methods_no_options)
        if missing_backend_methods:
            missing_in_backend[name] = {'route': route, 'missing_methods': missing_backend_methods}
        if missing_tf_methods:
            missing_in_terraform[name] = {'route': route, 'missing_methods': missing_tf_methods}
        if backend_methods and tf_methods_no_options and backend_methods != tf_methods_no_options:
            method_mismatches[name] = {
                'route': route,
                'frontend_methods': sorted(frontend_methods),
                'backend_methods': sorted(backend_methods),
                'terraform_methods': sorted(tf_methods),
            }

        frontend_body_keys = frontend_spec['body_keys']
        backend_body_keys = backend_spec.get('required_body_keys', {})
        for method, required_keys in backend_body_keys.items():
            supplied = set(frontend_body_keys.get(method, []))
            missing_keys = sorted(set(required_keys) - supplied)
            if missing_keys:
                body_key_gaps.setdefault(name, {})[method] = {
                    'route': route,
                    'required_keys': required_keys,
                    'frontend_keys': sorted(supplied),
                    'missing_keys': missing_keys,
                }

    return {
        'project_root': str(root),
        'frontend_routes': frontend_routes,
        'frontend_contracts_detected': frontend_contracts,
        'backend_contracts_detected': backend_contracts,
        'terraform_methods_detected': terraform_methods,
        'frontend_hardcoded_paths_outside_constants': hardcoded,
        'missing_in_backend': missing_in_backend,
        'missing_in_terraform': missing_in_terraform,
        'method_mismatches': method_mismatches,
        'body_key_gaps': body_key_gaps,
        'quo_bones_check': check_quo_bones(root),
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Audit frontend routes/contracts against backend handlers, Terraform API methods, and Quo setup guardrails.')
    parser.add_argument('--root', type=Path, default=None, help='Project root containing frontend/, backend/lambda/, and backend/terraform/ directories.')
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = detect_root(Path(__file__), args.root)
    summary = build_summary(root)
    print(json.dumps(summary, indent=2, sort_keys=True))
    ok = (
        not summary['missing_in_backend']
        and not summary['missing_in_terraform']
        and not summary['method_mismatches']
        and not summary['body_key_gaps']
        and all(summary['quo_bones_check'].values())
    )
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
