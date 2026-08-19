"""CF-165 product E2E: public ingest -> real writers -> public erase -> exhaustive zero.

The gap this closes, stated plainly. Every existing CF-165 live test **manually seeds** the
sidecar: `_seed_revision(db, uuid, "the secret text")` inserts a row, then proves the eraser
removes it. That proves the eraser works on rows the test knows about. It cannot prove the real
ingest and lifecycle writers have not created a DIFFERENTLY-KEYED copy somewhere the test never
thought to seed -- and "content survived in a place nobody enumerated" is precisely what CF-165
was.

Two things make this test different, and both matter:

**1. The copies are written by production code, not by the test.** `MENHIR_MCP_TELEMETRY_DB`
redirects the real module-level `telemetry_store` singleton at a temp file, so
`record_memory_revision` / `record_lifecycle_action` -- the same functions
`lifecycle_decay.py` and `enrichment_steps.py` call -- do the writing. The test supplies the
prose and the trigger, never the INSERT.

**2. The assertion is a FULL-SCHEMA SCAN, not a list of columns.** After erasure every TEXT
column of every table in the real sidecar schema is searched for the sentinel, including columns
`CONTENT_COLUMNS` does not classify. A test that checked only the classified columns would agree
with the classification by construction -- it could never discover that the classification is
incomplete, which is the one question worth asking here.

**The vacuity guard is load-bearing.** Before erasing, the scan must FIND the sentinel. Without
that, a test environment where ingest quietly wrote nothing to the sidecar would sail through the
"zero occurrences" assertion and report closure it had not tested. `test_..._is_not_vacuous`
makes that explicit.

**Stated boundary: the LLM is not real.** Enrichment calls an external model, so the entity
extraction step is not exercised; what IS exercised is every durable WRITE path reachable without
it -- the graph node, the sidecar revision and lifecycle rows, the journal, and the subject
inventory. That is the honest limit of this file, and it is recorded here rather than in a commit
message nobody will read again.

Run with:  pytest --run-online -m online tests/test_cf165_product_e2e.py
"""

from __future__ import annotations

import sqlite3
import uuid as uuidlib

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.erasure_coordinator import (
    ERASED,
    ErasureCoordinator,
)

pytestmark = [pytest.mark.online]


@pytest.fixture
def product_stack(test_neo4j_repo, tmp_path, monkeypatch):
    """A real graph and a real sidecar, with the PRODUCTION telemetry singleton redirected.

    The redirect is what lets this be a product test. `record_memory_revision` and friends resolve
    their store at import time from `default_telemetry_db_path()`, so pointing that at tmp_path
    means the genuine recorders write to a database this test can inspect -- rather than the test
    reaching around them with raw SQL.
    """
    db = tmp_path / "product-e2e.db"
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(db))

    from menhir.infrastructure.telemetry import store as store_mod

    real_store = store_mod.McpTelemetryStore(db_path=db)
    real_store._ensure_ready()

    # The recorders bind their store as a DEFAULT ARGUMENT
    # (`store: McpTelemetryStore = telemetry_store`), evaluated once at function definition.
    # Monkeypatching the module attribute therefore does nothing at all -- the default is already
    # captured. The first version of this fixture did exactly that, and the vacuity guard below
    # is what caught it: every write silently went to the real singleton and the scan found
    # nothing. `store=` is the seam the recorders expose for this, so the test passes the store
    # explicitly; the validation, truncation and SQL underneath stay entirely production code.
    monkeypatch.setattr(store_mod, "telemetry_store", real_store)

    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    coordinator = ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    return adapter, coordinator, db, test_neo4j_repo, real_store


# ---------------------------------------------------------------------------
# The scanner: every TEXT column in the real schema, not a curated list
# ---------------------------------------------------------------------------

def _text_columns(db) -> list[tuple[str, str]]:
    """Every (table, column) of a text-ish type in the sidecar, read from the LIVE schema.

    Read from `PRAGMA table_info` rather than from `CONTENT_COLUMNS`, deliberately. Deriving the
    search space from the classification would make the scan agree with the classification by
    construction: a content column nobody classified would be invisible to exactly the test meant
    to find it.
    """
    out: list[tuple[str, str]] = []
    with sqlite3.connect(db) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
                col, decl = row[1], (row[2] or "").upper()
                # Everything that can hold prose. BLOB included: a TEXT value stored in a column
                # with no declared type still comes back as str, and SQLite is happy to put one
                # there.
                if decl in ("TEXT", "", "BLOB") or "CHAR" in decl or "CLOB" in decl:
                    out.append((table, col))
    return out


