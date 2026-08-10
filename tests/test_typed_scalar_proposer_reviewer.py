"""Phase 0 of the proposer/reviewer vocabulary experiment -- deterministic offline fixtures.

Plan: `.agent/plans/menhir-proposer-reviewer-vocabulary-experiment-plan.md`, "Test Matrix / Phase 0".

Gate for proceeding to live trials: ZERO false commits and ZERO distinct-claim collisions. Every
test here is fake-LLM driven, so a failure is a design defect and never a sampling artifact.

The protocol under test is asymmetric by construction (one proposer names things, two reviewers
verify them), which makes acquiescence the danger rather than disagreement. Several fixtures below
exist only to prove the gate still refuses when refusing is expensive.
"""

from __future__ import annotations

import json

import pytest

from menhir.services.typed_scalar_proposer_reviewer import (
    CORRECTED,
    SUPPORTED,
    UNLISTED_CLAIM,
    UNSUPPORTED,
    gate_reviewed_cards,
    propose_claim_cards,
    review_claim_cards,
)


class _Ep:
    def __init__(self, uuid: str, content: str) -> None:
        self.uuid, self.content = uuid, content


def _llm(payload) -> "callable":
    """Fake LLM returning a fixed JSON array regardless of prompt."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda _s, _u: text


def _card(claim_id, attribute, value, span, *, episode=0, subject="user",
          kind="count", operation="absolute", scope="", unit=""):
    return {"claim_id": claim_id, "episode": episode, "subject": subject,
            "attribute": attribute, "scope": scope, "value_kind": kind, "unit": unit,
            "operation": operation, "value": value, "when": "", "stated_span": span}


def _review(claim_id, verdict, **kw):
    row = {"claim_id": claim_id, "verdict": verdict}
    row.update(kw)
    return row


def _run(episodes, proposer_rows, reviewer_rows_a, reviewer_rows_b):
    cards = propose_claim_cards(episodes, _llm(proposer_rows))
    ra = review_claim_cards(episodes, cards, _llm(reviewer_rows_a), order=None)
    rb = review_claim_cards(episodes, cards, _llm(reviewer_rows_b), order=None)
    return cards, gate_reviewed_cards(cards, [ra, rb])


# --- fixture 1 -------------------------------------------------------------------------------
def test_same_fact_three_attribute_names_commits_once():
    """THE POINT OF THE EXPERIMENT. Three samples naming one fact three ways is the dominant
    residual failure (26 of 35 aligned heads). Under the current gate this scatters and abstains;
    here the proposer fixes the label and the reviewers verify the FACT, so it commits once."""
    eps = [_Ep("e1", "[2026-07-23] I now lead a team of five engineers.")]
    span = "I now lead a team of five engineers"
    cards, decisions = _run(
        eps,
        [_card("C1", "team_size", 5, span)],
        # reviewers keep the offered label (as instructed) but re-read the fact themselves
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="team_size",
                 scope="", value_kind="count", unit="", operation="absolute", value=5,
                 when="", stated_span="lead a team of five engineers")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="team_size",
                 scope="", value_kind="count", unit="", operation="absolute", value=5,
                 when="", stated_span="a team of five engineers")],
    )
    assert len(cards) == 1
    assert [d.committed for d in decisions] == [True]
    # and note the reviewers grounded DIFFERENT spans -- correspondence is by claim_id, not offsets
    assert decisions[0].proposal.attribute == "team_size"


# --- fixture 2 -------------------------------------------------------------------------------
def test_nested_and_overlapping_spans_do_not_block_commit():
    """Span fragmentation (43.4% of the absence band) must not defeat this protocol: reviewers are
    expected to quote different extents of the same clause."""
    eps = [_Ep("e1", "[2026-07-23] After the promotion I now manage 12 people at the studio.")]
    cards, decisions = _run(
        eps,
        [_card("C1", "reports_count", 12, "I now manage 12 people")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="reports_count",
                 scope="", value_kind="count", unit="", operation="absolute", value=12,
                 when="", stated_span="manage 12 people at the studio")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="reports_count",
                 scope="", value_kind="count", unit="", operation="absolute", value=12,
                 when="", stated_span="I now manage 12 people at the studio")],
    )
    assert [d.committed for d in decisions] == [True]


# --- fixture 3 -------------------------------------------------------------------------------
def test_same_value_distinct_claims_stay_separate():
    """THE NEGATIVE CONTROL that makes namespace-wide attribute reconciliation unsafe: two unrelated
    facts that happen to share the value 30. They must remain two claims and must never merge."""
    eps = [_Ep("e1", "[2026-07-23] I keep 30 eggs in the cold room and 30 milk cartons out front.")]
    cards, decisions = _run(
        eps,
        [_card("C1", "eggs_stocked", 30, "30 eggs"),
         _card("C2", "milk_stock", 30, "30 milk cartons")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="eggs_stocked", scope="",
                 value_kind="count", unit="", operation="absolute", value=30, when="",
                 stated_span="30 eggs"),
         _review("C2", SUPPORTED, episode=0, subject="user", attribute="milk_stock", scope="",
                 value_kind="count", unit="", operation="absolute", value=30, when="",
                 stated_span="30 milk cartons")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="eggs_stocked", scope="",
                 value_kind="count", unit="", operation="absolute", value=30, when="",
                 stated_span="30 eggs"),
         _review("C2", SUPPORTED, episode=0, subject="user", attribute="milk_stock", scope="",
                 value_kind="count", unit="", operation="absolute", value=30, when="",
                 stated_span="30 milk cartons")],
    )
    assert len(cards) == 2
    assert [d.committed for d in decisions] == [True, True]
    attrs = {d.proposal.attribute for d in decisions}
    assert attrs == {"eggs_stocked", "milk_stock"}, "distinct claims collapsed into one"


# --- fixture 4 -------------------------------------------------------------------------------
def test_different_values_from_same_passage_abstain():
    eps = [_Ep("e1", "[2026-07-23] I bought 6 books last week and 9 the week before.")]
    _cards, decisions = _run(
        eps,
        [_card("C1", "books_bought", 6, "6 books")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="books_bought", scope="",
                 value_kind="count", unit="", operation="absolute", value=6, when="",
                 stated_span="6 books")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="books_bought", scope="",
                 value_kind="count", unit="", operation="absolute", value=9, when="",
                 stated_span="9 the week before")],
    )
    assert [d.committed for d in decisions] == [False]
    assert decisions[0].reason == "factual_core_disagreement"


# --- fixture 5 -------------------------------------------------------------------------------
def test_plausible_wrong_value_is_rejected_by_reviewers():
    """DECOY. Per plan addendum A3 this is the primary evidence reviewers read the source at all --
    value agreement is uninformative because samples already agree on value ~97% of the time."""
    eps = [_Ep("e1", "[2026-07-23] I have 37 coins in the jar.")]
    _cards, decisions = _run(
        eps,
        [_card("C1", "coins", 73, "37 coins")],   # transposed digits: plausible, wrong
        [_review("C1", CORRECTED, episode=0, subject="user", attribute="coins", scope="",
                 value_kind="count", unit="", operation="absolute", value=37, when="",
                 stated_span="37 coins")],
        [_review("C1", CORRECTED, episode=0, subject="user", attribute="coins", scope="",
                 value_kind="count", unit="", operation="absolute", value=37, when="",
                 stated_span="37 coins")],
    )
    assert [d.committed for d in decisions] == [False]
    # both reviewers AGREE on 37 -- a 2/3 majority the experiment deliberately declines
    assert decisions[0].reason == "reviewer_corrected"


# --- fixture 6 -------------------------------------------------------------------------------
def test_reviewer_unlisted_claim_never_commits():
    """A reviewer-originated fact has exactly one witness. Observable, never committing."""
    eps = [_Ep("e1", "[2026-07-23] I have 37 coins and 4 rare stamps.")]
    cards = propose_claim_cards(eps, _llm([_card("C1", "coins", 37, "37 coins")]))
    rows = review_claim_cards(eps, cards, _llm([
        _review("C1", SUPPORTED, episode=0, subject="user", attribute="coins", scope="",
                value_kind="count", unit="", operation="absolute", value=37, when="",
                stated_span="37 coins"),
        _review(UNLISTED_CLAIM, UNLISTED_CLAIM, episode=0, subject="user", attribute="stamps",
                scope="", value_kind="count", unit="", operation="absolute", value=4, when="",
                stated_span="4 rare stamps"),
    ]))
    assert any(r.claim_id == UNLISTED_CLAIM for r in rows), "unlisted claim must be observable"
    decisions = gate_reviewed_cards(cards, [rows, rows])
    assert {d.claim_id for d in decisions} == {"C1"}, "unlisted claim must not become a decision"


# --- fixture 7 -------------------------------------------------------------------------------
@pytest.mark.parametrize("second_verdict,expected", [
    (CORRECTED, "reviewer_corrected"),
    (UNSUPPORTED, "reviewer_unsupported"),
])
def test_one_rubber_stamp_one_dissent_abstains(second_verdict, expected):
    """Asymmetric protocols fail toward agreement. One dissenter must be enough to stop a commit."""
    eps = [_Ep("e1", "[2026-07-23] I have 37 coins in the jar.")]
    supported = _review("C1", SUPPORTED, episode=0, subject="user", attribute="coins", scope="",
                        value_kind="count", unit="", operation="absolute", value=37, when="",
                        stated_span="37 coins")
    dissent = _review("C1", second_verdict, episode=0, subject="user", attribute="coins", scope="",
                      value_kind="count", unit="", operation="absolute", value=12, when="",
                      stated_span="37 coins")
    _cards, decisions = _run(eps, [_card("C1", "coins", 37, "37 coins")], [supported], [dissent])
    assert [d.committed for d in decisions] == [False]
    assert decisions[0].reason == expected


# --- fixture 8 -------------------------------------------------------------------------------
def test_two_legitimate_claims_in_one_sentence_do_not_collapse():
    eps = [_Ep("e1", "[2026-07-23] I walk 3 miles each morning and swim 2 km each evening.")]
    cards, decisions = _run(
        eps,
        [_card("C1", "walk_distance", 3, "walk 3 miles", kind="measurement", unit="miles"),
         _card("C2", "swim_distance", 2, "swim 2 km", kind="measurement", unit="km")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="walk_distance", scope="",
                 value_kind="measurement", unit="miles", operation="absolute", value=3, when="",
                 stated_span="walk 3 miles"),
         _review("C2", SUPPORTED, episode=0, subject="user", attribute="swim_distance", scope="",
                 value_kind="measurement", unit="km", operation="absolute", value=2, when="",
                 stated_span="swim 2 km")],
        [_review("C1", SUPPORTED, episode=0, subject="user", attribute="walk_distance", scope="",
                 value_kind="measurement", unit="miles", operation="absolute", value=3, when="",
                 stated_span="walk 3 miles"),
         _review("C2", SUPPORTED, episode=0, subject="user", attribute="swim_distance", scope="",
                 value_kind="measurement", unit="km", operation="absolute", value=2, when="",
                 stated_span="swim 2 km")],
    )
    assert len(cards) == 2
    assert [d.committed for d in decisions] == [True, True]
    assert {d.proposal.unit for d in decisions} == {"miles", "km"}


# --- fixture 9 -------------------------------------------------------------------------------
def test_reviewer_emitting_two_readings_for_one_card_abstains():
    """Ambiguous correspondence must never resolve by choosing one reading."""
    eps = [_Ep("e1", "[2026-07-23] I had 20 coins, but now I have 37 coins.")]
    supported = [
        _review("C1", SUPPORTED, episode=0, subject="user", attribute="coins", scope="",
                value_kind="count", unit="", operation="absolute", value=37, when="",
                stated_span="now I have 37 coins"),
        _review("C1", SUPPORTED, episode=0, subject="user", attribute="coins", scope="",
                value_kind="count", unit="", operation="absolute", value=20, when="",
                stated_span="I had 20 coins"),
    ]
    clean = [_review("C1", SUPPORTED, episode=0, subject="user", attribute="coins", scope="",
                     value_kind="count", unit="", operation="absolute", value=37, when="",
                     stated_span="now I have 37 coins")]
    _cards, decisions = _run(eps, [_card("C1", "coins", 37, "now I have 37 coins")],
                             supported, clean)
    assert [d.committed for d in decisions] == [False]
    assert decisions[0].reason == "reviewer_conflicted"


# --- fixture 10 ------------------------------------------------------------------------------
def test_identical_rerun_is_deterministic():
    """Same inputs -> same decisions and same trace, so captured JSONL can be replayed against
    changed gate logic without LLM access (plan: Validation)."""
    eps = [_Ep("e1", "[2026-07-23] I now lead a team of five engineers.")]
    args = (eps,
            [_card("C1", "team_size", 5, "I now lead a team of five engineers")],
            [_review("C1", SUPPORTED, episode=0, subject="user", attribute="team_size", scope="",
                     value_kind="count", unit="", operation="absolute", value=5, when="",
                     stated_span="lead a team of five engineers")],
            [_review("C1", SUPPORTED, episode=0, subject="user", attribute="team_size", scope="",
                     value_kind="count", unit="", operation="absolute", value=5, when="",
                     stated_span="a team of five engineers")])
    first = _run(*args)[1]
    second = _run(*args)[1]
    assert [(d.claim_id, d.committed, d.reason, d.verdicts, d.labels) for d in first] == \
           [(d.claim_id, d.committed, d.reason, d.verdicts, d.labels) for d in second]


# --- protocol integrity ----------------------------------------------------------------------
def test_reviewer_verdict_for_unknown_card_is_dropped():
    """A reviewer must not be able to conjure a claim by inventing an id."""
    eps = [_Ep("e1", "[2026-07-23] I have 37 coins in the jar.")]
    cards = propose_claim_cards(eps, _llm([_card("C1", "coins", 37, "37 coins")]))
    rows = review_claim_cards(eps, cards, _llm([
        _review("C9", SUPPORTED, episode=0, subject="user", attribute="ghost", scope="",
                value_kind="count", unit="", operation="absolute", value=1, when="",
                stated_span="37 coins")]))
    assert rows == []


def test_supported_without_a_valid_own_reading_is_not_support():
    """The card must never become its own evidence: a SUPPORTED row whose reading fails grounding
    is dropped, so the card loses that reviewer and abstains."""
    eps = [_Ep("e1", "[2026-07-23] I have 37 coins in the jar.")]
    cards = propose_claim_cards(eps, _llm([_card("C1", "coins", 37, "37 coins")]))
    rows = review_claim_cards(eps, cards, _llm([
        _review("C1", SUPPORTED, episode=0, subject="user", attribute="coins", scope="",
                value_kind="count", unit="", operation="absolute", value=37, when="",
                stated_span="a span that does not occur in the episode")]))
    assert rows == []
    decisions = gate_reviewed_cards(cards, [rows, rows])
    assert decisions[0].committed is False
    assert decisions[0].reason == "reviewer_omitted_card"


def test_duplicate_claim_ids_are_dropped():
    """A colliding handle would silently merge two claims -- the collision the plan forbids."""
    eps = [_Ep("e1", "[2026-07-23] I keep 30 eggs and 12 crates.")]
    cards = propose_claim_cards(eps, _llm([
        _card("C1", "eggs_stocked", 30, "30 eggs"),
        _card("C1", "crates", 12, "12 crates"),
    ]))
    assert [c.proposal.attribute for c in cards] == ["eggs_stocked"]


def test_proposer_rows_obey_production_admission_rules():
    """Arms must not be more permissive than the baseline they are scored against: an ungrounded
    span and a hedged value are refused here exactly as in `extract_typed_scalars_once`."""
    eps = [_Ep("e1", "[2026-07-23] I have about 30 books. I said it. I said it.")]
    cards = propose_claim_cards(eps, _llm([
        _card("C1", "books", 30, "I have about 30 books"),   # hedged
        _card("C2", "z", 1, "I said it."),                   # occurs twice -> not unique
    ]))
    assert cards == []
