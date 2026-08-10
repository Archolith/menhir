"""Phase 2 of menhir-extraction-context-ablation-handoff.md: context-form ablation.

For each of the 3 real RCA fixtures, runs the 8 context-form conditions defined in
extraction_lab_context_ablation.py (A-G, J) for N interleaved trials, and reports
per-case proposition-success frequency with a Wilson 95% CI -- same methodology as
Phase 1's variance-quantification runner, applied to the "what kind of context helps"
question instead of "which prompt/lookup config helps."

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase2_context_ablation.py [--trials N]
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

from menhir.explorer.extraction_lab import (  # noqa: E402
    ExtractionLabArm,
    ExtractionLabRequest,
    run_extraction_lab,
)
from menhir.explorer.extraction_lab_context_ablation import (  # noqa: E402
    AblationCondition,
    build_all_conditions,
)


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


async def _run_one_trial(condition: AblationCondition, trial_index: int) -> dict:
    request = ExtractionLabRequest(
        current_message=condition.current_message,
        previous_episodes=condition.previous_episodes,
        gold=condition.gold,
        arms=[ExtractionLabArm(id=condition.condition_id, label=condition.condition_label, tuning=condition.tuning)],
    )
    payload = await run_extraction_lab(request)
    arm = payload.arms[0]
    record = {
        "fixture": condition.fixture_qid,
        "condition": condition.condition_id,
        "trial": trial_index,
        "ok": arm.ok,
        "error": arm.error,
        "elapsed_ms": arm.elapsed_ms,
        "known_entities_used": arm.known_entities_used,
    }
    if arm.ok:
        record["mentions"] = [m.get("text") for m in arm.extraction.mentions]
        record["propositions"] = [p.get("fact") for p in arm.extraction.propositions]
        record["gold_scores"] = arm.gold_scores.model_dump(mode="json")
        record["proposition_success"] = arm.gold_scores.proposition_recall >= 1.0
        record["mention_recall"] = arm.gold_scores.mention_recall
        record["mention_precision"] = arm.gold_scores.mention_precision
        record["unsupported_inference_rate"] = arm.gold_scores.unsupported_inference_rate
    else:
        record["proposition_success"] = False
    return record


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10, help="Trials per (fixture, condition) cell")
    parser.add_argument("--seed", type=int, default=42, help="Shuffle seed for reproducible ordering")
    args = parser.parse_args()

    conditions = build_all_conditions()
    grid: list[tuple[AblationCondition, int]] = [
        (cond, trial) for cond in conditions for trial in range(args.trials)
    ]
    rng = random.Random(args.seed)
    rng.shuffle(grid)

    print(f"Running {len(grid)} trials ({len(conditions)} (fixture,condition) cells x {args.trials} trials), shuffled order...")

    results: list[dict] = []
    for i, (condition, trial) in enumerate(grid):
        rec = await _run_one_trial(condition, trial)
        results.append(rec)
        status = "OK" if rec.get("proposition_success") else ("ERR" if not rec["ok"] else "miss")
        print(
            f"  [{i+1}/{len(grid)}] {condition.fixture_qid} | {condition.condition_id:22s} | "
            f"trial {trial} -> {status} ({rec['elapsed_ms']:.0f}ms)",
            flush=True,
        )

    out_path = MENHIR_ROOT / "results" / "extraction_lab_phase2_context_ablation.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nRaw trial results written to {out_path}")

    buckets: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for rec in results:
        buckets[(rec["fixture"], rec["condition"])].append(bool(rec["proposition_success"]))

    condition_order = [c.condition_id for c in build_conditions_first_fixture(conditions)]

    print("\n=== Per-case proposition-success frequency (95% Wilson CI) ===")
    fixture_qids = sorted({c.fixture_qid for c in conditions})
    for qid in fixture_qids:
        print(f"\n{qid}:")
        for condition_id in condition_order:
            outcomes = buckets.get((qid, condition_id), [])
            n = len(outcomes)
            s = sum(outcomes)
            if n:
                lo, hi = _wilson_interval(s, n)
                print(f"  {condition_id:22s} {s}/{n}   ({s/n:.0%})   95% CI [{lo:.0%}, {hi:.0%}]")
            else:
                print(f"  {condition_id:22s} NO DATA")

    print("\n=== Aggregate across all 3 fixtures ===")
    for condition_id in condition_order:
        all_outcomes = [rec["proposition_success"] for rec in results if rec["condition"] == condition_id]
        n = len(all_outcomes)
        s = sum(all_outcomes)
        lo, hi = _wilson_interval(s, n)
        print(f"  {condition_id:22s} {s}/{n}   ({s/n:.0%})   95% CI [{lo:.0%}, {hi:.0%}]")


def build_conditions_first_fixture(conditions: list[AblationCondition]) -> list[AblationCondition]:
    """Return conditions for just the first fixture qid, to get a stable, de-duplicated
    ordering of condition_ids for the report tables (all fixtures share the same 8
    condition ids, in the same build order)."""
    first_qid = conditions[0].fixture_qid
    return [c for c in conditions if c.fixture_qid == first_qid]


if __name__ == "__main__":
    asyncio.run(main())
