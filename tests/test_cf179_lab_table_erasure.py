"""CF-179 -- the two lab tables hold raw user text, and a namespace purge must actually reach it.

The entry says both tables are "unreachable by any node-keyed or namespace-keyed erasure" and leaves
one question explicitly open: whether redaction covers `recall_lab_runs.query`.

**Both halves resolved by measurement, and they landed in opposite directions.**

*Reachability: STALE.* CF-165 Phase C added a `namespace` column to both tables (migration at
`schema_migrations.py:320`) and classified every content column NAMESPACE_KEYED in
`erasure_inventory`. `erasure_purge` walks `CONTENT_COLUMNS` generically, so both are covered.

*Redaction of `query`: CONFIRMED ABSENT, and that is intentional.* The write path stores the query
verbatim; `request_json` carries it again; `result_json` carries full memory `content` and `name` per
hit. A redacted lab run would record nothing -- the query IS the experiment's independent variable.
So the fix is not to redact, it is to guarantee the rows stay erasable and to stop the comment above
the schema reading as a privacy assurance.

Which makes THE PURGE the load-bearing thing, so it is proven by executing it. Declaring a column
NAMESPACE_KEYED and having a purge reach it are different claims (trap T17), and the inventory is
just data until something consumes it.

Everything here runs against a throwaway SQLite file. The real sidecar holds 1,055 recall-lab rows
of the operator's data and the purge is irreversible.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.telemetry.erasure_inventory import (
    CONTENT_COLUMNS,
    ErasureShape,
)
from menhir.infrastructure.telemetry.erasure_purge import ErasureSubjects, purge_content
from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = pytest.mark.unit

LAB_TABLES = ("recall_lab_runs", "extraction_lab_runs")


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = tmp_path / "telemetry.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    c = sqlite3.connect(db)
    for ns, query in (("tenant-a", "SECRET USER QUERY"), ("tenant-b", "OTHER TENANT QUERY")):
        c.execute(
            "INSERT INTO recall_lab_runs (recorded_at, query, preset, namespace, judge_enabled, "
            "tied_ids_json, arms_json, request_json, result_json) VALUES ('t',?,'p',?,0,'[]','[]',?,?)",
            (
                query,
                ns,
                f'{{"query": "{query}"}}',
                f'{{"arms": [{{"results": [{{"content": "{query}"}}]}}]}}',
            ),
        )
    c.execute(
        "INSERT INTO extraction_lab_runs (recorded_at, current_message, arms_json, request_json, "
        "result_json, namespace) VALUES ('t','RAW USER MESSAGE','[]','{}','{}','tenant-a')"
    )
    c.commit()
    return c


def _rows(conn: sqlite3.Connection, table: str, column: str) -> dict[str, str]:
    return {ns: val for ns, val in conn.execute(f"SELECT namespace, {column} FROM {table}")}


# ---------------------------------------------------------------------------
# the purge, executed -- not the declaration, read
# ---------------------------------------------------------------------------


def test_a_namespace_purge_erases_the_lab_query(conn) -> None:
    """THE CLAIM THE ENTRY SAYS IS FALSE. It reports these rows as unreachable by namespace-keyed
    erasure; CF-165 Phase C made them reachable, and this executes it rather than citing it."""
    purge_content(conn, ErasureSubjects(namespaces=frozenset({"tenant-a"})), dry_run=False)
    conn.commit()

    assert "SECRET USER QUERY" not in _rows(conn, "recall_lab_runs", "query")["tenant-a"]


def test_the_query_is_erased_everywhere_it_is_duplicated(conn) -> None:
    """The query is stored THREE times: `query`, inside `request_json`, and inside `result_json`'s
    per-hit content. Erasing one column would leave the other two, which is the failure a
    column-by-column inventory exists to prevent."""
    purge_content(conn, ErasureSubjects(namespaces=frozenset({"tenant-a"})), dry_run=False)
    conn.commit()

    for column in ("query", "request_json", "result_json"):
        surviving = _rows(conn, "recall_lab_runs", column)["tenant-a"]
        assert "SECRET USER QUERY" not in surviving, f"{column} still holds the query"


def test_extraction_lab_raw_message_is_erased_too(conn) -> None:
    """The entry's harder half: `extraction_lab_runs` was filed as having NO scoping column at all.
    It has one, and the purge uses it."""
    purge_content(conn, ErasureSubjects(namespaces=frozenset({"tenant-a"})), dry_run=False)
    conn.commit()

    assert "RAW USER MESSAGE" not in _rows(conn, "extraction_lab_runs", "current_message")["tenant-a"]


def test_another_tenants_rows_are_untouched(conn) -> None:
    """POSITIVE CONTROL, and the one that matters most. A purge that erased everything would pass
    all three tests above while destroying an uninvolved tenant's experiment record."""
    purge_content(conn, ErasureSubjects(namespaces=frozenset({"tenant-a"})), dry_run=False)
    conn.commit()

    assert _rows(conn, "recall_lab_runs", "query")["tenant-b"] == "OTHER TENANT QUERY"


