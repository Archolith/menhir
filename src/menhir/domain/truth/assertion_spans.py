"""Proposition-scoped assertion spans over a captured user turn (CF-17).

The admission gate decides whether a claim declaring ``source="user"`` earns the apex trust tier.
It compares the claim against turn evidence, and the turn evidence holds the **whole raw user
prompt** -- not a proposition. That mismatch is the root of CF-17: a containment test against a
whole prompt cannot tell an assertion from a mention, so ``"Alice claimed the deploy failed"``
grounded ``"the deploy failed"`` at confidence 1.0.

This module supplies the missing unit. It splits a turn into sentences and returns only those
that are unambiguously a single plain assertion. Equality against one of those spans is then both
safe and usable: safe because the span asserts exactly what it says, usable because a memory no
longer has to equal the entire prompt to ground.

Two design decisions carry the safety argument.

**Refusal is the default, and refusing is free.** A sentence that trips any disqualifier yields
NO span. The claim then falls back to whole-text equality, which is the Option-1 behaviour the
CF-17 decision plan verified 11/11 -- so a filter miss degrades to the option already proven
safe, never to the defect. This is what keeps the plan's warning about Option 2 from biting: the
warning is that a naive extractor "recreates D2 upstream and out of sight", and it would, if
extraction were a best-effort attempt to find assertions. It is not. It is a refusal to emit
anything it cannot vouch for.

**Spans are derived here, never supplied.** The plan framed this as evidence-subsystem work, which
implied a schema change, a producer contract, and a backfill. None is needed: a span is a pure
deterministic function of text already stored, so it can be computed at comparison time and
applies retroactively to every turn ever captured. It also has to be computed here rather than
accepted from a producer -- a caller-supplied span would be attacker-controlled input to a trust
decision, which is the CF-32 defect class. The server derives what it trusts.

What this deliberately does NOT do: split below the sentence. ``"the deploy failed and I rolled
back"`` stays one span, so a claim of just ``"the deploy failed"`` does not ground. Clause
splitting needs to know that ``"a cat and dog collar"`` is not two propositions, and getting that
wrong manufactures a span the user never asserted -- the exact failure this module exists to
prevent. Sentence granularity is the win; clause granularity is not worth the risk.
"""

from __future__ import annotations

import re

#: Longest sentence that may become a span. A long sentence is more likely to be compound or
#: qualified in a way the disqualifiers below do not name, and the cost of refusing one is only
#: that it falls back to whole-text equality.
_MAX_SPAN_CHARS = 200

#: Sentence terminators. A newline also ends a sentence: prompts are often line-broken lists where
#: no terminal punctuation appears at all.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|[\r\n]+")

#: A decimal point or a version number is not a sentence boundary. Applied by re-joining a split
#: that separated two digits, so "Postgres 16.4 is deployed" stays one sentence.
_DIGIT_SPLIT_RE = re.compile(r"^\d")

#: Any quotation mark, straight or curly, and the colon that introduces quoted or listed material.
#: Quoted text is the canonical mention-not-assertion form: `I copied the text "the deploy failed"`.
_QUOTE_CHARS = "\"'`‘’“”«»:"

#: Verbs of saying, thinking and perceiving. These embed a proposition without asserting it --
#: "Alice claimed X", "I wondered whether X", "he denies X". Matched as whole words.
_ATTRIBUTION_WORDS = frozenset({
    "said", "says", "say", "saying",
    "claimed", "claims", "claim", "claiming",
    "told", "tells", "tell", "telling",
    "reported", "reports", "report", "reporting",
    "wrote", "writes", "write", "writing",
    "mentioned", "mentions", "mention", "mentioning",
    "asked", "asks", "ask", "asking",
    "answered", "answers", "answer",
    "thinks", "think", "thought", "thinking",
    "believes", "believe", "believed", "believing",
    "argues", "argue", "argued", "arguing",
    "suggests", "suggest", "suggested", "suggesting",
    "assumes", "assume", "assumed", "assuming",
    "denies", "deny", "denied", "denying",
    "heard", "hears", "hear", "hearing",
    "read", "reads", "reading",
    "quoted", "quotes", "quote", "quoting",
    "copied", "copies", "copy", "copying",
    "wondered", "wonders", "wonder", "wondering",
    "insists", "insist", "insisted",
    "recalls", "recall", "recalled",
    "noted", "notes", "note",
    "states", "state", "stated",
    "alleges", "allege", "alleged",
})

#: Hedges and modals. "X might have failed" does not assert that X failed.
_MODALITY_WORDS = frozenset({
    "might", "may", "could", "would", "should", "must", "shall",
    "perhaps", "maybe", "possibly", "probably", "presumably", "likely", "unlikely",
    "seems", "seem", "seemed", "seeming",
    "appears", "appear", "appeared",
    "apparently", "allegedly", "supposedly", "reportedly", "arguably",
    "guess", "guessing", "suspect", "suspects", "suspected",
    "hope", "hopes", "hoping", "hoped",
    "wish", "wishes", "wishing",
    "expect", "expects", "expecting", "expected",
    "want", "wants", "wanting", "wanted",
    "plan", "plans", "planning", "planned",
})

#: Subordinators. Each introduces a clause whose content is framed rather than asserted:
#: conditionals ("if"), complements ("that", after which anything can be embedded), concessives
#: ("although"), and indirect questions ("whether"). "that" is the widest net and the most
#: valuable one -- it catches "It is false that X", "Alice said that X", "I think that X" in a
#: single rule rather than needing each matrix verb enumerated above.
_SUBORDINATOR_WORDS = frozenset({
    "if", "unless", "whether", "although", "though", "because", "since",
    "while", "whereas", "until", "before", "after", "when", "whenever",
    "that", "which", "who", "whom", "whose", "where",
    "suppose", "supposing", "assuming", "provided", "providing",
    "instead", "rather",
})

