"""R1.2: every TEXT/BLOB column the telemetry sidecar can hold must be classified for erasure.

A fresh telemetry DB is initialized through every production initializer that creates tables in
that file. The table list is read from ``sqlite_master`` (never hand-listed), and each TEXT, BLOB
or untyped column must be classified in ``erasure_inventory`` (CONTENT, STRUCTURAL or
UNADDRESSABLE). A census over the source text fails on any ``CREATE TABLE`` that is neither
created in the sidecar by an initializer below nor declared in ``OTHER_DB_TABLES``.

Known gaps are held in ``KNOWN_UNCLASSIFIED`` as a ratchet: it may only shrink.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from menhir.infrastructure.telemetry import connect_telemetry_db
from menhir.infrastructure.telemetry import erasure_inventory as inv
from menhir.infrastructure.telemetry.erasure_denylist import DENYLIST_TABLE, ensure_erasure_denylist
from menhir.infrastructure.telemetry.erasure_purge import _WAIVER_TABLE, record_residue_waiver
from menhir.infrastructure.telemetry.lifecycle_store import initialize_lifecycle_schema
from menhir.infrastructure.telemetry.llm_usage_store import initialize_llm_usage_schema
from menhir.infrastructure.telemetry.operator_warnings_store import (
    initialize_operator_warnings_schema,
)
from menhir.infrastructure.telemetry.recall_store_query_log import (
    initialize_recall_query_log_schema,
)
from menhir.infrastructure.telemetry.schema_migrations import ensure_lineage_columns

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "menhir"

# Tables created by production code in a file OTHER than the telemetry sidecar.
OTHER_DB_TABLES: dict[str, str] = {
    "client_tokens": "api/client_token_store.py: ClientTokenStore(db_path) - API token DB, plain sqlite3.connect",
    "oauth_cimd_cache": "api/oauth_client_store.py: get_client_store().db_path - OAuth client store DB",
}

# (table, column) -> reason / R2 tag. Unclassified today; this list may only shrink.
def _gap(table: str, columns: str, reason: str) -> dict[tuple[str, str], str]:
    return {(table, c): reason for c in columns.split()}




# R2 CF-329 classified pending_actions.context/failure_reason as CONTENT; nothing is left.
KNOWN_UNCLASSIFIED: dict[tuple[str, str], str] = {}

KNOWN_UNCLASSIFIED_SEED_COUNT = 0  # seeded at 2 by R1; R2 CF-329 closed both

_CREATE_RE = re.compile(
    r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\{?\w+\}?)\s*(?:\(|USING)"
)
_FSTRING_NAMES = {"{DENYLIST_TABLE}": DENYLIST_TABLE, "{_WAIVER_TABLE}": _WAIVER_TABLE}


def _init_mcp_telemetry_store(db: Path) -> None:
    from menhir.infrastructure.telemetry.store import McpTelemetryStore

    McpTelemetryStore(db_path=db)._ensure_ready()


def _init_pending_actions(db: Path) -> None:
    from menhir.infrastructure.pending_actions import PendingActionStore

    PendingActionStore(db_path=db)._ensure_ready()


def _init_erasure_subjects(db: Path) -> None:
    from menhir.infrastructure.erasure_subjects import ErasureSubjectStore

    ErasureSubjectStore(db_path=db)._ensure_ready()


def _init_graph_operations(db: Path) -> None:
    from menhir.infrastructure.graph_operations import GraphOperationsJournal

    GraphOperationsJournal(db_path=db)._ensure_ready()


def _init_metric_receipts(db: Path) -> None:
    from menhir.infrastructure.metric_receipts import MetricReceiptStore

    MetricReceiptStore(db_path=db)._ensure_ready()


def _init_migration_batches(db: Path) -> None:
    from menhir.infrastructure.migration_batches import MigrationBatchStore

    MigrationBatchStore(db_path=db)._ensure_ready()


def _init_scheduler_leases(db: Path) -> None:
    from menhir.services.scheduler_lease import SchedulerLeaseStore

    SchedulerLeaseStore(db_path=db)._ensure_ready()


def _with_conn(fn):
    def run(db: Path) -> None:
        with connect_telemetry_db(db) as conn:
            fn(conn)
            conn.commit()

    return run


def _waiver(conn: sqlite3.Connection) -> None:
    record_residue_waiver(conn, note="r1.2 inventory coverage")


# Every production path that creates tables in the telemetry sidecar file.
INITIALIZERS = [
    # store.py _ensure_ready: mcp_events, failure_events, episode_task_events, memory_revisions,
    # merge_audit, conflict_resolutions, client/session registry, recall_receipts, lab runs;
    # it also calls the four mixin schemas and ensure_lineage_columns below.
    ("McpTelemetryStore._ensure_ready", _init_mcp_telemetry_store),
    # Same schemas called directly, so a store that stops calling one still gets covered.
    ("initialize_lifecycle_schema", _with_conn(initialize_lifecycle_schema)),
    ("initialize_llm_usage_schema", _with_conn(initialize_llm_usage_schema)),
    ("initialize_operator_warnings_schema", _with_conn(initialize_operator_warnings_schema)),
    ("initialize_recall_query_log_schema", _with_conn(initialize_recall_query_log_schema)),
    # schema_migrations.ensure_lineage_columns (also creates the denylist via the call below).
    ("ensure_lineage_columns", _with_conn(ensure_lineage_columns)),
    # pending_actions.py:122-134 creates its own table outside schema_migrations (CF-329).
    ("PendingActionStore._ensure_ready", _init_pending_actions),
    # erasure_subjects + erased_subject_denylist.
    ("ErasureSubjectStore._ensure_ready", _init_erasure_subjects),
    ("ensure_erasure_denylist", _with_conn(ensure_erasure_denylist)),
    # graph_operations, graph_operation_locks, graph_operation_subjects(+_backfill) journal.
    ("GraphOperationsJournal._ensure_ready", _init_graph_operations),
    # metric_receipts (also warmed by metric_write_coordinator_saga).
    ("MetricReceiptStore._ensure_ready", _init_metric_receipts),
    ("MigrationBatchStore._ensure_ready", _init_migration_batches),
    # scheduler_leases uses connect_telemetry_db with the default telemetry path.
    ("SchedulerLeaseStore._ensure_ready", _init_scheduler_leases),
    # erasure_residue_waiver: created lazily by the operator waiver action.
    ("record_residue_waiver", _with_conn(_waiver)),
]


def _build_db(db: Path) -> None:
    for _name, init in INITIALIZERS:
        init(db)


def _classified() -> frozenset[tuple[str, str]]:
    # CONTENT_COLUMNS includes shape=UNADDRESSABLE entries; NON_CONTENT_COLUMNS is STRUCTURAL.
    return inv.classified_columns() | inv.NON_CONTENT_COLUMNS


def _text_columns(db: Path) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    with sqlite3.connect(db) as conn:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")
        ]
        for table in tables:
            for _cid, name, ctype, *_ in conn.execute(f'PRAGMA table_info("{table}")'):
                decl = (ctype or "").upper()
                # SQLite affinity: untyped => BLOB; CHAR/CLOB/TEXT => TEXT; BLOB => BLOB.
                if decl == "" or "CHAR" in decl or "CLOB" in decl or "TEXT" in decl or "BLOB" in decl:
                    out.add((table, name))
    conn.close()
    return out


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")
        }
    finally:
        conn.close()


def _source_created_tables() -> set[str]:
    names: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for m in _CREATE_RE.finditer(line):
                raw = m.group(1)
                names.add(_FSTRING_NAMES.get(raw, raw))
        # Multi-line DDL where the name is on the line after CREATE TABLE is not used today.
    return names


@pytest.fixture()
def sidecar(tmp_path: Path) -> Path:
    db = tmp_path / "telemetry.db"
    _build_db(db)
    return db


def test_census_every_create_table_is_covered(sidecar: Path) -> None:
    created = _tables(sidecar)
    found = _source_created_tables()
    unaccounted = sorted(t for t in found if t not in created and t not in OTHER_DB_TABLES)
    assert not unaccounted, (
        f"CREATE TABLE in src/menhir for {unaccounted} is neither created by an INITIALIZERS entry "
        "nor listed in OTHER_DB_TABLES. Add the initializer or classify the table's DB."
    )
    stale = sorted(t for t in OTHER_DB_TABLES if t not in found or t in created)
    assert not stale, f"OTHER_DB_TABLES entries that are stale or actually in the sidecar: {stale}"


def test_every_text_column_is_classified(sidecar: Path) -> None:
    cols = _text_columns(sidecar)
    classified = _classified()
    unclassified = {c for c in cols if c not in classified}
    new = sorted(unclassified - set(KNOWN_UNCLASSIFIED))
    assert not new, (
        "TEXT/BLOB/untyped sidecar columns not classified in erasure_inventory "
        f"(CONTENT_COLUMNS or NON_CONTENT_COLUMNS): {new}"
    )


def test_known_unclassified_entries_are_not_stale(sidecar: Path) -> None:
    cols = _text_columns(sidecar)
    classified = _classified()
    gone = sorted(k for k in KNOWN_UNCLASSIFIED if k not in cols)
    fixed = sorted(k for k in KNOWN_UNCLASSIFIED if k in classified)
    assert not gone, f"KNOWN_UNCLASSIFIED entries no longer in the schema; remove them: {gone}"
    assert not fixed, f"KNOWN_UNCLASSIFIED entries now classified; remove them: {fixed}"


def test_known_unclassified_only_shrinks() -> None:
    assert len(KNOWN_UNCLASSIFIED) <= KNOWN_UNCLASSIFIED_SEED_COUNT
