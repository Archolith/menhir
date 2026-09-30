"""Merge lineage (merge_audit / merged_from / last_merge_op_id) never reaches a Graphiti prompt.

The fork serializes entity attributes into its dedup and summary contexts, so Menhir's merge
bookkeeping rode along and grew with every merge. These tests render the REAL fork prompts, so a
fork prompt-format change that the filter no longer understands fails here instead of silently
re-admitting the audit trail.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from graphiti_core.prompts import dedupe_nodes, extract_nodes

from menhir.infrastructure.graphiti_llm_adapter import (
    MERGE_LINEAGE_KEYS,
    _ProviderExtrasCompletions,
    _strip_merge_lineage,
)

pytestmark = [pytest.mark.unit]

_AUDIT = json.dumps({"snapshot_at": "2026-09-30T01:03:25Z", "absorbed_uuid": "a" * 36, "summary": "x" * 400})


def _candidate(i: int, name: str, *, lineage: bool) -> dict:
    # Same shape the fork builds: {**candidate.attributes, candidate_id, name, entity_types, summary}.
    attributes = {"namespace": "ns", "edge_count": 4, "source": "remote-api"}
    if lineage:
        attributes |= {"merge_audit": [_AUDIT, _AUDIT], "merged_from": ["a" * 36], "last_merge_op_id": "op-1"}
    return {**attributes, "candidate_id": i, "name": name, "entity_types": ["Entity"], "summary": f"{name} summary"}


def _messages(prompt) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in prompt]


def _dedup_prompt(*, lineage: bool) -> list[dict]:
    return _messages(dedupe_nodes.nodes({
        "previous_episodes": [],
        "episode_content": "Step 9: listed FINANCIAL_INSTITUTION_ENTITIES.json",
        "extracted_nodes": [{"id": 0, "name": "FINANCIAL_INSTITUTION_ENTITIES.json", "entity_type": ["Entity"],
                             "entity_type_description": "Default Entity Type"}],
        "existing_nodes": [_candidate(0, "FINANCIAL_INSTITUTION_ENTITIES", lineage=lineage),
                           _candidate(1, "ID_RSSD", lineage=False)],
    }))


def _block(content: str, tag: str) -> list:
    return json.loads(content.split(f"<{tag}>")[1].split(f"</{tag}>")[0])


def test_dedup_prompt_loses_only_lineage():
    original = _dedup_prompt(lineage=True)
    cleaned = _strip_merge_lineage(original)
    user = cleaned[1]["content"]

    existing = _block(user, "EXISTING ENTITIES")
    assert not any(key in entity for entity in existing for key in MERGE_LINEAGE_KEYS)
    assert existing == [{k: v for k, v in c.items() if k not in MERGE_LINEAGE_KEYS}
                        for c in _block(original[1]["content"], "EXISTING ENTITIES")]
    assert existing[0]["edge_count"] == 4 and existing[0]["namespace"] == "ns"
    # Everything outside the rewritten block is byte-identical, and the input was not mutated.
    before, after = original[1]["content"].split("<EXISTING ENTITIES>"), user.split("<EXISTING ENTITIES>")
    assert before[0] == after[0]
    assert before[1].split("</EXISTING ENTITIES>")[1] == after[1].split("</EXISTING ENTITIES>")[1]
    assert cleaned[0] is original[0]
    assert "merge_audit" in original[1]["content"]
    block = lambda text: text.split("<EXISTING ENTITIES>")[1].split("</EXISTING ENTITIES>")[0]
    assert len(block(user)) < len(block(original[1]["content"])) / 2


def test_dedup_prompt_without_lineage_is_returned_unchanged():
    original = _dedup_prompt(lineage=False)
    assert _strip_merge_lineage(original) is original


def test_summary_batch_prompt_loses_nested_lineage():
    entities = [{"name": "CYBERSYN", "summary": "schema", "entity_types": ["Entity"],
                 "attributes": {"merge_audit": [_AUDIT], "merged_from": ["b" * 36], "namespace": "ns"}}]
    original = _messages(extract_nodes.extract_summaries_batch({
        "previous_episodes": [], "episode_content": "Step 3: inspected CYBERSYN", "entities": entities,
    }))
    cleaned = _strip_merge_lineage(original)

    assert _block(cleaned[1]["content"], "ENTITIES") == [
        {"name": "CYBERSYN", "summary": "schema", "entity_types": ["Entity"], "attributes": {"namespace": "ns"}}
    ]
    assert "Step 3: inspected CYBERSYN" in cleaned[1]["content"]


def test_unparseable_block_mentioning_lineage_is_left_alone():
    content = "<NOTES>\n[merge_audit is not json here\n</NOTES>"
    messages = [{"role": "user", "content": content}]
    assert _strip_merge_lineage(messages) is messages


@pytest.mark.asyncio
async def test_provider_wrapper_sends_stripped_messages_and_keeps_other_kwargs():
    sent: dict = {}

    async def create(**kwargs):
        sent.update(kwargs)
        return "ok"

    wrapper = _ProviderExtrasCompletions(SimpleNamespace(create=create), "http://fake.invalid/v1")
    result = await wrapper.create(model="m", messages=_dedup_prompt(lineage=True), max_tokens=4096, temperature=0)

    assert result == "ok"
    assert "merge_audit" not in json.dumps(sent["messages"])
    assert sent["model"] == "m" and sent["max_tokens"] == 4096 and sent["temperature"] == 0
