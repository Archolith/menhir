"""Phase 4b of menhir-extraction-context-ablation-handoff.md: four-selector comparison
with structured eligibility filtering, per direct redesign instruction (2026-07-16).

Compares:
  A. llm_only            -- Phase 4's original prototype (prose only)
  B. structured_only      -- pure rule-based filter, no LLM (deterministic, 1 trial suffices)
  C. structured_then_llm  -- filter first, LLM only for residual ambiguity
  D. oracle               -- ground truth, upper-bound sanity reference (deterministic)

Across 21 cells (7 scenarios x 3 fixtures). LLM-involving selectors (A, C) run repeated
trials; deterministic selectors (B, D) run once per scenario (no sampling variance to
measure).

Two metrics, per direct instruction (precision prioritized over coverage):
  injection precision = of trials where something was injected, how often correct
  injection coverage  = of trials where a correct candidate existed, how often found

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase4b_eligibility.py [--trials N]
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
from menhir.explorer.extraction_lab_eligibility_selection import (  # noqa: E402
    EligibilityScenario,
    select_llm_only,
    select_structured_only,
    select_structured_then_llm,
    select_oracle,
)
from menhir.explorer.extraction_lab_eligibility_fixtures import (  # noqa: E402
    build_all_eligibility_scenarios,
)


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _precision_coverage(records: list[dict]) -> tuple[float, int, int, float, int, int]:
    """Returns (precision, precision_n_injected, precision_n_correct,
    coverage, coverage_n_had_correct, coverage_n_found)."""
    injected = [r for r in records if r["selected_id"] is not None]
    precision_correct = sum(1 for r in injected if r["correct"])
    precision = precision_correct / len(injected) if injected else 1.0  # vacuous: never injected wrongly

    had_correct = [r for r in records if r["correct_candidate_id"] is not None]
    coverage_found = sum(1 for r in had_correct if r["correct"])
    coverage = coverage_found / len(had_correct) if had_correct else 1.0

    return (precision, len(injected), precision_correct, coverage, len(had_correct), coverage_found)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10, help="Trials per scenario for LLM-involving selectors")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    llm_backend = build_chat_backend(MemorySettings.from_env())
    scenarios = build_all_eligibility_scenarios()

    # Deterministic selectors: 1 trial each, no point repeating identical calls.
    print("Running deterministic selectors (structured_only, oracle) -- 1 pass each...")
    det_results: list[dict] = []
    for s in scenarios:
        r_b = select_structured_only(s)
        r_b["trial"] = 0
        r_b["ok"] = True
        det_results.append(r_b)
        r_d = select_oracle(s)
        r_d["trial"] = 0
        r_d["ok"] = True
        det_results.append(r_d)

    # LLM-involving selectors: repeated interleaved trials.
    grid: list[tuple[EligibilityScenario, str, int]] = []
    for s in scenarios:
        for trial in range(args.trials):
            grid.append((s, "llm_only", trial))
            grid.append((s, "structured_then_llm", trial))
    rng = random.Random(args.seed)
    rng.shuffle(grid)

    print(f"Running {len(grid)} LLM-involving trials, shuffled order...")
    llm_results: list[dict] = []
    for i, (scenario, selector_name, trial) in enumerate(grid):
        try:
            if selector_name == "llm_only":
                r = await select_llm_only(llm_backend, scenario)
            else:
                r = await select_structured_then_llm(llm_backend, scenario)
            r["trial"] = trial
            r["ok"] = True
        except Exception as exc:
            r = {
                "scenario_id": scenario.scenario_id, "fixture_qid": scenario.fixture_qid,
                "selector": selector_name, "trial": trial, "ok": False,
                "error": f"{type(exc).__name__}: {exc}", "selected_id": None,
                "correct_candidate_id": scenario.correct_candidate_id, "correct": False,
                "is_decoy_selection": False, "llm_calls": 0,
            }
        llm_results.append(r)
        status = "correct" if r["correct"] else ("decoy!" if r.get("is_decoy_selection") else ("ERR" if not r["ok"] else "wrong"))
        print(f"  [{i+1}/{len(grid)}] {scenario.scenario_id:35s} [{selector_name:20s}] trial {trial} -> {status}", flush=True)

    all_results = det_results + llm_results
    out_path = MENHIR_ROOT / "results" / "extraction_lab_phase4b_eligibility.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"\nRaw trial results written to {out_path}")

    # Report per-selector: injection precision and injection coverage.
    print("\n=== Per-selector: injection precision vs. injection coverage ===")
    by_selector: dict[str, list[dict]] = defaultdict(list)
    for r in all_results:
        by_selector[r["selector"]].append(r)

    for selector_name in ("llm_only", "structured_only", "structured_then_llm", "oracle"):
        recs = by_selector.get(selector_name, [])
        if not recs:
            continue
        precision, p_n, p_correct, coverage, c_n, c_found = _precision_coverage(recs)
        total_llm_calls = sum(r.get("llm_calls", 0) for r in recs)
        decoy_count = sum(1 for r in recs if r.get("is_decoy_selection"))
        print(
            f"  {selector_name:22s} precision={precision:.0%} ({p_correct}/{p_n} injected-were-correct)  "
            f"coverage={coverage:.0%} ({c_found}/{c_n} correct-cases-found)  "
            f"decoy_selections={decoy_count}  total_llm_calls={total_llm_calls}  n_trials={len(recs)}"
        )

    # Break down by scenario type too (aggregated across fixtures) for llm_only and
    # structured_then_llm, since those are the ones with repeated trials worth a CI.
    print("\n=== llm_only vs structured_then_llm, by scenario type (95% Wilson CI on 'correct') ===")
    scenario_types = [
        "1_one_correct", "2_wrong_state_family", "3_wrong_scope", "4_stale_vs_current",
        "5_near_neighbor_decoys_only", "6_no_subject_match", "7_missing_metadata",
    ]
    for stype in scenario_types:
        for selector_name in ("llm_only", "structured_then_llm"):
            matching = [r for r in by_selector.get(selector_name, []) if r["scenario_id"].endswith(stype)]
            n = len(matching)
            s = sum(1 for r in matching if r["correct"])
            lo, hi = _wilson_interval(s, n)
            print(f"  {stype:32s} [{selector_name:20s}] {s}/{n} ({s/n:.0%} CI [{lo:.0%},{hi:.0%}])" if n else f"  {stype:32s} [{selector_name}] NO DATA")


if __name__ == "__main__":
    asyncio.run(main())
