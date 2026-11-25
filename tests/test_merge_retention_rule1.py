"""Offline: Rule 1 -- a merge or an unmerge never makes content easier to delete.

Covers the statements and predicates; the graph behaviour is in test_merge_retention_live.py.
Each test names what it fails on in the pre-change code.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.domain import merge_delta as md
from menhir.domain import merge_eligibility as me
from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.delete_coordinator import DeleteCoordinator


class _Capture:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_kw: Any):
        self.calls.append((query, params or {}))
        return self.responses.pop(0) if self.responses else []


def _sig(uuid: str, scope: str | None) -> me.NodeSignals:
    return me.NodeSignals(
        uuid=uuid, exists=True, ineligible_role=False, namespace="default",
        freshness=None, scope=scope, user_flagged=False, conflict_status=None,
    )


# --------------------------------------------------------------------------- eligibility

@pytest.mark.parametrize("bad", ["CANDIDATE", "candidate", None, "", "   ", "ARCHIVED"])
@pytest.mark.parametrize("side", ["survivor", "absorbed"])
def test_scope_other_than_session_or_persistent_is_refused(bad, side):
    """Fails on old code: CANDIDATE, null, blank and unknown scope were ELIGIBLE."""
    good = "SESSION"
    pair = (_sig("s", bad), _sig("a", good)) if side == "survivor" else (_sig("s", good), _sig("a", bad))
    verdict = me.evaluate(*pair)
    assert verdict.allowed is False
    assert verdict.reason_code == me.UNMERGEABLE_SCOPE


@pytest.mark.parametrize("scope", ["PROMOTED", " promoted "])
def test_promoted_keeps_its_own_reason(scope):
    assert me.evaluate(_sig("s", scope), _sig("a", "SESSION")).reason_code == me.PROMOTED_SCOPE


@pytest.mark.parametrize("scope", ["SESSION", "PERSISTENT", " persistent ", "Session"])
def test_normalized_session_and_persistent_are_eligible(scope):
    assert me.evaluate(_sig("s", scope), _sig("a", "PERSISTENT")).allowed is True


def test_one_constant_drives_evaluate_and_the_cypher_params():
    """Fails on old code: no mergeable_scopes parameter existed."""
    assert me.MUTABLE_PREDICATE_PARAMS["mergeable_scopes"] == sorted(me._MERGEABLE_SCOPES)
    cypher = me.mutable_eligibility_cypher()
    for var in ("survivor", "absorbed"):
        assert f"toUpper(trim(coalesce({var}.scope, ''))) IN $mergeable_scopes" in cypher
    # Every parameter the fragment names is bound, so no consumer can hit a missing-parameter error.
    named = set(re.findall(r"\$([a-z_]+)", cypher))
    assert named <= set(me.MUTABLE_PREDICATE_PARAMS)
    renamed = me.mutable_eligibility_cypher("s", "a")
    assert "toUpper(trim(coalesce(s.scope, ''))) IN $mergeable_scopes" in renamed
    assert "toUpper(trim(coalesce(a.scope, ''))) IN $mergeable_scopes" in renamed


@pytest.mark.parametrize(
    "scope", ["SESSION", "PERSISTENT", " session ", "PROMOTED", "CANDIDATE", None, "", "x"]
)
def test_evaluate_agrees_with_the_cypher_membership_rule(scope):
    """The Cypher admits scope iff upper(trim(scope)) is in the bound list; evaluate must match."""
    in_cypher = str(scope or "").strip().upper() in me.MUTABLE_PREDICATE_PARAMS["mergeable_scopes"]
    allowed = me.evaluate(_sig("s", scope), _sig("a", "SESSION")).allowed
    assert allowed is in_cypher


# --------------------------------------------------------------------------- merge statement

def _merge_statement():
    neo = _Capture([[{"uuid": "a", "relationships": [], "mentioned_by_episodes": []}], []])
    repo = CorrelationRepository(neo)
    repo.evaluate_merge_eligibility = lambda s, a: SimpleNamespace(  # type: ignore[method-assign]
        allowed=True, reason_code=me.ELIGIBLE, diagnostics={}
    )
    repo.merge_entity("s", "a", similarity=0.9)
    return neo.calls[1]


def test_merge_statement_raises_scope_clears_ttl_and_binds_scopes():
    """Fails on old code: the merge set neither scope nor ttl_expires."""
    query, params = _merge_statement()
    assert "survivor.ttl_expires = null" in query
    assert "survivor.scope = CASE WHEN merged_persistent THEN 'PERSISTENT' ELSE 'SESSION' END" in query
    assert "coalesce(survivor.promoted_at, datetime())" in query
    assert params["mergeable_scopes"] == ["PERSISTENT", "SESSION"]
    # Rank uses the same normalization as the predicate, over the live nodes.
    assert "toUpper(trim(survivor.scope)) = 'PERSISTENT'" in query
    assert "toUpper(trim(absorbed.scope)) = 'PERSISTENT'" in query
    # Computed in a WITH before the SET and before the absorbed node is deleted.
    assert query.index("AS merged_persistent") < query.index("survivor.scope = CASE")
    assert query.index("survivor.scope = CASE") < query.index("DELETE absorbed")
    assert set(re.findall(r"\$([a-z_]+)", query)) <= set(params)


def test_merge_owned_survivor_properties_do_not_include_retention():
    assert not {"scope", "ttl_expires", "promoted_at"} & set(md.MERGE_OWNED_SURVIVOR_PROPERTIES)


# --------------------------------------------------------------------------- restore statement

def _restore_statement(survivor_properties: dict[str, Any] | None = None):
    neo = _Capture([[{"out_restored": 0}]])
    CorrelationRepository(neo).restore_merge_snapshot(
        survivor_uuid="s", absorbed_uuid="a", absorbed_labels=["Entity"],
        # An old snapshot without scope/ttl_expires must still restore.
        absorbed_properties={"uuid": "a", "name": "a"}, out_rels=[], in_rels=[],
        survivor_properties=survivor_properties or {}, rebound_episodes=[], operation_id="op",
    )
    return neo.calls[0]


def test_restore_clears_ttl_on_the_restored_node_after_the_snapshot_props():
    """Fails on old code: the snapshot's (possibly expired) ttl_expires was restored as-is."""
    query, _ = _restore_statement()
    assert query.index("SET a = $absorbed_properties") < query.index("a.ttl_expires = null")
    assert "a.ttl_expires = null" in query.split("WITH s, a")[0]


