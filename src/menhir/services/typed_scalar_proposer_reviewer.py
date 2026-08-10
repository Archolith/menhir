"""EXPERIMENT ONLY -- asymmetric proposer/reviewer typed-scalar protocol.

Plan: `.agent/plans/menhir-proposer-reviewer-vocabulary-experiment-plan.md`.

NOT WIRED INTO PRODUCTION. Nothing in `menhir` imports this module; the scheduler, the API, and
`gate_typed_scalars` are untouched, so production behaviour is byte-identical while this exists.
A successful experiment earns a separate migration plan -- it does not graduate by being merged.

WHY THIS SHAPE
--------------
Today three independent samples must simultaneously discover a fact, invent a name for it, choose
evidence bounds, and read its value -- and then agree exactly on all four. Measurement says the
discovery and the reading are not the problem: of 35 claims where all k samples provably read the
SAME span and still failed, `attribute` blocked 26 and `value` blocked 1. They agree on the number
and disagree on the name.

So this protocol makes the naming asymmetric while keeping the FACT independent:
  * call 1 (proposer)  emits ephemeral claim cards, fixing the vocabulary and a candidate reading;
  * calls 2,3 (reviewers) see the episodes and the cards, and independently decide whether the
    SOURCE supports each card -- re-emitting the factual fields from their own reading.

The proposer is not authoritative. It supplies filing labels only. A card is a QUESTION, never
evidence, and the prompts say so explicitly.

THE FAILURE MODE THIS MUST NOT HAVE
-----------------------------------
Acquiescence. A reviewer that rubber-stamps every card produces perfect agreement and zero truth.
Note that reviewer value-agreement is NOT evidence against this: samples already agree on value ~97%
of the time when they read the same span, so a rubber-stamp reviewer scores about as well as an
attentive one on that metric. Decoy rejection is the only measurement that separates comprehension
from compliance -- see `plan` addendum A3. Callers MUST run decoys.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from menhir.services.typed_scalar_perception import (
    TypedScalarProposal,
    _interpretation_label,
    _parse_json_array,
    parse_scalar_row,
)

LlmComplete = Callable[[str, str], str]

#: verdicts a reviewer may return for a proposed card.
SUPPORTED = "SUPPORTED"
CORRECTED = "CORRECTED"
UNSUPPORTED = "UNSUPPORTED"
REVIEW_VERDICTS = (SUPPORTED, CORRECTED, UNSUPPORTED)

#: a reviewer-discovered fact the proposer missed. Observable, NEVER committing in this experiment:
#: promoting it would let a single reviewer originate a claim with only one corroborator.
UNLISTED_CLAIM = "UNLISTED_CLAIM"

_CLAIM_ID_RE = re.compile(r"^C\d{1,3}$")


PROPOSER_SYSTEM_PROMPT = """You extract scalar facts a user stated about themselves.

Return a JSON array of claim cards. Each card:
{"claim_id":"C1","episode":<int>,"subject":"user","attribute":"snake_case_name","scope":"",
 "value_kind":"count|money|measurement|duration|frequency|boolean|status|clock_time|weekday",
 "unit":"","operation":"absolute|delta","value":<number|string|bool>,"when":"",
 "stated_span":"<exact substring of that episode>"}

Rules:
- claim_id must be C1, C2, C3... unique within your response.
- stated_span MUST be copied EXACTLY from the episode text and must occur exactly once there.
- Only facts the user stated about themselves. Do not infer, estimate, or combine.
- If the text does not state a scalar value, return [].

Return ONLY the JSON array."""


REVIEWER_SYSTEM_PROMPT = """You verify proposed scalar claims against source text.

You are given episodes and a list of CANDIDATE claim cards. A candidate is a QUESTION, not evidence.
It may be wrong, mislabelled, or entirely unsupported. Rejecting candidates is expected and correct.

For EVERY candidate return one object:
{"claim_id":"C1","verdict":"SUPPORTED|CORRECTED|UNSUPPORTED","episode":<int>,
 "subject":"...","attribute":"...","scope":"","value_kind":"...","unit":"",
 "operation":"absolute|delta","value":<...>,"when":"","stated_span":"<exact substring>"}

- episode is the [n] index of the episode your quote comes from. REQUIRED except for UNSUPPORTED.

- SUPPORTED  : the source states this fact. Still fill in every field FROM YOUR OWN READING.
- CORRECTED  : the source states a related fact but the candidate got something wrong. Give yours.
- UNSUPPORTED: the source does not state this fact. Fields may be omitted.
- Your stated_span must be YOUR OWN exact quote from the episode, occurring exactly once.
- Keep the candidate's attribute name when it fits the fact, so claims can be matched up.

