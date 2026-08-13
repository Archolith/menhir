#!/usr/bin/env python3
'''Executable probes and required bug-class sweeps for Menhir M2.

Run from an Archolith/menhir checkout at
`eebf6d6dd83f15083167bf847b639d24b953fdc9`.
'''

from __future__ import annotations

import argparse
import ast
import asyncio
import inspect
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

EXPECTED_COMMIT = 'eebf6d6dd83f15083167bf847b639d24b953fdc9'
EXPECTED_LINES = {
    'src/menhir/api/routes.py': 799,
    'src/menhir/api/routes_support.py': 710,
    'src/menhir/api/oauth_authorize.py': 684,
    'src/menhir/api/auth.py': 676,
    'src/menhir/api/routes_handlers.py': 312,
    'src/menhir/api/oauth_preflight.py': 287,
    'src/menhir/api/oauth.py': 287,
    'src/menhir/api/client_token_store.py': 283,
    'src/menhir/api/server_support.py': 241,
    'src/menhir/api/oauth_as_register.py': 197,
    'src/menhir/api/oauth_rate_limit.py': 145,
    'src/menhir/api/mcp_remote.py': 111,
    'src/menhir/api/jose_provider.py': 110,
    'src/menhir/api/oauth_token.py': 109,
    'src/menhir/api/auth_code_store.py': 91,
    'src/menhir/api/server.py': 87,
    'src/menhir/api/oauth_keys.py': 80,
    'src/menhir/api/oauth_metadata.py': 77,
    'src/menhir/api/request_context.py': 71,
    'src/menhir/api/oauth_client_store.py': 65,
    'src/menhir/api/oauth_as_metadata.py': 65,
    'src/menhir/api/errors.py': 61,
    'src/menhir/api/auth_mode.py': 15,
    'src/menhir/api/__init__.py': 2,
}
TESTS = [
    'tests/test_api_auth.py',
    'tests/test_api_routes.py',
    'tests/test_api_tier_enforcement.py',
    'tests/test_auth_code_store.py',
    'tests/test_auth_mode.py',
    'tests/test_client_token_tier_auth.py',
    'tests/test_config_api_boundaries.py',
    'tests/test_loopback_auth_safety.py',
    'tests/test_oauth_as_consent_secret.py',
    'tests/test_oauth_as_e2e.py',
    'tests/test_oauth_as_metadata.py',
]


def heading(value: str) -> None:
    print('\n' + '=' * 78)
    print(value)
    print('=' * 78)


def root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [Path.cwd(), *here.parents]:
        if (candidate / 'src/menhir/api/auth.py').is_file():
            return candidate
    raise FileNotFoundError('run from the Menhir repository root')


def checkout_and_coverage(repo: Path) -> tuple[bool, bool]:
    heading('CHECKOUT AND COVERAGE')
    head = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=repo, text=True
    ).strip()
    print(f'git_head={head}')
    print(f'expected={EXPECTED_COMMIT}')
    pinned = head == EXPECTED_COMMIT
    total = 0
    counts_ok = True
    for rel, expected in EXPECTED_LINES.items():
        path = repo / rel
        if not path.is_file():
            print(f'MISSING {rel}')
            counts_ok = False
            continue
        actual = len(path.read_text(encoding='utf-8').splitlines())
        total += actual
        ok = actual == expected
        counts_ok &= ok
        print(f'{"OK" if ok else "MISMATCH":8} {actual:4} expected={expected:4} {rel}')
    counts_ok &= total == 5565
    print(f'pinned_commit_match={pinned}')
    print(f'line_total={total}')
    print(f'coverage_reconciled={counts_ok}')
    return pinned, counts_ok


def trees(repo: Path) -> dict[str, ast.Module]:
    return {
        rel: ast.parse((repo / rel).read_text(encoding='utf-8'), filename=rel)
        for rel in EXPECTED_LINES
    }


