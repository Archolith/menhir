"""A scalar View must declare contributors the FACT writer can actually accept.

``TypedAssertion.episode_uuid`` is a polymorphic anchor -- :Episodic on the legacy/fixture path,
:TurnEvidence on the production ADR-0001 path. Only :TurnEvidence is ever ``evidence_finalized``, so
an Episodic-anchored contributor made the write unsatisfiable and surfaced as a 500 out of
/api/phase3/run. These pin the normalization and, critically, that an unresolvable anchor stays LOUD:
scalar_state is a RECALL-audience view, so an empty receipt would publish an unrecallable View.
"""

from typing import Any

import pytest

from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin

EPISODIC = "5d98bc2b-ddc2-4099-8441-12ad9b51b1c9"
TURN = "c26410d0-53e3-4971-88d5-0dcca29d45ee"


class _StubNeo4j:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.params: dict[str, Any] | None = None

    def execute(self, _query: str, params: dict[str, Any] | None = None, **_: Any) -> list:
        self.params = params or {}
        return self._rows


def _repo(stub: _StubNeo4j) -> ScalarViewRepositoryMixin:
    """The mixin takes its driver from the concrete repository, so attach it directly."""
    repo = ScalarViewRepositoryMixin()
    repo.neo4j = stub  # type: ignore[attr-defined]
    return repo


def _resolve(rows: list[dict[str, Any]], eps: list[str]) -> list[str]:
    return _repo(_StubNeo4j(rows))._resolve_evidence_anchors(eps, namespace="lme-cc5ded98")


def test_turn_evidence_anchor_passes_through_unchanged() -> None:
    rows = [{"eid": TURN, "direct": TURN, "grounded": []}]

    assert _resolve(rows, [TURN]) == [TURN]


def test_episodic_anchor_resolves_to_its_grounding_turn() -> None:
    """The exact refused case: an Episodic anchor reaches the same evidence via ADMITTED_ON."""
    rows = [{"eid": EPISODIC, "direct": None, "grounded": [TURN]}]

    assert _resolve(rows, [EPISODIC]) == [TURN]


def test_finalized_episodic_passes_through_unrewritten() -> None:
    """The gate accepts a finalized, unquarantined :Episodic. Rewriting it would make this resolver
    stricter than the writer it feeds, rejecting evidence the gate would take."""
    rows = [{"eid": EPISODIC, "direct": None, "ep_finalized": True,
             "ep_quarantined": False, "grounded": [TURN]}]

    assert _resolve(rows, [EPISODIC]) == [EPISODIC]


def test_quarantined_episodic_is_not_treated_as_valid_evidence() -> None:
    """Mirrors the gate exactly: finalized AND NOT quarantined. A quarantined anchor falls through
    to ADMITTED_ON resolution rather than passing through."""
    rows = [{"eid": EPISODIC, "direct": None, "ep_finalized": True,
             "ep_quarantined": True, "grounded": [TURN]}]

    assert _resolve(rows, [EPISODIC]) == [TURN]


def test_both_anchor_kinds_collapse_onto_one_receipt_entry() -> None:
    """Two anchors naming one turn must not produce a duplicate receipt."""
    rows = [
        {"eid": EPISODIC, "direct": None, "grounded": [TURN]},
        {"eid": TURN, "direct": TURN, "grounded": []},
    ]

    assert _resolve(rows, [EPISODIC, TURN]) == [TURN]


def test_empty_receipt_stays_empty_without_querying() -> None:
    repo = _repo(_StubNeo4j([]))

    assert repo._resolve_evidence_anchors([], namespace="ns") == []


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([{"eid": EPISODIC, "direct": None, "grounded": []}], "no ADMITTED_ON"),
        ([{"eid": EPISODIC, "direct": None, "grounded": [TURN, "other-turn"]}], "ambiguous"),
        ([], "names no in-tenant evidence"),
    ],
)
def test_unresolvable_anchor_raises_instead_of_dropping_contributors(
    rows: list[dict[str, Any]], expected: str
) -> None:
    """LOUD, deliberately. Emptying the receipt was correct for the OPERATOR-audience admission
    audit; here it would publish a RECALL view that view_live_provenance_cypher can never return."""
    with pytest.raises(ValueError) as excinfo:
        _resolve(rows, [EPISODIC])

    assert expected in str(excinfo.value)
    assert EPISODIC in str(excinfo.value)


def test_resolution_is_tenant_scoped() -> None:
    """An Episodic must never be resolved onto a foreign silo's turn."""
    stub = _StubNeo4j([{"eid": EPISODIC, "direct": None, "grounded": [TURN]}])
    _repo(stub)._resolve_evidence_anchors([EPISODIC], namespace="lme-cc5ded98")

    assert stub.params is not None
    assert stub.params.get("tenant_namespaces") == ["lme-cc5ded98"]