def test_restore_leaves_survivor_scope_and_ttl_alone():
    query, params = _restore_statement({"summary": "x"})
    survivor_sets = [ln for ln in query.splitlines() if "s.scope" in ln or "s.ttl_expires" in ln]
    assert not [ln for ln in survivor_sets if not ln.strip().startswith("//")]
    assert "scope" not in params["survivor_properties"]
    assert "ttl_expires" not in params["survivor_properties"]


# --------------------------------------------------------------------------- TTL delete

def test_delete_statement_requires_expired_ttl_only_when_asked():
    """Fails on old code: the statement never rechecked ttl_expires."""
    neo = _Capture([[{"deleted_uuids": []}], [{"deleted_uuids": []}]])
    repo = ConsolidationRepository(neo)
    repo.delete_entities_returning_uuids(
        ["n"], require_scope="SESSION", protect_retention=True, require_expired_ttl=True
    )
    repo.delete_entities_returning_uuids(["n"], require_scope="SESSION", protect_retention=True)
    ttl_q, entity_q = neo.calls[0][0], neo.calls[1][0]
    assert "n.ttl_expires IS NOT NULL AND n.ttl_expires < datetime()" in ttl_q
    assert "ttl_expires" not in entity_q


class _Adapter:
    def __init__(self) -> None:
        self.present = {"n1"}
        self.kwargs: list[dict[str, Any]] = []

    def fetch_ttl_expired_session_uuids(self, session_id=None):
        return [{"uuid": "n1"}]

    def capture_node_state(self, uuid):
        return {"uuid": uuid, "labels": ["Entity"], "properties": {"uuid": uuid},
                "relationships": []} if uuid in self.present else None

    def newly_unreferenced_evidence(self, uuids):
        return []

    def delete_entities_returning_uuids(self, uuids, **kwargs):
        self.kwargs.append(kwargs)
        self.present -= set(uuids)
        return list(uuids)


def test_coordinator_threads_require_expired_ttl_for_the_sweep_only(tmp_path):
    """Fails on old code: neither caller passed the parameter."""
    adapter = _Adapter()
    coord = DeleteCoordinator(
        graph_adapter=adapter, journal=GraphOperationsJournal(db_path=tmp_path / "saga.db")
    )
    coord.delete_expired_session_nodes()
    assert adapter.kwargs[-1]["require_expired_ttl"] is True
    assert adapter.kwargs[-1]["protect_retention"] is True

    adapter.present = {"n2"}
    coord.delete_entity("n2")
    assert "require_expired_ttl" not in adapter.kwargs[-1]
    assert adapter.kwargs[-1]["protect_retention"] is False
