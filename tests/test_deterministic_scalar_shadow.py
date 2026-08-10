"""Phase 2A shadow audit behavior of TypedScalarPerceptionService.

Covers: flag off never runs the deterministic extractor; flag on runs it exactly once while the
LLM k-sample gate, persisted decisions, call counts, and returned results stay identical;
quote-free comparison payload fields; exact vs aligned agreement; router-missed counting only
within fully eligible episodes; bounded/truncated summaries; and fail-open when the
deterministic extractor raises. All injection-based; no network or Neo4j.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from menhir.domain.scalar_identity import RELATION_TYPES
from menhir.domain.typed_assertion import build_source_key
from menhir.services.deterministic_scalar_extractor import DeterministicScalarExtractor
from menhir.services.typed_scalar_service import (
    TypedScalarPerceptionService,
    _COMPOSITIONAL_REASON_CODES,
    _COMPOSITIONAL_STATUSES,
    _MISMATCH_DIMENSIONS,
    _aligned_shadow_match,
    _compare_deterministic_shadow,
    _identity_mismatch_dimensions,
    _unique_episode_source_map,
)
from menhir.services.structural_scalar_composer import compose_structural_scalar_identity
from menhir.services.typed_scalar_rules import TypedScalarProposal


class _FakeAdapter:
    def activate_scalar_state(self):
        return {"ok": True}

    def scalar_state_schema_ready(self):
        return True

    def fetch_linked_entities_for_episode(self, episode_uuid):
        return []

    def record_typed_assertion(self, **kwargs):
        return SimpleNamespace(uuid="assertion-1")

    def mark_projection_complete(self, ids):
        return None

    def ensure_self_entity(self, namespace):
        return None

    def pending_advisory_assertions(self, **kwargs):
        return []


class _FakeScalarStateService:
    def rebuild_scalar_state(self, subject_uuid, **kwargs):
        return None

    def rebuild_scalar_projections(self, subject_uuid, **kwargs):
        return None


class _CountingLlm:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return self._response


def _episode(uuid, content):
    return SimpleNamespace(uuid=uuid, content=content, reference_time=None)


def _observation(subject="user", attribute="coins", value_kind="count", operation="absolute",
                 value=37, stated_span="I have 37 coins", scope="", unit=""):
    return {"subject": subject, "attribute": attribute, "scope": scope,
            "value_kind": value_kind, "unit": unit, "operation": operation,
            "value": value, "stated_span": stated_span}


def _llm_response(observations_by_episode, episode_count):
    envelopes = [
        {"episode": i, "observations": observations_by_episode.get(i, [])}
        for i in range(episode_count)
    ]
    return json.dumps(envelopes)


@pytest.fixture
def audit_events(monkeypatch):
    events = []
    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.is_enabled", lambda: True)

    def _audit(event, state, **kwargs):
        events.append({"event": event, "state": state, **kwargs})

    monkeypatch.setattr("menhir.infrastructure.consolidation_audit.audit", _audit)
    return events


@pytest.fixture
def persisted_decisions(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "menhir.services.typed_scalar_service.bind_and_persist_typed_scalars",
        lambda decisions, **kwargs: persisted.append(decisions) or {"bound": 1, "advisory": 0})
    return persisted


def _make_service(shadow=False):
    return TypedScalarPerceptionService(
        _FakeAdapter(), _FakeScalarStateService(),
        perceiver_version="v1", deterministic_shadow_enabled=shadow)


def _run(service, episodes, llm, k=3, threshold=1.0, namespace="ns-test", canonical_self=False):
    return service.perceive_and_persist(
        episodes, llm, k=k, threshold=threshold, namespace=namespace,
        episode_reference_time=lambda uuid: None, canonical_self=canonical_self)


def _shadow_event(audit_events):
    return [e for e in audit_events if e["event"] == "deterministic_shadow"][0]


# ------------------------------------------------------------------------------------------------ #
# Flag off: deterministic extractor never runs; everything else unchanged
# ------------------------------------------------------------------------------------------------ #


def test_flag_off_never_calls_deterministic_extractor(
        monkeypatch, audit_events, persisted_decisions):
    def _explode(*args, **kwargs):
        raise AssertionError("deterministic extractor must not run when shadow is off")

    monkeypatch.setattr(
        "menhir.services.typed_scalar_service.DeterministicScalarExtractor", _explode)

    episodes = [_episode("ep-1", "I have 37 coins.")]
    llm = _CountingLlm(_llm_response({0: [_observation()]}, 1))
    result = _run(_make_service(shadow=False), episodes, llm)

    assert llm.calls == 3
    assert result["bound"] == 1
    assert result["committed"] == 1
    assert len(persisted_decisions) == 1
    assert persisted_decisions[0][0].committed is True
    assert not [e for e in audit_events if e["event"] == "deterministic_shadow"]


# ------------------------------------------------------------------------------------------------ #
# Flag on: extractor once, LLM k unchanged, identical decisions/result, shadow audit emitted
# ------------------------------------------------------------------------------------------------ #


def test_flag_on_runs_shadow_once_and_keeps_llm_path_identical(
        monkeypatch, audit_events, persisted_decisions):
    episodes = [_episode("ep-1", "I have 37 coins.")]
    response = _llm_response({0: [_observation()]}, 1)
    extractor_calls = []

    class _SpyExtractor:
        def __init__(self, *args, **kwargs):
            extractor_calls.append(1)

        def extract(self, episodes):
            return DeterministicScalarExtractor().extract(episodes)

    monkeypatch.setattr(
        "menhir.services.typed_scalar_service.DeterministicScalarExtractor", _SpyExtractor)

    off_llm = _CountingLlm(response)
    off_result = _run(_make_service(shadow=False), episodes, off_llm)
    off_decisions = list(persisted_decisions)

    persisted_decisions.clear()
    on_llm = _CountingLlm(response)
    on_result = _run(_make_service(shadow=True), episodes, on_llm)

    assert len(extractor_calls) == 1
    assert off_llm.calls == 3 and on_llm.calls == 3
    assert on_result == off_result
    assert persisted_decisions == off_decisions
    event = _shadow_event(audit_events)
    assert event["state"] == "ok"
    assert event["namespace"] == "ns-test"


# ------------------------------------------------------------------------------------------------ #
# Audit payload: expected fields, quote-free by contract
# ------------------------------------------------------------------------------------------------ #


def test_shadow_audit_payload_fields_and_quote_free(
        monkeypatch, audit_events, persisted_decisions):
    episodes = [_episode("ep-1", "I have 37 coins.")]
    llm = _CountingLlm(_llm_response({0: [_observation(stated_span="I have 37 coins")]}, 1))
    _run(_make_service(shadow=True), episodes, llm)

    event = _shadow_event(audit_events)
    assert event["state"] == "ok"
    details = event["details"]
    assert details["schema_version"] == 2
    assert details["extractor_version"] == "det-v0.1"
    assert details["template_version"] == "templates-v0.1"
    assert details["episodes_total"] == 1
    assert details["episodes_fully_eligible"] == 1
    assert details["proposals_all"] == 1
    assert details["proposals_router_eligible"] == 1
    assert details["committed_llm"] == 1
    assert details["exact_agreements"] == 1
    assert details["aligned_agreements"] == 1
    assert details["router_missed_llm_claims"] == 0
    assert details["deterministic_class_counts"] == {"c_count": 1}
    assert details["deterministic_outcome_counts"] == {"admitted": 1}
    assert details["deterministic_drop_reason_counts"] == {}
    assert details["source_summaries_truncated"] == 0
    assert details["candidate_summaries_truncated"] == 0
    assert details["episode_summaries_truncated"] == 0
    # quote-free contract: no stated_span field and no episode content anywhere in the payload
    blob = json.dumps(details)
    assert "stated_span" not in blob
    assert "I have 37 coins" not in blob
    source = details["source_summaries"][0]
    candidate = details["candidate_summaries"][0]
    assert source["source_key"] == candidate["source_key"]
    assert source["exact_matched"] is True
    assert source["aligned_matched"] is True
    assert source["normalized_value"] == "37"
    assert candidate["operation"] == "absolute"


def test_episode_summary_eligibility_uses_authoritative_membership():
    extraction = DeterministicScalarExtractor().extract(
        [_episode("ep-1", "I have 37 coins.")])
    episode = replace(extraction.episode_receipts[0], fully_covered=False)
    extraction = replace(
        extraction,
        episode_receipts=(episode,),
        fully_eligible_episode_uuids=("ep-1",),
    )

    details = _compare_deterministic_shadow(extraction, [])

    assert details["episode_summaries"] == [{
        "episode_uuid": "ep-1",
        "fully_eligible": True,
        "reason_counts": {},
        "admitted_count": 1,
        "dropped_count": 0,
    }]


def test_admitted_candidate_summary_uses_proposal_identity_and_value():
    extraction = DeterministicScalarExtractor().extract(
        [_episode("ep-1", "I have 37 coins.")])
    original_episode = extraction.episode_receipts[0]
    original_candidate = original_episode.candidate_receipts[0]
    proposal = replace(
        original_candidate.proposal,
        episode_uuid="proposal-episode",
        span_start=101,
        span_end=117,
        attribute="proposal_weight",
        scope="proposal_scope",
        value_kind="measurement",
        unit="kg",
        operation="delta",
        value=12.5,
    )
    candidate = replace(
        original_candidate,
        source_start=1,
        source_end=2,
        subject_text="receipt-only subject",
        attribute="receipt_attribute",
        scope="receipt_scope",
        value_kind="count",
        unit="receipt_unit",
        operation="absolute",
        value=999,
        proposal=proposal,
    )
    episode = replace(
        original_episode,
        episode_uuid="parent-episode",
        candidate_receipts=(candidate,),
    )
    extraction = replace(
        extraction,
        episode_receipts=(episode,),
        proposals=(proposal,),
        fully_eligible_episode_uuids=("parent-episode",),
    )

    summary = _compare_deterministic_shadow(extraction, [])[
        "candidate_summaries"][0]

    assert summary["episode_uuid"] == "proposal-episode"
    assert summary["source_key"] == proposal.source_key
    assert (summary["span_start"], summary["span_end"]) == (101, 117)
    assert summary["attribute"] == "proposal_weight"
    assert summary["scope"] == "proposal_scope"
    assert summary["value_kind"] == "measurement"
    assert summary["unit"] == "kg"
    assert summary["operation"] == "delta"
    assert summary["normalized_value"] == proposal.normalized_value
    assert "receipt-only subject" not in json.dumps(summary)


def test_dropped_candidate_summary_uses_parent_episode_and_receipt_fields():
    extraction = DeterministicScalarExtractor().extract(
        [_episode("ep-1", "I have 37 coins. I have 37 coins.")])
    original_episode = extraction.episode_receipts[0]
    original_candidate = original_episode.candidate_receipts[0]
    assert original_candidate.proposal is None
    candidate = replace(
        original_candidate,
        episode_index=42,
        source_start=11,
        source_end=14,
        attribute="receipt_attribute",
        scope="receipt_scope",
        value_kind="count",
        unit="receipt_unit",
        operation="expire",
        value=999,
    )
    episode = replace(
        original_episode,
        episode_uuid="parent-episode",
        candidate_receipts=(candidate,),
    )
    extraction = replace(extraction, episode_receipts=(episode,))

    summary = _compare_deterministic_shadow(extraction, [])[
        "candidate_summaries"][0]

    assert summary["episode_uuid"] == "parent-episode"
    assert summary["source_key"] == build_source_key("parent-episode", 11, 14, 0)
    assert summary["span_start"] == 11
    assert summary["span_end"] == 14
    assert summary["operation"] == "expire"
    assert summary["attribute"] == "receipt_attribute"
    assert summary["scope"] == "receipt_scope"
    assert summary["value_kind"] == "count"
    assert summary["unit"] == "receipt_unit"
    assert summary["normalized_value"] == "999"


def test_shadow_audit_does_not_copy_free_text_subject(
        monkeypatch, audit_events, persisted_decisions):
    episodes = [_episode("ep-1", "I have 37 coins.")]
    llm = _CountingLlm(_llm_response({
        0: [_observation(subject="private subject from the transcript")],
    }, 1))
    _run(_make_service(shadow=True), episodes, llm)

    details = _shadow_event(audit_events)["details"]
    assert "private subject from the transcript" not in json.dumps(details)


def test_aligned_vs_exact_agreement_distinction(
        monkeypatch, audit_events, persisted_decisions):
    # the LLM quote includes the terminal period (span 0..16); the deterministic extractor
    # quotes the minimal span (0..15): same claim, different source_key -> aligned, not exact
    episodes = [_episode("ep-1", "I have 37 coins.")]
    llm = _CountingLlm(_llm_response({0: [_observation(stated_span="I have 37 coins.")]}, 1))
    _run(_make_service(shadow=True), episodes, llm)

    details = _shadow_event(audit_events)["details"]
    assert details["committed_llm"] == 1
    assert details["exact_agreements"] == 0
    assert details["aligned_agreements"] == 1
    assert details["router_missed_llm_claims"] == 0
    source = details["source_summaries"][0]
    assert source["exact_matched"] is False
    assert source["aligned_matched"] is True
    compositional = details["compositional"]
    diagnostic = compositional["diagnostic_vs_llm"]
    assert diagnostic["compositional_exact_agreements"] == 0
    assert diagnostic["compositional_aligned_agreements"] == 1
    assert compositional["pair_summaries"][0]["status"] == "compositional_aligned"


def test_canonical_self_is_used_for_shadow_comparison(
        monkeypatch, audit_events, persisted_decisions):
    episodes = [_episode("ep-1", "I have 37 coins.")]
    llm = _CountingLlm(_llm_response({0: [_observation(subject="I")]}, 1))
    _run(_make_service(shadow=True), episodes, llm, canonical_self=True)

    details = _shadow_event(audit_events)["details"]
    assert details["exact_agreements"] == 1
    assert details["aligned_agreements"] == 1


def test_compositional_sidecar_always_canonicalizes_self_independent_of_raw_flag():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    llm = replace(extraction.proposals[0], subject_text="I")

    details = _compare_deterministic_shadow(
        extraction,
        [llm],
        canonical_self=False,
        source_by_episode={"ep-1": source},
    )
    diagnostic = details["compositional"]["diagnostic_vs_llm"]

    assert details["exact_agreements"] == 0
    assert details["aligned_agreements"] == 0
    assert diagnostic["compositional_exact_agreements"] == 1
    assert diagnostic["compositional_aligned_agreements"] == 1


def test_compositional_agreement_ignores_free_text_attribute_label():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    llm = replace(extraction.proposals[0], attribute="coin_count")

    details = _compare_deterministic_shadow(
        extraction, [llm], source_by_episode={"ep-1": source})
    diagnostic = details["compositional"]["diagnostic_vs_llm"]

    assert details["exact_agreements"] == 0
    assert details["aligned_agreements"] == 0
    assert diagnostic["compositional_exact_agreements"] == 1
    assert diagnostic["compositional_aligned_agreements"] == 1
    assert diagnostic["identity_disagreements"] == 0


def test_compositional_identity_disagreement_is_not_unresolved():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    llm = replace(extraction.proposals[0], scope="collection")

    details = _compare_deterministic_shadow(
        extraction, [llm], source_by_episode={"ep-1": source})
    compositional = details["compositional"]
    diagnostic = compositional["diagnostic_vs_llm"]

    assert diagnostic["identity_disagreements"] == 1
    assert diagnostic["compositional_unresolved_pairs"] == 0
    assert compositional["status_counts"] == {"identity_disagreement": 1}
    assert compositional["pair_summaries"][0]["mismatch_dimensions"] == ("scope",)
    assert (
        compositional["pair_summaries"][0]["det_semantic_hash"]
        != compositional["pair_summaries"][0]["llm_semantic_hash"]
    )


def test_compositional_unresolved_precedes_identity_disagreement():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    llm = replace(extraction.proposals[0], operation="delta")

    details = _compare_deterministic_shadow(
        extraction, [llm], source_by_episode={"ep-1": source})
    compositional = details["compositional"]
    diagnostic = compositional["diagnostic_vs_llm"]

    assert diagnostic["compositional_unresolved_pairs"] == 1
    assert diagnostic["identity_disagreements"] == 0
    assert compositional["pair_summaries"][0]["status"] == "unresolved"
    assert compositional["pair_summaries"][0]["llm_reason"] == "struct.operation_unsupported"


def test_diagnostic_llm_router_miss_counts_unmatched_eligible_deterministic_claim():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])

    details = _compare_deterministic_shadow(
        extraction, [], source_by_episode={"ep-1": source})
    diagnostic = details["compositional"]["diagnostic_vs_llm"]

    assert details["router_missed_llm_claims"] == 0
    assert diagnostic["diagnostic_llm_router_misses"] == 1
    assert diagnostic["unjoinable_deterministic_claims"] == 1


def test_compositional_metrics_are_one_to_one_for_duplicate_llm_claims():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    proposal = extraction.proposals[0]

    details = _compare_deterministic_shadow(
        extraction, [proposal, proposal], source_by_episode={"ep-1": source})
    diagnostic = details["compositional"]["diagnostic_vs_llm"]

    assert diagnostic["compositional_exact_agreements"] == 1
    assert diagnostic["compositional_aligned_agreements"] == 1
    assert diagnostic["unjoinable_llm_claims"] == 1


def test_compositional_pairing_prefers_semantic_matches_before_disagreements():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    base = extraction.proposals[0]
    det_a = replace(base, scope="a")
    det_b = replace(base, scope="b")
    extraction = replace(extraction, proposals=(det_a, det_b))
    llm_b = replace(base, scope="b")
    llm_a = replace(base, scope="a")

    details = _compare_deterministic_shadow(
        extraction, [llm_b, llm_a], source_by_episode={"ep-1": source})
    compositional = details["compositional"]
    diagnostic = compositional["diagnostic_vs_llm"]

    assert diagnostic["comparison_pairs"] == 2
    assert diagnostic["compositional_exact_agreements"] == 2
    assert diagnostic["compositional_aligned_agreements"] == 2
    assert diagnostic["identity_disagreements"] == 0
    assert compositional["status_counts"] == {"compositional_exact": 2}


def test_missing_or_duplicate_episode_source_fails_composition_closed():
    source = "I have 37 coins."
    episode = _episode("ep-1", source)
    extraction = DeterministicScalarExtractor().extract([episode])
    proposal = extraction.proposals[0]

    details = _compare_deterministic_shadow(extraction, [proposal])

    assert _unique_episode_source_map([episode, _episode("ep-1", source)]) == {}
    assert details["exact_agreements"] == 1
    assert details["compositional"]["deterministic_unresolved"] == 1
    assert details["compositional"]["llm_unresolved"] == 1
    assert details["compositional"]["diagnostic_vs_llm"][
        "compositional_unresolved_pairs"] == 1


def test_compositional_payload_is_bounded_to_hashes_and_closed_diagnostics():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    proposal = extraction.proposals[0]

    compositional = _compare_deterministic_shadow(
        extraction, [proposal], source_by_episode={"ep-1": source})["compositional"]
    row = compositional["pair_summaries"][0]
    forbidden_keys = {
        "target", "subject", "attribute", "scope", "unit", "value", "normalized_value",
        "stated_span", "episode_uuid", "source_key",
    }

    def _keys(value):
        if isinstance(value, dict):
            return set(value).union(*( _keys(item) for item in value.values()))
        if isinstance(value, (list, tuple)):
            return set().union(*(_keys(item) for item in value)) if value else set()
        return set()

    assert forbidden_keys.isdisjoint(_keys(compositional))
    for key in (
        "det_source_hash", "llm_source_hash", "det_semantic_hash", "llm_semantic_hash",
        "det_claim_hash", "llm_claim_hash",
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", row[key])
    assert "coins" not in json.dumps(compositional)
    assert '"37"' not in json.dumps(compositional)
    assert compositional["promotion_status"] == "not_evaluable"
    assert row["status"] in _COMPOSITIONAL_STATUSES
    assert row["det_relation"] in RELATION_TYPES
    assert row["llm_relation"] in RELATION_TYPES
    assert set(row["mismatch_dimensions"]) <= set(_MISMATCH_DIMENSIONS)
    assert row["det_reason"] is None or row["det_reason"] in _COMPOSITIONAL_REASON_CODES
    assert row["llm_reason"] is None or row["llm_reason"] in _COMPOSITIONAL_REASON_CODES
    repeated = _compare_deterministic_shadow(
        extraction, [proposal], source_by_episode={"ep-1": source})["compositional"]
    assert repeated == compositional


def test_all_identity_mismatch_dimensions_are_closed_enum_names():
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    identity = compose_structural_scalar_identity(
        extraction.proposals[0], source).identity
    assert identity is not None
    different = replace(
        identity,
        relation_type="state",
        target_or_scope=("tokens", "collection"),
        value_kind="measurement",
        value="38",
        unit="kg",
        operation="delta",
        effective_time="2026-01-03T00:00:00Z",
    )

    assert _identity_mismatch_dimensions(identity, different) == (
        "relation", "target", "scope", "value_kind", "value", "unit", "operation",
        "effective_time",
    )


def test_compositional_pair_summaries_are_bounded_before_emission(monkeypatch):
    import menhir.services.typed_scalar_service as service_module

    monkeypatch.setattr(service_module, "_SHADOW_SOURCE_SUMMARY_LIMIT", 1)
    source = "I have 37 coins. I have 12 books."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])

    compositional = _compare_deterministic_shadow(
        extraction,
        list(extraction.proposals),
        source_by_episode={"ep-1": source},
    )["compositional"]

    assert compositional["diagnostic_vs_llm"]["comparison_pairs"] == 2
    assert compositional["diagnostic_vs_llm"]["compositional_exact_agreements"] == 2
    assert len(compositional["pair_summaries"]) == 1
    assert compositional["pair_summaries_truncated"] == 1


def test_composer_exception_degrades_only_compositional_metrics(monkeypatch):
    source = "I have 37 coins."
    extraction = DeterministicScalarExtractor().extract([_episode("ep-1", source)])
    proposal = extraction.proposals[0]
    monkeypatch.setattr(
        "menhir.services.typed_scalar_service.compose_structural_scalar_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    details = _compare_deterministic_shadow(
        extraction, [proposal], source_by_episode={"ep-1": source})

    assert details["exact_agreements"] == 1
    assert details["aligned_agreements"] == 1
    assert details["compositional"]["deterministic_unresolved_reason_counts"] == {
        "struct.composer_error": 1,
    }
    assert details["compositional"]["llm_unresolved_reason_counts"] == {
        "struct.composer_error": 1,
    }


def test_aligned_match_rejects_punctuation_only_intersection():
    deterministic = TypedScalarProposal(
        subject_text="user", attribute="coins", scope="", value_kind="count", unit="",
        operation="absolute", value=37, stated_span="coins.", episode_uuid="ep-1",
        span_start=10, span_end=16,
    )
    llm = TypedScalarProposal(
        subject_text="user", attribute="coins", scope="", value_kind="count", unit="",
        operation="absolute", value=37, stated_span=".", episode_uuid="ep-1",
        span_start=15, span_end=16,
    )
    assert _aligned_shadow_match(deterministic, llm) is False


def test_shadow_agreement_is_one_to_one():
    episode = _episode("ep-1", "I have 37 coins.")
    extraction = DeterministicScalarExtractor().extract([episode])
    proposal = extraction.proposals[0]
    details = _compare_deterministic_shadow(extraction, [proposal, proposal])

    assert details["committed_llm"] == 2
    assert details["exact_agreements"] == 1
    assert details["aligned_agreements"] == 1
    assert details["router_missed_llm_claims"] == 1
    assert [row["aligned_matched"] for row in details["source_summaries"]] == [True, False]


# ------------------------------------------------------------------------------------------------ #
# Router-missed claims count only inside fully eligible episodes
# ------------------------------------------------------------------------------------------------ #


def test_router_missed_counts_only_within_eligible_episodes(
        monkeypatch, audit_events, persisted_decisions):
    episodes = [
        _episode("ep-1", "I have 37 coins."),
        _episode("ep-2", "I have 37 coins in a game."),
    ]
    observations = {
        # ep-1 is router-eligible; the LLM reads the slot under a different name -> missed
        0: [_observation(attribute="coin_count", stated_span="I have 37 coins")],
        # ep-2 is NOT eligible (unconsumed context); even an unmatched claim must not count
        1: [_observation(attribute="coin_count", stated_span="I have 37 coins in a game")],
    }
    llm = _CountingLlm(_llm_response(observations, 2))
    _run(_make_service(shadow=True), episodes, llm)

    details = _shadow_event(audit_events)["details"]
    assert details["episodes_total"] == 2
    assert details["episodes_fully_eligible"] == 1
    assert details["proposals_all"] == 2
    assert details["proposals_router_eligible"] == 1
    assert details["committed_llm"] == 2
    assert details["exact_agreements"] == 0
    assert details["aligned_agreements"] == 0
    assert details["router_missed_llm_claims"] == 1
    summaries = {s["episode_uuid"]: s for s in details["source_summaries"]}
    assert summaries["ep-1"]["aligned_matched"] is False
    assert summaries["ep-2"]["aligned_matched"] is False


# ------------------------------------------------------------------------------------------------ #
# Bounded/truncated summaries
# ------------------------------------------------------------------------------------------------ #


def test_shadow_summaries_bounded_with_truncation_counts(
        monkeypatch, audit_events, persisted_decisions):
    import menhir.services.typed_scalar_service as service_module

    monkeypatch.setattr(service_module, "_SHADOW_SOURCE_SUMMARY_LIMIT", 1)
    monkeypatch.setattr(service_module, "_SHADOW_CANDIDATE_SUMMARY_LIMIT", 1)
    monkeypatch.setattr(service_module, "_SHADOW_EPISODE_SUMMARY_LIMIT", 1)

    episodes = [_episode("ep-1", "I have 37 coins. I have 12 books. I weigh 70 kg.")]
    observations = {
        0: [
            _observation(attribute="coins", value=37, stated_span="I have 37 coins"),
            _observation(attribute="books", value=12, stated_span="I have 12 books"),
            _observation(attribute="weight", value_kind="measurement", unit="kg", value=70,
                         stated_span="I weigh 70 kg"),
        ]
    }
    llm = _CountingLlm(_llm_response(observations, 1))
    _run(_make_service(shadow=True), episodes, llm)

    details = _shadow_event(audit_events)["details"]
    assert details["committed_llm"] == 3
    assert len(details["source_summaries"]) == 1
    assert details["source_summaries_truncated"] == 2
    assert len(details["candidate_summaries"]) == 1
    assert details["candidate_summaries_truncated"] == 2
    assert len(details["episode_summaries"]) == 1
    assert details["episode_summaries_truncated"] == 0


# ------------------------------------------------------------------------------------------------ #
# Fail-open: deterministic extractor raising never changes the LLM path
# ------------------------------------------------------------------------------------------------ #


def test_extractor_raising_fail_open(monkeypatch, audit_events, persisted_decisions):
    class _BoomExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, episodes):
            raise RuntimeError("deterministic extractor exploded")

    monkeypatch.setattr(
        "menhir.services.typed_scalar_service.DeterministicScalarExtractor", _BoomExtractor)

    episodes = [_episode("ep-1", "I have 37 coins.")]
    response = _llm_response({0: [_observation()]}, 1)
    off_llm = _CountingLlm(response)
    off_result = _run(_make_service(shadow=False), episodes, off_llm)
    off_decisions = list(persisted_decisions)

    persisted_decisions.clear()
    on_llm = _CountingLlm(response)
    on_result = _run(_make_service(shadow=True), episodes, on_llm)

    assert on_result == off_result
    assert persisted_decisions == off_decisions
    assert on_llm.calls == 3 and off_llm.calls == 3
    event = _shadow_event(audit_events)
    assert event["state"] == "error"
    assert event["details"]["schema_version"] == 2
    assert event["details"]["error"] == "deterministic_shadow_failed"
    assert event["details"]["compositional"]["evaluation_status"] == "shadow_error"
    assert event["details"]["compositional"]["promotion_status"] == "not_evaluable"
    assert event["details"]["compositional"]["pair_summaries"] == []
