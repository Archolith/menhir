"""CF-163: ``_artifact_index_queries`` builds its label from the declared registry.

``ARTIFACT_NODE_LABELS`` was a registry nothing read; the artifact index Cypher hardcoded
``:Evidence``. This pins that the generator now reads the registry (the load-bearing check) and
that the real output is byte-identical to what it emitted before.

The baseline below is the output from BEFORE this change, taken from git -- not captured from the
edited function. The first version of this file did the latter, which pinned a regression instead
of catching it: the fix had dropped three :Entity statements and the 'byte-identical' control was
comparing them against their own absence.
"""

from __future__ import annotations

import pytest

import menhir.infrastructure.schema as s

pytestmark = pytest.mark.unit




def test_registry_is_read(monkeypatch) -> None:
    """The load-bearing check: the generated queries must reflect the registry, not a hardcode."""
    monkeypatch.setattr(s, "ARTIFACT_NODE_LABELS", ("SentinelLabel",))
    generated = s._artifact_index_queries()
    assert any("n:SentinelLabel" in q for q in generated)
    assert any(q.startswith("CREATE INDEX sentinellabel_") for q in generated)


def test_the_entity_indexes_are_not_derived_from_the_registry() -> None:
    """The :Entity statements index artifact FIELDS on ordinary entity nodes. They are not
    artifact node labels, so substituting the registry must leave them untouched."""
    import menhir.infrastructure.schema as schema_mod

    original = schema_mod._artifact_index_queries()
    entity_rows = [q for q in original if "(n:Entity)" in q]

    assert len(entity_rows) == 3, entity_rows


#: The exact statements this function produced before CF-163 touched it. Pinned verbatim because
#: an index is identified by its NAME: a renamed statement does not reuse the existing index, it
#: creates a second one.
_BASELINE = [
    "CREATE INDEX entity_artifact_id_idx IF NOT EXISTS FOR (n:Entity) ON (n.artifact_id)",
    "CREATE INDEX entity_is_artifact_idx IF NOT EXISTS FOR (n:Entity) ON (n.is_artifact)",
    "CREATE INDEX entity_artifact_status_idx IF NOT EXISTS FOR (n:Entity) ON (n.artifact_status)",
    "CREATE INDEX evidence_artifact_id_idx IF NOT EXISTS FOR (n:Evidence) ON (n.artifact_id)",
    "CREATE INDEX evidence_uuid_idx IF NOT EXISTS FOR (n:Evidence) ON (n.uuid)",
]


def test_the_generated_schema_is_byte_identical_to_the_hand_written_one() -> None:
    """THE REGRESSION THIS FILE EXISTS FOR, and it is not hypothetical.

    The first version of this fix generated the WHOLE list from ARTIFACT_NODE_LABELS and so emitted
    only the two :Evidence statements -- silently dropping the three :Entity indexes that back
    `find_artifacts`. Nothing failed: an existing deployment already has those indexes, so only a
    fresh install would ever have noticed, as a full scan rather than an error.

    The :Entity entries index artifact FIELDS on ordinary entity nodes. They are not artifact node
    labels and must not be derived from the registry.
    """
    from menhir.infrastructure.schema import _artifact_index_queries

    assert _artifact_index_queries() == _BASELINE
