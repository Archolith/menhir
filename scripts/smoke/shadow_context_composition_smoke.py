"""Manual smoke test for Stage 1 shadow-mode context composition
(.agent/plans/menhir-context-composition-production-integration.md).

Runs a real episode through the ACTUAL production ingest path
(IngestService.ingest_episode -> queue_episode_for_enrichment ->
run_graphiti_extraction -> shadow dispatch), against the real
menhir-lme-neo4j dev container, with MENHIR_SHADOW_CONTEXT_COMPOSITION=1.

Targets namespace "lme-830ce83f" -- the real graph from the Extraction Lab
investigation already has real Rachel/Housing fact-edges there (moved to
Chicago / Austin / the suburbs), so this exercises real candidate retrieval
and grounded classification instead of an empty graph. Confirms:
  1. the whole wired path doesn't crash end-to-end against real infra,
  2. the shadow trace actually gets logged to lifecycle_events,
  3. the real extraction result is completely unaffected by shadow mode.

Usage:
    .venv/Scripts/python.exe scripts/smoke/shadow_context_composition_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
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
    # The whole point of this smoke test: turn the flag on.
    os.environ["MENHIR_SHADOW_CONTEXT_COMPOSITION"] = "true"
    os.environ["MENHIR_SHADOW_COMPOSITION_TIMEOUT_S"] = "30"


_configure_environment()

from menhir.config import MemorySettings  # noqa: E402
from menhir.core.bootstrap import build_memory_services  # noqa: E402
from menhir.domain import new_session  # noqa: E402
from menhir.infrastructure.paths import telemetry_db_path  # noqa: E402

_NAMESPACE = "lme-830ce83f"
_EPISODE = "Rachel actually just moved back to the suburbs again."


async def _read_shadow_trace(episode_uuid: str, max_wait_s: float = 35.0) -> dict | None:
    """Poll the telemetry SQLite DB for the shadow trace's lifecycle_events row.
    The shadow task is a detached background task (by design -- see the plan), so
    it may still be running after ingest_episode() returns; poll rather than assume.

    MUST use asyncio.sleep, not time.sleep: this coroutine runs on the SAME event
    loop as the shadow background task it's waiting for. A blocking time.sleep here
    would starve that loop and prevent the shadow task from ever getting a chance to
    run at all -- which is exactly the bug this comment exists to prevent regressing.
    """
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
    print(f"shadow_context_composition={settings.shadow_context_composition} "
          f"timeout_s={settings.shadow_composition_timeout_s}")
    assert settings.shadow_context_composition is True, "flag did not parse as enabled"

    artifacts = build_memory_services(settings)
    ingest_service = artifacts.ingest_service

    print(f"\nIngesting into namespace={_NAMESPACE!r}: {_EPISODE!r}")
    session = new_session("shadow-smoke-test-user")
    started = time.monotonic()
    result = await ingest_service.ingest_episode(
        episode=_EPISODE, session=session, source="shadow-smoke-test", namespace=_NAMESPACE,
    )
    real_extraction_ms = int((time.monotonic() - started) * 1000)

    print(f"Real extraction result: status={result.status} episode_id={result.episode_id} "
          f"nodes_touched={result.nodes_touched} edges_touched={result.edges_touched} "
          f"({real_extraction_ms}ms)")

    print("\nPolling telemetry DB for the shadow trace (background task, up to 30s budget)...")
    trace = await _read_shadow_trace(result.episode_id)
    if trace is None:
        print("NO SHADOW TRACE FOUND within the poll window -- either the flag didn't take "
              "effect, or the background task is still running / never got dispatched.")
    else:
        print("\n=== Shadow trace ===")
        print(f"status: {trace['status']}")
        print(f"failure_stage: {trace['failure_stage']!r}  error: {trace['error']!r}")
        print(f"candidates retrieved: {len(trace['candidates'])}")
        for c in trace["candidates"]:
            print(f"  - fact_uuid={c['fact_uuid']} text={c['fact_text']!r} "
                  f"sources={c['retrieval_sources']}")
        print(f"message_hypotheses: {trace['message_hypotheses']}")
        print(f"candidate_labels: {trace['candidate_labels']}")
        print(f"rejected: {trace['rejected']}")
        print(f"selected_fact_uuid: {trace['selected_fact_uuid']!r}  "
              f"abstention_reason: {trace['abstention_reason']!r}")
        print(f"llm_tie_breaker_fired: {trace['llm_tie_breaker_fired']}")
        print(f"production_extracted_node_names: {trace['production_extracted_node_names']}")
        print(f"production_extracted_facts: {trace['production_extracted_facts']}")
        print(f"timing: candidate_retrieval_ms={trace['candidate_retrieval_ms']} "
              f"metadata_generation_ms={trace['metadata_generation_ms']} "
              f"selection_ms={trace['selection_ms']} shadow_total_ms={trace['shadow_total_ms']}")

    print("\nShutting down ingest_service (drains any still-running shadow tasks)...")
    await ingest_service.shutdown()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
