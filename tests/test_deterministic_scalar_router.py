from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from menhir.services.deterministic_scalar_extractor import DeterministicScalarExtractor
from menhir.services.deterministic_scalar_router import (
    ROUTE_DETERMINISTIC,
    ROUTE_LLM_REVIEW,
    DeterministicScalarRouter,
)


def _episodes(*rows: tuple[str, str]) -> list[SimpleNamespace]:
    return [SimpleNamespace(uuid=uuid, content=content) for uuid, content in rows]


def test_three_way_partition_preserves_order_and_truthful_decision_contract() -> None:
    episodes = _episodes(
        ("det", "I have 4 coins."),
        ("content", "No scalar claim here."),
        ("review", "Maybe I have 4 coins."),
    )
    extraction = DeterministicScalarExtractor().extract(episodes)
    routed = DeterministicScalarRouter(("c_count",)).route(episodes, extraction)
    assert routed.failure is None
    assert [item.route for item in routed.routes] == [ROUTE_DETERMINISTIC, ROUTE_LLM_REVIEW, ROUTE_LLM_REVIEW]
    decisions = DeterministicScalarRouter._to_decisions(routed)
    assert len(decisions) == 1
    assert decisions[0].committed and decisions[0].k == 1
    assert decisions[0].agreement == 1.0
    assert decisions[0].reason == "deterministic_scalar_committed:c_count"
    assert decisions[0].veto == "deterministic_admission"
    assert decisions[0].distribution == {"deterministic_scalar": 1}


def test_duplicate_uuid_or_tampered_proposal_fails_closed_to_llm_review() -> None:
    episodes = _episodes(("dup", "I have 4 coins."), ("dup", "I have 5 coins."))
    extraction = DeterministicScalarExtractor().extract(episodes)
    routed = DeterministicScalarRouter().route(episodes, extraction)
    assert routed.failure is not None
    assert all(item.route == ROUTE_LLM_REVIEW for item in routed.routes)


def test_empty_allowlist_reviews_scalar_and_zero_proposal_content() -> None:
    episodes = _episodes(("det", "I have 4 coins."), ("content", "No scalar claim here."))
    extraction = DeterministicScalarExtractor().extract(episodes)
    routed = DeterministicScalarRouter().route(episodes, extraction)
    assert [item.route for item in routed.routes] == [ROUTE_LLM_REVIEW, ROUTE_LLM_REVIEW]
    promoted = DeterministicScalarRouter(("c_count",)).route(episodes, extraction)
    assert [item.route for item in promoted.routes] == [ROUTE_DETERMINISTIC, ROUTE_LLM_REVIEW]


def test_mixed_promoted_and_unpromoted_classes_block_whole_episode() -> None:
    episodes = _episodes(("multi", "I have 4 coins and I wake up at 7:30."))
    extraction = DeterministicScalarExtractor().extract(episodes)
    count_only = DeterministicScalarRouter(("c_count",)).route(episodes, extraction)
    assert count_only.routes[0].route == ROUTE_LLM_REVIEW
    assert count_only.eligible_unpromoted == 1
    assert count_only.mixed_class_blocked == 1
    assert dict(count_only.class_counts).get("c_count", 0) >= 1
    all_promoted = DeterministicScalarRouter(("c_count", "c_clock_time")).route(episodes, extraction)
    assert all_promoted.routes[0].route == ROUTE_DETERMINISTIC


def test_unknown_promotion_or_extracted_class_fails_closed() -> None:
    try:
        DeterministicScalarRouter(("unknown",))
    except ValueError:
        pass
    else:
        raise AssertionError("unknown promotion must fail")
    episodes = _episodes(("one", "I have 4 coins."))
    extraction = DeterministicScalarExtractor().extract(episodes)
    receipt = extraction.episode_receipts[0]
    candidate = replace(receipt.candidate_receipts[0], class_id="unknown")
    bad = replace(extraction, episode_receipts=(replace(receipt, candidate_receipts=(candidate,)),))
    assert DeterministicScalarRouter(("c_count",)).route(episodes, bad).failure is not None

    clean = _episodes(("one", "I have 4 coins."))
    valid = DeterministicScalarExtractor().extract(clean)
    proposal = valid.proposals[0]
    forged = replace(proposal, value=99)
    tampered = replace(valid, proposals=(forged,))
    failed = DeterministicScalarRouter().route(clean, tampered)
    assert failed.failure is not None
    assert failed.deterministic_proposals == ()
    malformed = replace(valid, proposals=(replace(proposal, claim_ordinal=1),))
    assert DeterministicScalarRouter().route(clean, malformed).failure is not None


def test_receipt_index_and_admitted_candidate_contract_is_required() -> None:
    episodes = _episodes(("one", "I have 4 coins."))
    extraction = DeterministicScalarExtractor().extract(episodes)
    bad_receipt = replace(extraction.episode_receipts[0], episode_index=1)
    failed = DeterministicScalarRouter().route(episodes, replace(extraction, episode_receipts=(bad_receipt,)))
    assert failed.failure is not None
    assert all(item.route == ROUTE_LLM_REVIEW for item in failed.routes)


