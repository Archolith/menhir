"""Phase 5 of menhir-extraction-context-ablation-handoff.md: metadata production.

Compares 5 query-side routing approaches (graph_lookup_only, llm_ontology, two_stage,
hybrid_rules_then_llm, oracle) on two independent layers:

  1. Field-level accuracy: does the predicted (subject, facet, state_family) match
     ground truth for each of the 3 real RCA fixtures' current_message.
  2. Downstream chain (the metric that actually matters): feed the PREDICTED metadata
     into Phase 4b's real is_eligible() filter against that fixture's real Phase 4b
     candidate pools (all 7 scenarios), and score whether the final selection is still
     correct. The Phase 4b selector is frozen -- only the metadata feeding it varies.

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase5_metadata.py [--trials N]
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
from menhir.infrastructure.graphiti_client import GraphitiClient  # noqa: E402
from menhir.explorer.extraction_lab_metadata_production import (  # noqa: E402
    GROUND_TRUTH,
    RoutingGroundTruth,
    RoutingPrediction,
    field_matches,
    predict_graph_lookup_only,
    predict_llm_ontology,
    predict_two_stage,
    predict_hybrid_rules_then_llm,
    predict_oracle,
)
from menhir.explorer.extraction_lab_eligibility_selection import (  # noqa: E402
    select_structured_only,
)
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


def _downstream_chain(prediction: RoutingPrediction) -> list[dict]:
    """Re-run Phase 4b's frozen structured selector against this fixture's real
    candidate pools, but with target_subject/facet/state_family swapped for the
    ROUTING PREDICTION's (possibly wrong) values instead of ground truth. target_scope
    is left as each scenario originally defined it -- routing does not predict scope
    in this phase, so scope resolution is treated as already-known/out of scope here."""
    results = []
    for scenario in SCENARIOS_BY_FIXTURE[prediction.fixture_qid]:
        probe = dataclasses.replace(
            scenario,
            target_subject=prediction.subject or "__NONE__",
            target_facet=prediction.facet or "__NONE__",
            target_state_family=prediction.state_family or "__NONE__",
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


async def _run_field_and_chain(
    approach: str,
    llm_backend: object,
    clients: object,
    gt: RoutingGroundTruth,
    trial: int,
) -> dict:
    if approach == "graph_lookup_only":
        pred = await predict_graph_lookup_only(clients, gt)
    elif approach == "llm_ontology":
        pred = await predict_llm_ontology(llm_backend, gt)
    elif approach == "two_stage":
        pred = await predict_two_stage(llm_backend, gt)
    elif approach == "hybrid_rules_then_llm":
        pred = await predict_hybrid_rules_then_llm(llm_backend, gt)
    elif approach == "oracle":
        pred = predict_oracle(gt)
    else:
        raise ValueError(approach)

    chain = _downstream_chain(pred)
    return {
        "approach": approach,
        "fixture_qid": gt.fixture_qid,
        "trial": trial,
        "predicted_subject": pred.subject,
        "predicted_facet": pred.facet,
        "predicted_state_family": pred.state_family,
        "subject_correct": field_matches(pred.subject, gt.subject),
        "facet_correct": field_matches(pred.facet, gt.facet),
        "state_family_correct": field_matches(pred.state_family, gt.state_family),
        "all_fields_correct": (
            field_matches(pred.subject, gt.subject)
            and field_matches(pred.facet, gt.facet)
            and field_matches(pred.state_family, gt.state_family)
        ),
        "reasoning": pred.raw_reasoning,
        "downstream_chain": chain,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10, help="Trials per (fixture, LLM approach) cell")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    llm_backend = build_chat_backend(MemorySettings.from_env())
    graphiti_client = GraphitiClient.from_settings(MemorySettings.from_env())
    clients = graphiti_client.client.clients

    deterministic = ["graph_lookup_only", "oracle"]
    llm_involving = ["llm_ontology", "two_stage", "hybrid_rules_then_llm"]

    print("Running deterministic approaches (1 pass each: graph_lookup_only, oracle)...")
    det_results: list[dict] = []
    for gt in GROUND_TRUTH:
        for approach in deterministic:
            r = await _run_field_and_chain(approach, llm_backend, clients, gt, 0)
            det_results.append(r)
            print(f"  {gt.fixture_qid} [{approach}] subject={r['predicted_subject']!r} facet={r['predicted_facet']!r} state_family={r['predicted_state_family']!r}")

    grid: list[tuple[RoutingGroundTruth, str, int]] = [
        (gt, approach, trial)
        for gt in GROUND_TRUTH
        for approach in llm_involving
        for trial in range(args.trials)
    ]
    rng = random.Random(args.seed)
    rng.shuffle(grid)
    print(f"\nRunning {len(grid)} LLM-involving trials, shuffled order...")

    llm_results: list[dict] = []
    for i, (gt, approach, trial) in enumerate(grid):
        try:
            r = await _run_field_and_chain(approach, llm_backend, clients, gt, trial)
        except Exception as exc:
            r = {
                "approach": approach, "fixture_qid": gt.fixture_qid, "trial": trial,
                "error": f"{type(exc).__name__}: {exc}",
                "subject_correct": False, "facet_correct": False, "state_family_correct": False,
                "all_fields_correct": False, "downstream_chain": [],
            }
        llm_results.append(r)
        fields_ok = "OK" if r.get("all_fields_correct") else "partial/wrong"
        chain_correct = sum(1 for c in r.get("downstream_chain", []) if c["correct"])
        chain_total = len(r.get("downstream_chain", []))
        print(f"  [{i+1}/{len(grid)}] {gt.fixture_qid} [{approach:22s}] trial {trial} -> fields={fields_ok:14s} chain={chain_correct}/{chain_total}", flush=True)

    all_results = det_results + llm_results
    out_path = MENHIR_ROOT / "results" / "extraction_lab_phase5_metadata.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {out_path}")

    print("\n=== Field-level accuracy, per approach (aggregate across 3 fixtures) ===")
    by_approach: dict[str, list[dict]] = defaultdict(list)
    for r in all_results:
        by_approach[r["approach"]].append(r)
    for approach in deterministic + llm_involving:
        recs = by_approach.get(approach, [])
        if not recs:
            continue
        n = len(recs)
        subj = sum(1 for r in recs if r["subject_correct"])
        facet = sum(1 for r in recs if r["facet_correct"])
        sf = sum(1 for r in recs if r["state_family_correct"])
        allf = sum(1 for r in recs if r["all_fields_correct"])
        print(f"  {approach:22s} n={n:3d}  subject={subj}/{n} ({subj/n:.0%})  facet={facet}/{n} ({facet/n:.0%})  state_family={sf}/{n} ({sf/n:.0%})  all_3={allf}/{n} ({allf/n:.0%})")

    print("\n=== Downstream chain: injection precision / coverage using PREDICTED metadata ===")
    print("(feeds each approach's routing prediction into Phase 4b's frozen structured selector")
    print(" against that fixture's real 7 candidate pools -- this is the metric that matters)")
    for approach in deterministic + llm_involving:
        recs = by_approach.get(approach, [])
        if not recs:
            continue
        all_chain = [c for r in recs for c in r.get("downstream_chain", [])]
        injected = [c for c in all_chain if c["selected_id"] is not None]
        p_correct = sum(1 for c in injected if c["correct"])
        had_correct = [c for c in all_chain if c["correct_candidate_id"] is not None]
        c_found = sum(1 for c in had_correct if c["correct"])
        decoys = sum(1 for c in all_chain if c.get("is_decoy_selection"))
        precision = p_correct / len(injected) if injected else 1.0
        coverage = c_found / len(had_correct) if had_correct else 1.0
        print(
            f"  {approach:22s} precision={precision:.0%} ({p_correct}/{len(injected)})  "
            f"coverage={coverage:.0%} ({c_found}/{len(had_correct)})  decoy_selections={decoys}  n_chain_cells={len(all_chain)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
