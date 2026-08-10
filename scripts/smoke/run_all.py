#!/usr/bin/env python3
"""Run every self-serving smoke and report a combined pass/fail summary.

Each smoke spins up its own throwaway server (free port + instance-id handshake,
torn down after), so this needs no running Menhir and cannot touch a real one.
The Neo4j-backed smokes need Docker (or MENHIR_TEST_NEO4J_URI); use --fast to run
only the no-backend auth/OAuth smokes.

Usage:
    python scripts/smoke/run_all.py           # all four, self-served
    python scripts/smoke/run_all.py --fast    # skip the Neo4j-backed (Docker) ones
    python scripts/smoke/run_all.py --json     # machine-readable summary
    python scripts/smoke/run_all.py --only oauth_local,auth_shapes
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SMOKE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Smoke:
    name: str
    script: str
    needs_backend: bool  # True => needs Docker/Neo4j (skipped by --fast)


SMOKES = (
    Smoke("auth_shapes", "auth_shapes_smoke.py", needs_backend=False),
    Smoke("oauth_local", "oauth_local_smoke.py", needs_backend=False),
    # Live SaaS-IdP (Auth0) check. Skips itself (exit 0) when AUTH0_* env is unset,
    # so it is safe to run everywhere; it only exercises the tenant when configured.
    Smoke("auth0_live", "auth0_live_smoke.py", needs_backend=False),
    Smoke("hook_center_live", "hook_center_live_smoke.py", needs_backend=True),
    Smoke("hook_center_stale_lane", "hook_center_stale_lane_smoke.py", needs_backend=True),
)


def _run_one(smoke: Smoke, *, timeout_s: float) -> dict:
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(SMOKE_DIR / smoke.script)],
            capture_output=True, text=True, timeout=timeout_s,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = 124, exc.stdout or "", f"TIMEOUT after {timeout_s}s"
    elapsed = time.monotonic() - start
    return {
        "name": smoke.name,
        "returncode": rc,
        "passed": rc == 0,
        "seconds": round(elapsed, 1),
        "stdout": out,
        "stderr": err,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run all self-serving Menhir smokes.")
    ap.add_argument("--fast", action="store_true",
                    help="skip the Neo4j-backed (Docker) smokes")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    ap.add_argument("--only", default="", help="comma-separated smoke names to run")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="per-smoke timeout in seconds (default 300)")
    args = ap.parse_args(argv)

    only = {n.strip() for n in args.only.split(",") if n.strip()}
    selected = [
        s for s in SMOKES
        if (not only or s.name in only) and not (args.fast and s.needs_backend)
    ]
    if only:
        unknown = only - {s.name for s in SMOKES}
        if unknown:
            print(f"unknown smoke(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    results: list[dict] = []
    for smoke in selected:
        if not args.json:
            print(f"==> {smoke.name} ...", flush=True)
        res = _run_one(smoke, timeout_s=args.timeout)
        results.append(res)
        if not args.json:
            status = "PASS" if res["passed"] else "FAIL"
            print(f"    [{status}] {smoke.name} ({res['seconds']}s)")
            if not res["passed"]:
                # Surface the tail of the failing smoke's output for triage.
                tail = (res["stdout"] or res["stderr"] or "").splitlines()[-12:]
                for line in tail:
                    print(f"      | {line}")

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    all_pass = passed == total and total > 0

    if args.json:
        print(json.dumps({
            "passed": passed, "total": total, "all_pass": all_pass,
            "results": [{k: r[k] for k in ("name", "passed", "returncode", "seconds")}
                        for r in results],
        }))
    else:
        print()
        print(f"Summary: {passed}/{total} smokes passed")
        for r in results:
            print(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['name']}  ({r['seconds']}s)")

    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
