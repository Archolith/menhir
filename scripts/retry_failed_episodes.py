"""Re-enrich FAILED episodes in paced waves, with live progress.

Recovers episodes whose enrichment failed, by resetting them to PENDING and re-queueing
them through the normal ingest worker. Read-only against Neo4j; all mutations go through
the operator-tier backend API (`force_reset_failed_episode` + `enqueue_pending_episode`),
so the server owns lease/attempt bookkeeping rather than this script.

Motivating incident (2026-07-27): ~9% of production episodes were stuck FAILED on an
OpenAI 400 context-length error caused by `content_embedding` leaking into graphiti's
dedupe prompt (fixed in aa7c758). 200+ episodes needed a bulk recovery pass.

Recovery leaves the normal two-node twin shape: graphiti mints its own :Episodic node,
entities attach there, and the original points at it via `resolved_episode_uuid`. An
episode whose own entity count is 0 is NOT necessarily broken -- resolve the twin first.

Usage:
    # see what would run, change nothing (default)
    python scripts/retry_failed_episodes.py

    # recover the context-overrun batch
    python scripts/retry_failed_episodes.py --apply

    # everything currently FAILED, regardless of cause
    python scripts/retry_failed_episodes.py --apply --error-contains ""

Safety: refuses to run without --apply. Verify the underlying fix FIRST on a single
episode (force_reenrich MCP tool) -- a bulk pass against an unfixed root cause burns
retry counters on every episode for nothing.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ERROR_FILTER = "maximum context length"


def load_config() -> dict[str, str]:
    cfg = dict(dotenv_values(ROOT / ".env"))
    missing = [k for k in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD") if not cfg.get(k)]
    if missing:
        sys.exit(f"missing required .env keys: {', '.join(missing)}")
    return cfg


def api_base(cfg: dict[str, str]) -> str:
    host = cfg.get("MENHIR_API_HOST") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.get('MENHIR_API_PORT') or '8090'}"


def fetch_failed(driver, error_contains: str, limit: int) -> list[dict[str, Any]]:
    where = "e.processing_state = 'FAILED'"
    params: dict[str, Any] = {"limit": limit}
    if error_contains:
        where += " AND e.processing_error CONTAINS $needle"
        params["needle"] = error_contains
    query = f"""
        MATCH (e:Episodic) WHERE {where}
        RETURN e.uuid AS uuid, size(e.content) AS clen,
               coalesce(e.processing_attempts, 0) AS attempts
        ORDER BY coalesce(e.processing_completed_at, e.created_at) ASC
        LIMIT $limit
    """
    with driver.session() as s:
        return [dict(r) for r in s.run(query, params)]


def episode_state(driver, uuid: str) -> str | None:
    with driver.session() as s:
        row = s.run(
            "MATCH (e:Episodic {uuid:$u}) RETURN e.processing_state AS st", u=uuid
        ).single()
    return row["st"] if row else None


def post_op(base: str, token: str, operation: str, payload: dict[str, Any]) -> Any:
    resp = requests.post(
        f"{base}/api/internal/backend/{operation}",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{operation} -> HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def wait_for_settle(driver, uuids: list[str], timeout_s: float, poll_s: float) -> dict[str, str]:
    """Block until every uuid leaves PENDING/ENRICHING, or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    states: dict[str, str] = {}
    while time.monotonic() < deadline:
        states = {u: (episode_state(driver, u) or "GONE") for u in uuids}
        if not any(st in ("PENDING", "ENRICHING") for st in states.values()):
            return states
        time.sleep(poll_s)
    return states


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually re-enrich (default: dry run)")
    ap.add_argument("--limit", type=int, default=1000, help="max episodes to process")
    ap.add_argument("--batch", type=int, default=4, help="episodes queued per wave")
    ap.add_argument("--error-contains", default=DEFAULT_ERROR_FILTER,
                    help="only episodes whose processing_error contains this ('' = all FAILED)")
    ap.add_argument("--wave-timeout-s", type=float, default=900.0, help="max wait per wave")
    ap.add_argument("--poll-s", type=float, default=5.0, help="state poll interval")
    args = ap.parse_args()

    cfg = load_config()
    # force_reset_failed_episode is operator-tier (routes_support._OP_TIER_OPERATOR), so the
    # operator key is required; the agent/readonly keys will 401.
    token = cfg.get("MENHIR_OPERATOR_KEY") or ""
    if not token:
        sys.exit("MENHIR_OPERATOR_KEY not set in .env — this recovery is operator-tier")
    base = api_base(cfg)
    driver = GraphDatabase.driver(cfg["NEO4J_URI"], auth=(cfg["NEO4J_USER"], cfg["NEO4J_PASSWORD"]))

    try:
        targets = fetch_failed(driver, args.error_contains, args.limit)
        label = args.error_contains or "<any cause>"
        print(f"api={base}  filter={label!r}  matched={len(targets)}")
        if not targets:
            print("nothing to do")
            return 0
        if not args.apply:
            for t in targets[:10]:
                print(f"  would retry {t['uuid']}  chars={t['clen']}  attempts={t['attempts']}")
            extra = len(targets) - 10
            if extra > 0:
                print(f"  ... and {extra} more")
            print("\nDRY RUN — re-run with --apply to execute")
            return 0

        ok = failed = 0
        waves = [targets[i:i + args.batch] for i in range(0, len(targets), args.batch)]
        started = time.monotonic()

        for wave_no, wave in enumerate(waves, 1):
            queued: list[str] = []
            for t in wave:
                uuid = t["uuid"]
                try:
                    if not post_op(base, token, "force_reset_failed_episode", {"episode_uuid": uuid}):
                        print(f"  [skip] {uuid} — not in FAILED/PENDING")
                        continue
                    post_op(base, token, "enqueue_pending_episode", {"episode_uuid": uuid})
                    queued.append(uuid)
                except (RuntimeError, requests.RequestException) as exc:
                    failed += 1
                    print(f"  [ERROR] {uuid}: {exc}")

            if not queued:
                continue

            states = wait_for_settle(driver, queued, args.wave_timeout_s, args.poll_s)
            for uuid, st in states.items():
                if st == "READY":
                    ok += 1
                else:
                    failed += 1
                    print(f"  [{st}] {uuid}")

            done = ok + failed
            rate = done / max(1e-9, (time.monotonic() - started) / 60.0)
            remaining = len(targets) - done
            eta = remaining / rate if rate > 0 else 0
            print(f"wave {wave_no}/{len(waves)}  ok={ok} failed={failed}  "
                  f"{rate:.1f}/min  eta={eta:.0f}m", flush=True)

        print(f"\ndone: ok={ok} failed={failed} of {len(targets)}")
        return 0 if failed == 0 else 1
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
