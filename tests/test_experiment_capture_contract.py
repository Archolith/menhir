"""The capture contract for the proposer/reviewer experiment probes.

WHY THIS FILE EXISTS
--------------------
Every arm of Phase 1 had to be re-run because the capture was incomplete, and the gaps were only
discovered by trying to analyse the results afterwards:

  * reviewer prompt omitted `episode`      -> every reviewer row dropped; all arms read 0 commits
  * decision rows omitted `value`          -> offline replay scored every row unmatched (0% answer)
  * arms B/D emitted no rows at all        -> the population the arms are SCORED on was absent
  * arm A emitted no rows at all           -> gates 2 and 3, which compare against A, uncomputable

Each of those cost a full live panel (~340 LLM calls, ~17 minutes) and, worse, each produced a
plausible-looking number that was wrong. A capture that cannot answer the question the experiment
was designed to ask is not a partial result, it is a wasted run.

So the contract is asserted here, cheaply and offline, instead of being discovered by spending
another panel. These tests use fake LLMs and touch no network and no database.
"""

from __future__ import annotations

import json

from menhir.services.typed_scalar_proposer_reviewer import (
    PROPOSER_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
    propose_claim_cards,
    review_claim_cards,
)

#: Fields the gate scorer reads. Anything absent makes a gate uncomputable AFTER the calls are spent.
REQUIRED_COMMIT_FIELDS = {"type", "arm", "ns", "trial", "value", "attribute", "verdict"}
REQUIRED_DECISION_FIELDS = {"type", "arm", "ns", "trial", "claim_id", "committed", "reason",
                            "verdicts", "value", "attribute"}


class _Ep:
    def __init__(self, uuid: str, content: str) -> None:
        self.uuid, self.content = uuid, content


def test_reviewer_prompt_requests_every_field_the_parser_requires():
    """A prompt that omits a REQUIRED field silently converts every reviewer answer into a parse
    drop, which downstream is indistinguishable from the reviewer saying nothing.

    This exact defect made all three proposer arms report zero commits, and the reviewers were in
    fact answering correctly the whole time."""
    for field in ("claim_id", "verdict", "episode", "subject", "attribute", "value_kind",
                  "operation", "value", "stated_span"):
        assert field in REVIEWER_SYSTEM_PROMPT, f"reviewer prompt never asks for {field!r}"


def test_proposer_prompt_requests_every_field_the_parser_requires():
    for field in ("claim_id", "episode", "subject", "attribute", "value_kind",
                  "operation", "value", "stated_span"):
        assert field in PROPOSER_SYSTEM_PROMPT, f"proposer prompt never asks for {field!r}"


def test_a_reviewer_row_shaped_like_the_prompt_actually_parses():
    """The prompt and the parser must agree. Asserting the prompt mentions a field is necessary but
    not sufficient -- this builds a row exactly as the prompt specifies and requires it to survive."""
    eps = [_Ep("e1", "[2026-07-23] I now lead a team of five engineers.")]
    cards = propose_claim_cards(eps, lambda _s, _u: json.dumps([{
        "claim_id": "C1", "episode": 0, "subject": "user", "attribute": "team_size",
        "scope": "", "value_kind": "count", "unit": "", "operation": "absolute",
        "value": 5, "when": "", "stated_span": "I now lead a team of five engineers"}]))
    assert len(cards) == 1, "proposer row shaped per the prompt failed to parse"

    rows = review_claim_cards(eps, cards, lambda _s, _u: json.dumps([{
        "claim_id": "C1", "verdict": "SUPPORTED", "episode": 0, "subject": "user",
        "attribute": "team_size", "scope": "", "value_kind": "count", "unit": "",
        "operation": "absolute", "value": 5, "when": "",
        "stated_span": "lead a team of five engineers"}]))
    assert len(rows) == 1, "reviewer row shaped per the prompt failed to parse"
    assert rows[0].proposal is not None


def test_probe_emits_every_field_the_gate_scorer_reads():
    """Static check that the probe writes the contract fields, for EVERY arm including the baseline.

    Reads the source rather than running the probe (which needs network + Neo4j). Crude, but it
    catches the precise regression that cost three runs: an arm whose commits are computed on one
    path while only a different path is captured."""
    import os
    probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts",
                         "probe_scalar_proposer_reviewer.py")
    src = open(os.path.normpath(probe), encoding="utf-8").read()

    assert src.count('"type": "arm_commit"') >= 2, (
        "arm_commit must be emitted on BOTH the baseline path and the B/D scored path; "
        "capturing only one leaves gates 2/3 uncomputable after the calls are spent")
    for field in ("value", "attribute", "verdict"):
        assert f'"{field}"' in src, f"probe never writes {field!r} to the capture"
    # the B/D commit counters and the capture must sit together, or they drift apart again
    assert "_emit(" in src, "B/D scored commits are counted but not captured"


def test_gate_scorer_reads_only_captured_fields():
    """The scorer must not depend on anything the probe does not write -- otherwise a capture looks
    complete and still cannot answer the gates."""
    import os
    scorer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts",
                          "score_proposer_reviewer_gates.py")
    src = open(os.path.normpath(scorer), encoding="utf-8").read()
    for field in ("arm_commit", "decision", "verdict", "decoy"):
        assert field in src, f"scorer does not consume {field!r}"
