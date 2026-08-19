"""CF-17: apex-tier grounding must distinguish assertion from mention, and reject contradiction.

The decision plan's test matrix, executed. Every DENY row is a case the previous gate ADMITTED at
confidence 1.0.
"""

from __future__ import annotations

import pytest

from menhir.domain.truth.admission_gate import evaluate_user_tier_claim
from menhir.domain.truth.assertion_spans import (
    claim_is_grounded,
    extract_assertion_spans,
    is_plain_assertion,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# D1 -- token overlap admitted contradictions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claimed,source,why",
    [
        ("the deploy failed", "the deploy succeeded", "antonym, N=3"),
        ("the server is down", "the server is up", "antonym"),
        ("deploy failed", "deploy succeeded", "antonym at the N=2 boundary"),
        ("i own 100 coins", "i own 900 coins", "numeral substitution, 3 chars"),
        ("i own 10 coins", "i own 90 coins", "numeral dropped by the old length filter"),
        ("the deploy did not fail", "the deploy failed", "one-sided negation"),
        ("failed the deploy", "the deploy failed", "token reordering"),
    ],
)
def test_cf17_d1_a_claim_contradicting_its_source_is_denied(
    claimed: str, source: str, why: str
) -> None:
    """The exact invariant: for a claim with N >= 2 retained tokens, if all but one occur in the
    source, overlap = N-1 >= 0.5N -- so EVERY single-word contradiction of any multi-token claim
    was admitted at the apex tier, whatever the substituted word's polarity."""
    assert claim_is_grounded(claimed, source) is False, why


def test_cf17_d1_numeric_prefix_collision_is_denied() -> None:
    """The predecessor this gate copied its pattern from had the same hazard -- `'100' in
    '$1000'` -- and the adjacent helper in that module was digit-boundary guarded while the one
    the gate copied was not."""
    assert claim_is_grounded("$100", "the budget is $1000") is False


# ---------------------------------------------------------------------------
# D2 -- substring admitted mention as assertion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,why",
    [
        ("If the deploy failed, roll back.", "conditional"),
        ("Alice claimed the deploy failed.", "attribution"),
        ('I copied the text "the deploy failed" from the incident report.', "quotation"),
        ("We are testing whether the deploy failed.", "indirect question"),
        ("The deploy might have failed.", "modality"),
        ("It is false that the deploy failed.", "denial via complement clause"),
        ("Did the deploy fail?", "direct question"),
        ("I heard the deploy failed.", "perception verb"),
        ("I think the deploy failed.", "hedge"),
        ("Suppose the deploy failed.", "supposition"),
    ],
)
def test_cf17_d2_text_that_mentions_without_asserting_is_denied(source: str, why: str) -> None:
    """Each of these contains the claim verbatim while asserting nothing of the kind. None
    involves an antonym or a negation, which is why the two guard-based fixes that preceded this
    one could not catch them -- and why removing the overlap branch alone was insufficient."""
    assert claim_is_grounded("the deploy failed", source) is False, why


# ---------------------------------------------------------------------------
# What must still ground -- a fix that denies everything is not a fix
# ---------------------------------------------------------------------------


def test_cf17_exact_match_grounds() -> None:
    assert claim_is_grounded("the deploy failed", "the deploy failed") is True


def test_cf17_case_and_whitespace_are_normalized() -> None:
    assert claim_is_grounded("The Deploy   Failed", "the deploy failed") is True


def test_cf17_a_sentence_of_a_longer_prompt_grounds() -> None:
    """This is the whole point of doing the span work rather than shipping strict equality: under
    equality alone a memory had to equal the ENTIRE user prompt, so apex tier was effectively
    unreachable for any real turn."""
    prompt = "I use Postgres 16. The deploy failed and I rolled back."
    assert claim_is_grounded("I use Postgres 16", prompt) is True
    assert claim_is_grounded("the deploy failed and I rolled back", prompt) is True


def test_cf17_terminal_punctuation_does_not_block_a_match() -> None:
    assert claim_is_grounded("I use Postgres 16.", "I use Postgres 16. Something else.") is True


def test_cf17_whole_text_equality_survives_as_the_floor() -> None:
    """Option 1 of the decision plan remains reachable even when NO span qualifies, so a filter
    miss degrades to the behaviour already verified safe rather than to the defect."""
    prompt = "Alice said the deploy failed."
    assert extract_assertion_spans(prompt) == ()
    assert claim_is_grounded(prompt, prompt) is True


# ---------------------------------------------------------------------------
# The extractor's own contract
# ---------------------------------------------------------------------------


def test_cf17_extraction_refuses_what_it_cannot_vouch_for() -> None:
    text = "I use Postgres 16.4 is fine. Alice said it broke. The deploy failed."
    assert extract_assertion_spans(text) == (
        "i use postgres 16.4 is fine",
        "the deploy failed",
    )