def test_dry_run_writes_nothing(conn) -> None:
    """The purge is irreversible on 1,055 rows of real data; a dry run that mutated would be the
    worst available defect in it."""
    before = _rows(conn, "recall_lab_runs", "query")
    purge_content(conn, ErasureSubjects(namespaces=frozenset({"tenant-a"})), dry_run=True)
    conn.commit()

    assert _rows(conn, "recall_lab_runs", "query") == before


def test_purging_an_unrelated_namespace_is_a_no_op(conn) -> None:
    """Second positive control: the purge must key on the namespace it was given, not simply run
    whenever it is called."""
    before = _rows(conn, "recall_lab_runs", "query")
    purge_content(conn, ErasureSubjects(namespaces=frozenset({"tenant-zzz"})), dry_run=False)
    conn.commit()

    assert _rows(conn, "recall_lab_runs", "query") == before


# ---------------------------------------------------------------------------
# the inventory -- so a new lab column cannot be added unclassified
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", LAB_TABLES)
def test_every_content_column_of_both_lab_tables_is_classified(table: str) -> None:
    """The purge is inventory-driven, so an unclassified column is an invisible one. These tables
    are where raw user text lands, which makes an omission here the CF-179 defect returning."""
    classified = {e.column for e in CONTENT_COLUMNS if e.table == table}
    expected = {"query", "arms_json", "request_json", "result_json"} if table == "recall_lab_runs" \
        else {"current_message", "arms_json", "request_json", "result_json"}
    assert expected <= classified, f"{table} unclassified: {sorted(expected - classified)}"


@pytest.mark.parametrize("table", LAB_TABLES)
def test_both_lab_tables_are_keyed_on_namespace(table: str) -> None:
    """Not node-keyed. Neither table carries a `node_uuid` -- the entry is right about that -- so
    namespace is the only lineage they have.

    **Asserts `key_columns`, not `shape`, and the difference is not cosmetic.** An earlier version
    of this test asserted the shape and claimed a change to DIRECT_SUBJECT_UUID would make these
    rows unreachable. That is false: `_where_clause_for` resolves subjects from `key_columns`, and
    uses `shape` only to pick OR-vs-AND combination and to skip UNADDRESSABLE. Flipping the shape
    leaves the purge working. Caught by mutation -- the shape flip failed only this test and none
    of the erasure ones, which is what exposed the wrong claim."""
    entries = [e for e in CONTENT_COLUMNS if e.table == table]
    assert entries, f"{table} has no inventory entries at all"
    for entry in entries:
        assert entry.key_columns == ("namespace",), (
            f"{table}.{entry.column} is keyed on {entry.key_columns}; namespace is the only "
            "lineage these tables carry, so any other key column makes it unreachable"
        )
        assert entry.shape is not ErasureShape.UNADDRESSABLE, (
            f"{table}.{entry.column} is marked UNADDRESSABLE; the purge skips those outright"
        )


def test_the_schema_comment_no_longer_reads_as_a_privacy_assurance() -> None:
    """CF-179's open question, resolved in the direction the entry could not determine: `query` is
    NOT redacted, and neither is the memory content in `result_json`. That is intended -- a redacted
    lab run records nothing -- but the comment above the schema implied filtering, which is the
    CF-141 item-2 pattern of a comment inviting a stronger reading than the code supports."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/menhir/infrastructure/telemetry/store.py").read_text(encoding="utf-8")
    assert "CF-179:" in src
    assert "privacy-filtered payload shown to the operator; raw judge-only" not in src