#: Sentence-initial words that make it a question even without a question mark.
_INTERROGATIVE_OPENERS = frozenset({
    "is", "are", "was", "were", "am", "be", "been",
    "do", "does", "did", "doesnt", "dont", "didnt",
    "can", "cant", "could", "will", "wont", "would", "shall", "should",
    "have", "has", "had", "havent", "hasnt",
    "how", "what", "why", "when", "where", "who", "whom", "which", "whose",
})


def normalize_claim_text(text: str | None) -> str:
    """Lowercase and collapse whitespace for comparison.

    The single normalization authority for admission decisions. The gate compares against what
    this produces, and spans are emitted in the same form, so the two can never drift into
    comparing differently-shaped strings -- the CF-47 failure mode.
    """
    if not text:
        return ""
    return " ".join(str(text).lower().split())


def _split_sentences(text: str) -> list[str]:
    """Split on terminal punctuation and line breaks, without breaking decimals or versions."""
    parts = _SENTENCE_SPLIT_RE.split(text)
    merged: list[str] = []
    for part in parts:
        if part is None:
            continue
        piece = part.strip()
        if not piece:
            continue
        # "16.4" split into "16." and "4 is deployed" -- rejoin when the next piece opens with a
        # digit and the previous ended on one.
        if merged and _DIGIT_SPLIT_RE.match(piece) and re.search(r"\d[.]$", merged[-1]):
            merged[-1] = f"{merged[-1]}{piece}"
            continue
        merged.append(piece)
    return merged


def _words(normalized: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", normalized) if w]


def is_plain_assertion(sentence: str) -> bool:
    """Whether this sentence asserts exactly one proposition, in its own voice, unhedged.

    Every rule here is a REFUSAL. There is no rule that admits a sentence for a positive reason,
    because no deterministic test can establish "this is an assertion" -- only that no known
    marker of non-assertion is present. Being explicit about that is the point: this returns
    "nothing disqualified it", and the caller treats that as permission to compare only because
    the fallback for a refusal is already safe.

    Note that negation is deliberately NOT disqualifying. "the deploy did not fail" asserts its
    negation perfectly well, and under equality it can only ground a claim that says the same
    thing. The plan's matrix lists one-sided negation as a deny case, and equality denies it
    without any help from here.
    """
    normalized = normalize_claim_text(sentence)
    if not normalized or len(normalized) > _MAX_SPAN_CHARS:
        return False

    # Questions assert nothing.
    if sentence.strip().endswith("?"):
        return False

    words = _words(normalized)
    if not words:
        return False

    if words[0] in _INTERROGATIVE_OPENERS:
        return False

    # Quoted or list-introducing material: the sentence carries text it is not vouching for.
    if any(ch in sentence for ch in _QUOTE_CHARS):
        return False

    for word in words:
        if word in _ATTRIBUTION_WORDS:
            return False
        if word in _MODALITY_WORDS:
            return False
        if word in _SUBORDINATOR_WORDS:
            return False

    return True


def extract_assertion_spans(text: str | None) -> tuple[str, ...]:
    """Return the normalized single-assertion spans of a captured turn, in order.

    Empty when nothing qualifies, which is the common case for conversational prompts and is not
    an error -- it means this turn can ground a claim only by whole-text equality.
    """
    if not text:
        return ()
    spans: list[str] = []
    seen: set[str] = set()
    for sentence in _split_sentences(str(text)):
        if not is_plain_assertion(sentence):
            continue
        normalized = normalize_claim_text(sentence).rstrip(".!")
        normalized = normalize_claim_text(normalized)
        if normalized and normalized not in seen:
            seen.add(normalized)
            spans.append(normalized)
    return tuple(spans)


def claim_is_grounded(claimed: str, source_span: str) -> bool:
    """Whether ``claimed`` is asserted by ``source_span``, for apex-tier admission (CF-17).

    Two ways to ground, and the first is the floor:

    1. The normalized claim EQUALS the whole normalized evidence text. This is Option 1 of the
       CF-17 decision plan, verified 11/11 against every recorded contradiction and
       mention-as-assertion case.
    2. The normalized claim equals one extracted assertion span. This is the extension that makes
       apex tier reachable for a memory drawn from part of a longer prompt.

    Both are equality. Neither containment nor token overlap survives, and that is the whole fix:
    ``_text_grounded`` granted on a contiguous substring OR on >= 50% of retained tokens appearing
    anywhere in the source. The overlap branch admitted every single-word contradiction of any
    multi-token claim (for N retained tokens with one substituted, overlap = N-1 >= 0.5N for all
    N >= 2), and the substring branch admitted quotation, attribution, conditionals and
    interrogatives, none of which involve an antonym or a negation for a guard to catch.

    Paraphrase is an intentional denial. "I purchased a bicycle" does not ground "I bought a
    bicycle", and no deterministic offline test can safely make it. Such a claim is stored at
    ``agent_inference`` (0.5), which is an accurate description of what it is.
    """
    normalized_claim = normalize_claim_text(claimed)
    if not normalized_claim:
        return False

    normalized_source = normalize_claim_text(source_span)
    if not normalized_source:
        return False

    if normalized_claim == normalized_source:
        return True

    # Compare against the claim with any terminal punctuation removed too, so a memory stored as
    # "I use Postgres 16." matches the span extracted from that same sentence.
    bare_claim = normalize_claim_text(normalized_claim.rstrip(".!"))
    return bare_claim in extract_assertion_spans(source_span)
