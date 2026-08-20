"""Event-history perception (Phase 3) — offline unit tests, no live model/network/db.

A fake `llm_complete` returns canned JSON so the admission parser and builder are exercised
deterministically. Proves the fail-closed contract: envelopes-only completeness (one per episode,
machine-checkable), registry canonicalization to ``acquired``, exact unique quote grounding with real
offsets, deterministic object-key normalization, conservative intent/plan/hypothetical/
recommendation/negation/possession vetoes, a completed-acquisition evidence gate, and a builder that
uses explicit world time or the episode reference — never ``learned_at`` — and abstains when neither
is usable. No persistence, no projection, no wiring."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.domain.event_history import TypedEventAssertion
from menhir.services.event_history_perception import (
    CANONICAL_PREDICATE_ACQUIRED,
    EVENT_SYSTEM_PROMPT,
    EventAssertionBuildResult,
    EventPerceptionProposal,
    build_event_assertion,
    extract_events_once,
)


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows) -> "object":
    """A fake LlmComplete returning `rows` as a JSON array; records the system prompt it was given."""
    calls: list[tuple[str, str]] = []

    def complete(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps(rows)

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def _event(**over):
    base = dict(
        episode=0, subject="user", predicate="bought", object="notebook",
        object_display="", domain="", when="", stated_span="bought my new notebook",
    )
    base.update(over)
    return base


# --------------------------------------------------------------------------- envelope completeness


@pytest.mark.unit
def test_empty_episodes_short_circuits():
    assert extract_events_once([], _llm([{"episode": 0, "events": []}])) == []


@pytest.mark.unit
def test_every_episode_envelope_completeness_and_explicit_empty_coverage():
    episodes = [
        _Ep(uuid="ep-0", content="Nothing acquired here."),
        _Ep(uuid="ep-1", content="I bought a notebook for work."),
    ]
    rows = [
        {"episode": 0, "events": []},
        {"episode": 1, "events": [_event(episode=1, stated_span="bought a notebook")]},
    ]
    drops: list[str] = []
    out = extract_events_once(episodes, _llm(rows), on_drop=drops.append)
    assert len(out) == 1
    assert out[0].episode_uuid == "ep-1"
    assert out[0].predicate == CANONICAL_PREDICATE_ACQUIRED
    assert drops == []  # every episode covered, nothing missing


@pytest.mark.unit
def test_single_bare_envelope_is_accepted():
    episode = [_Ep(uuid="ep-0", content="I got a pen yesterday.")]
    out = extract_events_once(
        episode, _llm({"episode": 0, "events": [_event(object="pen", stated_span="got a pen")]})
    )
    assert len(out) == 1
    assert out[0].predicate == CANONICAL_PREDICATE_ACQUIRED


@pytest.mark.unit
def test_duplicate_envelope_drops_and_missing_coverage_audited():
    episodes = [_Ep(uuid="ep-0", content="I bought a pen."), _Ep(uuid="ep-1", content="I bought a book.")]
    rows = [
        {"episode": 0, "events": [_event(stated_span="bought a pen")]},
        {"episode": 0, "events": [_event(stated_span="bought a pen", object="pencil")]},
    ]
    drops: list[str] = []
    out = extract_events_once(episodes, _llm(rows), on_drop=drops.append)
    assert out == []  # a structural envelope defect invalidates the ENTIRE sample
    assert drops == ["duplicate_episode_envelope", "missing_episode_envelope"]


@pytest.mark.unit
def test_missing_envelope_is_audited():
    episodes = [_Ep(uuid="ep-0", content="I bought a pen."), _Ep(uuid="ep-1", content="I bought a book.")]
    rows = [{"episode": 0, "events": [_event(stated_span="bought a pen")]}]
    drops: list[str] = []
    out = extract_events_once(episodes, _llm(rows), on_drop=drops.append)
    assert out == []  # missing coverage invalidates the ENTIRE sample
    assert drops == ["missing_episode_envelope"]


@pytest.mark.unit
def test_flat_event_array_is_rejected_as_not_envelopes():
    episode = [_Ep(uuid="ep-0", content="I bought a pen.")]
    drops: list[str] = []
    assert extract_events_once(episode, _llm([_event(stated_span="bought a pen")]), on_drop=drops.append) == []
    assert drops == ["bad_episode_envelope", "missing_episode_envelope"]


@pytest.mark.unit
def test_malformed_envelope_is_rejected():
    episode = [_Ep(uuid="ep-0", content="I bought a pen.")]
    drops: list[str] = []
    assert extract_events_once(episode, _llm([{"episode": 0}]), on_drop=drops.append) == []
    assert drops == ["bad_episode_envelope", "missing_episode_envelope"]


# --------------------------------------------------------------------------- registry canonicalization


@pytest.mark.unit
@pytest.mark.parametrize("surface", ["purchased", "bought", "got", "acquired"])
def test_registry_aliases_canonicalize_to_acquired(surface):
    episode = [_Ep(uuid="ep-0", content=f"I {surface} a thing.")]
    out = extract_events_once(episode, _llm([{"episode": 0, "events": [_event(predicate=surface, object="thing", stated_span=f"{surface} a thing")]}]))
    assert len(out) == 1
    assert out[0].predicate == CANONICAL_PREDICATE_ACQUIRED


# --------------------------------------------------------------------------- acceptance / vetoes


@pytest.mark.unit
def test_neutral_possessive_new_is_accepted():
    episode = [_Ep(uuid="ep-0", content="I love my new notebook.")]
    out = extract_events_once(episode, _llm([{"episode": 0, "events": [_event(object="my new notebook", object_display="my new notebook", stated_span="my new notebook")]}]))
    assert len(out) == 1
    assert out[0].predicate == CANONICAL_PREDICATE_ACQUIRED
    # 'my new notebook' -> determiner + acquisition-status 'new' stripped from the identity key,
    # while the natural phrase is preserved in object_display.
    assert out[0].object_key == "notebook"
    assert out[0].object_display == "my new notebook"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("I want a new camera", "intent_or_hypothetical"),          # intent
        ("I plan to buy a house", "intent_or_hypothetical"),        # plans
        ("maybe I should get a dog", "intent_or_hypothetical"),     # hypothetical
        ("I recommend the blender", "intent_or_hypothetical"),      # recommendation
        ("I did not buy the couch", "negated_claim"),               # negation
        ("I've got a camera", "possession_not_acquisition"),        # have-got possession
    ],
)
def test_intent_plan_hypothetical_recommendation_negation_possession_rejected(quote, expected):
    episode = [_Ep(uuid="ep-0", content=quote)]
    drops: list[str] = []
    assert extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(stated_span=quote)]}]), on_drop=drops.append
    ) == []
    assert drops == [expected]


@pytest.mark.unit
def test_hallucinated_event_over_arbitrary_prose_rejected_by_evidence_gate():
    episode = [_Ep(uuid="ep-0", content="I went for a long walk today.")]
    drops: list[str] = []
    assert extract_events_once(
        episode,
        _llm([{"episode": 0, "events": [_event(stated_span="I went for a long walk today", object="camera")]}]),
        on_drop=drops.append,
    ) == []
    assert drops == ["no_completed_acquisition_evidence"]


@pytest.mark.unit
def test_object_not_grounded_rejects_unrelated_object():
    """A valid acquisition quote whose object does not appear in the quote is fail-closed."""
    episode = [_Ep(uuid="ep-0", content="I bought a pen.")]
    drops: list[str] = []
    assert extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(object="camera", stated_span="bought a pen")]}]), on_drop=drops.append
    ) == []
    assert drops == ["object_not_grounded"]


@pytest.mark.unit
def test_object_grounding_word_boundary_rejects_pen_over_pencil():
    """CF-123: substring-only grounding admitted object='pen' over 'I bought a pencil'; word-boundary
    grounding must reject it as not the same object."""
    episode = [_Ep(uuid="ep-0", content="I bought a pencil.")]
    drops: list[str] = []
    assert extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(object="pen", stated_span="bought a pencil")]}]), on_drop=drops.append
    ) == []
    assert drops == ["object_not_grounded"]


@pytest.mark.unit
def test_object_grounding_word_boundary_accepts_full_object():
    """CF-123 positive control: the same quote with the true object is still admitted, so word-boundary
    grounding did not reject everything."""
    episode = [_Ep(uuid="ep-0", content="I bought a pencil.")]
    out = extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(object="pencil", stated_span="bought a pencil")]}])
    )
    assert len(out) == 1
    assert out[0].object_key == "pencil"


@pytest.mark.unit
def test_bare_negation_rejects_possessive_new_with_non_registry_verb():
    """CF-124: a negated NON-registry verb (never received) plus possessive-new is NOT admitted as a
    completed acquisition — the bare negator refuses the possessive-new limb."""
    episode = [_Ep(uuid="ep-0", content="I never received my new notebook.")]
    drops: list[str] = []
    assert extract_events_once(
        episode,
        _llm([{"episode": 0, "events": [_event(object="my new notebook", object_display="my new notebook", stated_span="never received my new notebook")]}]),
        on_drop=drops.append,
    ) == []
    assert drops == ["no_completed_acquisition_evidence"]


@pytest.mark.unit
def test_possessive_new_without_negation_is_admitted():
    """CF-124 positive control: possessive-new WITHOUT a negator is still admitted, so the limb works."""
    episode = [_Ep(uuid="ep-0", content="I received my new notebook.")]
    out = extract_events_once(
        episode,
        _llm([{"episode": 0, "events": [_event(object="my new notebook", object_display="my new notebook", stated_span="received my new notebook")]}]),
    )
    assert len(out) == 1
    assert out[0].object_key == "notebook"


@pytest.mark.unit
def test_object_grounding_strips_leading_determiners():
    """Object with a differing article still grounds when the noun phrase occurs in the quote."""
    episode = [_Ep(uuid="ep-0", content="I bought the camera yesterday.")]
    out = extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(object="a camera", stated_span="bought the camera")]}]),
    )
    assert len(out) == 1
    # 'a camera' / 'the camera' share the stable identity key 'camera'; display keeps the phrase.
    assert out[0].object_key == "camera"
    assert out[0].object_display == "a camera"


# --------------------------------------------------------------------------- fail-closed row rejection


@pytest.mark.unit
def test_unknown_predicate_is_rejected():
    episode = [_Ep(uuid="ep-0", content="I borrowed a book.")]
    drops: list[str] = []
    assert extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(predicate="borrowed", stated_span="borrowed a book")]}]), on_drop=drops.append
    ) == []
    assert drops == ["unknown_predicate"]


@pytest.mark.unit
def test_blank_object_is_rejected():
    episode = [_Ep(uuid="ep-0", content="I bought a thing.")]
    drops: list[str] = []
    assert extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(object="", stated_span="bought a thing")]}]), on_drop=drops.append
    ) == []
    assert drops == ["missing_required_field"]


@pytest.mark.unit
def test_missing_and_duplicate_quote_are_rejected():
    episode = [_Ep(uuid="ep-0", content="I bought a pen and I bought a pen.")]
    drops: list[str] = []
    # duplicate occurrence -> ambiguous which one is meant
    assert extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(stated_span="I bought a pen")]}]), on_drop=drops.append
    ) == []
    assert drops == ["span_not_uniquely_located"]

    # zero occurrences -> ungrounded
    drops.clear()
    assert extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(stated_span="I bought a hat")]}]), on_drop=drops.append
    ) == []
    assert drops == ["span_not_uniquely_located"]


@pytest.mark.unit
def test_malformed_explicit_when_is_rejected():
    episode = [_Ep(uuid="ep-0", content="I bought a pen.")]
    drops: list[str] = []
    assert extract_events_once(
        episode, _llm([{"episode": 0, "events": [_event(when="not-a-date", stated_span="bought a pen")]}]), on_drop=drops.append
    ) == []
    assert drops == ["malformed_when"]


# --------------------------------------------------------------------------- grounding / normalization


@pytest.mark.unit
def test_exact_span_offsets_and_deterministic_object_key():
    episode = [_Ep(uuid="ep-0", content="Last week I bought my New   Notebook for work.")]
    out = extract_events_once(
        episode,
        _llm([{"episode": 0, "events": [_event(object="New   Notebook", object_display="my new notebook", stated_span="bought my New   Notebook")]}]),
    )
    assert len(out) == 1
    p = out[0]
    assert episode[0].content[p.span_start:p.span_end] == "bought my New   Notebook"
    assert p.span_start >= 0 and p.span_end == p.span_start + len("bought my New   Notebook")
    assert p.claim_ordinal == 0
    # object identity is deterministically normalized (trim/lower/collapse whitespace, then strip a
    # leading article/possessive determiner AND acquisition-status 'new'); display kept separate
    assert p.object_key == "notebook"
    assert p.object_display == "my new notebook"
    assert p.domain is None  # absent/blank domain -> None


# --------------------------------------------------------------------------- builder


def _proposal(**over):
    base = dict(
        subject_text="user", predicate=CANONICAL_PREDICATE_ACQUIRED,
        object_key="notebook", object_display="notebook", stated_span="bought a notebook",
        episode_uuid="ep-0", span_start=0, span_end=len("bought a notebook"),
        domain=None, when=None,
    )
    base.update(over)
    return EventPerceptionProposal(**base)


@pytest.mark.unit
def test_builder_uses_explicit_when_not_learned_at():
    p = _proposal(when="2026-07-01T00:00:00+00:00")
    res = build_event_assertion(
        p, subject_uuid="u-1", namespace="n", learned_at="2026-08-01T00:00:00+00:00",
        episode_reference_time="2026-06-01T00:00:00+00:00", turn_evidence_uuid="te-1", perceiver_version="v1",
    )
    assert isinstance(res, EventAssertionBuildResult)
    assert res.built and res.reason is None
    assert res.assertion is not None
    assert res.assertion.valid_at == "2026-07-01T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"
    assert res.assertion.evidence_tier == "agent"


@pytest.mark.unit
def test_builder_falls_back_to_episode_reference_time():
    p = _proposal(when=None)
    res = build_event_assertion(
        p, subject_uuid="u-1", namespace="n", learned_at="2026-08-01T00:00:00+00:00",
        episode_reference_time="2026-06-15T00:00:00+00:00", turn_evidence_uuid="te-1", perceiver_version="v1",
    )
    assert res.built
    assert res.assertion is not None
    assert res.assertion.valid_at == "2026-06-15T00:00:00+00:00"
    assert res.assertion.time_basis == "episode_reference"


@pytest.mark.unit
def test_builder_abstains_no_valid_time_and_never_uses_learned_at_as_valid_at():
    p = _proposal(when=None)
    res = build_event_assertion(
        p, subject_uuid="u-1", namespace="n", learned_at="2026-08-01T00:00:00+00:00",
        episode_reference_time=None, turn_evidence_uuid="te-1", perceiver_version="v1",
    )
    assert not res.built
    assert res.assertion is None
    assert res.reason == "no_valid_time"


@pytest.mark.unit
def test_source_key_parity_between_proposal_and_built_assertion():
    episode = [_Ep(uuid="ep-0", content="I bought a notebook yesterday.")]
    out = extract_events_once(episode, _llm([{"episode": 0, "events": [_event(stated_span="bought a notebook", when="2026-07-01")]}]))
    assert len(out) == 1
    p = out[0]
    res = build_event_assertion(
        p, subject_uuid="u-1", namespace="n", learned_at="2026-08-01T00:00:00+00:00",
        episode_reference_time="2026-06-15T00:00:00+00:00", turn_evidence_uuid="te-1", perceiver_version="v1",
    )
    assert isinstance(res.assertion, TypedEventAssertion)
    assert res.assertion.source_key == p.source_key


# --------------------------------------------------------------------------- prompt


@pytest.mark.unit
def test_prompt_is_generic_and_has_no_benchmark_ids():
    prompt = EVENT_SYSTEM_PROMPT
    assert "EXACTLY ONE envelope for EVERY numbered episode" in prompt
    assert "never invent or infer an object or time" in prompt
    assert "my new notebook" in prompt       # possessive-new evidence explicitly allowed
    assert "considering a new notebook" in prompt  # ...while considering/plans rejected
    assert "recommendations" in prompt
    # no benchmark-specific vocabulary or fixture ids
    assert "70-200mm" not in prompt
    assert "kitchen" not in prompt
    assert "lme-" not in prompt
    assert "41698283" not in prompt
    assert "0977f2af" not in prompt
