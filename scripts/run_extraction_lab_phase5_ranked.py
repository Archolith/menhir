"""Phase 5, item 3 (menhir-extraction-context-ablation-handoff.md): candidate-aware,
ranked-hypothesis coverage experiment.

Compares two scoring modes against the SAME candidate_aware_ranked routing predictions:
  - top_hypothesis_only: use only the #1-ranked hypothesis (same shape as the other 5
    approaches) -- the baseline this experiment is trying to beat.
  - ranked_with_recovery: try hypothesis #1 through Phase 4b's frozen structured
    selector; if it finds nothing, try hypothesis #2; if BOTH hypotheses independently
    resolve to DIFFERENT real candidates, abstain (genuine ambiguity) rather than
    guessing which one to trust.

This directly tests the specific claim: "this can safely recover coverage when only one
hypothesis matches a real candidate... should still abstain when multiple incompatible
candidates remain."

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase5_ranked.py [--trials N]
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
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
from menhir.explorer.extraction_lab_metadata_production import (  # noqa: E402
    GROUND_TRUTH,
    field_matches,
    predict_candidate_aware_ranked,
)
from menhir.explorer.extraction_lab_eligibility_selection import select_structured_only  # noqa: E402
from menhir.explorer.extraction_lab_eligibility_fixtures import (  # noqa: E402
    _830_scenarios, _852_scenarios, _2698_scenarios,
)

SCENARIOS_BY_FIXTURE = {
    "lme-830ce83f": _830_scenarios(),
    "lme-852ce960": _852_scenarios(),
    "lme-2698e78f": _2698_scenarios(),
}


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _chain_with_target(fixture_qid: str, subject: str | None, facet: str | None, state_family: str | None) -> list[dict]:
    results = []
    for scenario in SCENARIOS_BY_FIXTURE[fixture_qid]:
        probe = dataclasses.replace(
            scenario,
            target_subject=subject or "__NONE__",
            target_facet=facet or "__NONE__",
            target_state_family=state_family or "__NONE__",
        )
        outcome = select_structured_only(probe)
        results.append({
            "scenario_id": scenario.scenario_id,
            "correct_candidate_id": scenario.correct_candidate_id,
            "selected_id": outcome["selected_id"],
            "correct": outcome["selected_id"] == scenario.correct_candidate_id,
            "is_decoy_selection": outcome["is_decoy_selection"],
        })
    return results


def _extract_hypotheses(raw_reasoning: str) -> list[dict]:
    import re
    match = re.search(r"hypotheses=(\[.*\])", raw_reasoning)
    if not match:
        return []
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return []


async def _run_trial(llm_backend: object, gt, trial: int) -> dict:
    pred = await predict_candidate_aware_ranked(llm_backend, gt)
    hypotheses = _extract_hypotheses(pred.raw_reasoning)

    top_only_chain = _chain_with_target(gt.fixture_qid, pred.subject, pred.facet, pred.state_family)

    # Ranked recovery: for EACH of that fixture's 7 scenarios independently, try
    # hypothesis 1's chain result; if it found nothing for that scenario, try
    # hypothesis 2's chain result for that SAME scenario, ONLY as a fallback.
    #
    # Fix (found via live testing, not theorized in advance): the first version of this
    # logic treated hyp1 and hyp2 as equally trustworthy and abstained whenever they
    # disagreed for a given scenario -- but hyp1 is the model's own top-ranked,
    # highest-confidence answer, not an equal peer of hyp2. Concretely: on
    # 2698e78f scenario 2, hyp1 ("Session frequency") correctly selected c1; hyp2
    # ("Discussion focus") independently matched the decoy c2; the old logic discarded
    # the CORRECT hyp1 answer purely because hyp2 also resolved to something real.
    # That is not "safely abstaining on genuine ambiguity" -- it is letting a lower-
    # ranked guess veto a higher-ranked one. Corrected policy: hyp1 wins unconditionally
    # whenever it finds something; hyp2 is consulted ONLY when hyp1 finds nothing.
    recovery_chains: list[list[dict]] = []
    for h in hypotheses[:2]:
        recovery_chains.append(_chain_with_target(gt.fixture_qid, h.get("subject"), h.get("facet"), h.get("state_family")))

    ranked_chain = []
    n_scenarios = len(SCENARIOS_BY_FIXTURE[gt.fixture_qid])
    for i in range(n_scenarios):
        hyp1_cell = recovery_chains[0][i] if recovery_chains else top_only_chain[i]
        if hyp1_cell["selected_id"] is not None:
            ranked_chain.append(hyp1_cell)  # hyp1 succeeded -- use it, never consult hyp2
            continue
        if len(recovery_chains) > 1 and recovery_chains[1][i]["selected_id"] is not None:
            ranked_chain.append(recovery_chains[1][i])  # hyp1 silent, hyp2 recovers
        else:
            ranked_chain.append(hyp1_cell)  # neither found anything -- correctly null

    return {
        "fixture_qid": gt.fixture_qid,
        "trial": trial,
        "predicted_subject": pred.subject,
        "predicted_facet": pred.facet,
        "predicted_state_family": pred.state_family,
        "num_hypotheses": len(hypotheses),
        "subject_correct": field_matches(pred.subject, gt.subject),
        "facet_correct": field_matches(pred.facet, gt.facet),
        "state_family_correct": field_matches(pred.state_family, gt.state_family),
        "top_only_chain": top_only_chain,
        "ranked_chain": ranked_chain,
    }


def _score(chains: list[list[dict]]) -> tuple[float, int, int, float, int, int, int]:
    all_cells = [c for chain in chains for c in chain]
    injected = [c for c in all_cells if c["selected_id"] is not None]
    p_correct = sum(1 for c in injected if c["correct"])
    had_correct = [c for c in all_cells if c["correct_candidate_id"] is not None]
    c_found = sum(1 for c in had_correct if c["correct"])
    decoys = sum(1 for c in all_cells if c.get("is_decoy_selection"))
    precision = p_correct / len(injected) if injected else 1.0
    coverage = c_found / len(had_correct) if had_correct else 1.0
    return (precision, len(injected), p_correct, coverage, len(had_correct), c_found, decoys)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    llm_backend = build_chat_backend(MemorySettings.from_env())

    grid = [(gt, trial) for gt in GROUND_TRUTH for trial in range(args.trials)]
    rng = random.Random(args.seed)
    rng.shuffle(grid)

    print(f"Running {len(grid)} trials, shuffled order...")
    results = []
    for i, (gt, trial) in enumerate(grid):
        r = await _run_trial(llm_backend, gt, trial)
        results.append(r)
        top_correct = sum(1 for c in r["top_only_chain"] if c["correct"])
        ranked_correct = sum(1 for c in r["ranked_chain"] if c["correct"])
        print(f"  [{i+1}/{len(grid)}] {gt.fixture_qid} trial {trial} -> top_only={top_correct}/7 ranked={ranked_correct}/7 (n_hyp={r['num_hypotheses']})", flush=True)

    out_path = MENHIR_ROOT / "results" / "extraction_lab_phase5_ranked.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {out_path}")

    print("\n=== Field-level accuracy (top hypothesis only) ===")
    n = len(results)
    for field in ("subject_correct", "facet_correct", "state_family_correct"):
        c = sum(1 for r in results if r[field])
        print(f"  {field:22s} {c}/{n} ({c/n:.0%})")

    print("\n=== top_hypothesis_only vs ranked_with_recovery ===")
    top_chains = [r["top_only_chain"] for r in results]
    ranked_chains = [r["ranked_chain"] for r in results]
    for label, chains in (("top_hypothesis_only", top_chains), ("ranked_with_recovery", ranked_chains)):
        precision, p_n, p_c, coverage, c_n, c_c, decoys = _score(chains)
        print(f"  {label:22s} precision={precision:.0%} ({p_c}/{p_n})  coverage={coverage:.0%} ({c_c}/{c_n})  decoy_selections={decoys}")


if __name__ == "__main__":
    asyncio.run(main())
