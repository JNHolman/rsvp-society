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
HTTP_METHOD_RE = re.compile(r'http_method\s*=\s*"([^"]+)"')
BACKEND_ROUTE_METHOD_RE = re.compile(r'if\s+method\s*==\s*"([A-Z]+)"\s+and\s+path\.endswith\("([^"]+)"\)')
HARDCODED_ROUTE_RE = re.compile(r'''['"](/admin/[^'"]+|/event(?:/current)?)['"]''')
FRONTEND_CALL_RE = re.compile(r'api(?:Json|Fetch)\(\s*(?:`[^`]*ROUTES\.([A-Z0-9_]+)[^`]*`|ROUTES\.([A-Z0-9_]+))', re.S)
METHOD_IN_WINDOW_RE = re.compile(r'method\s*:\s*["\']([A-Z]+)["\']')
INLINE_BODY_RE = re.compile(r'body\s*:\s*\{(.*?)\}', re.S)
BODY_COLON_KEY_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*:')
BODY_SHORTHAND_KEY_RE = re.compile(r'(^|[,\n])\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?=,|\n|$)')
IN_PATH_ROUTE_RE = re.compile(r'if\s+method\s*==\s*"([A-Z]+)"\s+and\s+"([^"]+)"\s+in\s+path')

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
    'terraform_quo_secret_variable': ('terraform/main.tf', 'variable "quo_api_key_secret_id"'),
    'terraform_webhook_secret_variable': ('terraform/main.tf', 'variable "webhook_secret_id"'),
    'terraform_sms_provider_quo': ('terraform/main.tf', 'SMS_PROVIDER             = "quo"'),
    'terraform_quo_key_env': ('terraform/main.tf', 'QUO_API_KEY_SECRET_ID'),
    'terraform_sms_disabled_default': ('terraform/main.tf', 'SMS_ENABLED              = "false"'),
    'eventbridge_quo_env': ('terraform/eventbridge.tf', 'SMS_PROVIDER          = "quo"'),
    'sms_adapter_quo_secret_lookup': ('python/sms_adapter.py', 'QUO_API_KEY_SECRET_ID'),
    'sms_adapter_stub_send': ('python/sms_adapter.py', '[stub] would send'),
    'sms_handler_quo_signature': ('python/sms_handler.py', 'x-quo-signature'),
    'access_request_welcome_hook': ('python/access_request.py', 'maybe_send_welcome'),
}


def detect_root(script_path: Path, explicit_root: Optional[Path]) -> Path:
    if explicit_root:
        return explicit_root.resolve()
    candidates = [script_path.resolve().parent.parent, script_path.resolve().parent, Path.cwd()]
    for candidate in candidates:
        if (candidate / 'frontend').exists() and (candidate / 'python').exists() and (candidate / 'terraform').exists():
            return candidate.resolve()
    raise SystemExit('Could not locate project root containing frontend/, python/, and terraform/.')


def load_frontend_routes(frontend_dir: Path) -> Dict[str, str]:
    text = (frontend_dir / 'constants.js').read_text()
    match = re.search(r"const ROUTES = Object\.freeze\(\{(.*?)\}\);", text, re.S)
    if not match:
        raise SystemExit('Could not parse ROUTES from frontend/constants.js')
    return dict(ROUTE_CONST_RE.findall(match.group(1)))


def load_frontend_contracts(frontend_dir: Path, frontend_routes: Dict[str, str]) -> Dict[str, Dict[str, object]]:
    contracts: Dict[str, Dict[str, object]] = {name: {'path': path, 'methods': set(), 'body_keys': {}} for name, path in frontend_routes.items()}
    for js_path in frontend_dir.glob('*.js'):
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
                keys.update(match.group(2) for match in BODY_SHORTHAND_KEY_RE.finditer(body_text))
                keys = sorted(keys)
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


def load_backend_contracts(python_dir: Path) -> Dict[str, Dict[str, object]]:
    contracts: Dict[str, Dict[str, object]] = {}
    for path in python_dir.glob('*.py'):
        if path.name == 'route_contract_audit.py':
            continue
        text = path.read_text()
        for method, route in BACKEND_ROUTE_METHOD_RE.findall(text):
            spec = contracts.setdefault(route, {'methods': set(), 'required_body_keys': {}})
            spec['methods'].add(method)
        for method, route in IN_PATH_ROUTE_RE.findall(text):
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
    for path in frontend_dir.glob('*'):
        if path.suffix not in {'.js', '.html'} or path.name == 'constants.js':
            continue
        text = path.read_text()
        for match in HARDCODED_ROUTE_RE.findall(text):
            findings.append((path.name, match))
    return findings