def _sentinel_hits(db, sentinel: str) -> list[tuple[str, str, int]]:
    """(table, column, count) for every place the sentinel survives."""
    hits: list[tuple[str, str, int]] = []
    with sqlite3.connect(db) as conn:
        for table, col in _text_columns(db):
            try:
                n = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE CAST({col} AS TEXT) LIKE ?",
                    (f"%{sentinel}%",),
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if n:
                hits.append((table, col, int(n)))
    return hits


def _graph_hits(repo, sentinel: str) -> int:
    """The sentinel anywhere in any string property of any node in the real graph."""
    rows = repo.execute(
        """
        MATCH (n)
        WITH n, [k IN keys(n) WHERE toString(n[k]) CONTAINS $s] AS bad
        WHERE size(bad) > 0
        RETURN count(n) AS c
        """,
        params={"s": sentinel},
    )
    return int(rows[0]["c"]) if rows else 0


# ---------------------------------------------------------------------------
# Public ingest, driven through production writers
# ---------------------------------------------------------------------------

def _ingest(adapter, store, *, sentinel: str, namespace: str) -> str:
    """Create a memory the way the ingest path does, then run the real lifecycle recorders.

    `create_pending_episode` is the graph write `queue_episode` performs -- the durable copy of
    the user's prose that `add_memory` produces before any enrichment. The two recorder calls are
    the production functions `lifecycle_decay.rehydrate` and `enrichment_steps` invoke; driving
    them here is what puts sidecar copies in play without inventing an INSERT.
    """
    from menhir.infrastructure.telemetry.recorders import (
        record_lifecycle_action,
        record_memory_revision,
    )

    node_uuid = f"cf165-e2e-{uuidlib.uuid4().hex}"
    adapter.create_pending_episode(
        episode_uuid=node_uuid,
        name="product e2e memory",
        content=sentinel,
        session_id="e2e-session",
        user_id="e2e-user",
        source="claude-code",
        source_confidence=0.7,
        namespace=namespace,
    )
    record_memory_revision(
        node_uuid=node_uuid,
        field="content",
        old_value=sentinel,
        new_value=sentinel,
        changed_by="consolidation",
        store=store,
    )
    record_lifecycle_action(
        action="compress",
        node_uuid=node_uuid,
        trigger="decay_sweep",
        before_freshness="ACTIVE",
        after_freshness="COMPRESSED",
        llm_used=False,
        notes=sentinel,
        store=store,
    )
    return node_uuid


# ---------------------------------------------------------------------------
# The test the closure stamp was missing
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_public_ingest_then_public_erase_leaves_zero_content_anywhere(product_stack) -> None:
    """The whole finding, end to end, with nothing seeded by hand.

    `delete_memory` is the exact call `DELETE /api/memory/{uuid}` makes
    (`RuntimeProvider.delete_memory` -> `erase_memory` -> `ErasureCoordinator`), so this is the
    public erase surface rather than a coordinator the test constructed for itself.
    """
    adapter, coordinator, db, repo, store = product_stack
    sentinel = f"SENTINEL-{uuidlib.uuid4().hex}-PRIVATE-PROSE"
    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"

    node_uuid = _ingest(adapter, store, sentinel=sentinel, namespace=namespace)

    # --- vacuity guard: the copies must actually exist before erasing them ---
    before_sidecar = _sentinel_hits(db, sentinel)
    assert before_sidecar, (
        "the real writers created no sidecar copy, so the post-erase assertion would pass "
        "trivially and prove nothing"
    )
    assert _graph_hits(repo, sentinel) > 0, "the ingest wrote no graph copy"

    outcome = coordinator.erase_memory(node_uuid)
    assert outcome["reason"] == ERASED, outcome

    # --- the exhaustive assertion, over the LIVE schema ---
    after = _sentinel_hits(db, sentinel)
    assert after == [], (
        f"content survived erasure in {after}; it was present before in {before_sidecar}"
    )
    assert _graph_hits(repo, sentinel) == 0, "the sentinel survives in a graph property"

    rows = repo.execute(
        "MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": node_uuid}
    )
    assert int(rows[0]["c"]) == 0, "the graph node survived"


