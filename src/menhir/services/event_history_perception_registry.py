"""Event-history perception — predicate registry, veto patterns, and object canonicalization.

The conservative completed-acquisition allowlist (surface verb -> canonical predicate), the
fail-closed regex gates (intent/modal cues, possession, negation, possessive-new evidence), and the
deterministic object-key canonicalizer behind ``menhir.services.event_history_perception``. Pure
parsing/grounding helpers only: no LLM, no IO, no wiring. Shared grounding helpers are imported from
``typed_scalar_rules``/``event_history_recall`` rather than re-derived, so this rule set cannot drift
from the established typed-scalar and recall conventions."""

from __future__ import annotations

import re

from menhir.services.event_history_recall import _has_whole_token

#: The single canonical predicate for completed-acquisition semantics.
CANONICAL_PREDICATE_ACQUIRED = "acquired"

#: Conservative surface forms accepted for a completed acquisition. Any one of these maps to the
#: canonical ``acquired`` predicate; anything else fails closed as an unknown predicate. This is an
#: EXACT allowlist, never a prefix or synonym matcher, so ``wanted``/``buying``/``purchase intent``
#: can never slip in as a completed event.
_ACQUIRED_ALIASES = ("purchased", "bought", "got", "acquired")

#: surface verb -> canonical predicate (completed acquisition registry).
_ACQUIRED_REGISTRY: dict[str, str] = {
    surface: CANONICAL_PREDICATE_ACQUIRED for surface in _ACQUIRED_ALIASES
}

#: Word-boundary match for the surface verbs that mark a clearly COMPLETED acquisition. Used as the
#: exception to the intent/modal rejection: a quote with intent cues is admitted only if it also
#: carries one of these completed verbs (conservative — see ``parse_event_row``).
_COMPLETED_ACQUISITION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(a) for a in _ACQUIRED_ALIASES) + r")\b", re.IGNORECASE)

#: Obvious intent/modal/hypothetical cues. Word-boundary matched against the quote; multi-word cues
#: are listed verbatim. This is a conservative defense-in-depth gate: the prompt already instructs
#: the model to omit intent/plan/hypothetical/recommendation/desire, so a row that still carries a
#: cue is admitted only when it simultaneously states a clearly completed acquisition.
_INTENT_CUE_WORDS = (
    "want", "wants", "wanting", "wanted",
    "plan", "plans", "planning", "planned",
    "hope", "hopes", "hoping", "hoped", "hopefully",
    "going to", "gonna", "will", "would", "should", "might", "may",
    "could", "think", "thinking", "thought", "maybe", "perhaps",
    "consider", "considers", "considering", "considered",
    "recommend", "recommends", "recommending", "recommended",
    "suggest", "suggests", "suggesting", "suggested",
    "try", "tries", "trying", "tried",
    "intend", "intends", "intending", "intended",
    "desire", "desires", "desiring", "wish", "wishes", "wishing", "wished",
    "need to", "would like", "i'd like", "ideally",
)
_INTENT_CUE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(c) for c in _INTENT_CUE_WORDS) + r")\b", re.IGNORECASE)

#: Completed-acquisition verbs OTHER than ``got``, used as the "clear event cue" exception to the
#: have-got/has-got possession veto: a possession construction is admitted only when a distinctly
#: eventive acquisition verb is also present in the same quote.
_ACQUIRED_NON_GOT_RE = re.compile(r"\b(?:purchased|bought|acquired)\b", re.IGNORECASE)

#: Possession constructions ("I've got a notebook", "he has got a pen") that must NOT count as an
#: acquisition. Conservative: any have-got/has-got form with no distinctly eventive acquisition verb
#: nearby is vetoed as possession, not acquisition.
_POSSESSION_RE = re.compile(
    r"\b(?:i|you|we|they|he|she|it)\b(?:'?\s*(?:have|has)|'ve|'s)\s+got\b|"
    r"\b(?:have|has|having)\s+got\b",
    re.IGNORECASE,
)

#: Negation that vetoes a completed acquisition when a registry verb is present ("I did not buy...",
#: "never purchased"). A negating auxiliary/adverb immediately preceding a registry verb asserts the
#: event did NOT happen, so it is rejected deterministically BEFORE the completion-marker exception can
#: admit it. This veto covers only registry verbs; a negated NON-registry verb falls to the bare-negator
#: refusal on the possessive-new evidence limb instead. Includes base-form verbs too, so "I did not buy"
#: is caught explicitly rather than relying only on the completed-acquisition evidence gate.
_NEGATION_VERBS = (
    "purchased", "bought", "got", "acquired",
    "buy", "purchase", "get", "acquire",
)
_NEGATION_RE = re.compile(
    r"\b(?:did not|didn'?t|never|not|no|won'?t|wouldn'?t|don'?t|doesn'?t|"
    r"can'?t|wasn'?t|weren'?t|haven'?t|hasn'?t|hadn'?t)\s+"
    r"(?:" + "|".join(re.escape(v) for v in _NEGATION_VERBS) + r")\b",
    re.IGNORECASE,
)

