"""Exact-context replay of Trial 10 dedup failure under 4 conditions.

Uses Graphiti's actual dedupe_nodes.nodes() prompt builder with the exact
context from the suburbs benchmark failure (Trial 10):
  - Extracted entity: "the suburbs" (Entity)
  - Existing candidate: "Chicago" (Entity, summary="Rachel lives in Chicago.")
  - Episode: "My friend Rachel actually just moved back to the suburbs again..."
  - Previous episodes: 15 benchmark turns

Runs 40 trials per condition in a 2×2 matrix:
  temp0×stock    temp0×patched
  temp1×stock    temp1×patched

If temp1×stock reproduces merges and temp1×patched doesn't, the prompt patch
has demonstrated value.

Usage:
    python scripts/smoke/replay_trial10_dedup.py
    DEDUP_TRIALS=60 python scripts/smoke/replay_trial10_dedup.py
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

# ── Graphiti imports for the actual prompt builder ──

from graphiti_core.prompts import dedupe_nodes
from graphiti_core.prompts.dedupe_nodes import NodeResolutions

# Import the Menhir anti-conflation patch
from menhir.infrastructure.graphiti_patches import _DEDUP_ANTI_CONFLATION_EXAMPLE


# ── Trial 10 exact context ──

# The current episode that triggered the dedup
EPISODE_CONTENT = (
    "user: My friend Rachel actually just moved back to the suburbs again,"
    " so I was thinking of somewhere not too far from a major city."
)

# All previous episodes as they'd appear in the pipeline.
# Episode 1 establishes Chicago, then 12 filler turns, then 2 pre-suburbs turns.
PREVIOUS_EPISODES = [
    {"content": "user: My friend Rachel lives in Chicago.",
     "timestamp": "2024-01-01T00:00:00"},
]
PREVIOUS_EPISODES.extend(
    {"content": f"user: Thanks for the information. Turn {i}.",
     "timestamp": f"2024-01-01T00:{i:02d}:00"}
    for i in range(1, 13)
)
PREVIOUS_EPISODES.append(
    {"content": "user: Miami Beach sounds fun, but I've been there before.",
     "timestamp": "2024-01-01T00:13:00"}
)
PREVIOUS_EPISODES.append(
    {"content": "user: I'm thinking of somewhere more relaxed.",
     "timestamp": "2024-01-01T00:14:00"}
)

# Only "the suburbs" goes to LLM dedup — Rachel resolves deterministically.
# The pipeline builds extracted_nodes with 0-based IDs for unresolved nodes.
EXTRACTED_NODES = [
    {
        "id": 0,
        "name": "the suburbs",
        "entity_type": ["Entity"],
        "entity_type_description": "Default Entity Type",
    },
]

# Cosine search returns Chicago as the only candidate above the 0.6 threshold.
# Summary is truncated to 120 chars as the pipeline does.
EXISTING_NODES = [
    {
        "candidate_id": 0,
        "name": "Chicago",
        "entity_types": ["Entity"],
        "summary": "Rachel lives in Chicago.",
    },
]

# ── Build prompts using Graphiti's actual builder ──

CONTEXT = {
    "extracted_nodes": EXTRACTED_NODES,
    "existing_nodes": EXISTING_NODES,
    "episode_content": EPISODE_CONTENT,
    "previous_episodes": PREVIOUS_EPISODES,
}


def _messages_to_dicts(messages) -> list[dict[str, str]]:
    """Convert Graphiti Message objects to OpenAI-compatible dicts."""
    return [{"role": m.role, "content": m.content} for m in messages]


def build_stock_messages() -> list[dict[str, str]]:
    """Use Graphiti's actual dedupe_nodes.nodes() prompt builder."""
    return _messages_to_dicts(dedupe_nodes.nodes(CONTEXT))


def build_patched_messages() -> list[dict[str, str]]:
    """Stock prompt + the Menhir anti-conflation example injected at </EXAMPLE>."""
    msgs = build_stock_messages()
    patched = False
    for msg in msgs:
        if msg["role"] == "user" and "</EXAMPLE>" in msg["content"]:
            msg["content"] = msg["content"].replace(
                "</EXAMPLE>",
                _DEDUP_ANTI_CONFLATION_EXAMPLE + "</EXAMPLE>",
            )
            patched = True
    if not patched:
        print("WARNING: could not inject anti-conflation example", file=sys.stderr)
    return msgs


# ── Response schema (matches NodeResolutions) ──

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "NodeResolutions",
        "schema": {
            "type": "object",
            "properties": {
                "entity_resolutions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string"},
                            "duplicate_candidate_id": {"type": "integer"},
                        },
                        "required": ["id", "name", "duplicate_candidate_id"],
                    },
                }
            },
            "required": ["entity_resolutions"],
        },
    },
}


# ── Run one trial ──

@dataclass
class TrialResult:
    condition: str
    trial: int
    suburb_merged: bool
    duplicate_candidate_id: int
    resolved_name: str


async def run_one(
    client: openai.AsyncOpenAI,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    condition: str,
    trial: int,
) -> TrialResult:
    """Send the dedup prompt and parse the response."""
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=256,
        response_format=RESPONSE_SCHEMA,
    )
    raw = response.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    resolutions = parsed.get("entity_resolutions", [])

    # Find the resolution for "the suburbs" (id=0)
    dup_id = -1
    name = "?"
    for r in resolutions:
        if r.get("id") == 0:
            dup_id = r.get("duplicate_candidate_id", -1)
            name = r.get("name", "?")
            break

    return TrialResult(
        condition=condition,
        trial=trial,
        suburb_merged=dup_id >= 0,
        duplicate_candidate_id=dup_id,
        resolved_name=name,
    )


# ── Condition matrix ──

