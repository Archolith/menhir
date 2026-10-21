"""Merge lineage (merge_audit / merged_from / last_merge_op_id) never reaches a Graphiti prompt.

Menhir stores merge bookkeeping on entity nodes, and the fork serializes node attributes into its
dedup and summary contexts. Since archolith-graphiti-core 0.30.2.post2 (graphiti #2) the fork drops
these keys itself (`to_prompt_json`), so Menhir no longer rewrites requests. These tests render the
REAL fork prompts with lineage-carrying nodes, so a fork regression that re-admits the audit trail
fails here.
"""

from __future__ import annotations

import json

import pytest
from graphiti_core.prompts import dedupe_nodes, extract_nodes

pytestmark = [pytest.mark.unit]

MERGE_LINEAGE_KEYS = ("merge_audit", "merged_from", "last_merge_op_id")
_AUDIT = json.dumps({"snapshot_at": "2026-09-30T01:03:25Z", "absorbed_uuid": "a" * 36, "summary": "x" * 400})


def _candidate(i: int, name: str, *, lineage: bool) -> dict:
    # Same shape the fork builds: {**candidate.attributes, candidate_id, name, entity_types, summary}.
    attributes = {"namespace": "ns", "edge_count": 4, "source": "remote-api"}
    if lineage:
        attributes |= {"merge_audit": [_AUDIT, _AUDIT], "merged_from": ["a" * 36], "last_merge_op_id": "op-1"}
    return {**attributes, "candidate_id": i, "name": name, "entity_types": ["Entity"], "summary": f"{name} summary"}


def _text(prompt) -> str:
    return "\n".join(m.content for m in prompt)


def _block(content: str, tag: str) -> list:
    return json.loads(content.split(f"<{tag}>")[1].split(f"</{tag}>")[0])


def test_dedup_prompt_drops_only_lineage():
    prompt = dedupe_nodes.nodes({
        "previous_episodes": [],
        "episode_content": "Step 9: listed FINANCIAL_INSTITUTION_ENTITIES.json",
        "extracted_nodes": [{"id": 0, "name": "FINANCIAL_INSTITUTION_ENTITIES.json", "entity_type": ["Entity"],
                             "entity_type_description": "Default Entity Type"}],
        "existing_nodes": [_candidate(0, "FINANCIAL_INSTITUTION_ENTITIES", lineage=True),
                           _candidate(1, "ID_RSSD", lineage=False)],
    })
    text = _text(prompt)

    assert not any(key in text for key in MERGE_LINEAGE_KEYS)
    existing = _block(text, "EXISTING ENTITIES")
    assert existing[0]["edge_count"] == 4 and existing[0]["namespace"] == "ns"
    assert existing[0]["summary"] == "FINANCIAL_INSTITUTION_ENTITIES summary"
    assert existing[1]["name"] == "ID_RSSD"


def test_summary_batch_prompt_drops_nested_lineage():
    entities = [{"name": "CYBERSYN", "summary": "schema", "entity_types": ["Entity"],
                 "attributes": {"merge_audit": [_AUDIT], "merged_from": ["b" * 36], "namespace": "ns"}}]
    text = _text(extract_nodes.extract_summaries_batch({
        "previous_episodes": [], "episode_content": "Step 3: inspected CYBERSYN", "entities": entities,
    }))

    assert not any(key in text for key in MERGE_LINEAGE_KEYS)
    assert _block(text, "ENTITIES") == [
        {"name": "CYBERSYN", "summary": "schema", "entity_types": ["Entity"], "attributes": {"namespace": "ns"}}
    ]
    assert "Step 3: inspected CYBERSYN" in text


def test_menhir_no_longer_rewrites_requests():
    # The Menhir-side stopgap (#205) is gone; the fork owns the policy.
    import menhir.infrastructure.graphiti_llm_adapter as adapter

    assert not hasattr(adapter, "_strip_merge_lineage")
    assert not hasattr(adapter, "MERGE_LINEAGE_KEYS")
