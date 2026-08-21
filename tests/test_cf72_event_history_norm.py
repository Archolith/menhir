"""CF-72: the event-history family hand-wrote its normalization helper four times.

The register recorded three private `_norm` copies in `services/event_history_*`, one of which did
not collapse internal whitespace, and concluded the odd one out was a bug that let a stale View
survive. Consolidating surfaced a fourth copy -- in `domain/event_history.py`, the module all three
services import from -- and that fourth copy is what makes the register's conclusion backwards.

`EventLane.key`, `lane_key` and the sha256 `assertion_key` are all built with the DOMAIN copy,
which does not collapse. `record_event_timeline` persists `canonical.predicate`, i.e. that exact
output. So `_exact_lane_views` comparing with a non-collapsing helper was CORRECT: both sides came
from the same normalization, and they agreed. Collapsing the read side is what breaks the match --
and worse, it makes a lane match a *whitespace-variant* lane's View, which `_exact_lane_views`
exists to exclude ("Sibling lanes ... are excluded so they are never retired").

So there are two jobs here, not one implementation with a typo:

* identity normalization -- feeds durable keys, must not collapse, and cannot be changed without
  rewriting every stored `assertion_key`;
* query-text normalization -- free text and anchors, must collapse.

The defect is that four copies of two different jobs shared one name, so nothing at a call site
said which you were getting. Both now live in `domain/event_history.py` under names that say it.
No behavior changed at any call site.
"""

from __future__ import annotations

import pytest

from menhir.domain import event_history as domain_eh
from menhir.domain.event_history import (
    EventLane,
    normalize_identity_component,
    normalize_text,
)
from menhir.services.event_history_authority import _norm as authority_norm
from menhir.services.event_history_recall import _norm as recall_norm
from menhir.services.event_history_service import EventHistoryService, _norm as service_norm

pytestmark = pytest.mark.unit

_WHITESPACE_VARIANTS = ["a  b", " A\tB ", "x\n\ny"]


class _ListOnlySink:
    def __init__(self, views: list[dict]) -> None:
        self._views = [dict(v) for v in views]

    def list_event_timeline_views(self, **kwargs):
        return [dict(v) for v in self._views]


# ---------------------------------------------------------------------------
# The duplication itself
# ---------------------------------------------------------------------------


def test_no_event_history_module_defines_its_own_normalizer() -> None:
    """The finding. Each of these was a hand-written module-level copy; all now resolve to one of
    the two shared domain functions."""
    shared = {normalize_text, normalize_identity_component}
    for name, fn in (
        ("authority", authority_norm),
        ("recall", recall_norm),
        ("service", service_norm),
        ("domain", domain_eh._norm),
    ):
        assert fn in shared, f"{name} still has its own private normalizer"


def test_query_text_callers_share_the_collapsing_normalizer() -> None:
    """authority and recall normalize QUERY text and anchors, where a user's spacing must not
    change a match."""
    assert authority_norm is normalize_text
    assert recall_norm is normalize_text


def test_identity_callers_share_the_non_collapsing_normalizer() -> None:
    """The service compares against `EventLane.key`, which the domain builds with this function."""
    assert service_norm is normalize_identity_component
    assert domain_eh._norm is normalize_identity_component


# ---------------------------------------------------------------------------
# The two jobs are deliberately different, and differ in exactly one way
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", _WHITESPACE_VARIANTS)
def test_the_two_jobs_differ_only_on_internal_whitespace(value: str) -> None:
    assert normalize_text(value) != normalize_identity_component(value)
    # Collapsing the identity form yields the text form: the sole difference.
    assert " ".join(normalize_identity_component(value).split()) == normalize_text(value)


@pytest.mark.parametrize("value", ["Hello World", "  Trimmed  ", "single"])
def test_they_agree_whenever_there_is_no_internal_run_of_whitespace(value: str) -> None:
    assert normalize_text(value) == normalize_identity_component(value)


@pytest.mark.parametrize("value", [None, ""])
def test_both_are_blank_tolerant(value: str | None) -> None:
    assert normalize_text(value) == ""
    assert normalize_identity_component(value) == ""


# ---------------------------------------------------------------------------
# The invariant the consolidation must not break
# ---------------------------------------------------------------------------


def _service_with_views(views: list[dict]) -> EventHistoryService:
    return EventHistoryService(source=object(), sink=_ListOnlySink(views))


def test_a_lane_retires_the_view_its_own_writer_would_have_persisted() -> None:
    """POSITIVE CONTROL, and the load-bearing one. `record_event_timeline` is called with
    `predicate=canonical.predicate`, so the stored value IS the canonical. Whatever normalization
    the read side uses, it must still match that."""
    lane = EventLane(
        subject_uuid="ent-a", predicate="acquired  thing", namespace="ns", domain="life  events",
    )
    canonical = EventHistoryService._canonical_lane(lane)
    service = _service_with_views([
        {"uuid": "view-1", "view_key": "vk1",
         "predicate": canonical.predicate, "domain": canonical.domain},
    ])

    assert [v["view_key"] for v in service._exact_lane_views(canonical)] == ["vk1"]


def test_a_whitespace_variant_lanes_view_is_NOT_retired() -> None:
    """The counterexample. Collapsing the read side makes "acquired  thing" and "acquired thing"
    the same lane, so reconciling one would retire the other's View -- exactly what
    `_exact_lane_views` documents that it excludes.

    They are different lanes under the identity normalization that built their durable keys, so
    they must stay different here.
    """
    lane = EventLane(
        subject_uuid="ent-a", predicate="acquired thing", namespace="ns", domain="life events",
    )
    canonical = EventHistoryService._canonical_lane(lane)
    service = _service_with_views([
        {"uuid": "view-1", "view_key": "vk1",
         "predicate": "acquired  thing", "domain": "life  events"},
    ])

    assert service._exact_lane_views(canonical) == []


def test_identity_normalization_must_not_collapse_or_stored_keys_change() -> None:
    """A guard on the thing that makes this irreversible. `assertion_key` is a sha256 over
    identity-normalized components and is already persisted on live nodes. If someone "fixes" the
    identity normalizer to collapse whitespace, every such key changes and the nodes keyed by them
    are orphaned. Pin the distinction rather than the digest, so this stays readable.
    """
    assert normalize_identity_component("a  b") == "a  b"
    assert EventLane(
        subject_uuid="s", predicate="has  bought", namespace="ns", domain="d",
    ).key != EventLane(
        subject_uuid="s", predicate="has bought", namespace="ns", domain="d",
    ).key