def bug_sweeps(repo: Path, parsed: dict[str, ast.Module]) -> None:
    heading('BUG CLASS 1 — DUPLICATE DEFINITIONS')
    duplicate_count = 0
    for rel, tree in parsed.items():
        def scan(body, prefix=''):
            nonlocal duplicate_count
            found = defaultdict(list)
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    found[node.name].append(node)
            for name, nodes in sorted(found.items()):
                if len(nodes) > 1:
                    duplicate_count += 1
                    dumps = [ast.dump(node, include_attributes=False) for node in nodes]
                    print(
                        f'{rel}:{prefix}{name} lines={[node.lineno for node in nodes]} '
                        f'identical={len(set(dumps)) == 1}'
                    )
            for node in body:
                if isinstance(node, ast.ClassDef):
                    scan(node.body, prefix + node.name + '.')
        scan(tree.body)
    print(f'duplicate_groups={duplicate_count}')

    heading('BUG CLASS 2 — PYFLAKES')
    command = [sys.executable, '-m', 'pyflakes', 'src/menhir/api']
    print('$ ' + ' '.join(command))
    result = subprocess.run(command, cwd=repo, text=True, capture_output=True)
    print(result.stdout, end='')
    print(result.stderr, end='')
    print(f'pyflakes_exit={result.returncode}')

    heading('BUG CLASS 3 — except Exception IN ASYNC FUNCTIONS')
    broad = []
    for rel, tree in parsed.items():
        async_stack = []
        class Visitor(ast.NodeVisitor):
            def visit_AsyncFunctionDef(self, node):
                async_stack.append(node.name)
                self.generic_visit(node)
                async_stack.pop()
            def visit_ExceptHandler(self, node):
                if async_stack:
                    target = node.type
                    catches = isinstance(target, ast.Name) and target.id == 'Exception'
                    if isinstance(target, ast.Tuple):
                        catches = any(
                            isinstance(item, ast.Name) and item.id == 'Exception'
                            for item in target.elts
                        )
                    if catches:
                        broad.append(f'{rel}:{node.lineno} async={async_stack[-1]}')
                self.generic_visit(node)
        Visitor().visit(tree)
    print('\n'.join(broad))
    print(f'async_except_exception_count={len(broad)}')
    print('manual_check=CancelledError is BaseException on Python 3.12; verify cleanup uses finally')

    heading('BUG CLASS 4 — TIMESTAMP TEXT PATTERNS')
    timestamp_hits = []
    for rel in EXPECTED_LINES:
        for number, line in enumerate((repo / rel).read_text(encoding='utf-8').splitlines(), 1):
            if "datetime('now')" in line or 'datetime("now")' in line or 'isoformat()' in line:
                timestamp_hits.append(f'{rel}:{number}: {line.strip()}')
    print('\n'.join(timestamp_hits))
    print(f'timestamp_pattern_hits={len(timestamp_hits)}')

    heading('BUG CLASS 5 — UNUSED MODULE CONSTANTS')
    unused = []
    for rel, tree in parsed.items():
        assigned = set()
        reads = Counter()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        assigned.add(target.id)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id.isupper():
                    assigned.add(node.target.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                reads[node.id] += 1
        unused.extend(f'{rel}:{name}' for name in assigned if reads[name] == 0)
    print('\n'.join(sorted(unused)))
    print(f'unused_module_constants={len(unused)}')

    heading('BUG CLASS 6 — LOCAL KEYWORD CONTRACTS')
    definitions = defaultdict(list)
    for rel, tree in parsed.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions[node.name].append((rel, node))
    mismatches = []
    for rel, tree in parsed.items():
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            targets = definitions.get(call.func.id, [])
            if len(targets) != 1:
                continue
            target_rel, target = targets[0]
            accepted = {
                arg.arg
                for arg in [
                    *target.args.posonlyargs,
                    *target.args.args,
                    *target.args.kwonlyargs,
                ]
            }
            if target.args.kwarg is not None:
                continue
            for keyword in call.keywords:
                if keyword.arg is not None and keyword.arg not in accepted:
                    mismatches.append(
                        f'{rel}:{call.lineno} {call.func.id}({keyword.arg}=...) -> '
                        f'{target_rel}:{target.lineno}'
                    )
    print('\n'.join(mismatches))
    print(f'local_keyword_mismatches={len(mismatches)}')


async def explorer_probe() -> None:
    heading('M2-H01 — EXPLORER PROXY BYPASS')
    from menhir.api.auth import BearerAuthMiddleware
    reached = False
    sent = []
    async def app(scope, receive, send):
        nonlocal reached
        reached = True
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})
    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    async def send(message):
        sent.append(message)
    middleware = BearerAuthMiddleware(app, api_key='secret', loopback_bound=True)
    scope = {
        'type': 'http', 'path': '/explorer', 'method': 'GET',
        'headers': [(b'x-forwarded-for', b'198.51.100.77')],
        'client': ('127.0.0.1', 40000), 'query_string': b'',
    }
    await middleware(scope, receive, send)
    status = next((m.get('status') for m in sent if m.get('type') == 'http.response.start'), None)
    print(f'inner_reached={reached}')
    print(f'status={status}')
    print(f'finding_reproduced={reached and status == 200}')