#: A bare negator (not/never/no or a "n't" contraction) anywhere in a possessive-new quote refutes
#: current possession, so that limb is refused even when no registry verb is present to trip the
#: existing _NEGATION_RE veto. Deliberately generic and conservative: it gates only the
#: possessive-new evidence limb, never the registry-verb veto.
_BARE_NEGATION_RE = re.compile(r"\b(?:not|never|no)\b|n't", re.IGNORECASE)

#: A conservative generic possessive-new construction ("my new notebook", "his new pen") that states
#: current possession of something newly acquired, admissible as completed-acquisition evidence even
#: when no eventive acquisition verb is present. Kept deliberately generic (possessive determiner +
#: "new") so no benchmark/task-specific object vocabulary leaks in.
_POSSESSIVE_NEW_RE = re.compile(
    r"\b(?:my|your|our|his|her|their|its)\s+new\b", re.IGNORECASE)

#: Any run of whitespace (including newlines) — collapsed to a single space for the deterministic
#: object-key canonicalizer.
_WHITESPACE_RE = re.compile(r"\s+")


#: Leading articles/possessive determiners stripped from the object phrase during canonicalization, so
#: object="pen" and object="the pen" both normalize to the SAME meaningful noun phrase "pen"
#: (and both ground against a quote saying "bought a pen" / "bought the pen").
_OBJECT_DETERMINERS = (
    "a ", "an ", "the ", "my ", "your ", "our ", "his ", "her ", "their ", "its ",
)


def _canon_object_key(raw: str) -> str:
    """Deterministic normalization of the object identity: trim, lowercase, collapse any run of
    whitespace to a single space, strip a leading article/possessive determiner, then strip a single
    leading adjective ``new`` (which describes acquisition status, not object identity — so "my new
    notebook" keys to "notebook"). Other adjectives are kept. This is the stable spelling that
    keys/lanes the event; the natural human-readable phrase is kept separately in ``object_display``."""
    text = _WHITESPACE_RE.sub(" ", (raw or "").strip().lower()).strip()
    changed = True
    while changed:
        changed = False
        for determiner in _OBJECT_DETERMINERS:
            if text.startswith(determiner):
                text = text[len(determiner):].strip()
                changed = True
                break
    if text.startswith("new "):
        text = text[len("new "):].strip()
    return text


def _object_grounded(stated_span: str, object_key: str) -> bool:
    """Fail-closed object grounding: the canonical object phrase must occur in the exact stated_span
    as a whole word under case/whitespace normalization. Prevents admitting object="pen" over a quote
    that says the user acquired a completely different thing ("I bought a pencil"). A blank object is
    never grounded. Reuses the recall side's word-boundary matcher so the write and read sides cannot
    drift apart."""
    span = _WHITESPACE_RE.sub(" ", (stated_span or "").strip().lower()).strip()
    obj = object_key  # already trimmed/lowercased/whitespace-collapsed/determiner-stripped
    return bool(obj and span) and _has_whole_token(span, obj)


def canonicalize_predicate(token: str) -> str | None:
    """Map a surface predicate to its canonical form, or None for an unknown predicate (fail closed).

    Only the small completed-acquisition registry is recognized; unknown predicates return None so the
    admission parser drops the row rather than inventing semantics."""
    return _ACQUIRED_REGISTRY.get((token or "").strip().lower())


def _has_intent_cues(text: str) -> bool:
    return bool(_INTENT_CUE_RE.search(text or ""))


def _expresses_completed_acquisition(text: str) -> bool:
    return bool(_COMPLETED_ACQUISITION_RE.search(text or ""))


def _has_completed_acquisition_evidence(text: str) -> bool:
    """True when the quote itself carries completed-acquisition evidence: either an explicit registry
    acquisition verb (purchased/bought/got/acquired) OR a conservative possessive-new construction
    ("my new notebook"). This is the admission gate that keeps a hallucinated row over arbitrary prose
    (no completed-acquisition evidence at all) from being admitted. Intent/negation are vetoed BEFORE
    this gate by the caller. The possessive-new limb refuses a bare negator (not/never/no or "n't"):
    a quote stating the user does NOT have the thing is not a completed acquisition, even when it
    carries no registry verb to trip the caller's _NEGATION_RE veto."""
    if _expresses_completed_acquisition(text):
        return True
    if _POSSESSIVE_NEW_RE.search(text or ""):
        return not _BARE_NEGATION_RE.search(text or "")
    return False