You may also return facts the candidates MISSED, using "claim_id":"UNLISTED_CLAIM" with all fields.

Return ONLY a JSON array."""


@dataclass(frozen=True)
class ClaimCard:
    """A proposer-emitted candidate. `claim_id` is EPHEMERAL -- a correspondence handle for this
    exchange only. It is never persisted and never becomes a durable slot identity; doing so would
    make one stochastic label permanent before any corroboration (see plan, Alternatives)."""

    claim_id: str
    proposal: TypedScalarProposal


@dataclass(frozen=True)
class ReviewRow:
    """One reviewer's verdict on one card. `proposal` is None for UNSUPPORTED (no reading offered)
    and for rows that failed validation."""

    claim_id: str
    verdict: str
    proposal: TypedScalarProposal | None


@dataclass
class CardDecision:
    """Per-card outcome with the full trace needed to replay the gate without re-running the LLM."""

    claim_id: str
    committed: bool
    reason: str
    verdicts: list[str] = field(default_factory=list)
    labels: list[str | None] = field(default_factory=list)
    proposal: TypedScalarProposal | None = None


def _card_id(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    cid = raw.strip().upper()
    if cid == UNLISTED_CLAIM or _CLAIM_ID_RE.match(cid):
        return cid
    return None


def propose_claim_cards(
    episodes: list[Any], llm_complete: LlmComplete,
    *, on_drop: Callable[[str], None] | None = None,
) -> list[ClaimCard]:
    """Call 1. Emit validated, grounded claim cards.

    Rows go through `parse_scalar_row` -- the SAME admission rules as the production extractor -- so
    an arm's candidate set can never be more permissive than the baseline it is compared against.
    A row with a missing or malformed `claim_id`, or a duplicate one, is dropped: correspondence is
    the entire point of the protocol and a colliding handle would silently merge two claims."""

    def _drop(reason: str) -> None:
        if on_drop is not None:
            on_drop(reason)

    if not episodes:
        return []
    log = "\n".join(f"[{i}] {getattr(e, 'content', '')}" for i, e in enumerate(episodes))
    rows, parse_failure = _parse_json_array(llm_complete(PROPOSER_SYSTEM_PROMPT, log))
    if parse_failure is not None:
        _drop(parse_failure)

    out: list[ClaimCard] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            _drop("not_an_object")
            continue
        cid = _card_id(row.get("claim_id"))
        if cid is None or cid == UNLISTED_CLAIM:
            _drop("bad_claim_id")
            continue
        if cid in seen:
            _drop("duplicate_claim_id")
            continue
        proposal = parse_scalar_row(row, episodes, _drop)
        if proposal is None:
            continue
        seen.add(cid)
        out.append(ClaimCard(claim_id=cid, proposal=proposal))
    return out


def render_candidates(cards: list[ClaimCard], order: list[int] | None = None) -> str:
    """Render cards for a reviewer prompt. `order` permutes presentation so a reviewer cannot infer
    anything from position, and so two reviewers see different orderings (plan: ordering bias).

    The rendering deliberately OMITS nothing about the claim -- hiding the value would change the
    task from verification to extraction -- but the prompt frames it as a question, not evidence."""
    idxs = order if order is not None else list(range(len(cards)))
    lines = []
    for i in idxs:
        c = cards[i]
        p = c.proposal
        lines.append(json.dumps({
            "claim_id": c.claim_id,
            "subject": p.subject_text,
            "attribute": p.attribute,
            "scope": p.scope,
            "value_kind": p.value_kind,
            "unit": p.unit,
            "operation": p.operation,
            "value": p.value,
            "stated_span": p.stated_span,
        }, ensure_ascii=False))
    return "\n".join(lines)


def review_claim_cards(
    episodes: list[Any], cards: list[ClaimCard], llm_complete: LlmComplete,
    *, order: list[int] | None = None, on_drop: Callable[[str], None] | None = None,
) -> list[ReviewRow]:
    """Calls 2 and 3. One reviewer's independent verdicts.

    A reviewer never sees the other reviewer's output. A verdict for an unknown `claim_id` is
    dropped rather than invented into existence."""

    def _drop(reason: str) -> None:
        if on_drop is not None:
            on_drop(reason)

    if not episodes or not cards:
        return []
    known = {c.claim_id for c in cards}
    log = (
        "\n".join(f"[{i}] {getattr(e, 'content', '')}" for i, e in enumerate(episodes))
        + "\n\nCANDIDATE CLAIMS:\n" + render_candidates(cards, order)
    )
    rows, parse_failure = _parse_json_array(llm_complete(REVIEWER_SYSTEM_PROMPT, log))
    if parse_failure is not None:
        _drop(parse_failure)

    out: list[ReviewRow] = []
    for row in rows:
        if not isinstance(row, dict):
            _drop("not_an_object")
            continue
        cid = _card_id(row.get("claim_id"))
        if cid is None or (cid != UNLISTED_CLAIM and cid not in known):
            _drop("unknown_claim_id")
            continue
        verdict = str(row.get("verdict") or "").strip().upper()
        if cid == UNLISTED_CLAIM:
            verdict = UNLISTED_CLAIM
        elif verdict not in REVIEW_VERDICTS:
            _drop("bad_verdict")
            continue
        proposal = None
        if verdict != UNSUPPORTED:
            proposal = parse_scalar_row(row, episodes, _drop)
            if proposal is None and verdict == SUPPORTED:
                # A SUPPORTED verdict whose own reading does not validate/ground is not support.
                # Treat as a dropped row rather than silently accepting the candidate's fields --
                # accepting them would make the card its own evidence.
                _drop("supported_without_valid_reading")
                continue
        out.append(ReviewRow(claim_id=cid, verdict=verdict, proposal=proposal))
    return out


def gate_reviewed_cards(
    cards: list[ClaimCard], reviews: list[list[ReviewRow]],
) -> list[CardDecision]:
    """The experimental commit rule (plan: "Initial commit rule").

    A card commits ONLY when every reviewer returned SUPPORTED, each with its OWN grounded reading,
    and all readings agree with the proposer's on the factual core after the existing deterministic
    normalization (`_interpretation_label` -- subject, slot, operation, value, when).

    Deliberate conservatism, all measured rather than promoted:
      * CORRECTED abstains EVEN WHEN both reviewers agree with each other. That is a 2/3 majority,
        and admitting it here would quietly install a 2/3 policy under an experiment about
        vocabulary. Reported separately so the size of what is declined stays visible.
      * UNSUPPORTED from any reviewer abstains.
      * A reviewer emitting TWO readings for one card is conflicted -> abstain. Ambiguous
        correspondence must never resolve by picking one.
      * UNLISTED_CLAIM never commits: a reviewer-originated fact has only one witness.

    Note what is NOT checked: span equality. Reviewers ground their own spans and those spans are
    expected to differ -- that is the fragmentation the whole experiment exists to route around.
    Agreement is on the FACT, correspondence is via claim_id.
    """
    by_card: dict[str, list[list[ReviewRow]]] = {c.claim_id: [[] for _ in reviews] for c in cards}
    for ri, rows in enumerate(reviews):
        for r in rows:
            if r.claim_id in by_card:
                by_card[r.claim_id][ri].append(r)

    decisions: list[CardDecision] = []
    for card in cards:
        per_reviewer = by_card[card.claim_id]
        verdicts: list[str] = []
        labels: list[str | None] = []
        ok = True
        reason = "commit"

        for rows in per_reviewer:
            if len(rows) == 0:
                verdicts.append("MISSING")
                labels.append(None)
                ok, reason = False, "reviewer_omitted_card"
                continue
            if len(rows) > 1 and len({_row_key(r) for r in rows}) > 1:
                verdicts.append("CONFLICTED")
                labels.append(None)
                ok, reason = False, "reviewer_conflicted"
                continue
            r = rows[0]
            verdicts.append(r.verdict)
            labels.append(_interpretation_label(r.proposal) if r.proposal else None)
            if r.verdict != SUPPORTED:
                ok = False
                reason = f"reviewer_{r.verdict.lower()}"

        if ok:
            want = _interpretation_label(card.proposal)
            if any(lbl != want for lbl in labels):
                ok, reason = False, "factual_core_disagreement"

        decisions.append(CardDecision(
            claim_id=card.claim_id, committed=ok, reason=reason,
            verdicts=verdicts, labels=labels,
            proposal=card.proposal if ok else None,
        ))
    return decisions


def _row_key(r: ReviewRow) -> str:
    return f"{r.verdict}\x1f{_interpretation_label(r.proposal) if r.proposal else ''}"


def summarize(decisions: list[CardDecision]) -> Counter[str]:
    """Reason histogram -- the diagnostic the plan's metrics section consumes."""
    return Counter(d.reason if not d.committed else "COMMIT" for d in decisions)
