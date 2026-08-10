"""list_committed_merges — the read-only merge-lineage source for the ScalarStateView C.4.4 repair
passes (offline, real SQLite journal in a tmp file)."""

from __future__ import annotations

import json

import pytest

from menhir.infrastructure.graph_operations import GraphOperationsJournal


def _prep_commit(journal, *, absorbed, survivor, kind="ENTITY_MERGE", commit=True):
    op_id = journal.prepare(
        operation_kind=kind,
        request_json=json.dumps({"absorbed_uuid": absorbed, "survivor_uuid": survivor}))
    if commit:
        journal.mark_committed(op_id)
    return op_id


@pytest.mark.unit
def test_list_committed_merges_returns_committed_pairs_oldest_first(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op1 = _prep_commit(journal, absorbed="A", survivor="B")
    op2 = _prep_commit(journal, absorbed="B", survivor="C")
    out = journal.list_committed_merges()
    assert [o["op_id"] for o in out] == [op1, op2]                 # oldest-first (chain order)
    assert out[0] == {"op_id": op1, "absorbed_uuid": "A", "survivor_uuid": "B"}
    assert out[1]["absorbed_uuid"] == "B" and out[1]["survivor_uuid"] == "C"


@pytest.mark.unit
def test_list_committed_merges_excludes_prepared_and_non_merge(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    committed = _prep_commit(journal, absorbed="A", survivor="B")
    _prep_commit(journal, absorbed="C", survivor="D", commit=False)          # still PREPARED
    _prep_commit(journal, absorbed="E", survivor="F", kind="ENTITY_UNMERGE") # wrong kind (prepared)
    out = journal.list_committed_merges()
    assert [o["op_id"] for o in out] == [committed]               # only the committed merge


@pytest.mark.unit
def test_list_committed_merges_skips_rows_missing_a_pair(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    good = _prep_commit(journal, absorbed="A", survivor="B")
    # a committed merge whose request lacks a survivor -> skipped, not returned half-formed.
    bad = journal.prepare(operation_kind="ENTITY_MERGE",
                          request_json=json.dumps({"absorbed_uuid": "X"}))
    journal.mark_committed(bad)
    out = journal.list_committed_merges()
    assert [o["op_id"] for o in out] == [good]


@pytest.mark.unit
def test_list_committed_merges_pages_through_all_beyond_one_page(tmp_path):
    # blocker 3: with more merges than one page, EVERY merge (incl. the last chain link) is returned —
    # not just the oldest page — so a later root / downstream op is never permanently hidden.
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_ids = [_prep_commit(journal, absorbed=f"a{i}", survivor=f"a{i+1}") for i in range(5)]
    out = journal.list_committed_merges(page_size=2)   # 5 merges, page size 2 -> must still get all 5
    assert [o["op_id"] for o in out] == op_ids
    assert out[-1] == {"op_id": op_ids[-1], "absorbed_uuid": "a4", "survivor_uuid": "a5"}


@pytest.mark.unit
def test_list_committed_merges_malformed_rows_do_not_hide_valid_lineage(tmp_path):
    # malformed committed merges preceding valid ones must not consume a page budget and hide them.
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    for _ in range(3):
        bad = journal.prepare(operation_kind="ENTITY_MERGE", request_json="{not json")
        journal.mark_committed(bad)
    good = _prep_commit(journal, absorbed="A", survivor="B")
    out = journal.list_committed_merges(page_size=2)
    assert [o["op_id"] for o in out] == [good]        # valid lineage still reached past malformed rows


@pytest.mark.unit
def test_list_committed_merges_has_no_total_cutoff(tmp_path):
    # blocker 3: pagination must run to EXHAUSTION. A total ceiling would hide rows beyond it on EVERY
    # call (each call restarts at offset 0), making a beyond-ceiling op permanently invisible.
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_ids = [_prep_commit(journal, absorbed=f"a{i}", survivor=f"a{i+1}") for i in range(25)]
    out = journal.list_committed_merges(page_size=1)     # smallest page: 25 sequential pages
    assert [o["op_id"] for o in out] == op_ids          # every row retrieved, none cut off
    assert not hasattr(GraphOperationsJournal, "_LINEAGE_MAX_ROWS")   # no permanent ceiling remains


@pytest.mark.unit
def test_list_committed_unmerges_carries_merge_op_id_from_reverses(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    merge = _prep_commit(journal, absorbed="A", survivor="B")
    un = journal.prepare(operation_kind="ENTITY_UNMERGE",
                         request_json=json.dumps({"absorbed_uuid": "A", "survivor_uuid": "B",
                                                  "merge_op_id": merge}),
                         reverses_op_id=merge)
    journal.mark_committed(un)
    out = journal.list_committed_unmerges()
    assert out == [{"op_id": un, "absorbed_uuid": "A", "survivor_uuid": "B", "merge_op_id": merge}]
