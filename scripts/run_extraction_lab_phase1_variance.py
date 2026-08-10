"""Phase 1 of menhir-extraction-context-ablation-handoff.md: quantify model variance.

Runs N repeated trials of 4 configurations (baseline, update_aware, baseline+lookup,
update_aware+lookup) against the 3 real RCA fixtures, with execution order randomized
across the whole (fixture, config, trial) grid to avoid time/order bias, and reports
per-case SUCCESS FREQUENCY (proposition_recall >= 1.0 -- full proposition capture, not
just a mention match, per the handoff's own success definition) rather than a single
mean, since the whole point of this phase is to tell signal apart from gpt-4o-mini's
run-to-run sampling variance at temperature=0.0.

Context is fixed at context_episode_count=3 for every trial (the config that showed a
real effect in the earlier one-off spot-check) so this phase isolates model variance,
not context-window variance -- that's Phase 2 (context-form ablation)'s job.

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase1_variance.py [--trials N]
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
    ExtractionLabTuning,
    run_extraction_lab,
)
from menhir.explorer.extraction_lab_fixtures import RCA_FIXTURES  # noqa: E402

CONTEXT_EPISODE_COUNT = 3  # fixed for this phase; see module docstring

CONFIGS = [
    ("baseline", ExtractionLabTuning(prompt_variant="baseline", context_episode_count=CONTEXT_EPISODE_COUNT)),
    ("update_aware", ExtractionLabTuning(prompt_variant="update_aware", context_episode_count=CONTEXT_EPISODE_COUNT)),
    ("baseline_lookup", ExtractionLabTuning(prompt_variant="baseline", context_episode_count=CONTEXT_EPISODE_COUNT, enable_candidate_lookup=True)),
    ("update_aware_lookup", ExtractionLabTuning(prompt_variant="update_aware", context_episode_count=CONTEXT_EPISODE_COUNT, enable_candidate_lookup=True)),
]


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval -- more honest than a normal-approx CI at small n
    (won't produce nonsensical bounds outside [0,1] the way a naive CI can at n=10)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


async def _run_one_trial(
    fixture_qid: str,
    fixture: ExtractionLabRequest,
    config_id: str,
    tuning: ExtractionLabTuning,
    trial_index: int,
) -> dict:
    request = ExtractionLabRequest(
        current_message=fixture.current_message,
        previous_episodes=fixture.previous_episodes,
        gold=fixture.gold,
        source_namespace=fixture.source_namespace,
        arms=[ExtractionLabArm(id=config_id, label=config_id, tuning=tuning)],
    )
    payload = await run_extraction_lab(request)
    arm = payload.arms[0]
    record = {
        "fixture": fixture_qid,
        "config": config_id,
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
    else:
        record["proposition_success"] = False
    return record


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10, help="Trials per (fixture, config) cell")
    parser.add_argument("--seed", type=int, default=42, help="Shuffle seed for reproducible ordering")
    args = parser.parse_args()

    grid: list[tuple[str, ExtractionLabRequest, str, ExtractionLabTuning, int]] = []
    for fixture in RCA_FIXTURES:
        qid = fixture.source_namespace or "unknown"
        for config_id, tuning in CONFIGS:
            for trial in range(args.trials):
                grid.append((qid, fixture, config_id, tuning, trial))

    rng = random.Random(args.seed)
    rng.shuffle(grid)  # interleave to avoid time/order bias, per the handoff's requirement

    print(f"Running {len(grid)} trials ({len(RCA_FIXTURES)} fixtures x {len(CONFIGS)} configs x {args.trials} trials), shuffled order...")

    results: list[dict] = []
    for i, (qid, fixture, config_id, tuning, trial) in enumerate(grid):
        rec = await _run_one_trial(qid, fixture, config_id, tuning, trial)
        results.append(rec)
        status = "OK" if rec.get("proposition_success") else ("ERR" if not rec["ok"] else "miss")
        print(f"  [{i+1}/{len(grid)}] {qid} | {config_id:22s} | trial {trial} -> {status} ({rec['elapsed_ms']:.0f}ms)", flush=True)

    out_path = MENHIR_ROOT / "results" / "extraction_lab_phase1_variance.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nRaw trial results written to {out_path}")

    # Aggregate: per-(fixture, config) success frequency + Wilson 95% CI.
    buckets: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for rec in results:
        buckets[(rec["fixture"], rec["config"])].append(bool(rec["proposition_success"]))

    print("\n=== Per-case proposition-success frequency (95% Wilson CI) ===")
    for qid in [f.source_namespace for f in RCA_FIXTURES]:
        print(f"\n{qid}:")
        for config_id, _ in CONFIGS:
            outcomes = buckets.get((qid, config_id), [])
            n = len(outcomes)
            s = sum(outcomes)
            lo, hi = _wilson_interval(s, n)
            print(f"  {config_id:22s} {s}/{n}   ({s/n:.0%})   95% CI [{lo:.0%}, {hi:.0%}]" if n else f"  {config_id:22s} NO DATA")

    print("\n=== Aggregate across all 3 fixtures ===")
    for config_id, _ in CONFIGS:
        all_outcomes = [rec["proposition_success"] for rec in results if rec["config"] == config_id]
        n = len(all_outcomes)
        s = sum(all_outcomes)
        lo, hi = _wilson_interval(s, n)
        print(f"  {config_id:22s} {s}/{n}   ({s/n:.0%})   95% CI [{lo:.0%}, {hi:.0%}]")


if __name__ == "__main__":
    asyncio.run(main())
