#!/usr/bin/env python3
"""Hook Center Stale Anchor Lane — real-DB smoke harness (v1).

Proves the full stale-file-anchor lane end-to-end against a REAL Neo4j backend:

    file/tool event
    -> file marked dirty
    -> stale file-anchored memory detected
    -> recall labels stale memory
    -> formatter/context warns agent to inspect current file
    -> stale verification receipt records inspection outcome
    -> receipt enriches stale recall only when path-aware and post-dirty

Core invariant proved: a WRONG current-state view is worse than a miss. A receipt
never marks a stale memory fresh; a wrong-path / pre-dirty / malformed receipt never
reassures.

This is a VALIDATION slice, not a feature slice. It adds no lifecycle behavior
(no auto-refresh, no dirty clearing, no down-ranking, no deletion/expiration).

How it exercises the real backend
----------------------------------
* HTTP endpoints (tool-events / dirty / stale / verification receipts) run against a
  throwaway Menhir server (self-served in a subprocess by default) that mounts the
  REAL FastAPI router over a REAL Neo4j-backed graph adapter.
* Recall labeling, the MCP formatter advisory, the context-builder warning, and the
  receipt-matching / enrichment logic run in-process through the REAL
  ``RecallService.recall()`` / ``ContextBuilderService.build_context()`` /
  ``menhir.mcp.formatters`` against the same throwaway Neo4j. Only the embedding-
  dependent graphiti vector search is seeded (it decides *which* memory is under
  test, not the behavior under test) — everything downstream is the shipped code
  path against real Cypher.

Safety
------
* Never uploads file contents. Never captures transcripts.
* Uses a clearly disposable project namespace.
* Cleanup only deletes data under the smoke project namespace; never clears dirty
  flags globally.

Usage
-----
    python scripts/smoke/hook_center_stale_lane_smoke.py \
        --neo4j-uri bolt://localhost:7688 --neo4j-password smokepass --json

    # against an already-running server + DB (no self-serve):
    python scripts/smoke/hook_center_stale_lane_smoke.py \
        --no-self-serve --url http://127.0.0.1:8099 \
        --neo4j-uri bolt://localhost:7688 --neo4j-password smokepass

Environment fallbacks
---------------------
    MENHIR_URL / MENHIR_TOOL_EVENTS_URL   base server URL
    MENHIR_AGENT_KEY                      bearer token (agent tier)
    MENHIR_READONLY_KEY                   bearer token (readonly tier)
    NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time  # retained: tests patch MODULE.time (monkeypatch.setattr(mod.time, "sleep", ...))

# Make `import menhir` work when run as a script from anywhere in the repo.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_SRC = os.path.join(_REPO_ROOT, "src")
# The hook_center_stale_lane_smoke_* sibling modules live beside this script; make
# them importable in every load mode (direct script run, spec_from_file_location, -m).
_SMOKE_DIR = os.path.dirname(os.path.abspath(__file__))
for _path in (_SRC, _SMOKE_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Facade re-exports: every symbol moved to a hook_center_stale_lane_smoke_* sibling
# stays importable from this module path.
from hook_center_stale_lane_smoke_constants import (  # noqa: E402
    ANCHORED_AT, CHECK_KEYS, CONTROL_UUID, DEFAULT_MEMORY_UUID, DEFAULT_PATH,
    DEFAULT_PROJECT, EVENT_HASH, MEMORY_SENTINEL, QUERY, RESULT_FAIL, RESULT_PASS,
    RESULT_PASS_WITH_SKIPS, SERVER_READY_TIMEOUT_S, WRONG_PATH,
)
from hook_center_stale_lane_smoke_http import (  # noqa: E402
    HTTPClient, SmokeHTTPError, _headers, _request, _resolve_base_url,
)
from hook_center_stale_lane_smoke_backend import Neo4jBackend, _SeededGraphiti  # noqa: E402
from hook_center_stale_lane_smoke_server import _quiet_neo4j_logs, _serve  # noqa: E402
from hook_center_stale_lane_smoke_run import _find, _shift_iso, run_smoke  # noqa: E402


# ---------------------------------------------------------------------------
# Throwaway server (self-serve): mount the REAL router over the REAL adapter.
# No full runtime bootstrap, no embedder, no scheduler.
# ---------------------------------------------------------------------------


def _spawn_server(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        sys.executable, os.path.abspath(__file__), "serve",
        "--host", "127.0.0.1", "--port", str(args.port),
        "--neo4j-uri", args.neo4j_uri, "--neo4j-user", args.neo4j_user,
        "--neo4j-password", args.neo4j_password, "--neo4j-database", args.neo4j_database,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Seams (monkeypatched by unit tests)
# ---------------------------------------------------------------------------


def _make_backend(config: argparse.Namespace):
    return Neo4jBackend(config.neo4j_uri, config.neo4j_user,
                        config.neo4j_password, config.neo4j_database)


def _maybe_spawn_server(config: argparse.Namespace):
    if config.no_self_serve:
        return None
    return _spawn_server(config)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Hook Center stale-anchor lane real-DB smoke")
    sub = p.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the throwaway server (internal)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8099)
    serve.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7688"))
    serve.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    serve.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "smokepass"))
    serve.add_argument("--neo4j-database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))

    p.add_argument("--url", default=None, help="base server URL (default self-served)")
    p.add_argument("--port", type=int, default=8099, help="self-serve port")
    p.add_argument("--no-self-serve", action="store_true",
                   help="use an already-running server at --url")
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--path", default=DEFAULT_PATH)
    p.add_argument("--memory-uuid", default=DEFAULT_MEMORY_UUID)
    p.add_argument("--agent-key", default=os.environ.get("MENHIR_AGENT_KEY"))
    p.add_argument("--readonly-key", default=os.environ.get("MENHIR_READONLY_KEY"))
    p.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7688"))
    p.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    p.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "smokepass"))
    p.add_argument("--neo4j-database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    p.add_argument("--json", action="store_true", help="emit JSON only on stdout")
    p.add_argument("--keep-data", action="store_true", help="do not delete smoke data")
    p.add_argument("--skip-context-builder", action="store_true")
    p.add_argument("--skip-recall", action="store_true")
    p.add_argument("--require-clean-start", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)

    _quiet_neo4j_logs()
    use_json = args.json

    def out(msg: str = "") -> None:
        # Diagnostics never pollute JSON stdout.
        print(msg, file=sys.stderr if use_json else sys.stdout)

    if not args.url:
        args.url = f"http://127.0.0.1:{args.port}"
    base = _resolve_base_url(args.url)
    http = HTTPClient(base, args.agent_key, args.readonly_key)

    server = None
    backend = None
    summary: dict = {}
    try:
        server = _maybe_spawn_server(args)
        if not http.wait_ready(SERVER_READY_TIMEOUT_S):
            raise SmokeHTTPError(f"server not reachable at {base}")
        out(f"server ready: {base}")

        backend = _make_backend(args)
        if not backend.ping():
            raise SmokeHTTPError(f"neo4j not reachable at {args.neo4j_uri}")

        summary = run_smoke(args, http, backend, out)

        if not args.keep_data:
            backend.clean(args.project, [args.memory_uuid, CONTROL_UUID])
            out("smoke data cleaned")
    except SmokeHTTPError as exc:
        summary = {
            "result": RESULT_FAIL, "project": args.project, "path": args.path,
            "memory_uuid": args.memory_uuid, "error": str(exc),
            "checks": {}, "limitations": [f"aborted: {exc}"],
        }
        out(f"FAIL: {exc}")
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except Exception:
                server.kill()

    if use_json:
        print(json.dumps(summary))
    else:
        out("")
        out(f"Result: {summary.get('result')}")
        for k in CHECK_KEYS:
            if k in summary.get("checks", {}):
                out(f"  {'PASS' if summary['checks'][k] else 'FAIL'}  {k}")
        for k, reason in (summary.get("skipped") or {}).items():
            out(f"  SKIP  {k}: {reason}")

    return 0 if summary.get("result") != RESULT_FAIL else 1


def _cli() -> int:
    """Self-serve via the unified launcher unless an external target is given.

    Default: bring up a throwaway Neo4j-backed Menhir through the shared launcher
    (free port + instance-id handshake + disposable Docker Neo4j, torn down after)
    and run the smoke against it in ``--no-self-serve`` mode — writing graph
    fixtures directly to the same throwaway Neo4j. Requires Docker (or
    MENHIR_TEST_NEO4J_URI). Explicit ``--no-self-serve``/``--url`` or MENHIR_URL /
    MENHIR_TOOL_EVENTS_URL skips the launch and targets the running server.
    """
    argv = sys.argv[1:]
    external = (
        "--no-self-serve" in argv or "--url" in argv
        or os.environ.get("MENHIR_URL") or os.environ.get("MENHIR_TOOL_EVENTS_URL")
    )
    if external:
        return main()

    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
    from dev.test_server import launch

    with launch("no-auth", backend="neo4j") as srv:
        return main([
            "--no-self-serve", "--url", srv.base_url,
            "--neo4j-uri", srv.neo4j_uri, "--neo4j-user", srv.neo4j_user,
            "--neo4j-password", srv.neo4j_password, "--neo4j-database", "neo4j",
            *argv,
        ])


if __name__ == "__main__":
    raise SystemExit(_cli())
