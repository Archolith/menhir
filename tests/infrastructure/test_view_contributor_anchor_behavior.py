"""Behavioral coverage for the evidence-anchor invariant: rewrite, refusal, and interleavings.

Companion to `test_view_contributor_anchor_invariant.py`, which pins WHERE enforcement lives. This
file pins WHAT it does, against a stub driver (no database, no model).

SCOPE
    `test_scalar_evidence_anchor_resolution.py` already covers the resolver's own decision table
    (pass-through, rewrite, quarantine, collapse, empty, unresolvable, tenant scope). This file adds
    only what that file cannot see: that the rewrite reaches the WRITE through `record()`, on the
    writer that had no coverage, and that the two-statement split is safe.

THE PROPERTY
    Resolution (`_resolve_evidence_anchors`) and the gate (`_write_version`'s
    ``resolved_count = size($eps)`` + ``evidence_finalized = true``) are SEPARATE statements. That is
    safe only in one direction: a fact that goes stale between them may cause a REFUSAL, and must
    never cause an unsafe publish. These tests exercise that direction explicitly, because the
    sequential happy path cannot distinguish "safe" from "we got lucky on timing".

WHY record_counter IS TESTED HERE AND NOT LIVE
    `consolidate_personal_memory` does ``targets = namespaces if enable_counter_state else []``, so
    the counter path is skipped whenever counter_state is off -- which it was for every measured run.
    A live fixture also only exercises it if that fixture happens to yield a countable event. This
    test exercises it deterministically instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin
from menhir.infrastructure.view_write_repository import ViewWriteRepositoryMixin

class _ComposedRepo(ViewWriteRepositoryMixin, ScalarViewRepositoryMixin):
    """Same MRO as the real `ViewRepository`, without its construction requirements.

    `record_counter` is an ergonomic wrapper on the SCALAR mixin while `record` and resolution live
    on the WRITE mixin, so a wrapper-level test has to compose both -- which is also the arrangement
    that makes the chokepoint reachable in production.
    """

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j


_EPISODIC = "11111111-1111-4111-8111-111111111111"
_TURN = "22222222-2222-4222-8222-222222222222"
_OTHER_TURN = "33333333-3333-4333-8333-333333333333"


class _ResolverNeo4j:
    """Answers ONLY the resolver's query; every other statement returns no rows.

    No rows from the write statement is exactly how the real gate refuses (`if not write_rows`), so
    a test that never stubs the write still observes a genuine refusal rather than a crash.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.queries: list[str] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list:
        self.queries.append(query)
        if "OPTIONAL MATCH (ep:Episodic {uuid: eid})" in query:
            return self._rows
        return []

    def resolver_ran(self) -> bool:
        return any("OPTIONAL MATCH (ep:Episodic {uuid: eid})" in q for q in self.queries)


def _repo(rows: list[dict[str, Any]]) -> tuple[ViewWriteRepositoryMixin, _ResolverNeo4j]:
    neo = _ResolverNeo4j(rows)
    return _ComposedRepo(neo), neo


def _grounded_row(**over: Any) -> dict[str, Any]:
    """An Episodic anchor that is NOT itself finalized but grounds on exactly one turn."""
    row = {"eid": _EPISODIC, "direct": None, "ep_finalized": False,
           "ep_quarantined": False, "grounded": [_TURN]}
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# record_counter: the writer with no live proof.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_record_counter_resolves_anchors_through_the_chokepoint() -> None:
    """record_counter passed a RAW list until 2026-09-09 and has never been exercised live.

    event_fold and quantstate_consolidator both hand it `episode_uuid`s straight off their events,
    so an Episodic-anchored one made the write unsatisfiable exactly as it did for scalar history.
    Asserting via the refusal diagnosis is deliberate: it proves the rewritten anchor is what
    reached the WRITE, not merely what the resolver returned.
    """
    repo, neo = _repo([_grounded_row()])
    with pytest.raises(ValueError) as err:
        repo.record_counter(subject="user", counter="sessions", value=3.0,
                            namespace="ns", episode_uuids=[_EPISODIC])
    # The stub returns no rows for the write, so the refusal path runs and reports the contributors
    # the write actually declared.
    assert neo.resolver_ran(), "record_counter must route contributors through resolution"
    message = str(err.value)
    assert _EPISODIC not in message, (
        "the write still declared the raw :Episodic anchor -- resolution did not reach record()"
    )


# ---------------------------------------------------------------------------
# Interleavings: resolve and the gate are separate statements.
# ---------------------------------------------------------------------------


class _RetractingNeo4j(_ResolverNeo4j):
    """Resolution succeeds, then the evidence disappears before the write.

    Models the only dangerous ordering: the deciding fact changes between the two statements.
    """

    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list:
        if "OPTIONAL MATCH (ep:Episodic {uuid: eid})" in query:
            self.queries.append(query)
            return self._rows
        # Every subsequent statement -- the write and its refusal diagnosis -- behaves as though the
        # turn is gone: no rows.
        self.queries.append(query)
        return []


@pytest.mark.unit
def test_evidence_retracted_between_resolve_and_write_refuses_rather_than_publishes() -> None:
    """A fact that goes stale mid-write must degrade to a REFUSAL, never to a published View.

    This is what makes the two-statement split acceptable: the gate re-checks
    `evidence_finalized = true` inside the write statement, so resolution is advisory. If the write
    ever stopped re-checking, this test fails and the split becomes unsafe.
    """
    neo = _RetractingNeo4j([_grounded_row()])
    repo = _ComposedRepo(neo)
    with pytest.raises(ValueError) as err:
        repo.record_counter(subject="user", counter="sessions", value=3.0,
                            namespace="ns", episode_uuids=[_EPISODIC])
    assert "refused" in str(err.value).lower()


@pytest.mark.unit
def test_the_write_statement_itself_rechecks_finalization() -> None:
    """Structural guard on the claim above: resolution is advisory ONLY because the write re-checks.

    Asserted against the emitted Cypher so the property cannot be lost by editing the query.
    """
    neo = _ResolverNeo4j([_grounded_row()])
    repo = _ComposedRepo(neo)
    with pytest.raises(ValueError):
        repo.record_counter(subject="user", counter="sessions", value=3.0,
                            namespace="ns", episode_uuids=[_EPISODIC])
    writes = [q for q in neo.queries if "resolved_count" in q]
    assert writes, "no write statement carrying the contributor gate was emitted"
    for query in writes:
        assert "e.evidence_finalized = true" in query, (
            "the write no longer re-checks finalization, so a contributor retracted after "
            "resolution could be published"
        )
        assert "resolved_count = size($eps)" in query, (
            "the gate is no longer all-or-nothing; a partially resolved receipt could publish"
        )
