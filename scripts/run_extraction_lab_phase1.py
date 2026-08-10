"""Phase 1 runner: the 8-condition prompt ablation from
.agent/plans/menhir-extraction-prompt-recency-recall-research.md, executed via the
extraction_lab harness built for Phase 0.

Usage:
    .venv/Scripts/python.exe scripts/run_extraction_lab_phase1.py [--smoke] [--fixture NAME]

--smoke runs only the first (cheapest) fixture across all 8 arms, for a quick
end-to-end sanity check before committing to the full 10-fixture x 8-arm matrix.
--fixture NAME restricts to one fixture by its module-level variable name.

Requires: GRAPHITI_LLM_PROVIDER=openai + a real OPENAI_API_KEY (set by this script from
menhir's own .env, matching exactly how archolith-bench/scripts/longmemeval/build_graph.sh
routes LME extraction -- see that script's `export OPENAI_API_KEY=... OPENAI_CHAT_MODEL=...`
block), and a reachable Neo4j (defaults to the LME graph on bolt://localhost:7689, which is
otherwise untouched -- this harness never writes, and uses group_id="extraction-lab", a
namespace that does not exist in that graph, so dedup resolution always finds zero
candidates -- see extraction_lab.py's fidelity-contract docstring).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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
    """Mirror archolith-bench/scripts/longmemeval/build_graph.sh's provider routing
    exactly, so this experiment is comparable to the confirmed RCA A/B test and the
    production LME harness default (gpt-4o-mini), not whatever menhir's own local
    default model happens to be."""
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
from menhir.explorer.extraction_lab_fixtures import ALL_FIXTURES  # noqa: E402

VARIANTS = [
    ("baseline", "Baseline (production prompt, unmodified)"),
    ("minus_when_in_doubt", "Condition 2: minus 'When in doubt, do NOT extract'"),
    ("minimal_recall_patch", "Condition 3: Minimal Recall Patch"),
    ("mention_first", "Condition 4: Mention-First"),
    ("update_aware", "Condition 5: Update-Aware"),
    ("proposition_first", "Condition 6: Proposition-First"),
    ("mention_first_update_aware", "Condition 7: Mention-First + Update-Aware"),
    ("proposition_first_structured", "Condition 8: Proposition-First + Structured Uncertainty"),
]


def _build_arm_request(
    fixture: ExtractionLabRequest,
    variants: list[tuple[str, str]],
    *,
    candidate_lookup: bool = False,
) -> ExtractionLabRequest:
    arms = [
        ExtractionLabArm(
            id=variant,
            label=label,
            tuning=ExtractionLabTuning(prompt_variant=variant, enable_candidate_lookup=candidate_lookup),
        )
        for variant, label in variants
    ]
    return ExtractionLabRequest(
        current_message=fixture.current_message,
        previous_episodes=fixture.previous_episodes,
        gold=fixture.gold,
        arms=arms,
        # Bug fix: this was previously dropped, silently making Phase 2's candidate
        # lookup impossible to exercise even for fixtures that define a real namespace
        # (the 3 RCA fixtures) -- found while wiring the --candidate-lookup flag.
        source_namespace=fixture.source_namespace,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Run only the first fixture")
    parser.add_argument("--fixture", type=str, default=None, help="Restrict to one fixture index")
    parser.add_argument(
        "--variants", type=str, default=None,
        help="Comma-separated variant ids to run (default: all 8). e.g. baseline,update_aware",
    )
    parser.add_argument(
        "--expanded", action="store_true",
        help="Include the 20 additional synthetic fixtures (extraction_lab_fixtures_expanded), "
        "for 30 total fixtures instead of the original 10.",
    )
    parser.add_argument(
        "--candidate-lookup", action="store_true",
        help="Phase 2: enable the extraction-time candidate lookup on every selected arm "
        "(composes with prompt_variant). Only has an effect on fixtures with a real "
        "source_namespace (the 3 RCA fixtures) -- synthetic fixtures have no backing "
        "graph to look up against, so the lookup is a documented no-op for them.",
    )
    args = parser.parse_args()

    variants = VARIANTS
    if args.variants:
        wanted = set(args.variants.split(","))
        variants = [(vid, label) for vid, label in VARIANTS if vid in wanted]
        missing = wanted - {vid for vid, _ in variants}
        if missing:
            raise SystemExit(f"Unknown variant id(s): {missing}")

    fixtures = list(ALL_FIXTURES)
    if args.expanded:
        from menhir.explorer.extraction_lab_fixtures_expanded import EXPANDED_FIXTURES
        fixtures = fixtures + list(EXPANDED_FIXTURES)

    if args.smoke:
        fixtures = fixtures[:1]
    elif args.fixture is not None:
        idx = int(args.fixture)
        fixtures = [fixtures[idx]]

    all_results: list[dict] = []
    for i, fixture in enumerate(fixtures):
        label = fixture.current_message[:60].replace("\n", " ")
        print(f"\n=== Fixture {i}: {label!r} ===", flush=True)
        request = _build_arm_request(fixture, variants, candidate_lookup=args.candidate_lookup)
        payload = await run_extraction_lab(request)
        result = payload.model_dump(mode="json")
        all_results.append({"fixture_index": i, "current_message": fixture.current_message, "result": result})

        for arm in result["arms"]:
            if not arm["ok"]:
                print(f"  [{arm['id']:32s}] ERROR: {arm['error']}")
                continue
            gs = arm["gold_scores"]
            known = arm.get("known_entities_used") or []
            known_suffix = f"  known={known}" if known else ""
            print(
                f"  [{arm['id']:32s}] mention R/P={gs['mention_recall']:.2f}/{gs['mention_precision']:.2f}"
                f"  prop R/P={gs['proposition_recall']:.2f}/{gs['proposition_precision']:.2f}"
                f"  update={gs['update_capture_rate']:.2f}"
                f"  unsupported={gs['unsupported_inference_rate']:.2f}"
                f"  ({arm['elapsed_ms']:.0f}ms){known_suffix}"
            )

    variant_tag = "-".join(vid for vid, _ in variants) if args.variants else "all8"
    fixture_tag = "30fixtures" if args.expanded else "10fixtures"
    lookup_tag = "_lookup" if args.candidate_lookup else ""
    out_path = MENHIR_ROOT / "results" / f"extraction_lab_phase1_{variant_tag}_{fixture_tag}{lookup_tag}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"\nFull results written to {out_path}")

    # Aggregate mean scores per variant across all fixtures run.
    print("\n=== Aggregate (mean across fixtures run) ===")
    totals: dict[str, dict[str, float]] = {}
    counts: dict[str, int] = {}
    for entry in all_results:
        for arm in entry["result"]["arms"]:
            if not arm["ok"]:
                continue
            vid = arm["id"]
            counts[vid] = counts.get(vid, 0) + 1
            gs = arm["gold_scores"]
            bucket = totals.setdefault(vid, {k: 0.0 for k in gs})
            for k, v in gs.items():
                bucket[k] += v
    for vid, _label in variants:
        if vid not in totals:
            continue
        n = counts[vid]
        means = {k: v / n for k, v in totals[vid].items()}
        print(
            f"  {vid:32s} n={n:2d}  mention R/P={means['mention_recall']:.2f}/{means['mention_precision']:.2f}"
            f"  prop R/P={means['proposition_recall']:.2f}/{means['proposition_precision']:.2f}"
            f"  update={means['update_capture_rate']:.2f}"
            f"  unsupported={means['unsupported_inference_rate']:.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