def parse_tf_resources(terraform_dir: Path) -> Dict[str, Dict[str, Optional[str]]]:
    resources: Dict[str, Dict[str, Optional[str]]] = {}
    for tf_path in terraform_dir.glob('*.tf'):
        text = tf_path.read_text()
        for name, body in RESOURCE_BLOCK_RE.findall(text):
            path_part_match = PATH_PART_RE.search(body)
            if not path_part_match:
                continue
            parent_match = PARENT_RESOURCE_RE.search(body)
            resources[name] = {
                'path_part': path_part_match.group(1),
                'parent_resource': parent_match.group(1) if parent_match else None,
                'is_root_child': bool(ROOT_PARENT_RE.search(body)),
            }
    return resources


def build_full_path(resource_name: str, resources: Dict[str, Dict[str, Optional[str]]], cache: Dict[str, str]) -> str:
    if resource_name in cache:
        return cache[resource_name]
    node = resources.get(resource_name)
    if not node:
        return ''
    path_part = node['path_part'] or ''
    if node.get('is_root_child'):
        full_path = f'/{path_part}'
    else:
        parent_name = node.get('parent_resource')
        parent_path = build_full_path(parent_name, resources, cache) if parent_name else ''
        full_path = f'{parent_path}/{path_part}' if parent_path else f'/{path_part}'
    cache[resource_name] = full_path
    return full_path


def load_terraform_methods(terraform_dir: Path, resources: Dict[str, Dict[str, Optional[str]]]) -> Dict[str, Set[str]]:
    cache: Dict[str, str] = {}
    methods_by_route: Dict[str, Set[str]] = {}
    for tf_path in terraform_dir.glob('*.tf'):
        text = tf_path.read_text()
        for _name, body in METHOD_BLOCK_RE.findall(text):
            resource_match = RESOURCE_ID_RE.search(body)
            method_match = HTTP_METHOD_RE.search(body)
            if not resource_match or not method_match:
                continue
            route = build_full_path(resource_match.group(1), resources, cache)
            if route:
                methods_by_route.setdefault(route, set()).add(method_match.group(1))
    return methods_by_route


def check_quo_bones(root: Path) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    for name, (rel_path, needle) in QUO_BONE_CHECKS.items():
        try:
            text = (root / rel_path).read_text()
            results[name] = needle in text
        except FileNotFoundError:
            results[name] = False
    return results


def build_summary(root: Path) -> Dict[str, object]:
    frontend_dir = root / 'frontend'
    python_dir = root / 'python'
    terraform_dir = root / 'terraform'

    frontend_routes = load_frontend_routes(frontend_dir)
    frontend_contracts = load_frontend_contracts(frontend_dir, frontend_routes)
    backend_contracts = load_backend_contracts(python_dir)
    tf_resources = parse_tf_resources(terraform_dir)
    terraform_methods = {route: sorted(methods) for route, methods in load_terraform_methods(terraform_dir, tf_resources).items()}
    hardcoded = load_frontend_hardcoded(frontend_dir)

    missing_in_backend = {}
    missing_in_terraform = {}
    method_mismatches = {}
    body_key_gaps = {}

    for name, route in frontend_routes.items():
        frontend_spec = frontend_contracts[name]
        backend_spec = backend_contracts.get(route)
        tf_methods = set(terraform_methods.get(route, []))
        if not backend_spec:
            missing_in_backend[name] = route
            continue
        if route not in terraform_methods:
            missing_in_terraform[name] = route
        frontend_methods = set(frontend_spec['methods']) or {'GET'}
        backend_methods = set(backend_spec['methods'])
        missing_backend_methods = sorted(frontend_methods - backend_methods)
        missing_tf_methods = sorted(frontend_methods - tf_methods)
        if missing_backend_methods or missing_tf_methods:
            method_mismatches[name] = {
                'route': route,
                'frontend_methods': sorted(frontend_methods),
                'backend_methods': sorted(backend_methods),
                'terraform_methods': sorted(tf_methods),
                'missing_in_backend': missing_backend_methods,
                'missing_in_terraform': missing_tf_methods,
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

    quo_checks = check_quo_bones(root)

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
        'quo_bones_check': quo_checks,
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Audit frontend routes/contracts against backend handlers, Terraform API methods, and Quo setup guardrails.')
    parser.add_argument('--root', type=Path, default=None, help='Project root containing frontend/, python/, and terraform/ directories.')
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
