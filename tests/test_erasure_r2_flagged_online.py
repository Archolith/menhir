"""R2 CF-335 online counterexamples: node-keyed sidecar content of a live item the cascade
scrubbed is purged with the erased memory, and the item's row survives.

The shared entity S is mentioned by the erased memory and a live one, so the cascade keeps it,
scrubs it and flags it for rederive. Its ``memory_revisions`` rows quote the erased text.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry import McpTelemetryStore
from menhir.services.erasure_coordinator import ErasureCoordinator
from tests._erasure_canary import all_hits, new_canary
from tests.test_erasure_rebuild_p0_scrub_online import (
    _adapter_and_coordinator,
    _seed_memory,
    _u,
)

pytestmark = pytest.mark.online


def _revisions(db: Any, node_uuid: str) -> list[tuple[Any, Any]]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(
            "SELECT old_value, new_value FROM memory_revisions WHERE node_uuid = ?", (node_uuid,)
        ))
    finally:
        conn.close()


def _seed_revision(db: Any, node_uuid: str, canary: str) -> None:
    McpTelemetryStore(db_path=db).record_memory_revision(
        node_uuid=node_uuid, field="summary", old_value="before", new_value=f"S now {canary}",
        changed_by="ingest",
    )
    assert len(_revisions(db, node_uuid)) == 1


def test_cf335_revisions_of_a_flagged_shared_entity_are_purged_and_kept(
    test_neo4j_repo, tmp_path
) -> None:
    """Fails if the cascade's flagged uuids are not recorded as FLAGGED_UUID subjects (the
    column purge never reaches the shared entity's revisions)."""
    repo = test_neo4j_repo
    ns, canary = f"r2-335-{_u()}", new_canary("CF335")
    ids = _seed_memory(repo, ns, canary)
    _, coord, db = _adapter_and_coordinator(repo, tmp_path)
    _seed_revision(db, ids["shared"], canary)

    out = coord.erase_memory(ids["queue"])

    assert out["reason"] == "erased", out
    assert all_hits(repo, db, canary) == []
    assert _revisions(db, ids["shared"]) == [(None, None)]  # the row is kept, its text blanked
    # A live item is never denylisted: its later revisions still land.
    McpTelemetryStore(db_path=db).record_memory_revision(
        node_uuid=ids["shared"], field="summary", old_value=None, new_value="rederived",
        changed_by="ingest",
    )
    assert ("rederived" in {r[1] for r in _revisions(db, ids["shared"])})


def test_cf335_flagged_items_are_recovered_when_the_erase_crashes_before_recording_them(
    test_neo4j_repo, tmp_path, monkeypatch
) -> None:
    """The graph scrub commits, then the process dies before FLAGGED_UUID is recorded. The replay
    must still purge S's revisions. Fails if replay does not read the flagged items back from
    their DerivedRederiveRepair keys."""
    repo = test_neo4j_repo
    ns, canary = f"r2-335c-{_u()}", new_canary("CF335C")
    ids = _seed_memory(repo, ns, canary)
    adapter, coord, db = _adapter_and_coordinator(repo, tmp_path)
    _seed_revision(db, ids["shared"], canary)
    real_record = ErasureSubjectStore.record_subjects

    def crash_on_flagged(self, op_id, subjects, **kwargs):
        subjects = list(subjects)
        if any(t == "FLAGGED_UUID" for t, _ in subjects):
            raise RuntimeError("simulated crash after the graph scrub")
        return real_record(self, op_id, subjects, **kwargs)

    monkeypatch.setattr(ErasureSubjectStore, "record_subjects", crash_on_flagged)
    with pytest.raises(RuntimeError, match="simulated crash"):
        coord.erase_memory(ids["queue"])
    monkeypatch.setattr(ErasureSubjectStore, "record_subjects", real_record)
    # The scrub committed: S is flagged in the graph, its sidecar text is still there.
    assert canary in str(_revisions(db, ids["shared"]))

    resumed = ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    prepared = list(resumed.journal.list_by_state("PREPARED", limit=50))
    assert prepared
    outcomes = [resumed.replay_prepared_row(row)[0] for row in prepared]

    assert "REPLAYED" in outcomes, outcomes
    assert all_hits(repo, db, canary) == []
    assert _revisions(db, ids["shared"]) == [(None, None)]


def test_cf335_archive_read_is_suppressed_while_a_flagged_purge_is_pending(tmp_path) -> None:
    """Between the erase's intent and its purge, rehydrate's archive read must not serve the
    flagged item's pre-scrub text. Fails if FLAGGED_UUID does not suppress node-keyed reads."""
    from menhir.infrastructure.erasure_subjects import suppressed_node_uuids

    db = tmp_path / "s.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    store = ErasureSubjectStore(db_path=db)
    store._ensure_ready()
    s = _u()
    store.record_subjects("op-1", [("FLAGGED_UUID", s)])

    assert suppressed_node_uuids(db, [s]) == frozenset({s})
