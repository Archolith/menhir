"""Result dataclasses for the suburbs grounding trace smoke script.

Moved verbatim from trace_suburbs_grounding.py (facade); the facade
re-exports EdgeTrace and TrialResult so existing imports keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EdgeTrace:
    source_entity_name: str
    target_entity_name: str
    fact: str
    source_uuid_pre: str = ""
    target_uuid_pre: str = ""
    source_uuid_post: str = ""
    target_uuid_post: str = ""
    source_resolved_name: str = ""
    target_resolved_name: str = ""


@dataclass
class TrialResult:
    trial: int
    namespace: str
    model: str
    temperature: float | None = None

    # Step 1: raw combined extraction output
    raw_extracted_entities: list[str] = field(default_factory=list)
    raw_extracted_edges: list[dict] = field(default_factory=list)
    orphaned_entities: list[str] = field(default_factory=list)

    # Step 2: node dedup
    dedup_decisions: list[dict] = field(default_factory=list)
    uuid_map: dict[str, str] = field(default_factory=dict)

    # Step 3: edge remapping
    edge_traces: list[dict] = field(default_factory=list)

    # Step 3b: edge invalidation
    invalidation_traces: list[dict] = field(default_factory=list)

    # Step 4: final graph
    final_entities: list[str] = field(default_factory=list)
    final_edges: list[dict] = field(default_factory=list)
    suburb_entity_exists: bool = False
    suburb_edge_correct: bool = False
    chicago_edge_expired: bool = False
    verdict: str = ""  # PASS, FAIL_A (extraction), FAIL_B (resolution), FAIL_C (invalidation), FAIL_OTHER

