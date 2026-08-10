"""Broadened manual smoke test for Stage 1 shadow-mode context composition
(.agent/plans/menhir-context-composition-production-integration.md).

The single-episode smoke test (shadow_context_composition_smoke.py) proved the
mechanism works end-to-end against real infra. This is the plan's actual Stage 1
exit criterion: "shadow logs run clean across a MEANINGFUL SLICE of real ingest
traffic (no crashes, no unbounded latency/cost growth, candidate retrieval
actually returns sane pools)".

Runs 7 real episodes through the ACTUAL production ingest path (same as the
single-episode smoke test), spread across 6 DIFFERENT real LongMemEval namespaces
already loaded in menhir-lme-neo4j from the M1 full-corpus benchmark run
(archolith-bench/benchmarks/longmemeval-menhir-2026-07-15.md) -- cycling/donation,
video games, marketing certifications, book collecting, cancer research, plus the
known-good Rachel/housing fixture -- instead of one hand-picked message. One
episode is deliberately unrelated to its namespace's content to confirm clean
abstention (abstained_no_candidates) still works cleanly at this breadth.

Usage:
    .venv/Scripts/python.exe scripts/smoke/shadow_context_composition_broad_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

MENHIR_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(MENHIR_ROOT / "src"))


def _load_openai_key_from_env_file() -> str:
    env_path = MENHIR_ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"OPENAI_API_KEY not found in {env_path}")


def _configure_environment() -> None:
    os.environ.setdefault("OPENAI_API_KEY", _load_openai_key_from_env_file())
    os.environ["GRAPHITI_LLM_PROVIDER"] = "openai"
    os.environ["MEMORY_GRAPHITI_PROVIDER"] = "openai"
    os.environ["LLM_CHAT_PROVIDER"] = "openai"
    os.environ["MEMORY_CHAT_PROVIDER"] = "openai"
    os.environ["OPENAI_CHAT_MODEL"] = "gpt-4o-mini"
    os.environ.setdefault("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    os.environ.setdefault("NEO4J_URI", "bolt://localhost:7689")
    os.environ.setdefault("NEO4J_PASSWORD", "lmedata123")
    os.environ["MENHIR_SHADOW_CONTEXT_COMPOSITION"] = "true"
    os.environ["MENHIR_SHADOW_COMPOSITION_TIMEOUT_S"] = "30"


_configure_environment()

from menhir.config import MemorySettings  # noqa: E402
from menhir.core.bootstrap import build_memory_services  # noqa: E402
from menhir.domain import new_session  # noqa: E402
from menhir.infrastructure.paths import telemetry_db_path  # noqa: E402

# (namespace, episode_body, note) -- 6 real namespaces with real pre-existing content
# (surveyed directly from menhir-lme-neo4j), plus one deliberately-unrelated control.
_CASES: list[tuple[str, str, str]] = [
    ("lme-830ce83f", "Rachel actually just moved back to the suburbs again.",
     "known-good: real Rachel/Housing content"),
    ("lme-a3838d2b", "I decided to donate $250 to breast cancer research after all.",
     "real cycling/donation persona"),
    ("lme-28dc39ac", "I've been replaying The Witcher 3 again this week.",
     "real video-games persona"),
    ("lme-81507db6", "I finally finished the Hootsuite Social Media Marketing Certification.",
     "real marketing-certifications persona"),
    ("lme-e3038f8c", "I found a professional book conservator for my first edition.",
     "real book-collecting persona"),
    ("lme-b46e15ed", "The new AI models are improving success rates in treating cancer.",
     "real cancer-research persona"),
    ("lme-830ce83f", "I just adopted a new puppy named Max.",
     "control: deliberately unrelated to the Rachel/Housing content in this namespace"),
]


async def _read_shadow_trace(episode_uuid: str, max_wait_s: float = 35.0) -> dict | None:
    """Poll (asyncio.sleep, never blocking time.sleep -- see the single-episode
    smoke test's comment for why that matters) for this episode's shadow trace."""
    db_path = telemetry_db_path()
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT details_json FROM lifecycle_events "
                    "WHERE phase = 'ingest_shadow' AND event = 'extraction_composition' "
                    "AND episode_uuid = ? ORDER BY recorded_at DESC LIMIT 1",
                    (episode_uuid,),
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                return json.loads(row[0])
        await asyncio.sleep(1.0)
    return None


async def main() -> None:
    settings = MemorySettings.from_env()
    assert settings.shadow_context_composition is True, "flag did not parse as enabled"

    artifacts = build_memory_services(settings)
    ingest_service = artifacts.ingest_service

    results: list[dict] = []
    for i, (namespace, episode, note) in enumerate(_CASES, 1):
        print(f"\n[{i}/{len(_CASES)}] namespace={namespace!r} note={note!r}")
        print(f"    episode: {episode!r}")
        session = new_session(f"shadow-broad-smoke-user-{i}")
        started = time.monotonic()
        try:
            result = await ingest_service.ingest_episode(
                episode=episode, session=session, source="shadow-broad-smoke", namespace=namespace,
            )
            real_ms = int((time.monotonic() - started) * 1000)
        except Exception as exc:
            print(f"    CRASHED during real extraction: {exc!r}")
            results.append({"namespace": namespace, "note": note, "crashed": True})
            continue

        print(f"    real: status={result.status} nodes={result.nodes_touched} "
              f"edges={result.edges_touched} ({real_ms}ms)")

        trace = await _read_shadow_trace(result.episode_id)
        row = {
            "namespace": namespace, "note": note, "crashed": False,
            "real_status": str(result.status), "real_ms": real_ms,
            "shadow_found": trace is not None,
        }
        if trace is None:
            print("    shadow: NOT FOUND within poll window")
            row["shadow_status"] = "MISSING"
        else:
            row["shadow_status"] = trace["status"]
            row["candidate_count"] = len(trace["candidates"])
            row["shadow_total_ms"] = trace["shadow_total_ms"]
            row["selected"] = trace["selected_fact_uuid"] is not None
            row["tie_breaker_fired"] = trace["llm_tie_breaker_fired"]
            print(f"    shadow: status={trace['status']} candidates={len(trace['candidates'])} "
                  f"selected={trace['selected_fact_uuid']!r} "
                  f"tie_breaker_fired={trace['llm_tie_breaker_fired']} "
                  f"shadow_total_ms={trace['shadow_total_ms']}")
        results.append(row)

    print("\n" + "=" * 70)
    print("AGGREGATE SUMMARY")
    print("=" * 70)
    crashed = sum(1 for r in results if r.get("crashed"))
    print(f"Real-extraction crashes: {crashed}/{len(results)}")
    missing = sum(1 for r in results if not r.get("crashed") and not r.get("shadow_found"))
    print(f"Shadow traces missing entirely: {missing}/{len(results)}")

    shadow_statuses = Counter(r.get("shadow_status", "N/A") for r in results if not r.get("crashed"))
    print(f"Shadow status distribution: {dict(shadow_statuses)}")

    failure_statuses = {"candidate_query_failed", "metadata_generation_failed", "malformed_llm_response", "timed_out"}
    hard_failures = sum(1 for r in results if r.get("shadow_status") in failure_statuses)
    print(f"Hard shadow failures (not clean abstentions): {hard_failures}/{len(results)}")

    timings = [r["shadow_total_ms"] for r in results if "shadow_total_ms" in r]
    if timings:
        print(f"Shadow total_ms: min={min(timings)} max={max(timings)} "
              f"avg={sum(timings) / len(timings):.0f}")

    candidate_counts = [r["candidate_count"] for r in results if "candidate_count" in r]
    if candidate_counts:
        print(f"Candidate pool sizes: {candidate_counts}")

    print("\nPer-case:")
    for r in results:
        print(f"  {r['namespace']:16s} {r.get('shadow_status', 'CRASHED'):32s} "
              f"real={r.get('real_status', 'N/A')} {r['note']}")

    print("\nShutting down ingest_service...")
    await ingest_service.shutdown()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