def test_stale_eligibility_and_duplicate_admitted_candidates_fail_closed() -> None:
    episodes = _episodes(("one", "I have 4 coins."))
    extraction = DeterministicScalarExtractor().extract(episodes)
    stale = replace(extraction, fully_eligible_episode_uuids=())
    assert DeterministicScalarRouter().route(episodes, stale).failure is not None
    receipt = extraction.episode_receipts[0]
    duplicate = replace(receipt, candidate_receipts=receipt.candidate_receipts + (receipt.candidate_receipts[0],))
    failed = DeterministicScalarRouter().route(episodes, replace(extraction, episode_receipts=(duplicate,)))
    assert failed.failure is not None

    stale_version = replace(extraction, extractor_version="det-v0.0")
    assert DeterministicScalarRouter().route(episodes, stale_version).failure is not None


def test_candidate_receipt_index_outcome_and_class_derivation_are_closed() -> None:
    episodes = _episodes(("one", "I have 4 coins."))
    extraction = DeterministicScalarExtractor().extract(episodes)
    receipt = extraction.episode_receipts[0]
    candidate = receipt.candidate_receipts[0]
    assert DeterministicScalarRouter(("c_count",)).route(
        episodes, replace(extraction, episode_receipts=(replace(receipt, candidate_receipts=(replace(candidate, episode_index=1),)),))
    ).failure is not None
    malformed = replace(candidate, outcome="admitted", proposal=None)
    assert DeterministicScalarRouter(("c_count",)).route(
        episodes, replace(extraction, episode_receipts=(replace(receipt, candidate_receipts=(malformed,)),))
    ).failure is not None
    spoofed = replace(candidate, class_id="c_clock_time")
    assert DeterministicScalarRouter(("c_clock_time",)).route(
        episodes, replace(extraction, episode_receipts=(replace(receipt, candidate_receipts=(spoofed,)),))
    ).failure is not None
    dropped = replace(candidate, outcome="dropped", proposal=None, drop_reason=None)
    bad_drop = replace(receipt, fully_covered=False, reasons=("drop",), candidate_receipts=(dropped,))
    assert DeterministicScalarRouter().route(
        episodes, replace(extraction, episode_receipts=(bad_drop,), proposals=(), fully_eligible_episode_uuids=())
    ).failure is not None


def test_legitimate_derived_classes_are_replay_validated() -> None:
    episodes = _episodes(
        ("correction", "Actually, I weigh 70 kg."),
        ("history", "I used to have 2 coins and I have 4 coins."),
    )
    extraction = DeterministicScalarExtractor().extract(episodes)
    classes = {
        candidate.class_id
        for receipt in extraction.episode_receipts
        for candidate in receipt.candidate_receipts
        if candidate.outcome == "admitted"
    }
    assert {"c_correction", "c_expire", "c_prev_current"}.issubset(classes)
    routed = DeterministicScalarRouter(tuple(sorted(classes))).route(episodes, extraction)
    assert routed.failure is None
    assert all(item.route == "deterministic_scalar" for item in routed.routes)


def test_extract_and_route_owns_exactly_one_extractor_pass(monkeypatch) -> None:
    episodes = _episodes(("one", "I have 4 coins."))
    from menhir.services import deterministic_scalar_router as router_module
    original = router_module.DeterministicScalarExtractor
    calls = []

    class _Spy:
        def extract(self, values):
            calls.append(1)
            return original().extract(values)

    monkeypatch.setattr(router_module, "DeterministicScalarExtractor", _Spy)
    extraction, routed = router_module.DeterministicScalarRouter(("c_count",)).extract_and_route(episodes)
    assert calls == [1]
    assert routed.failure is None
    assert extraction.proposals


def test_public_route_replay_catches_unexpected_extractor_exception(monkeypatch) -> None:
    episodes = _episodes(("one", "I have 4 coins."))
    extraction = DeterministicScalarExtractor().extract(episodes)
    from menhir.services import deterministic_scalar_router as router_module

    class _ExplodingExtractor:
        def extract(self, values):
            raise RuntimeError("replay exploded")

    monkeypatch.setattr(router_module, "DeterministicScalarExtractor", _ExplodingExtractor)
    routed = DeterministicScalarRouter(("c_count",)).route(episodes, extraction)
    assert routed.failure == "deterministic_router_contract_failure:RuntimeError"
    assert [item.route for item in routed.routes] == [ROUTE_LLM_REVIEW]


def test_to_decisions_requires_valid_promoted_class_mapping() -> None:
    episodes = _episodes(("one", "I have 4 coins."))
    _, routed = DeterministicScalarRouter(("c_count",)).extract_and_route(episodes)
    assert DeterministicScalarRouter._to_decisions(routed)

    assert not DeterministicScalarRouter._to_decisions(
        replace(routed, deterministic_class_by_source=())
    )
    assert not DeterministicScalarRouter._to_decisions(
        replace(routed, promoted_class_ids=())
    )
    source_key = routed.deterministic_proposals[0].source_key
    assert not DeterministicScalarRouter._to_decisions(
        replace(routed, deterministic_class_by_source=((source_key, "unknown"),))
    )
