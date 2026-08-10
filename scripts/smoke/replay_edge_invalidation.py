"""Replay the exact edge-invalidation prompt from FAIL_C trials against multiple models.

The suburbs trace shows that gpt-4.1-nano sees the Chicago edge as an invalidation
candidate but returns contradicted_facts=[] — it fails to recognize that "Rachel moved
back to the suburbs" supersedes "Rachel lives in Chicago."

This script replays the exact Graphiti dedupe_edges.resolve_edge prompt with the
captured context and tests nano vs mini at temperature=0.

Usage:
    python scripts/smoke/replay_edge_invalidation.py
    DEDUP_TRIALS=60 python scripts/smoke/replay_edge_invalidation.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

MENHIR_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(MENHIR_ROOT / "src"))
load_dotenv(MENHIR_ROOT / ".env")

try:
    import openai
except ImportError:
    print("openai package required", file=sys.stderr)
    sys.exit(1)

from graphiti_core.prompts import dedupe_edges
from graphiti_core.prompts.dedupe_edges import EdgeDuplicate


# ── Exact context from Trial 7/8 FAIL_C_LLM_MISSED_CONTRADICTION ──

# The new suburbs edge fact (varies slightly between trials)
NEW_FACTS = [
    "Rachel moved back to the suburbs",
    "Rachel just moved back to the suburbs again.",
    "Rachel moved back to the suburbs again.",
]

# No related edges (same-endpoint) — 0 EXISTING FACTS
RELATED_EDGES: list[dict] = []

# One invalidation candidate: the Chicago edge
INVALIDATION_CANDIDATES = [
    {"idx": 0, "fact": "Rachel lives in Chicago."},
]

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "EdgeDuplicate",
        "schema": {
            "type": "object",
            "properties": {
                "duplicate_facts": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "contradicted_facts": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["duplicate_facts", "contradicted_facts"],
        },
    },
}


def _messages_to_dicts(messages) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def build_prompt(new_fact: str) -> list[dict[str, str]]:
    """Build the exact Graphiti resolve_edge prompt."""
    context = {
        "existing_edges": RELATED_EDGES,
        "new_edge": new_fact,
        "edge_invalidation_candidates": INVALIDATION_CANDIDATES,
    }
    return _messages_to_dicts(dedupe_edges.resolve_edge(context))


@dataclass
class TrialResult:
    model: str
    new_fact: str
    trial: int
    contradicted: list[int]
    duplicated: list[int]

    @property
    def detected(self) -> bool:
        return 0 in self.contradicted


@dataclass
class ConditionResult:
    model: str
    trials: list[TrialResult] = field(default_factory=list)

    @property
    def detection_count(self) -> int:
        return sum(1 for t in self.trials if t.detected)

    @property
    def detection_rate(self) -> float:
        return self.detection_count / len(self.trials) if self.trials else 0.0


async def run_one(
    client: openai.AsyncOpenAI,
    model: str,
    new_fact: str,
    trial: int,
) -> TrialResult:
    messages = build_prompt(new_fact)
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        max_tokens=128,
        response_format=RESPONSE_SCHEMA,
    )
    raw = response.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    return TrialResult(
        model=model,
        new_fact=new_fact,
        trial=trial,
        contradicted=parsed.get("contradicted_facts", []),
        duplicated=parsed.get("duplicate_facts", []),
    )


async def run_model(
    client: openai.AsyncOpenAI,
    model: str,
    num_trials: int,
    concurrency: int = 5,
) -> ConditionResult:
    result = ConditionResult(model=model)
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(trial: int) -> TrialResult:
        async with sem:
            # Rotate through fact variants
            fact = NEW_FACTS[trial % len(NEW_FACTS)]
            return await run_one(client, model, fact, trial)

    tasks = [_bounded(i) for i in range(num_trials)]
    trial_results = await asyncio.gather(*tasks, return_exceptions=True)

    for tr in trial_results:
        if isinstance(tr, Exception):
            print(f"  {model}: trial error: {tr}", file=sys.stderr)
        else:
            result.trials.append(tr)

    return result


async def main() -> None:
    num_trials = int(os.getenv("DEDUP_TRIALS", "40"))
    models = os.getenv("MODELS", "gpt-4.1-nano,gpt-4o-mini").split(",")

    print(f"Trials per model: {num_trials}")
    print(f"Models: {models}")
    print(f"Temperature: 0")
    print(f"New fact variants: {NEW_FACTS}")
    print()

    # Show prompt for reference
    sample_prompt = build_prompt(NEW_FACTS[0])
    print("=== PROMPT (user message) ===")
    print(sample_prompt[1]["content"][:600])
    print("...")
    print()

    client = openai.AsyncOpenAI()
    results: list[ConditionResult] = []

    for model in models:
        print(f"Running {model} ({num_trials} trials)...")
        cr = await run_model(client, model, num_trials)
        results.append(cr)

        for t in cr.trials:
            tag = "DETECTED" if t.detected else "MISSED"
            print(f"  [{tag}] trial {t.trial}: contradicted={t.contradicted} fact={t.new_fact[:50]}")

        print(f"  => {cr.detection_count}/{len(cr.trials)} detected ({cr.detection_rate:.1%})")
        print()

    # Summary
    print("=" * 64)
    print("EDGE INVALIDATION DETECTION MATRIX")
    print("=" * 64)
    print(f"{'Model':<25} {'Detected':<12} {'Rate':<10}")
    print("-" * 64)
    for cr in results:
        n = len(cr.trials)
        print(f"{cr.model:<25} {cr.detection_count}/{n:<10} {cr.detection_rate:<10.1%}")

    # Dump results
    output_path = MENHIR_ROOT / "results" / "edge_invalidation_model_comparison.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dump = {
        "trials_per_model": num_trials,
        "temperature": 0,
        "new_facts": NEW_FACTS,
        "invalidation_candidate": INVALIDATION_CANDIDATES[0]["fact"],
        "models": [
            {
                "model": cr.model,
                "detection_count": cr.detection_count,
                "detection_rate": cr.detection_rate,
                "trials": [
                    {
                        "trial": t.trial,
                        "new_fact": t.new_fact,
                        "detected": t.detected,
                        "contradicted": t.contradicted,
                        "duplicated": t.duplicated,
                    }
                    for t in cr.trials
                ],
            }
            for cr in results
        ],
    }
    output_path.write_text(json.dumps(dump, indent=2), encoding="utf-8")
    print(f"\nFull results: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
