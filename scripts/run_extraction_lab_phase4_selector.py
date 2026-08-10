"""Phase 4 of menhir-extraction-context-ablation-handoff.md: context-SELECTION test.

Tests whether an LLM-based selector can pick the single correct fact from a
hand-authored candidate pool (or correctly decline when nothing is correct),
across the 5 required negative-control scenarios x 3 real RCA fixtures = 15 cells,
with repeated interleaved trials -- same Wilson-CI methodology as Phases 1-3.

This tests SELECTION in isolation (per direct instruction: measure this on its own
terms, not inferred from downstream extraction success, since Phase 2's finding 4
showed extraction can succeed for the wrong reason). Entirely inside Recall Labs --
hand-authored pools, no graph schema changes (confirmed 2026-07-16).

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase4_selector.py [--trials N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

MENHIR_ROOT = Path(__file__).resolve().parent.parent
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


_configure_environment()

from menhir.config import MemorySettings  # noqa: E402
from menhir.infrastructure.providers import build_chat_backend  # noqa: E402
from menhir.explorer.extraction_lab_context_selection import (  # noqa: E402
    SelectionScenario,
    build_all_scenarios,
    run_selector,
)


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


async def _run_one_trial(llm_backend: object, scenario: SelectionScenario, trial_index: int) -> dict:
    try:
        result = await run_selector(llm_backend, scenario)
        result["trial"] = trial_index
        result["ok"] = True
        result["error"] = None
    except Exception as exc:
        result = {
            "scenario_id": scenario.scenario_id,
            "fixture_qid": scenario.fixture_qid,
            "trial": trial_index,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "correct": False,
            "is_decoy_selection": False,
            "selected_id": None,
        }
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10, help="Trials per scenario")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    llm_backend = build_chat_backend(MemorySettings.from_env())

    scenarios = build_all_scenarios()
    grid: list[tuple[SelectionScenario, int]] = [
        (s, trial) for s in scenarios for trial in range(args.trials)
    ]
    rng = random.Random(args.seed)
    rng.shuffle(grid)

    print(f"Running {len(grid)} trials ({len(scenarios)} scenarios x {args.trials} trials), shuffled order...")

    results: list[dict] = []
    for i, (scenario, trial) in enumerate(grid):
        rec = await _run_one_trial(llm_backend, scenario, trial)
        results.append(rec)
        status = "correct" if rec["correct"] else ("decoy!" if rec.get("is_decoy_selection") else ("ERR" if not rec["ok"] else "wrong"))
        print(f"  [{i+1}/{len(grid)}] {scenario.scenario_id:45s} trial {trial} -> {status}", flush=True)

    out_path = MENHIR_ROOT / "results" / "extraction_lab_phase4_selector.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nRaw trial results written to {out_path}")

    # Per-scenario correctness.
    print("\n=== Per-scenario selection correctness (95% Wilson CI) ===")
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for rec in results:
        by_scenario[rec["scenario_id"]].append(rec)
    for scenario in scenarios:
        recs = by_scenario.get(scenario.scenario_id, [])
        n = len(recs)
        s = sum(1 for r in recs if r["correct"])
        decoy_count = sum(1 for r in recs if r.get("is_decoy_selection"))
        lo, hi = _wilson_interval(s, n)
        print(
            f"  {scenario.scenario_id:45s} {s}/{n} correct ({s/n:.0%} CI [{lo:.0%},{hi:.0%}])"
            f"  decoy_selections={decoy_count}" if n else f"  {scenario.scenario_id:45s} NO DATA"
        )

    # Aggregate by scenario TYPE (1-5), across all 3 fixtures -- this is the real
    # generalizable signal, since scenario numbering is shared across fixtures.
    print("\n=== Aggregate by scenario type (across all 3 fixtures) ===")
    scenario_types = ["1_one_correct", "2_same_subject_multi_facet", "3_same_facet_multi_subject",
                       "4_stale_vs_current", "5_no_relevant_fact"]
    for stype in scenario_types:
        matching = [r for r in results if r["scenario_id"].endswith(stype)]
        n = len(matching)
        s = sum(1 for r in matching if r["correct"])
        decoy_count = sum(1 for r in matching if r.get("is_decoy_selection"))
        lo, hi = _wilson_interval(s, n)
        print(f"  {stype:35s} {s}/{n} correct ({s/n:.0%} CI [{lo:.0%},{hi:.0%}])  decoy_selections={decoy_count}")

    # The single most important number: did the selector EVER pick a decoy when it
    # shouldn't have -- the concrete false-grounding failure mode.
    total_decoy_selections = sum(1 for r in results if r.get("is_decoy_selection"))
    print(f"\n=== TOTAL decoy selections across all {len(results)} trials: {total_decoy_selections} ===")
    print("(A decoy selection means the selector picked a wrong-subject, wrong-facet, or")
    print(" stale fact instead of the correct one or 'nothing' -- the precise failure mode")
    print(" this phase exists to catch before any production wiring.)")


if __name__ == "__main__":
    asyncio.run(main())
