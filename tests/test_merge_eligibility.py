from __future__ import annotations

import pytest

from menhir.domain import merge_eligibility as me


pytestmark = pytest.mark.unit


def _node(
    uuid: str = "n",
    *,
    exists: bool = True,
    ineligible_role: bool = False,
    namespace: str = "default",
    freshness: str | None = None,
    scope: str = "PERSISTENT",
    user_flagged: bool = False,
    conflict_status: str | None = None,
) -> me.NodeSignals:
    return me.NodeSignals(
        uuid=uuid,
        exists=exists,
        ineligible_role=ineligible_role,
        namespace=namespace,
        freshness=freshness,
        scope=scope,
        user_flagged=user_flagged,
        conflict_status=conflict_status,
    )


class TestMergeEligibility:
    def test_mutation_predicate_rechecks_canonical_self_markers(self) -> None:
        predicate = me.mutable_eligibility_cypher()
        for variable in ("survivor", "absorbed"):
            assert f"coalesce({variable}.is_self, false) = false" in predicate
            assert (
                f"toLower(trim(coalesce({variable}.entity_role, ''))) <> 'self'"
                in predicate
            )

    def test_two_plain_nodes_are_eligible(self) -> None:
        survivor = _node(uuid="s")
        absorbed = _node(uuid="a")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is True
        assert result.reason_code == me.ELIGIBLE

    def test_null_freshness_is_treated_as_active(self) -> None:
        survivor = _node(uuid="s", freshness=None)
        absorbed = _node(uuid="a", freshness=None)
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is True
        assert result.reason_code == me.ELIGIBLE

    def test_session_scope_is_allowed(self) -> None:
        survivor = _node(uuid="s", scope="SESSION")
        absorbed = _node(uuid="a", scope="SESSION")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is True
        assert result.reason_code == me.ELIGIBLE

    def test_missing_survivor_vetoes(self) -> None:
        survivor = _node(uuid="s", exists=False)
        absorbed = _node(uuid="a")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.NODE_MISSING

    def test_missing_absorbed_vetoes(self) -> None:
        survivor = _node(uuid="s")
        absorbed = _node(uuid="a", exists=False)
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.NODE_MISSING

    def test_same_uuid_vetoes(self) -> None:
        survivor = _node(uuid="x")
        absorbed = _node(uuid="x")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.SAME_UUID

    def test_structural_role_vetoes(self) -> None:
        survivor = _node(uuid="s")
        absorbed = _node(uuid="a", ineligible_role=True)
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.INELIGIBLE_ROLE

    def test_namespace_mismatch_vetoes(self) -> None:
        survivor = _node(uuid="s", namespace="proj-a")
        absorbed = _node(uuid="a", namespace="proj-b")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.NAMESPACE_MISMATCH

    def test_namespace_empty_and_default_are_equal(self) -> None:
        survivor = _node(uuid="s", namespace="")
        absorbed = _node(uuid="a", namespace="default")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is True
        assert result.reason_code == me.ELIGIBLE

    def test_compressed_freshness_vetoes(self) -> None:
        survivor = _node(uuid="s")
        absorbed = _node(uuid="a", freshness="COMPRESSED")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.NON_ACTIVE_FRESHNESS

    def test_gone_freshness_vetoes(self) -> None:
        survivor = _node(uuid="s", freshness="GONE")
        absorbed = _node(uuid="a")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.NON_ACTIVE_FRESHNESS

    def test_promoted_scope_vetoes(self) -> None:
        survivor = _node(uuid="s")
        absorbed = _node(uuid="a", scope="PROMOTED")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.PROMOTED_SCOPE

    def test_user_flagged_vetoes(self) -> None:
        survivor = _node(uuid="s", user_flagged=True)
        absorbed = _node(uuid="a")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.USER_FLAGGED

    def test_pending_conflict_vetoes(self) -> None:
        survivor = _node(uuid="s")
        absorbed = _node(uuid="a", conflict_status="pending_llm_review")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.PROTECTED_CONFLICT

    def test_unresolved_conflict_vetoes(self) -> None:
        survivor = _node(uuid="s", conflict_status="unresolved")
        absorbed = _node(uuid="a")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.PROTECTED_CONFLICT

    def test_resolved_conflict_is_allowed(self) -> None:
        survivor = _node(uuid="s")
        absorbed = _node(uuid="a", conflict_status="resolved")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is True
        assert result.reason_code == me.ELIGIBLE

    def test_priority_missing_beats_other_vetoes(self) -> None:
        survivor = _node(uuid="s", exists=False)
        absorbed = _node(uuid="a", ineligible_role=True, namespace="other")
        result = me.evaluate(survivor, absorbed)
        assert result.allowed is False
        assert result.reason_code == me.NODE_MISSING
