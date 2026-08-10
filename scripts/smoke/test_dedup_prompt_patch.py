"""Quick bench test for the dedup anti-conflation prompt patch.

Simulates the exact context the dedup LLM sees when "suburbs" is compared
against an existing "Chicago" entity, runs it through gpt-4o-mini with both
the stock and patched prompts, and reports whether the merge is rejected.

Usage:
    python scripts/smoke/test_dedup_prompt_patch.py

Requires OPENAI_API_KEY in env or .env.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

MENHIR_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(MENHIR_ROOT / ".env")

# Ensure openai is available
try:
    import openai
except ImportError:
    print("openai package required", file=sys.stderr)
    sys.exit(1)


# ---- Reproduce the exact dedup prompt context ----

EPISODE_CONTENT = (
    "user: My friend Rachel actually just moved back to the suburbs again,"
    " so I was thinking of somewhere not too far from a major city."
)

# Full previous episodes as the real pipeline sees them: turn 1 + 12 filler turns
PREVIOUS_EPISODES = [
    {"content": "user: My friend Rachel lives in Chicago.", "timestamp": "2024-01-01T00:00:00"},
] + [
    {"content": f"user: Thanks for the information. Turn {i}.", "timestamp": f"2024-01-01T00:{i:02d}:00"}
    for i in range(12)
]

# Minimal context: just suburbs vs Chicago
EXTRACTED_NODES_MINIMAL = [
    {
        "id": 0,
        "name": "the suburbs",
        "entity_type": ["Entity", "Location"],
        "entity_type_description": "Default Entity Type",
    },
]

# Match the exact graph state: cosine search only returns Chicago as a candidate
# (Rachel resolves deterministically via exact name match)
EXISTING_NODES_MINIMAL = [
    {
        "candidate_id": 0,
        "name": "Chicago",
        "entity_types": ["Entity"],
        "summary": "Rachel lives in Chicago.",
    },
]

# Realistic: Rachel resolves deterministically, so only suburbs goes to LLM.
# But "a major city" might also be unresolved. Include both.
EXTRACTED_NODES_REALISTIC = [
    {
        "id": 0,
        "name": "the suburbs",
        "entity_type": ["Entity"],
        "entity_type_description": "Default Entity Type",
    },
    {
        "id": 1,
        "name": "a major city",
        "entity_type": ["Entity"],
        "entity_type_description": "Default Entity Type",
    },
]

# Only Chicago comes back from cosine search (Rachel resolves deterministically)
EXISTING_NODES_REALISTIC = [
    {
        "candidate_id": 0,
        "name": "Chicago",
        "entity_types": ["Entity"],
        "summary": "Rachel lives in Chicago.",
    },
]


# ---- Stock Graphiti dedup prompt (nodes variant) ----

def build_stock_prompt(
    extracted_nodes: list[dict],
    existing_nodes: list[dict],
) -> list[dict[str, str]]:
    """Reproduce the stock dedupe_nodes.nodes() prompt."""
    n = len(extracted_nodes)
    return [
        {
            "role": "system",
            "content": (
                "You are an entity deduplication assistant. "
                "NEVER fabricate entity names or mark distinct entities as duplicates."
            ),
        },
        {
            "role": "user",
            "content": f"""
<PREVIOUS MESSAGES>
{json.dumps(PREVIOUS_EPISODES)}
</PREVIOUS MESSAGES>

<CURRENT MESSAGE>
{EPISODE_CONTENT}
</CURRENT MESSAGE>

<ENTITIES>
{json.dumps(extracted_nodes)}
</ENTITIES>

<EXISTING ENTITIES>
{json.dumps(existing_nodes)}
</EXISTING ENTITIES>

Each of the above ENTITIES was extracted from the CURRENT MESSAGE.
For each entity, determine if it is a duplicate of any EXISTING ENTITY.
Entities should only be considered duplicates if they refer to the *same real-world object or concept*.

NEVER mark entities as duplicates if:
- They are related but distinct.
- They have similar names or purposes but refer to separate instances or concepts.

Task:
ENTITIES contains {n} entities with IDs 0 through {n - 1}.
Your response MUST include EXACTLY {n} resolutions with IDs 0 through {n - 1}. Do not skip or add IDs.

For every entity, provide:
- `id`: integer id from ENTITIES
- `name`: the best full name for the entity (preserve the original name unless a duplicate has a more complete name)
- `duplicate_candidate_id`: the `candidate_id` of the EXISTING ENTITY that is the best duplicate match, or -1 if there is no duplicate

<EXAMPLE>
ENTITY: "Sam" (Person)
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Sam", "entity_types": ["Person"], "summary": "Sam enjoys hiking and photography"}}]
Result: duplicate_candidate_id = 0 (same person referenced in conversation)

ENTITY: "NYC"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "New York City", "entity_types": ["Location"]}}, {{"candidate_id": 1, "name": "New York Knicks", "entity_types": ["Organization"]}}]
Result: duplicate_candidate_id = 0 (same location, abbreviated name)

ENTITY: "Java" (programming language)
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Java", "entity_types": ["Location"], "summary": "An island in Indonesia"}}]
Result: duplicate_candidate_id = -1 (same name but distinct real-world things)

