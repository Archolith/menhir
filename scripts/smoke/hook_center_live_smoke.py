#!/usr/bin/env python3
"""Hook Center Live Smoke v1 — end-to-end smoke harness against a throwaway Menhir instance.

Proves that Hook Center components work together:
  1. POST /api/tool-events accepts a file_changed event
  2. GET /api/tool-events/dirty returns diagnostics
  3. GET /api/tool-events/stale returns stale anchors
  4. report_dirty_files.py CLI produces output
  5. menhir_policy_guard.py warns/blocks locally

Self-serves a throwaway Neo4j-backed Menhir by default (Docker required, or set
MENHIR_TEST_NEO4J_URI); use --url to target an already-running instance instead.

Usage:
    python scripts/smoke/hook_center_live_smoke.py                 # self-serve
    python scripts/smoke/hook_center_live_smoke.py --url http://127.0.0.1:8099
    python scripts/smoke/hook_center_live_smoke.py --skip-policy
    python scripts/smoke/hook_center_live_smoke.py --require-stale
    python scripts/smoke/hook_center_live_smoke.py --json

Config (env):
    MENHIR_SMOKE_URL          Target an already-running server at this URL
    MENHIR_TEST_NEO4J_URI     Reuse this Neo4j for self-serve instead of Docker
    MENHIR_AGENT_KEY          Bearer token for authenticated endpoints
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8099"
RESULT_PASS = "PASS"
RESULT_PASS_UNSCANNED = "PASS_WITH_UNSCANNED_FILE"
RESULT_FAIL = "FAIL"


def _resolve_base_url() -> str:
    return (os.environ.get("MENHIR_SMOKE_URL") or DEFAULT_URL).rstrip("/")


def _build_request(url: str, data: bytes | None = None) -> urllib.request.Request:
    headers = {"Content-Type": "application/json"}
    key = (os.environ.get("MENHIR_AGENT_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return urllib.request.Request(url, data=data, headers=headers)


def _http_get(url: str) -> dict:
    req = _build_request(url)
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = _build_request(url, data=body)
    req.method = "POST"
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def step_check_server(base: str) -> dict | None:
    """Step 1 — call GET /api/tool-events/dirty to verify the server is reachable."""
    url = f"{base}/api/tool-events/dirty"
    try:
        return _http_get(url)
    except Exception as exc:
        print(f"FAIL: server unreachable at {url}: {exc}", file=sys.stderr)
        return None


def step_send_event(base: str, project: str, path: str) -> dict:
    """Step 2 — POST a file_changed event."""
    payload = {
        "event_type": "file_changed",
        "source_client": "smoke",
        "source_kind": "smoke",
        "project": project,
        "path": path,
        "operation": "edit",
        "after_hash": "smoke-hash",
        "metadata": {"smoke": True},
    }
    url = f"{base}/api/tool-events"
    return _http_post(url, payload)


def step_check_dirty(base: str, project: str) -> dict:
    """Step 3 — GET /api/tool-events/dirty."""
    return _http_get(f"{base}/api/tool-events/dirty?project={project}")


def step_check_stale(base: str, project: str) -> dict:
    """Step 4 — GET /api/tool-events/stale."""
    return _http_get(f"{base}/api/tool-events/stale?project={project}")


def step_report_script(base: str) -> str:
    """Step 5 — run report_dirty_files.py CLI via subprocess."""
    dirty_url = f"{base}/api/tool-events/dirty"
    result = subprocess.run(
        [sys.executable, str(_script_path("maintenance/report_dirty_files.py")),
         "--url", dirty_url],
        capture_output=True, text=True, timeout=15.0,
    )
    if result.returncode != 0:
        raise RuntimeError(f"report script exited {result.returncode}: {result.stderr}")
    return result.stdout


def step_policy_guard_warn() -> str:
    """Step 6a — run menhir_policy_guard.py with warn-mode policy via subprocess."""
    policy = {"mode": "warn", "frozen_paths": [".env*", "*.key"],
              "branch_scope": ["src/**", "docs/**"]}
    payload = {"tool_name": "Edit", "tool_input": {"file_path": ".env.local"}}
    return _run_policy_guard(policy, payload)


def step_policy_guard_block() -> tuple[str, int]:
    """Step 6b — run menhir_policy_guard.py with block-mode policy via subprocess."""
    policy = {"mode": "block", "frozen_paths": [".env*"]}
    payload = {"tool_name": "Edit", "tool_input": {"file_path": ".env.local"}}
    return _run_policy_guard_full(policy, payload)


def _run_policy_guard(policy: dict, payload: dict) -> str:
    out, _ = _run_policy_guard_full(policy, payload)
    return out


def _run_policy_guard_full(policy: dict, payload: dict) -> tuple[str, int]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as pf:
        json.dump(policy, pf)
        pf.flush()
        pfile = pf.name
    try:
        env = {**os.environ, "MENHIR_POLICY_FILE": pfile}
        result = subprocess.run(
            [sys.executable, str(_script_path("hooks/menhir_policy_guard.py"))],
            input=json.dumps(payload), capture_output=True, text=True, timeout=10.0,
            env=env,
        )
        return result.stdout.strip(), result.returncode
    finally:
        try:
            os.unlink(pfile)
        except OSError:
            pass


def _script_path(relative: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", relative)


def _human_result(result: str, marked_dirty: bool) -> str:
    if result == RESULT_FAIL:
        return RESULT_FAIL
    if not marked_dirty:
        return RESULT_PASS_UNSCANNED
    return RESULT_PASS


def _build_summary(*, base, project, path, accepted, marked_dirty, dirty_count, stale_count,
                    report_ok, policy_warn_ok, policy_block_ok, policy_skipped, final):
    return {
        "server": base, "project": project, "path": path,
        "accepted": accepted, "marked_dirty": marked_dirty,
        "dirty_count": dirty_count, "stale_count": stale_count,
        "report_ok": report_ok,
        "policy_warn_ok": policy_warn_ok, "policy_block_ok": policy_block_ok,
        "policy_skipped": policy_skipped,
        "result": final,
    }


def main() -> int:
    args = set(sys.argv[1:])
    base = _resolve_base_url()
    if "--url" in args:
        idx = sys.argv.index("--url") + 1
        if idx < len(sys.argv):
            base = sys.argv[idx].rstrip("/")
    project = "menhir-smoke"
    if "--project" in args:
        idx = sys.argv.index("--project") + 1
        if idx < len(sys.argv):
            project = sys.argv[idx]
    path = "src/smoke_target.py"
    if "--path" in args:
        idx = sys.argv.index("--path") + 1
        if idx < len(sys.argv):
            path = sys.argv[idx]
    require_stale = "--require-stale" in args
    skip_policy = "--skip-policy" in args
    use_json = "--json" in args

    result = RESULT_PASS
    accepted = False
    marked_dirty = False
    dirty_count = 0
    stale_count = 0
    report_ok = False
    policy_warn_ok = False
    policy_block_ok = False

    lines: list[str] = []
    def _out(msg: str = "", err: bool = False) -> None:
        if use_json:
            lines.append(msg)
        else:
            print(msg, file=sys.stderr if err else None)

    _out("Hook Center Live Smoke")
    _out(f"Server: {base}")
    _out()

    # Step 1 — check server
    dirty_before = step_check_server(base)
    if dirty_before is None:
        _out("Result: FAIL", err=True)
        if use_json:
            print(json.dumps({"result": RESULT_FAIL, "server": base, "project": project,
                              "error": "server unreachable"}))
        return 1
    _out("Server reachable: yes")

    # Step 2 — send event
    try:
        post_resp = step_send_event(base, project, path)
        accepted = post_resp.get("accepted", False)
        marked_dirty = post_resp.get("marked_dirty", False)
        _out(f"POST /api/tool-events: accepted={accepted} marked_dirty={marked_dirty}")
        if not accepted:
            _out("FAIL: event was not accepted", err=True)
            _out("Result: FAIL", err=True)
            if use_json:
                print(json.dumps({"result": RESULT_FAIL, "server": base, "project": project,
                                  "error": "event not accepted"}))
            return 1
        if not marked_dirty:
            result = RESULT_PASS_UNSCANNED
            _out("  (accepted but file not in structure graph — expected in v0)")
    except Exception as exc:
        _out(f"FAIL: POST rejected unexpectedly: {exc}", err=True)
        _out("Result: FAIL", err=True)
        if use_json:
            print(json.dumps({"result": RESULT_FAIL, "server": base, "project": project,
                              "error": f"POST rejected: {exc}"}))
        return 1

    # Step 3 — check dirty endpoint
    try:
        dirty_resp = step_check_dirty(base, project)
        dirty_count = len(dirty_resp.get("dirty_files", []))
        stale_count_in_dirty = len(dirty_resp.get("stale_anchors", []))
        _out(f"Dirty endpoint: dirty_files={dirty_count} stale_anchors={stale_count_in_dirty}")
    except Exception as exc:
        _out(f"FAIL: dirty endpoint unavailable: {exc}", err=True)
        _out("Result: FAIL", err=True)
        if use_json:
            print(json.dumps({"result": RESULT_FAIL, "error": f"dirty endpoint: {exc}"}))
        return 1

    # Step 4 — check stale endpoint
    try:
        stale_resp = step_check_stale(base, project)
        stale_count = stale_resp.get("count", 0)
        _out(f"Stale endpoint: count={stale_count}")
        if require_stale and stale_count == 0:
            _out("FAIL: --require-stale demanded stale anchors but count is 0", err=True)
            _out("Result: FAIL", err=True)
            if use_json:
                print(json.dumps({"result": RESULT_FAIL, "error": "require-stale not met"}))
            return 1
    except Exception as exc:
        _out(f"FAIL: stale endpoint unavailable: {exc}", err=True)
        _out("Result: FAIL", err=True)
        if use_json:
            print(json.dumps({"result": RESULT_FAIL, "error": f"stale endpoint: {exc}"}))
        return 1

    # Step 5 — run report script
    try:
        report_out = step_report_script(base)
        report_ok = "Dirty files count" in report_out or "dirty_files" in report_out
        if not report_ok:
            _out("FAIL: report script output missing expected fields", err=True)
            _out("Result: FAIL", err=True)
            if use_json:
                print(json.dumps({"result": RESULT_FAIL, "error": "report script bad output"}))
            return 1
        _out("Report script: PASS")
    except Exception as exc:
        _out(f"FAIL: report script failed: {exc}", err=True)
        _out("Result: FAIL", err=True)
        if use_json:
            print(json.dumps({"result": RESULT_FAIL, "error": f"report script: {exc}"}))
        return 1

    # Step 6 — policy guard
    if not skip_policy:
        try:
            warn_out = step_policy_guard_warn()
            policy_warn_ok = "warning" in warn_out.lower()
            if not policy_warn_ok:
                _out(f"FAIL: policy guard warn mode unexpected output: {warn_out!r}", err=True)
                _out("Result: FAIL", err=True)
                if use_json:
                    print(json.dumps({"result": RESULT_FAIL, "error": "policy warn failed"}))
                return 1
            _out("Policy guard warn: PASS")

            block_out, block_code = step_policy_guard_block()
            policy_block_ok = block_code == 2 and "blocked" in block_out.lower()
            if not policy_block_ok:
                _out(f"FAIL: policy guard block mode expected exit 2, got {block_code}", err=True)
                _out("Result: FAIL", err=True)
                if use_json:
                    print(json.dumps({"result": RESULT_FAIL, "error": "policy block failed"}))
                return 1
            _out("Policy guard block: PASS")
        except Exception as exc:
            _out(f"FAIL: policy guard error: {exc}", err=True)
            _out("Result: FAIL", err=True)
            if use_json:
                print(json.dumps({"result": RESULT_FAIL, "error": f"policy guard: {exc}"}))
            return 1
    else:
        _out("Policy guard: SKIPPED (--skip-policy)")

    final = _human_result(result, marked_dirty)
    _out(f"\nResult: {final}")

    summary = _build_summary(
        base=base, project=project, path=path,
        accepted=accepted, marked_dirty=marked_dirty,
        dirty_count=dirty_count, stale_count=stale_count,
        report_ok=report_ok,
        policy_warn_ok=policy_warn_ok, policy_block_ok=policy_block_ok,
        policy_skipped=skip_policy,
        final=final,
    )

    if use_json:
        print(json.dumps(summary))
    return 0 if final != RESULT_FAIL else 1


def _cli() -> int:
    """Self-serve a throwaway Neo4j-backed Menhir unless an external target is given.

    If neither ``--url`` nor ``MENHIR_SMOKE_URL`` is set, launch a throwaway
    no-auth Menhir backed by a disposable Neo4j (free port + instance-id
    handshake, torn down after) and point the smoke at it — no external service,
    no prod risk. Requires Docker (or MENHIR_TEST_NEO4J_URI) for the Neo4j.
    """
    if "--url" in sys.argv[1:] or os.environ.get("MENHIR_SMOKE_URL"):
        return main()

    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _server import smoke_server

    with smoke_server("no-auth", url=None, backend="neo4j") as base:
        prev = os.environ.get("MENHIR_SMOKE_URL")
        os.environ["MENHIR_SMOKE_URL"] = base
        try:
            return main()
        finally:
            if prev is None:
                os.environ.pop("MENHIR_SMOKE_URL", None)
            else:
                os.environ["MENHIR_SMOKE_URL"] = prev


if __name__ == "__main__":
    raise SystemExit(_cli())