@dataclass
class ConditionResult:
    condition: str
    temperature: float
    prompt_variant: str
    trials: list[TrialResult] = field(default_factory=list)

    @property
    def merge_count(self) -> int:
        return sum(1 for t in self.trials if t.suburb_merged)

    @property
    def merge_rate(self) -> float:
        return self.merge_count / len(self.trials) if self.trials else 0.0


async def run_condition(
    client: openai.AsyncOpenAI,
    model: str,
    temperature: float,
    prompt_variant: str,
    num_trials: int,
    concurrency: int = 5,
) -> ConditionResult:
    """Run all trials for one condition with bounded concurrency."""
    condition_name = f"temp{temperature}×{prompt_variant}"
    messages_fn = build_patched_messages if prompt_variant == "patched" else build_stock_messages

    result = ConditionResult(
        condition=condition_name,
        temperature=temperature,
        prompt_variant=prompt_variant,
    )

    sem = asyncio.Semaphore(concurrency)

    async def _bounded(trial_num: int) -> TrialResult:
        async with sem:
            # Build fresh messages each trial (prompt patch mutates in place)
            msgs = messages_fn()
            return await run_one(client, model, msgs, temperature, condition_name, trial_num)

    tasks = [_bounded(i) for i in range(num_trials)]
    trial_results = await asyncio.gather(*tasks, return_exceptions=True)

    for tr in trial_results:
        if isinstance(tr, Exception):
            print(f"  {condition_name}: trial error: {tr}", file=sys.stderr)
        else:
            result.trials.append(tr)

    return result


async def main() -> None:
    model = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    num_trials = int(os.getenv("DEDUP_TRIALS", "40"))

    print(f"Model: {model}")
    print(f"Trials per condition: {num_trials}")
    print(f"Conditions: temp0×stock, temp0×patched, temp1×stock, temp1×patched")
    print()

    # Verify prompt builds correctly
    stock_msgs = build_stock_messages()
    patched_msgs = build_patched_messages()
    print(f"Stock prompt length: {sum(len(m['content']) for m in stock_msgs)} chars")
    print(f"Patched prompt length: {sum(len(m['content']) for m in patched_msgs)} chars")
    print()

    client = openai.AsyncOpenAI()

    conditions = [
        (0.0, "stock"),
        (0.0, "patched"),
        (1.0, "stock"),
        (1.0, "patched"),
    ]

    results: list[ConditionResult] = []
    for temp, variant in conditions:
        cond_name = f"temp{temp}×{variant}"
        print(f"Running {cond_name} ({num_trials} trials)...")
        cr = await run_condition(client, model, temp, variant, num_trials)
        results.append(cr)

        # Print per-trial results
        for t in cr.trials:
            tag = "MERGED" if t.suburb_merged else "ok"
            print(f"  [{tag}] trial {t.trial}: dup_id={t.duplicate_candidate_id} name={t.resolved_name!r}")

        print(f"  => {cr.merge_count}/{len(cr.trials)} merges ({cr.merge_rate:.1%})")
        print()

    # ── Summary matrix ──
    print("=" * 64)
    print("RESULTS MATRIX")
    print("=" * 64)
    print(f"{'Condition':<25} {'Merges':<12} {'Rate':<10} {'Verdict'}")
    print("-" * 64)
    for cr in results:
        n = len(cr.trials)
        verdict = "CLEAN" if cr.merge_count == 0 else "HAS MERGES"
        print(f"{cr.condition:<25} {cr.merge_count}/{n:<10} {cr.merge_rate:<10.1%} {verdict}")

    print()

    # Interpret
    stock_t1 = next((r for r in results if r.temperature == 1.0 and r.prompt_variant == "stock"), None)
    patched_t1 = next((r for r in results if r.temperature == 1.0 and r.prompt_variant == "patched"), None)
    stock_t0 = next((r for r in results if r.temperature == 0.0 and r.prompt_variant == "stock"), None)
    patched_t0 = next((r for r in results if r.temperature == 0.0 and r.prompt_variant == "patched"), None)

    if stock_t1 and patched_t1:
        if stock_t1.merge_count > 0 and patched_t1.merge_count == 0:
            print(">> PROMPT PATCH VALIDATED: temp1×stock has merges, temp1×patched has none.")
        elif stock_t1.merge_count > 0 and patched_t1.merge_count > 0:
            if patched_t1.merge_rate < stock_t1.merge_rate:
                print(f">> PROMPT PATCH PARTIAL: reduces merge rate from {stock_t1.merge_rate:.1%} to {patched_t1.merge_rate:.1%}")
            else:
                print(">> PROMPT PATCH NO EFFECT: both variants merge at similar rates")
        elif stock_t1.merge_count == 0:
            print(">> INCONCLUSIVE: temp1×stock didn't reproduce any merges (need more trials?)")

    if stock_t0 and stock_t0.merge_count == 0:
        print(">> TEMPERATURE PIN VALIDATED: temp0×stock has zero merges")
    elif stock_t0 and stock_t0.merge_count > 0:
        print(f">> TEMPERATURE PIN INSUFFICIENT: temp0×stock still has {stock_t0.merge_count} merges")

    # Dump results
    output_path = MENHIR_ROOT / "results" / "trial10_replay_matrix.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dump = {
        "model": model,
        "trials_per_condition": num_trials,
        "conditions": [
            {
                "condition": cr.condition,
                "temperature": cr.temperature,
                "prompt_variant": cr.prompt_variant,
                "total_trials": len(cr.trials),
                "merge_count": cr.merge_count,
                "merge_rate": cr.merge_rate,
                "trials": [
                    {
                        "trial": t.trial,
                        "suburb_merged": t.suburb_merged,
                        "duplicate_candidate_id": t.duplicate_candidate_id,
                        "resolved_name": t.resolved_name,
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