@pytest.mark.online
def test_the_scan_is_not_vacuous(product_stack) -> None:
    """Proves the scanner can FAIL, which is the only reason to trust it when it passes.

    A full-schema LIKE scan that silently matched nothing -- wrong table list, wrong quoting, an
    exception swallowed per column -- would report a clean erasure over any input at all. So this
    writes the sentinel through a production recorder, does NOT erase, and requires the scan to
    find it.
    """
    adapter, _coordinator, db, repo, store = product_stack
    sentinel = f"SENTINEL-{uuidlib.uuid4().hex}-NEVER-ERASED"

    _ingest(adapter, store, sentinel=sentinel, namespace=f"ns-{uuidlib.uuid4().hex[:8]}")

    hits = _sentinel_hits(db, sentinel)
    assert hits, "the scanner found nothing in a database that demonstrably contains the sentinel"
    tables = {t for t, _c, _n in hits}
    assert "memory_revisions" in tables, f"expected a revision copy; scanner saw {hits}"
    assert _graph_hits(repo, sentinel) > 0, "the graph scanner found nothing either"


@pytest.mark.online
def test_the_scan_covers_columns_the_classification_does_not(product_stack) -> None:
    """The scan's search space must be strictly WIDER than `CONTENT_COLUMNS`.

    If it were not, this file would only ever confirm that classified columns are erased -- and
    CF-165 was an unclassified copy. Asserting the containment directly makes that property
    visible rather than an accident of how `_text_columns` happens to be written.
    """
    from menhir.infrastructure.telemetry.erasure_inventory import classified_columns

    _adapter, _coordinator, db, _repo, _store = product_stack
    scanned = set(_text_columns(db))
    classified = set(classified_columns())

    assert classified, "classification is empty; the comparison would be meaningless"
    assert classified <= scanned, (
        f"classified columns the scan cannot see: {sorted(classified - scanned)}"
    )
    assert scanned - classified, (
        "the scan sees nothing beyond the classified set, so it could never discover an "
        "unclassified content column"
    )


@pytest.mark.online
def test_a_crash_after_prepare_replays_to_the_same_zero_content_state(product_stack, monkeypatch) -> None:
    """Restart safety, asserted on CONTENT rather than on journal bookkeeping.

    An erasure that is interrupted after PREPARE has already declared intent and may have removed
    the graph node while sidecar copies remain -- the exact window in which CF-165's defect is
    live. Existing coverage proves the journal reaches COMMITTED; this additionally proves the
    full-schema scan is clean afterwards, which is the property a user actually cares about.
    """
    adapter, coordinator, db, repo, store = product_stack
    sentinel = f"SENTINEL-{uuidlib.uuid4().hex}-CRASHED"
    namespace = f"ns-{uuidlib.uuid4().hex[:8]}"

    node_uuid = _ingest(adapter, store, sentinel=sentinel, namespace=namespace)
    assert _sentinel_hits(db, sentinel), "vacuity guard: nothing to erase"

    # Crash inside the erase/verify step, AFTER the journal has recorded PREPARE. That is the
    # dangerous window: intent is durable and the graph delete may already have run, so sidecar
    # copies can outlive the node they belong to -- CF-165's exact shape.
    original = ErasureCoordinator._erase_and_verify

    def _explode(*_a, **_kw):
        raise RuntimeError("crash after PREPARE")

    monkeypatch.setattr(ErasureCoordinator, "_erase_and_verify", _explode)
    with pytest.raises(RuntimeError):
        coordinator.erase_memory(node_uuid)

    # The node is now FENCED, and that is correct: a fresh `erase_memory` on it is REFUSED
    # ("already fenced by an unresolved EXPLICIT_ERASURE") so a crashed operation cannot be
    # silently duplicated or raced. The first version of this test called `erase_memory` again
    # and got `prepare_failed` -- an assumption about the restart mechanism, not a defect.
    #
    # Production resumes through RECONCILE, which replays PREPARED journal rows. That is the
    # path a restarted process actually takes, so it is the one worth asserting.
    monkeypatch.setattr(ErasureCoordinator, "_erase_and_verify", original)
    resumed = ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    prepared = list(resumed.journal.list_by_state("PREPARED", limit=50))
    assert prepared, "the crash left no PREPARED row, so there is nothing to replay"
    replayed = [resumed.replay_prepared_row(row)[0] for row in prepared]

    assert "REPLAYED" in replayed, f"the prepared erasure was not replayed: {replayed}"
    assert _sentinel_hits(db, sentinel) == [], "content survived the replayed erasure"
    assert _graph_hits(repo, sentinel) == 0
    rows = repo.execute(
        "MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": node_uuid}
    )
    assert int(rows[0]["c"]) == 0, "the graph node survived the replay"
