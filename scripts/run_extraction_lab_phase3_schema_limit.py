"""Phase 3 of menhir-extraction-context-ablation-handoff.md: RELEVANT_SCHEMA_LIMIT
as a causal control.

Tests context_episode_count (the harness's stand-in for graphiti-core's
RELEVANT_SCHEMA_LIMIT) using the REAL, full previous_episodes list already stored per
RCA fixture (not hand-authored ablation content, unlike Phase 2) -- this drives the
harness's real production-style window-selection path
(previous_episodes[-context_episode_count:]), the same slicing logic
GraphitiClient.from_settings()'s real add_episode() call performs via
retrieve_episodes(last_n=RELEVANT_SCHEMA_LIMIT).

Per direct instruction (2026-07-16): test 10, 20, 40, 80. IMPORTANT DATA-AVAILABILITY
NOTE: the RCA fixtures only have 12 (830ce83f) or 24 (2698e78f) previous_episodes
available (852ce960 is skipped entirely -- confirmed ceiling case, no repeated run
needed per instruction). For 830ce83f, every value >= 12 (so 20/40/80, and 12 itself)
produces an IDENTICAL context_episode_count slice -- the full available list. Testing
those three values would just triple-run the same condition. Instead: the requested
10/20/40/80 values are run where they are NOT degenerate (2698e78f only, since it has
24 available), plus a finer-grained set of values chosen to bracket the actual
establishing-episode inclusion/exclusion boundary for each fixture -- this is what
actually answers the causal question ("does inclusion in the window matter"), which a
coarse 10/20/40/80 sweep would mostly miss for the 12-episode fixtures.

The primary analysis is P(success | relevant establishing episode present in the
truncated window) vs P(success | absent) -- computed across ALL limit values tested for
a fixture, since presence is a deterministic function of (limit, fixture) here, not a
separate experiment.

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase3_schema_limit.py [--trials N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
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
from menhir.explorer.extraction_lab_fixtures import (  # noqa: E402
    FIXTURE_RCA_830CE83F,
    FIXTURE_RCA_2698E78F,
)


@dataclass(frozen=True)
class SchemaLimitFixtureSpec:
    qid: str
    fixture: ExtractionLabRequest
    establishing_indices: tuple[int, ...]  # into fixture.previous_episodes (0-indexed)
    # Requested-by-instruction values (10/20/40/80), kept for direct traceability, plus
    # finer-grained values bracketing the real inclusion/exclusion boundary. Deduplicated
    # and clamped to [1, len(previous_episodes)] at run time.
    limit_values: tuple[int, ...]


_SPECS: list[SchemaLimitFixtureSpec] = [
    SchemaLimitFixtureSpec(
        qid="lme-830ce83f",
        fixture=FIXTURE_RCA_830CE83F,
        establishing_indices=(2, 4),
        # boundary: N=8 includes idx4 only; N=10 includes both idx2 and idx4 (matches
        # production's real RELEVANT_SCHEMA_LIMIT=10 exactly at this fixture's length).
        limit_values=(1, 3, 5, 8, 9, 10, 12, 20, 40, 80),
    ),
    SchemaLimitFixtureSpec(
        qid="lme-2698e78f",
        fixture=FIXTURE_RCA_2698E78F,
        # NOTE: this fixture's establishing content appears TWICE (idx 2 and idx 21 --
        # the real conversation restates the "every two weeks" framing later). N=10
        # (production's real default) already includes idx21 but not idx2 -- worth
        # treating as its own reportable data point, not an error.
        establishing_indices=(2, 21),
        limit_values=(1, 3, 10, 20, 22, 24, 40, 80),
    ),
]


def _included_indices(total: int, limit: int) -> tuple[int, int]:
    """Return (start, end) of the slice previous_episodes[-limit:] for a list of
    length `total` -- matches _run_extraction_arm's real slicing exactly."""
    if limit >= total:
        return (0, total)
    return (total - limit, total)


def _establishing_present(spec: SchemaLimitFixtureSpec, effective_limit: int) -> tuple[bool, list[int]]:
    total = len(spec.fixture.previous_episodes)
    start, end = _included_indices(total, effective_limit)
    present_indices = [i for i in spec.establishing_indices if start <= i < end]
    return (len(present_indices) > 0, present_indices)


def _effective_limits(spec: SchemaLimitFixtureSpec) -> list[int]:
    """Dedupe + clamp requested limit_values to [1, total episodes] -- values above the
    fixture's actual episode count all collapse to the same full-list slice, so only
    keep one representative (the smallest such value) to avoid wasting trials on
    identical conditions."""
    total = len(spec.fixture.previous_episodes)
    seen_full = False
    result: list[int] = []
    for limit in sorted(set(spec.limit_values)):
        if limit >= total:
            if seen_full:
                continue
            seen_full = True
            result.append(total)  # report as the fixture's actual length, not the requested number
        else:
            result.append(limit)
    return result


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


