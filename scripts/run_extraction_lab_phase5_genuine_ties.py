"""Phase 5, item 4 (menhir-extraction-context-ablation-handoff.md): genuine-tie suite.

Runs select_structured_then_llm against 3 scenarios deliberately constructed so 2
candidates survive the hard filter with IDENTICAL valid_at -- the one case the
recency tie-break cannot resolve. This is the FIRST time in the entire investigation
this code path has actually been exercised (confirmed: 0 llm_calls across all of
Phase 4b's 210 trials).

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase5_genuine_ties.py [--trials N]
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
from menhir.explorer.extraction_lab_eligibility_selection import select_structured_then_llm  # noqa: E402
from menhir.explorer.extraction_lab_genuine_tie_suite import build_genuine_tie_scenarios  # noqa: E402


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    llm_backend = build_chat_backend(MemorySettings.from_env())
    scenarios = build_genuine_tie_scenarios()

    grid = [(gts, trial) for gts in scenarios for trial in range(args.trials)]
    rng = random.Random(args.seed)
    rng.shuffle(grid)

    print(f"Running {len(grid)} trials, shuffled order...")
    results = []
    for i, (gts, trial) in enumerate(grid):
        outcome = await select_structured_then_llm(llm_backend, gts.scenario)
        acceptable = outcome["selected_id"] in gts.acceptable_selections
        results.append({
            "scenario_id": gts.scenario_id,
            "trial": trial,
            "selected_id": outcome["selected_id"],
            "llm_calls": outcome["llm_calls"],
            "acceptable": acceptable,
            "reasoning": outcome.get("reasoning", ""),
        })
        print(f"  [{i+1}/{len(grid)}] {gts.scenario_id:32s} trial {trial} -> selected={outcome['selected_id']!s:6s} llm_calls={outcome['llm_calls']} acceptable={acceptable}", flush=True)

    out_path = MENHIR_ROOT / "results" / "extraction_lab_phase5_genuine_ties.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {out_path}")

    print("\n=== Per-scenario: did the LLM actually get consulted, and what did it pick? ===")
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_scenario[r["scenario_id"]].append(r)
    for gts in scenarios:
        recs = by_scenario[gts.scenario_id]
        total_llm_calls = sum(r["llm_calls"] for r in recs)
        acceptable_count = sum(1 for r in recs if r["acceptable"])
        selections = defaultdict(int)
        for r in recs:
            selections[r["selected_id"]] += 1
        lo, hi = _wilson_interval(acceptable_count, len(recs))
        print(f"\n{gts.scenario_id}: {gts.description}")
        print(f"  total_llm_calls={total_llm_calls}/{len(recs)} (confirms the fallback path fired every trial)")
        print(f"  selection distribution: {dict(selections)}")
        print(f"  acceptable outcomes: {acceptable_count}/{len(recs)} ({acceptable_count/len(recs):.0%} CI [{lo:.0%},{hi:.0%}])")


if __name__ == "__main__":
    asyncio.run(main())
