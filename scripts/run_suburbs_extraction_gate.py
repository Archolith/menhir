"""Repeated read-only gate for the current-message value extraction candidate.

This runner never calls Graphiti ``add_episode`` and therefore never writes to the
canonical LME namespace. It exercises the same node/edge extraction functions through
the Extraction Lab and retains every raw arm result for review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

MENHIR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MENHIR_ROOT / "src"))

load_dotenv(MENHIR_ROOT / ".env")

from menhir.explorer.extraction_lab import (  # noqa: E402
    ExtractionLabArm,
    ExtractionLabRequest,
    ExtractionLabTuning,
    run_extraction_lab,
)
from menhir.explorer.extraction_lab_fixtures import (  # noqa: E402
    FIXTURE_DIRECT_NAMED_UPDATE,
    FIXTURE_GENERIC_NON_FACT,
    FIXTURE_INFORMAL_LOCATION,
    FIXTURE_REVERSAL,
    FIXTURE_UNSUPPORTED_IMPLICATION,
)

_FIXTURES = {
    "suburbs": FIXTURE_DIRECT_NAMED_UPDATE,
    "downtown": FIXTURE_INFORMAL_LOCATION,
    "generic_non_fact": FIXTURE_GENERIC_NON_FACT,
    "unsupported_move": FIXTURE_UNSUPPORTED_IMPLICATION,
    "named_places": FIXTURE_REVERSAL,
}


def _configure_runtime() -> None:
    password = os.getenv("MENHIR_LME_NEO4J_PASSWORD")
    if not password:
        raise RuntimeError("MENHIR_LME_NEO4J_PASSWORD is required")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required (load it from Menhir's .env)")

    os.environ["NEO4J_URI"] = os.getenv("MENHIR_LME_NEO4J_URI", "bolt://localhost:7689")
    os.environ["NEO4J_PASSWORD"] = password
    os.environ["GRAPHITI_LLM_PROVIDER"] = "openai"
    os.environ["MEMORY_GRAPHITI_PROVIDER"] = "openai"
    os.environ["LLM_CHAT_PROVIDER"] = "openai"
    os.environ["MEMORY_CHAT_PROVIDER"] = "openai"
    os.environ["OPENAI_CHAT_MODEL"] = os.getenv("MENHIR_EXTRACTION_GATE_MODEL", "gpt-4o-mini")
    os.environ.setdefault("OPENAI_EMBED_MODEL", "text-embedding-3-small")


def _request_for_trial(name: str, trial: int) -> ExtractionLabRequest:
    fixture = _FIXTURES[name]
    variants = ["baseline", "combined_extraction"]
    if trial % 2:
        variants.reverse()
    return ExtractionLabRequest(
        current_message=fixture.current_message,
        previous_episodes=fixture.previous_episodes,
        gold=fixture.gold,
        source_namespace=fixture.source_namespace,
        arms=[
            ExtractionLabArm(
                id=variant,
                label=variant,
                tuning=ExtractionLabTuning(
                    prompt_variant=variant,
                    temperature=0.0,
                    context_episode_count=10,
                ),
            )
            for variant in variants
        ],
    )


def _arm_success(fixture: str, arm: dict[str, Any]) -> bool:
    extraction = arm.get("extraction") or {}
    mentions = {
        str(item.get("text") or "").lower()
        for item in extraction.get("mentions") or []
    }
    facts = [str(item.get("fact") or "").lower() for item in extraction.get("propositions") or []]

    if fixture == "suburbs":
        has_suburbs_value = any(mention in {"suburbs", "the suburbs"} for mention in mentions)
        return "rachel" in mentions and has_suburbs_value and any(
            "rachel" in fact and "suburb" in fact for fact in facts
        )
    if fixture == "downtown":
        return "david" in mentions and "downtown" in mentions and any(
            "david" in fact and "downtown" in fact for fact in facts
        )
    if fixture == "generic_non_fact":
        return not facts
    if fixture == "unsupported_move":
        return not any(
            token in fact for fact in facts for token in ("move", "resid", "suburb", "downtown")
        )
    if fixture == "named_places":
        return {"maya", "google", "microsoft"}.issubset(mentions) and any(
            "microsoft" in fact and "maya" in fact for fact in facts
        )
    raise AssertionError(f"Unknown fixture: {fixture}")


async def _run(trials: int) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for trial in range(trials):
        for fixture_name in _FIXTURES:
            payload = await run_extraction_lab(_request_for_trial(fixture_name, trial))
            dumped = payload.model_dump(mode="json")
            for arm in dumped["arms"]:
                if not arm["ok"]:
                    raise RuntimeError(
                        f"Extraction arm failed fixture={fixture_name} trial={trial + 1} "
                        f"arm={arm['id']}: {arm['error']}"
                    )
                success = _arm_success(fixture_name, arm)
                counts[fixture_name][arm["id"]] += int(success)
                records.append(
                    {
                        "fixture": fixture_name,
                        "trial": trial + 1,
                        "arm": arm,
                        "gate_success": success,
                    }
                )
            print(
                f"completed fixture={fixture_name} trial={trial + 1}/{trials}",
                flush=True,
            )

    summary = {
        fixture: {
            arm: {"successes": successes, "trials": trials}
            for arm, successes in arm_counts.items()
        }
        for fixture, arm_counts in counts.items()
    }
    candidate_target = summary["suburbs"]["combined_extraction"]["successes"]
    baseline_target = summary["suburbs"]["baseline"]["successes"]
    recall_floor = math.ceil(trials * 0.8)
    improvement_floor = math.ceil(trials * 0.3)
    passed = (
        candidate_target >= recall_floor
        and candidate_target >= baseline_target + improvement_floor
        and summary["downtown"]["combined_extraction"]["successes"] >= recall_floor
        and summary["generic_non_fact"]["combined_extraction"]["successes"] == trials
        and summary["unsupported_move"]["combined_extraction"]["successes"] == trials
        and summary["named_places"]["combined_extraction"]["successes"]
        >= summary["named_places"]["baseline"]["successes"]
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": os.environ["OPENAI_CHAT_MODEL"],
        "temperature": 0.0,
        "trials": trials,
        "summary": summary,
        "passed": passed,
        "records": records,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument(
        "--output",
        type=Path,
        default=MENHIR_ROOT / "results" / "suburbs_extraction_gate.json",
    )
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")

    _configure_runtime()
    result = await _run(args.trials)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"summary": result["summary"], "passed": result["passed"]}, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