async def _run_one_trial(spec: SchemaLimitFixtureSpec, limit: int, trial_index: int) -> dict:
    fixture = spec.fixture
    total = len(fixture.previous_episodes)
    present, present_indices = _establishing_present(spec, limit)
    start, end = _included_indices(total, limit)
    # Position within the (possibly truncated) supplied window, 0 = oldest included.
    positions = [i - start for i in present_indices]

    request = ExtractionLabRequest(
        current_message=fixture.current_message,
        previous_episodes=fixture.previous_episodes,
        gold=fixture.gold,
        arms=[
            ExtractionLabArm(
                id=f"limit_{limit}",
                label=f"limit={limit}",
                tuning=ExtractionLabTuning(prompt_variant="baseline", context_episode_count=limit),
            )
        ],
    )
    payload = await run_extraction_lab(request)
    arm = payload.arms[0]
    record = {
        "fixture": spec.qid,
        "limit": limit,
        "trial": trial_index,
        "total_episodes_available": total,
        "window_size_actual": end - start,
        "establishing_present": present,
        "establishing_indices_in_window": present_indices,
        "establishing_positions_in_window": positions,
        "ok": arm.ok,
        "error": arm.error,
        "elapsed_ms": arm.elapsed_ms,
    }
    if arm.ok:
        record["mentions"] = [m.get("text") for m in arm.extraction.mentions]
        record["propositions"] = [p.get("fact") for p in arm.extraction.propositions]
        record["gold_scores"] = arm.gold_scores.model_dump(mode="json")
        record["proposition_success"] = arm.gold_scores.proposition_recall >= 1.0
        record["mention_precision"] = arm.gold_scores.mention_precision
    else:
        record["proposition_success"] = False
    return record


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10, help="Trials per (fixture, limit) cell")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print("Effective limit values per fixture (deduped/clamped to available episode count):")
    grid: list[tuple[SchemaLimitFixtureSpec, int, int]] = []
    for spec in _SPECS:
        limits = _effective_limits(spec)
        total = len(spec.fixture.previous_episodes)
        print(f"  {spec.qid}: requested {spec.limit_values} -> effective {limits} (total available: {total})")
        for limit in limits:
            for trial in range(args.trials):
                grid.append((spec, limit, trial))

    rng = random.Random(args.seed)
    rng.shuffle(grid)
    print(f"\nRunning {len(grid)} trials, shuffled order...")

    results: list[dict] = []
    for i, (spec, limit, trial) in enumerate(grid):
        rec = await _run_one_trial(spec, limit, trial)
        results.append(rec)
        status = "OK" if rec.get("proposition_success") else ("ERR" if not rec["ok"] else "miss")
        present_flag = "P" if rec["establishing_present"] else "-"
        print(
            f"  [{i+1}/{len(grid)}] {spec.qid} | limit={limit:3d} [{present_flag}] | "
            f"trial {trial} -> {status} ({rec['elapsed_ms']:.0f}ms)",
            flush=True,
        )

    out_path = MENHIR_ROOT / "results" / "extraction_lab_phase3_schema_limit.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nRaw trial results written to {out_path}")

    # Per-(fixture, limit) success frequency.
    print("\n=== Per-(fixture, limit) proposition-success frequency (95% Wilson CI) ===")
    by_fixture_limit: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for rec in results:
        by_fixture_limit[(rec["fixture"], rec["limit"])].append(rec)
    for spec in _SPECS:
        limits = _effective_limits(spec)
        print(f"\n{spec.qid}:")
        for limit in limits:
            recs = by_fixture_limit.get((spec.qid, limit), [])
            n = len(recs)
            s = sum(1 for r in recs if r["proposition_success"])
            present = recs[0]["establishing_present"] if recs else None
            lo, hi = _wilson_interval(s, n)
            print(
                f"  limit={limit:3d}  established_present={present!s:5s}  "
                f"{s}/{n} ({s/n:.0%} CI [{lo:.0%},{hi:.0%}])" if n else f"  limit={limit:3d}  NO DATA"
            )

    # THE primary analysis: P(success | present) vs P(success | absent), per fixture and aggregate.
    print("\n=== PRIMARY: P(success | establishing episode present) vs P(success | absent) ===")
    for spec in _SPECS:
        fixture_recs = [r for r in results if r["fixture"] == spec.qid]
        present_recs = [r for r in fixture_recs if r["establishing_present"]]
        absent_recs = [r for r in fixture_recs if not r["establishing_present"]]
        for label, recs in (("present", present_recs), ("absent", absent_recs)):
            n = len(recs)
            s = sum(1 for r in recs if r["proposition_success"])
            lo, hi = _wilson_interval(s, n)
            print(f"  {spec.qid} | {label:8s} n={n:3d}  {s}/{n} ({s/n:.0%} CI [{lo:.0%},{hi:.0%}])" if n else f"  {spec.qid} | {label:8s} NO DATA")

    all_present = [r for r in results if r["establishing_present"]]
    all_absent = [r for r in results if not r["establishing_present"]]
    for label, recs in (("present", all_present), ("absent", all_absent)):
        n = len(recs)
        s = sum(1 for r in recs if r["proposition_success"])
        lo, hi = _wilson_interval(s, n)
        print(f"  AGGREGATE | {label:8s} n={n:3d}  {s}/{n} ({s/n:.0%} CI [{lo:.0%},{hi:.0%}])" if n else f"  AGGREGATE | {label:8s} NO DATA")

    # Exploratory position effect (within "present" trials only).
    print("\n=== Exploratory: establishing-episode position within window (present trials only) ===")
    for spec in _SPECS:
        present_recs = [r for r in results if r["fixture"] == spec.qid and r["establishing_present"]]
        by_pos_bucket: dict[str, list[dict]] = defaultdict(list)
        for r in present_recs:
            window = r["window_size_actual"]
            positions = r["establishing_positions_in_window"]
            if not positions:
                continue
            min_pos = min(positions)
            frac = min_pos / max(1, window - 1)
            bucket = "near-start" if frac < 0.33 else ("mid" if frac < 0.66 else "near-end")
            by_pos_bucket[bucket].append(r)
        print(f"\n{spec.qid}:")
        for bucket in ("near-start", "mid", "near-end"):
            recs = by_pos_bucket.get(bucket, [])
            n = len(recs)
            s = sum(1 for r in recs if r["proposition_success"])
            print(f"  {bucket:12s} n={n:3d}  {s}/{n} ({s/n:.0%})" if n else f"  {bucket:12s} NO DATA")


if __name__ == "__main__":
    asyncio.run(main())
