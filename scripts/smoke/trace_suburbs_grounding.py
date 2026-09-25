"""Definitive trace: where does suburbs→Chicago corruption occur?

Each trial uses a completely fresh namespace. Captures:
  1. Raw combined-extraction entities (names)
  2. Raw combined-extraction edge endpoints (entity names, not UUIDs)
  3. Node dedup decisions (per-entity: new vs merged, target name if merged)
  4. Pre-resolution UUID → resolved UUID map
  5. Edge endpoints after UUID remapping
  6. Final persisted graph state

Reports per-trial structured results and aggregate statistics.

Usage:
    python scripts/smoke/trace_suburbs_grounding.py [trials]
    OPENAI_CHAT_MODEL=gpt-4o-mini python scripts/smoke/trace_suburbs_grounding.py 20
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

MENHIR_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(MENHIR_ROOT / "src"))
load_dotenv(MENHIR_ROOT / ".env")

# The trace_suburbs_grounding_* sibling modules live beside this script; make
# them importable in every load mode (direct script run, spec_from_file_location, -m).
_SMOKE_DIR = os.path.dirname(os.path.abspath(__file__))
if _SMOKE_DIR not in sys.path:
    sys.path.insert(0, _SMOKE_DIR)

os.environ["NEO4J_URI"] = os.getenv("MENHIR_LME_NEO4J_URI", "bolt://localhost:7694")
os.environ["NEO4J_PASSWORD"] = os.environ.get("MENHIR_BENCH_NEO4J_PASSWORD", "")
os.environ["GRAPHITI_LLM_PROVIDER"] = "openai"
os.environ["MEMORY_GRAPHITI_PROVIDER"] = "openai"
os.environ["LLM_CHAT_PROVIDER"] = "openai"
os.environ["MEMORY_CHAT_PROVIDER"] = "openai"
os.environ["OPENAI_CHAT_MODEL"] = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
os.environ.setdefault("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Suppress noisy loggers, keep our trace and graphiti extraction/dedup
logging.basicConfig(level=logging.WARNING, format="%(name)s %(levelname)s %(message)s")
logging.getLogger("trace").setLevel(logging.INFO)

trace_log = logging.getLogger("trace")

# Facade re-exports: every symbol moved to a trace_suburbs_grounding_* sibling
# stays importable from this module path.
from trace_suburbs_grounding_model import EdgeTrace, TrialResult  # noqa: E402
from trace_suburbs_grounding_trial import _get_node_name, run_trial  # noqa: E402

__all__ = [
    "MENHIR_ROOT", "EdgeTrace", "TrialResult", "trace_log", "run_trial", "_get_node_name", "main",
]


async def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    trace_log.info("Model: %s", os.environ["OPENAI_CHAT_MODEL"])
    trace_log.info("Trials: %d (each in fresh namespace)", trials)

    results: list[TrialResult] = []
    for i in range(trials):
        trace_log.info("--- Trial %d/%d ---", i + 1, trials)
        r = await run_trial(i + 1, trials)
        results.append(r)

        # Print per-trial summary
        raw_edge_targets = [e.get("target", "") for e in r.raw_extracted_edges]
        trace_log.info(
            "  raw_entities=%s  raw_edge_targets=%s  orphaned=%s  verdict=%s",
            r.raw_extracted_entities,
            raw_edge_targets,
            r.orphaned_entities,
            r.verdict,
        )
        if r.verdict != "PASS":
            trace_log.info("  final_entities=%s", r.final_entities)
            trace_log.info("  final_edges=%s", r.final_edges)
            if r.edge_traces:
                trace_log.info("  edge_remap=%s", r.edge_traces)
            # Invalidation trace detail for FAIL_C verdicts
            if r.verdict.startswith("FAIL_C") and r.invalidation_traces:
                for inv in r.invalidation_traces:
                    if "suburb" in inv.get("new_fact", "").lower():
                        trace_log.info("  inv_new_fact=%s", inv["new_fact"])
                        trace_log.info("  inv_chicago_in_candidates=%s", inv.get("chicago_in_candidates"))
                        trace_log.info("  inv_candidates=%d related + %d invalidation",
                                       len(inv.get("related_edges", [])),
                                       len(inv.get("invalidation_candidates", [])))
                        for c in inv.get("invalidation_candidates", []):
                            trace_log.info("    candidate[%d]: %s", c["idx"], c["fact"][:80])
                        llm_resp = inv.get("llm_response", {})
                        trace_log.info("  inv_llm_contradicted=%s  inv_llm_duplicates=%s",
                                       llm_resp.get("contradicted_facts", []),
                                       llm_resp.get("duplicate_facts", []))
                        trace_log.info("  inv_invalidated=%s",
                                       [e["fact"][:60] for e in inv.get("invalidated_facts", [])])

    # Aggregate
    verdicts = {}
    for r in results:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1

    trace_log.info("")
    trace_log.info("=" * 60)
    trace_log.info("AGGREGATE (%d trials, model=%s, temperature=%s)",
                   trials, results[0].model if results else "?",
                   results[0].temperature if results else "?")
    trace_log.info("=" * 60)
    for verdict, count in sorted(verdicts.items()):
        pct = count / trials * 100
        trace_log.info("  %-30s %3d/%d (%.0f%%)", verdict, count, trials, pct)

    # Dump full results to file
    output_path = MENHIR_ROOT / "results" / "suburbs_grounding_trace.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([asdict(r) for r in results], indent=2, default=str),
        encoding="utf-8",
    )
    trace_log.info("Full results: %s", output_path)


if __name__ == "__main__":
    asyncio.run(main())