ENTITY: "Marco's car"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Marco's vehicle", "entity_types": ["Entity"], "summary": "Marco drives a red sedan."}}]
Result: duplicate_candidate_id = 0 (synonym — "car" and "vehicle" refer to the same thing, same possessor)
</EXAMPLE>
""",
        },
    ]


ANTI_CONFLATION = """
ENTITY: "the suburbs"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Chicago", "entity_types": ["Location"], "summary": "A city where someone lives"}}]
Result: duplicate_candidate_id = -1 (a relative or descriptive location like "the suburbs", "downtown", "the countryside" is NEVER the same real-world object as a specific named city, even if a person moved from one to the other or they are geographically related)
"""


def build_patched_prompt(
    extracted_nodes: list[dict],
    existing_nodes: list[dict],
) -> list[dict[str, str]]:
    """Stock prompt + the anti-conflation example we want to test."""
    msgs = build_stock_prompt(extracted_nodes, existing_nodes)
    for msg in msgs:
        if msg["role"] == "user" and "</EXAMPLE>" in msg["content"]:
            msg["content"] = msg["content"].replace(
                "</EXAMPLE>",
                ANTI_CONFLATION + "</EXAMPLE>",
            )
    return msgs


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


async def run_dedup(label: str, messages: list[dict[str, str]], model: str, temperature: float = 0) -> dict:
    """Call the dedup prompt and return the parsed response."""
    client = openai.AsyncOpenAI()
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
    return {"label": label, "raw": raw, "resolutions": resolutions}


def find_suburbs_merge(resolutions: list[dict]) -> bool:
    """Return True if any resolution merges 'suburbs' into an existing entity."""
    for r in resolutions:
        name = r.get("name", "").lower()
        if "suburb" in name and r.get("duplicate_candidate_id", -1) >= 0:
            return True
    return False


def describe_resolutions(resolutions: list[dict], existing: list[dict]) -> str:
    """One-line summary of each resolution."""
    parts = []
    existing_by_id = {e["candidate_id"]: e["name"] for e in existing}
    for r in resolutions:
        dup_id = r.get("duplicate_candidate_id", -1)
        name = r.get("name", "?")
        if dup_id >= 0:
            target = existing_by_id.get(dup_id, f"?id={dup_id}")
            parts.append(f"{name!r}->MERGE({target})")
        else:
            parts.append(f"{name!r}->NEW")
    return ", ".join(parts)


async def run_scenario(
    label: str,
    extracted_nodes: list[dict],
    existing_nodes: list[dict],
    model: str,
    trials: int,
    temperature: float = 0,
) -> None:
    """Run stock vs patched for one scenario and print results."""
    print(f"\n{'='*60}")
    print(f"Scenario: {label}")
    print(f"  Extracted: {[n['name'] for n in extracted_nodes]}")
    print(f"  Existing:  {[n['name'] for n in existing_nodes]}")
    print(f"{'='*60}")

    stock_merges = 0
    patched_merges = 0

    for i in range(trials):
        stock_result = await run_dedup(
            f"stock-{i}",
            build_stock_prompt(extracted_nodes, existing_nodes),
            model,
            temperature=temperature,
        )
        patched_result = await run_dedup(
            f"patched-{i}",
            build_patched_prompt(extracted_nodes, existing_nodes),
            model,
            temperature=temperature,
        )

        for result in [stock_result, patched_result]:
            merged = find_suburbs_merge(result["resolutions"])
            if "stock" in result["label"]:
                stock_merges += int(merged)
            else:
                patched_merges += int(merged)
            desc = describe_resolutions(result["resolutions"], existing_nodes)
            tag = "SUBURB-MERGED" if merged else "ok"
            print(f"  {result['label']}: [{tag}] {desc}")

    print(f"\n  Stock:   {stock_merges}/{trials} suburb-merges (want 0)")
    print(f"  Patched: {patched_merges}/{trials} suburb-merges (want 0)")

    if stock_merges > 0 and patched_merges == 0:
        print("  >> PASS — patch fixes the conflation")
    elif stock_merges == 0:
        print("  >> NOTE — stock prompt didn't merge either")
    elif patched_merges > 0:
        print("  >> FAIL — patch did NOT prevent the merge")


async def main() -> None:
    model = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    trials = int(os.getenv("DEDUP_TRIALS", "20"))
    temperature = float(os.getenv("DEDUP_TEMPERATURE", "1"))

    print(f"Model: {model}")
    print(f"Temperature: {temperature}")
    print(f"Trials per prompt variant: {trials}")

    # Scenario 1: minimal — just suburbs vs Chicago (matches real pipeline)
    await run_scenario(
        "minimal (1 extracted node)",
        EXTRACTED_NODES_MINIMAL,
        EXISTING_NODES_MINIMAL,
        model,
        trials,
        temperature=temperature,
    )

    # Scenario 2: realistic — multiple extracted nodes, merged candidate list
    await run_scenario(
        "realistic (2 extracted nodes, batch dedup)",
        EXTRACTED_NODES_REALISTIC,
        EXISTING_NODES_REALISTIC,
        model,
        trials,
        temperature=temperature,
    )


if __name__ == "__main__":
    asyncio.run(main())
