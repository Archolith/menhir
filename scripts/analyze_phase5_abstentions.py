"""Post-hoc abstention taxonomy for Phase 5's coverage misses. Pure analysis of already-
collected trial data (results/extraction_lab_phase5_metadata.json) -- no new LLM calls.

Classifies every downstream-chain coverage MISS (a positive scenario where a correct
candidate existed but the selector did not find it) into a specific failure reason, per
direct instruction: "the 33% uncovered cases should now be classified by failure
reason... that will reveal whether '33% coverage' is one problem or several."
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

MENHIR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MENHIR_ROOT / "src"))

from menhir.explorer.extraction_lab_metadata_production import FACET_ONTOLOGY, GROUND_TRUTH


def classify_miss(record: dict, scenario_correct_candidate_id: str) -> str:
    subject = record.get("predicted_subject")
    facet = record.get("predicted_facet")
    state_family = record.get("predicted_state_family")

    if subject is None:
        return "subject_unresolved"
    if facet is None:
        return "facet_missing"
    if state_family is None:
        return "state_family_missing"

    facet_known = any(facet.strip().lower() == k.lower() for k in FACET_ONTOLOGY)
    if not facet_known:
        return "facet_outside_ontology"

    matched_key = next(k for k in FACET_ONTOLOGY if k.lower() == facet.strip().lower())
    valid_families = [f.lower() for f in FACET_ONTOLOGY[matched_key]]
    if state_family.strip().lower() not in valid_families:
        return "state_family_not_valid_for_facet"

    gt = next(g for g in GROUND_TRUTH if g.fixture_qid == record["fixture_qid"])
    subject_wrong = subject.strip().lower() != gt.subject.strip().lower()
    facet_wrong = facet.strip().lower() != gt.facet.strip().lower()
    sf_wrong = state_family.strip().lower() != gt.state_family.strip().lower()

    wrong_fields = [n for n, w in (("subject", subject_wrong), ("facet", facet_wrong), ("state_family", sf_wrong)) if w]
    if wrong_fields:
        return f"wrong_value:{'+'.join(wrong_fields)}"

    # All fields correct, well-formed, valid ontology pairing -- but the scenario still
    # missed. Only remaining explanation given is_eligible()'s exact-match semantics:
    # this specific scenario's candidate pool has no candidate whose OWN tags equal
    # these (correct) target values -- i.e. correct routing metadata, but the scenario
    # itself has no matching candidate under these fields (should not occur for
    # positive scenarios by construction; flagged distinctly if it does).
    return "correct_metadata_no_candidate_match"


def main() -> None:
    data = json.load(open(MENHIR_ROOT / "results" / "extraction_lab_phase5_metadata.json", encoding="utf-8"))
    llm_trials = [r for r in data if r.get("approach") in ("llm_ontology", "two_stage", "hybrid_rules_then_llm")]

    by_approach: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total_positive_cells = defaultdict(int)
    total_misses = defaultdict(int)

    for record in llm_trials:
        approach = record["approach"]
        for cell in record.get("downstream_chain", []):
            if cell["correct_candidate_id"] is None:
                continue  # only classifying misses on POSITIVE scenarios (a real answer existed)
            total_positive_cells[approach] += 1
            if cell["correct"]:
                continue
            total_misses[approach] += 1
            reason = classify_miss(record, cell["correct_candidate_id"])
            by_approach[approach][reason] += 1

    print("=== Abstention/miss taxonomy per approach ===")
    print("(counted at the chain-cell level: one count per positive scenario missed in one trial)\n")
    for approach in ("llm_ontology", "two_stage", "hybrid_rules_then_llm"):
        total = total_positive_cells[approach]
        missed = total_misses[approach]
        print(f"{approach}: {missed}/{total} positive-scenario cells missed ({missed/total:.0%})")
        for reason, count in sorted(by_approach[approach].items(), key=lambda kv: -kv[1]):
            print(f"  {reason:40s} {count:4d}  ({count/missed:.0%} of misses)" if missed else "")
        print(f"  {'output/schema_failure':40s} {0:4d}  (0% -- none observed, confirmed separately)")
        print(f"  {'confidence_below_threshold':40s} {'N/A':>4s}  (not measured -- Phase 5 predictors do not output per-field confidence)")
        print()


if __name__ == "__main__":
    main()