async def phase3_reset_probe() -> None:
    heading('M2-H02 — AGENT PHASE3 RESET')
    from menhir.api.routes_handlers import phase3_reset_impl
    tiers = []
    class Backend:
        async def delete_namespace(self, namespace):
            return {'deleted': 7}
    class Adapter:
        def purge_turn_evidence(self, namespace):
            return 3
    result = await phase3_reset_impl(
        object(), 'probe',
        require_tier=tiers.append,
        try_record_destructive_op_rest=lambda name: None,
        require_phase3_adapter=lambda request: (None, Adapter()),
        get_backend=lambda request: Backend(),
    )
    print(f'required_tier={tiers}')
    print(f'nodes_deleted={result.nodes_deleted}')
    print(f'turn_evidence_deleted={result.turn_evidence_deleted}')
    print(f'finding_reproduced={tiers == ["agent"] and result.nodes_deleted == 7}')


def consent_probe() -> None:
    heading('M2-M01 — REPLACEMENT CONSENT TOKEN')
    from menhir.api import oauth_authorize as oa
    from menhir.api.oauth_client_store import OAuthClient
    oa._spent_consent_jtis.clear()
    settings = SimpleNamespace(
        oauth_as_consent_secret='probe-secret', oauth_as_consent_ttl_s=300
    )
    fields = {
        'client_id': 'client', 'redirect_uri': 'https://client.example/cb',
        'scope': 'menhir:read', 'code_challenge': 'a' * 43,
        'code_challenge_method': 'S256',
        'resource': 'https://memory.example/mcp-http', 'state': 's',
    }
    old = oa._sign_consent(fields, settings)
    jti = oa._consent_jti(old)
    consumed = bool(jti and oa._consume_consent_jti(jti, settings))
    client = OAuthClient(
        client_id='client', client_name='Probe',
        redirect_uris=('https://client.example/cb',), scopes=('menhir:read',),
        client_secret_hash='', created_at=time.time(),
        token_endpoint_auth_method='none',
    )
    html = oa._render_consent(fields, client, error='Invalid admin secret.', settings=settings)
    match = re.search(r'name="consent_token" value="([^"]+)"', html)
    replacement = match.group(1) if match else ''
    print(f'old_consumed={consumed}')
    print(f'replacement_present={bool(replacement)}')
    print(f'replacement_differs={replacement != old}')
    print(f'finding_reproduced={consumed and bool(replacement) and replacement != old}')


async def blocking_store_probe() -> None:
    heading('M2-M02 — SYNC SQLITE LOOKUP BLOCKS LOOP')
    from menhir.api.auth import BearerAuthMiddleware
    record = SimpleNamespace(client_id='c', client_name='c', tier='readonly')
    class Store:
        def resolve(self, token):
            time.sleep(0.08)
            return record
        def has_active(self):
            return True
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})
    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    async def send(message):
        pass
    middleware = BearerAuthMiddleware(app, client_token_store=Store(), loopback_bound=False)
    scope = {
        'type': 'http', 'path': '/api/secure', 'method': 'GET',
        'headers': [(b'authorization', b'Bearer token')],
        'client': ('203.0.113.10', 40000), 'query_string': b'',
    }
    delay = 0.0
    async def ticker():
        nonlocal delay
        start = time.perf_counter()
        await asyncio.sleep(0.01)
        delay = time.perf_counter() - start
    await asyncio.gather(middleware(scope, receive, send), ticker())
    print(f'10ms_ticker_elapsed={delay:.4f}')
    print(f'finding_reproduced={delay >= 0.06}')


