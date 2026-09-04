"""Phase 6 acceptance: replay the corpus and require zero false-positive self binds.

This is the gate that decides whether enforce mode is safe to activate. It is deliberately
written so that a regression widening the evidence contract fails HERE, on a control case, rather
than in production on a project scan.
"""

from __future__ import annotations

import pytest

from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.infrastructure.self_binding import (
    AmbiguousSelfBindingError,
    SelfBindMode,
    SelfBindOutcome,
    SelfBindResult,
    bind_canonical_self,
)

from tests.corpus_self_binding import CORPUS, NEGATIVE_CASES, CorpusCase


class _Node:
    def __init__(self, uuid: str, name: str) -> None:
        self.uuid = uuid
        self.name = name


def _replay(case: CorpusCase, mode: SelfBindMode):
    """Replay one case. A refusal is a RESULT, not a test error: failing closed is the designed
    behavior for a payload whose subject cannot be proven, so it is classified like any other."""
    nodes = [_Node(f"extracted-{i}", n) for i, n in enumerate(case.entity_names)]
    edges: list[object] = []
    index_map = {n.uuid: [0] for n in nodes}
    try:
        result = bind_canonical_self(nodes, edges, index_map, case.identity(), mode)
    except AmbiguousSelfBindingError:
        result = SelfBindResult(SelfBindOutcome.AMBIGUOUS, mode=mode)
    return result, nodes


@pytest.mark.unit
@pytest.mark.parametrize("case", CORPUS, ids=lambda c: c.name)
def test_enforce_matches_expected_classification(case: CorpusCase):
    result, _ = _replay(case, SelfBindMode.ENFORCE)
    assert result.outcome is case.expected, (
        f"{case.name} ({case.category}): expected {case.expected}, got {result.outcome}. {case.note}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=lambda c: c.name)
def test_no_false_positive_binds(case: CorpusCase):
    """Phase 6's activation gate. Every control must leave the payload untouched."""
    result, nodes = _replay(case, SelfBindMode.ENFORCE)

    canonical = self_uuid_for_namespace(case.namespace)
    assert result.bound is False, f"{case.name} falsely bound the human. {case.note}"
    assert all(n.uuid != canonical for n in nodes), (
        f"{case.name} rewrote a node to the canonical self uuid. {case.note}"
    )


@pytest.mark.unit
def test_corpus_covers_every_category_the_plan_names():
    """A corpus missing its controls proves only that binding works, never that it is narrow."""
    required = {
        "ambiguous_subject",
        "explicit_self_subject",
        "quoted_speech",
        "trusted_user_turn",
        "assistant_echo",
        "project_scan",
        "generic_account_user",
        "manual_memory",
        "retry_repair",
        "downgraded_claim",
        "untrusted_producer",
    }
    assert required <= {c.category for c in CORPUS}


@pytest.mark.unit
def test_corpus_has_more_controls_than_positives():
    """False positives are the dangerous direction, so the corpus must weight them."""
    positives = [c for c in CORPUS if c.expects_self]
    assert len(NEGATIVE_CASES) > len(positives)


@pytest.mark.unit
@pytest.mark.parametrize("case", CORPUS, ids=lambda c: c.name)
def test_observe_mode_agrees_with_enforce_and_mutates_nothing(case: CorpusCase):
    """Observe is the pre-activation instrument: its verdict must match what enforce would do,
    or the observation window proves nothing about the mode that follows it."""
    observed, observed_nodes = _replay(case, SelfBindMode.OBSERVE)
    enforced, _ = _replay(case, SelfBindMode.ENFORCE)

    assert observed.outcome is enforced.outcome
    assert observed.self_uuid == enforced.self_uuid
    # ...while having rewritten nothing.
    assert all(n.uuid.startswith("extracted-") for n in observed_nodes)
    assert observed.bound is False


@pytest.mark.unit
def test_unclassified_self_like_emissions_are_visible_in_the_controls():
    """The activation signal: controls that mention `user` without evidence must be COUNTED, not
    silently ignored, so an operator can see whether untrusted producers still emit self-likes."""
    counted = 0
    for case in NEGATIVE_CASES:
        result, _ = _replay(case, SelfBindMode.ENFORCE)
        counted += result.self_like_without_subject_authority
    assert counted >= 5, "self-like emissions from untrusted producers are not being counted"


@pytest.mark.unit
def test_off_mode_binds_nothing_anywhere_in_the_corpus():
    for case in CORPUS:
        result, nodes = _replay(case, SelfBindMode.OFF)
        assert result.bound is False
        assert all(n.uuid.startswith("extracted-") for n in nodes)