def test_cf17_a_decimal_is_not_a_sentence_boundary() -> None:
    """Over-splitting is not merely a usability problem: a fragment of a sentence can be an
    assertion the user never made."""
    assert extract_assertion_spans("Postgres 16.4 is deployed") == ("postgres 16.4 is deployed",)


def test_cf17_newline_separated_lines_are_separate_spans() -> None:
    """Prompts are often line-broken lists with no terminal punctuation at all."""
    assert extract_assertion_spans("I use Postgres\nI deploy on Friday") == (
        "i use postgres",
        "i deploy on friday",
    )


@pytest.mark.parametrize(
    "sentence",
    [
        'He said "it broke"',
        "The list is: a, b, c",
        "Is the deploy broken?",
        "What broke",
        "It might be broken",
        "I assume it broke",
        "The thing that broke was the deploy",
        "It broke because the disk filled",
        "x" * 201,
        "",
        "   ",
    ],
)
def test_cf17_disqualified_sentences_yield_no_span(sentence: str) -> None:
    assert is_plain_assertion(sentence) is False


@pytest.mark.parametrize(
    "sentence",
    [
        "the deploy failed",
        "I use Postgres 16",
        "the deploy did not fail",
        "I rolled back the release",
    ],
)
def test_cf17_plain_assertions_are_admitted(sentence: str) -> None:
    """Negation is deliberately NOT disqualifying: 'the deploy did not fail' asserts its negation
    perfectly well, and under equality it can only ever ground a claim that says the same thing."""
    assert is_plain_assertion(sentence) is True


def test_cf17_extraction_is_deterministic_and_order_preserving() -> None:
    text = "The deploy failed. I use Postgres. The deploy failed."
    assert extract_assertion_spans(text) == ("the deploy failed", "i use postgres")
    assert extract_assertion_spans(text) == extract_assertion_spans(text)


# ---------------------------------------------------------------------------
# End to end through the gate -- the other gates must be untouched
# ---------------------------------------------------------------------------


def _evidence(text: str) -> dict[str, str]:
    return {
        "turn_id": "turn-1",
        "role": "user",
        "declarant": "user",
        "text": text,
        "session_id": "sess-1",
        "namespace": "ns-1",
    }


def _verdict(claimed: str, evidence_text: str):
    return evaluate_user_tier_claim(
        requested_source="user",
        turn_evidence=_evidence(evidence_text),
        claimed_text=claimed,
        session_id="sess-1",
        namespace="ns-1",
    )


def test_cf17_gate_denies_a_contradiction_end_to_end() -> None:
    verdict = _verdict("the deploy failed", "the deploy succeeded")
    assert verdict.granted is False
    assert verdict.effective_source == "agent_inference"


def test_cf17_gate_denies_an_attribution_end_to_end() -> None:
    verdict = _verdict("the deploy failed", "Alice claimed the deploy failed.")
    assert verdict.granted is False
    assert verdict.effective_source == "agent_inference"


def test_cf17_gate_grants_a_genuine_span_end_to_end() -> None:
    verdict = _verdict("I use Postgres 16", "I use Postgres 16. The deploy failed.")
    assert verdict.granted is True
    assert verdict.effective_source == "user"


def test_cf17_the_bounding_gates_are_unchanged() -> None:
    """These are what held this finding at High rather than Critical, and the decision plan is
    explicit that they must not be modified."""
    assert evaluate_user_tier_claim(
        requested_source="user",
        turn_evidence=None,
        claimed_text="the deploy failed",
        session_id="sess-1",
        namespace="ns-1",
    ).granted is False

    assert evaluate_user_tier_claim(
        requested_source="user",
        turn_evidence={**_evidence("the deploy failed"), "role": "assistant"},
        claimed_text="the deploy failed",
        session_id="sess-1",
        namespace="ns-1",
    ).granted is False

    assert evaluate_user_tier_claim(
        requested_source="user",
        turn_evidence={**_evidence("the deploy failed"), "session_id": "other"},
        claimed_text="the deploy failed",
        session_id="sess-1",
        namespace="ns-1",
    ).granted is False

    assert evaluate_user_tier_claim(
        requested_source="user",
        turn_evidence={**_evidence("the deploy failed"), "namespace": "other"},
        claimed_text="the deploy failed",
        session_id="sess-1",
        namespace="ns-1",
    ).granted is False

    # A non-apex source is passed through untouched.
    assert evaluate_user_tier_claim(
        requested_source="claude-code",
        turn_evidence=None,
        claimed_text="anything",
        session_id=None,
        namespace=None,
    ).granted is True