async def views_probe() -> None:
    heading('M2-M03 — VIEWS N+1')
    from menhir.api.routes_handlers import phase3_views_impl
    class Adapter:
        def __init__(self):
            self.calls = 0
        def list_counters(self, *, namespace, limit):
            return [{'subject': f's{i}', 'counter': 'c'} for i in range(limit)]
        def counter_history(self, *, subject, counter, namespace):
            self.calls += 1
            return [{'value': 1, 'current': True}]
    adapter = Adapter()
    result = await phase3_views_impl(
        object(), 'probe', 500,
        require_phase3_adapter=lambda request: (None, adapter),
    )
    print(f'history_calls={adapter.calls}')
    print(f'total_storage_calls={1 + adapter.calls}')
    print(f'finding_reproduced={adapter.calls == 500 and result.count == 500}')


async def logging_probe() -> None:
    heading('M2-M04 — FULL BODY LOGGING')
    from menhir.api.routes_handlers import backend_invoke_impl
    calls = []
    class Logger:
        def exception(self, *args, **kwargs):
            calls.append(args)
    class Backend:
        async def queue_episode(self, **kwargs):
            raise RuntimeError('probe')
    body = {'episode': 'PRIVATE', 'token': 'SECRET'}
    try:
        await backend_invoke_impl(
            object(), 'queue_episode', body,
            backend_methods={'queue_episode'},
            required_tier_for_operation=lambda operation: 'agent',
            require_tier=lambda tier: None,
            try_record_destructive_op_rest=lambda operation: None,
            resolve_caller_session=lambda request: SimpleNamespace(session_id='s'),
            get_backend=lambda request: Backend(),
            drain_background_errors=lambda session: [],
            logger=Logger(),
        )
    except RuntimeError:
        pass
    print(f'logger_args={calls!r}')
    print(f'finding_reproduced={bool(calls) and body in calls[0]}')


def low_probes() -> None:
    heading('M2-L01 / M2-L02')
    from menhir.api.oauth_preflight import _is_https_or_loopback_http
    from menhir.api import oauth_as_register, oauth_token
    ftp_safe = _is_https_or_loopback_http('ftp://example.com/jwks')
    malformed_safe = _is_https_or_loopback_http('http://[::1')
    refresh_accepted = 'refresh_token' in oauth_as_register._SUPPORTED_GRANT_TYPES
    code_only = 'grant_type != "authorization_code"' in inspect.getsource(oauth_token.token)
    print(f'ftp_reported_safe={ftp_safe}')
    print(f'malformed_reported_safe={malformed_safe}')
    print(f'dcr_refresh_accepted={refresh_accepted}')
    print(f'token_endpoint_code_only={code_only}')
    print(f'M2-L01_reproduced={ftp_safe and malformed_safe}')
    print(f'M2-L02_reproduced={refresh_accepted and code_only}')


async def reproductions() -> None:
    await explorer_probe()
    await phase3_reset_probe()
    consent_probe()
    await blocking_store_probe()
    await views_probe()
    await logging_probe()
    low_probes()


def run_tests(repo: Path) -> int:
    heading('NAMED TESTS')
    missing = [path for path in TESTS if not (repo / path).is_file()]
    if missing:
        print('missing=' + json.dumps(missing))
        return 4
    command = [sys.executable, '-m', 'pytest', '-q', *TESTS]
    print('$ ' + ' '.join(command))
    result = subprocess.run(command, cwd=repo)
    print(f'pytest_exit={result.returncode}')
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-tests', action='store_true')
    parser.add_argument('--sweeps-only', action='store_true')
    parser.add_argument('--repros-only', action='store_true')
    args = parser.parse_args()
    try:
        repo = root()
    except Exception as exc:
        print(f'FATAL checkout_unavailable: {type(exc).__name__}: {exc}')
        return 2
    pinned, coverage = checkout_and_coverage(repo)
    parsed = trees(repo)
    if not args.repros_only:
        bug_sweeps(repo, parsed)
    if not args.sweeps_only:
        asyncio.run(reproductions())
    test_exit = run_tests(repo) if args.run_tests else 0
    heading('SUMMARY')
    print(f'pinned_commit_match={pinned}')
    print(f'coverage_reconciled={coverage}')
    print(f'test_exit={test_exit if args.run_tests else "NOT REQUESTED"}')
    if not pinned or not coverage:
        return 3
    return test_exit


if __name__ == '__main__':
    raise SystemExit(main())
