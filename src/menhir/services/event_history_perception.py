"""Event-history perception boundary — the LLM extraction/admission seam for grounded categorical
occurrences (Phase 3 of the approved event-history work).

This is a *generic, offline-only* boundary. It turns prose episodes into unbound, source-grounded
``EventPerceptionProposal`` objects via a single LLM pass, then a pure builder turns a proposal
into a durable ``TypedEventAssertion``. It wires up NOTHING: no settings/flag, no runtime/scheduler/
endpoint, no repository/View write, no recall authority, and no telemetry beyond the injected
``on_drop`` observability seam. Perceiving and admitting are pure given the injected ``llm_complete``;
the caller owns persistence, projection, and authority.

Only *completed acquisition* semantics are recognized: a small conservative predicate registry maps
surface verbs to the single canonical predicate ``acquired``. There is deliberately NO ownership
supersession — a newer acquisition records an occurrence and never invalidates an older one (that is
the event-history invariant the domain contract already enforces).

The parser is fail-closed and mirrors the typed-scalar boundary's conventions (same required-field
helpers, unique substring grounding, and drop-reason codes) so the two perception seams behave
consistently. All parsing/grounding helpers are imported from ``typed_scalar_rules`` rather than
re-derived, so this admission rule set cannot drift from the established one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from menhir.domain.event_history import TypedEventAssertion
from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import build_source_key
from menhir.services.event_history_recall import _has_whole_token
from menhir.services.typed_scalar_rules import (
    _ground_span,
    _opt_str,
    _req_episode_index,
    _req_str,
)
from menhir.services.seam_types import LlmComplete

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


#: (system, user) -> completion text. Injected so this boundary is decoupled from any specific LLM,
#: exactly like ``perception.LlmComplete`` and the typed-scalar boundary.

EVENT_SYSTEM_PROMPT = (
    "You read a user's memory episodes and extract COMPLETED ACQUISITION EVENTS only, for a "
    "deterministic memory system. A completed acquisition is a fact that the user DID acquire, buy, "
    "purchase, or get something as a finished past event. It may also be stated as current possession "
    "of something new ('my new notebook', 'his new pen') — treat that as acquired. Do NOT emit: "
    "considering ('considering a new notebook'), intent ('I want to buy'), plans, hopes, "
    "hypotheticals, recommendations, desires, things the user only considered, assistant/model "
    "claims, or negated claims ('I did not buy'). "
    "For each completed event emit an object: "
    "{\"subject\": <who acquired it; use 'user' when the speaker acquired it for themself>, "
    "\"predicate\": one of \"purchased\"|\"bought\"|\"got\"|\"acquired\", "
    "\"object\": <the thing acquired, e.g. 'a notebook'>, "
    "\"object_display\": <the same natural phrase, or \"\" to reuse object>, "
    "\"domain\": <optional category such as 'stationery'; \"\" if none>, "
    "\"when\": <ISO date the acquisition happened ONLY if the episode EXPLICITLY states a calendar "
    "date ('on July 18', '2026-07-18', 'last year' is NOT a date); otherwise \"\" — never infer, "
    "guess, or use today's date>, "
    "\"stated_span\": <the SHORTEST exact source substring that states THIS completed acquisition; "
    "quote ONLY that clause, never other clauses, and keep it identical each time you read the same "
    "fact>}. "
    "Copy stated_span verbatim from the episode; never invent or infer an object or time that is not "
    "grounded in the source. If you cannot copy an exact source substring, omit that event. Emit each "
    "event only once. "
    "Output ONLY a JSON array with EXACTLY ONE envelope for EVERY numbered episode, in the same "
    "order: {\"episode\": <number>, \"events\": [<zero or more event objects>]}. Use an empty events "
    "array when that episode has no completed acquisition. Do not omit an episode, and do not repeat "
    "the episode number inside each event."
)


@dataclass(frozen=True)
class EventPerceptionProposal:
    """One UNBOUND, source-grounded completed-acquisition proposal from a single extraction pass.

    It carries everything a ``TypedEventAssertion`` needs EXCEPT the resolved ``subject_uuid`` (binding
    is the caller's job) and the ingest time. ``predicate`` is already canonicalized from the registry.
    A proposal only exists when its ``stated_span`` was UNIQUELY located in the episode text, so the
    offsets are always known (>=0) and ``claim_ordinal`` is always 0 — two proposals citing the same
    source span share a ``source_key`` and are competing interpretations of ONE claim. ``when`` is the
    validated/normalized ISO world-time, or None when the model gave none (the builder then falls to
    the episode reference time, never to ingest time)."""

    subject_text: str
    predicate: str            # canonical predicate (from the acquisition registry)
    object_key: str
    object_display: str
    stated_span: str
    episode_uuid: str
    span_start: int
    span_end: int
    domain: str | None = None
    when: str | None = None   # validated ISO world-time, or None
    claim_ordinal: int = 0    # disambiguates same-lane claims when offsets are absent (always 0 here)

    def __post_init__(self) -> None:
        if self.claim_ordinal < 0:
            raise ValueError("claim_ordinal must be >= 0")

    @property
    def source_key(self) -> str:
        """BINDING-STABLE claim locator, computed by the SAME domain helper the ``TypedEventAssertion``
        persists, so the durable identity this proposal predicts cannot drift from the one the store
        writes."""
        return build_source_key(
            self.episode_uuid, self.span_start, self.span_end, self.claim_ordinal)


def _parse_json_array(text: str) -> "tuple[list[Any], str | None]":
    """Deterministic parse of the model's JSON response; tolerant of code fences and a leading '+'
    on numbers (LLMs emit '+1', invalid JSON).

    Returns ``(rows, failure)``. A clean parse of an empty array is legitimate (the model saying "no
    completed acquisitions"). A bare object is accepted ONLY when it has the episode-envelope shape
    (``episode`` plus ``events``), mirroring the typed-scalar boundary; arbitrary bare event objects
    remain invalid. A failure is ``malformed_json`` (a truncated response is the common cause) or
    ``not_a_json_array`` (valid JSON of the wrong shape)."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    text = re.sub(r"(:\s*)\+(\d)", r"\1\2", text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [], "malformed_json"
    if isinstance(data, dict) and "episode" in data and "events" in data:
        data = [data]
    if not isinstance(data, list):
        return [], "not_a_json_array"
    return data, None


def _expand_episode_envelopes(
    rows: list[Any],
    episode_count: int,
    drop: "Callable[[str], None]",
) -> list[Any]:
    """Expand the machine-checkable episode-envelope response shape into flat event rows.

    The response MUST be a JSON array of episode envelopes (or a single bare envelope, already
    wrapped by ``_parse_json_array``) — one object per input episode including an explicit empty
    ``events`` list when no completed acquisition exists. Legacy flat event arrays are NOT accepted:
    completeness is machine-checkable precisely because every episode 0..n-1 appears exactly once.
    Envelope STRUCTURAL integrity is all-or-nothing: a bad, duplicate, missing, or internally
    mismatched envelope invalidates the ENTIRE extraction sample (returns no rows) rather than
    preserving rows from otherwise valid envelopes, while still emitting every stable drop reason.
    Explicit empty coverage (an episode with an empty ``events`` list) is preserved and never counts
    as a defect."""
    expanded: list[Any] = []
    seen: set[int] = set()
    defective = False
    for envelope in rows:
        if not isinstance(envelope, dict):
            drop("bad_episode_envelope")
            defective = True
            continue
        index = _req_episode_index(envelope, episode_count)
        events = envelope.get("events")
        if index is None or not isinstance(events, list):
            drop("bad_episode_envelope")
            defective = True
            continue
        if index in seen:
            drop("duplicate_episode_envelope")
            defective = True
            continue
        seen.add(index)
        for event in events:
            if not isinstance(event, dict):
                expanded.append(event)  # parse_event_row records the precise row-shape drop
                continue
            row = dict(event)
            supplied = row.get("episode")
            if supplied is not None and supplied != index:
                drop("episode_envelope_mismatch")
                defective = True
                continue
            row["episode"] = index
            expanded.append(row)

    for _missing in range(episode_count - len(seen)):
        drop("missing_episode_envelope")
        defective = True
    return [] if defective else expanded


def parse_event_row(
    row: Any, episodes: list[Any], drop: "Callable[[str], None]",
) -> "EventPerceptionProposal | None":
    """Validate ONE raw model row into a grounded proposal, or None with a `drop` reason.

    Fail-closed admission: a row survives ONLY if it is a real object with every required field
    (``subject``, ``predicate``, ``object``, ``stated_span``), ``predicate`` canonicalizes through the
    acquisition registry, ``episode`` is a real in-range integer, any supplied ``when`` parses as ISO,
    ``stated_span`` occurs EXACTLY ONCE in the episode text (unique grounding -> located offsets;
    zero/multiple -> dropped as ungrounded/ambiguous), and the quote itself carries completed-
    acquisition evidence — an explicit acquisition verb (purchased/bought/got/acquired) or a
    conservative possessive-new construction ("my new X") — so a hallucinated row over arbitrary prose
    is rejected. Intent/modal/hypothetical and negation are vetoed BEFORE the evidence gate: a
    cue-laden or negated quote is admitted only when it also clearly states a completed acquisition
    (conservative). Everything else fails closed to omission; malformed model output never acquires a
    semantic default.

    Pure. ``drop`` is called at most once, immediately before returning None.
    """
    if not isinstance(row, dict):
        drop("not_an_object")
        return None
    subject_text = _req_str(row, "subject")
    predicate = _req_str(row, "predicate")
    object_key = _req_str(row, "object")
    stated_span = _req_str(row, "stated_span")
    if subject_text is None or predicate is None or object_key is None or stated_span is None:
        drop("missing_required_field")
        return None
    canonical = canonicalize_predicate(predicate)
    if canonical is None:
        drop("unknown_predicate")
        return None

    # Optional fields: an ABSENT or BLANK domain/object_display is valid (domain -> None,
    # object_display -> the object key); a PRESENT-but-non-string value fails closed (None), so a
    # structured/object domain can never be stringified into a bogus value.
    domain = _opt_str(row, "domain")
    object_display = _opt_str(row, "object_display")
    when = _opt_str(row, "when")
    if domain is None or object_display is None or when is None:
        drop("bad_optional_field")
        return None
    domain = domain or None
    object_display = object_display or object_key
    object_key = _canon_object_key(object_key)

    idx = _req_episode_index(row, len(episodes))
    if idx is None:
        drop("bad_episode_index")
        return None
    episode_uuid = str(getattr(episodes[idx], "uuid", "") or "").strip()
    if not episode_uuid:
        drop("unresolvable_episode")
        return None

    when_parsed: str | None = None
    if when:
        dt = parse_iso8601(when)
        if dt is None:
            drop("malformed_when")
            return None
        when_parsed = dt.isoformat()

    content = str(getattr(episodes[idx], "content", "") or "")
    span = _ground_span(content, stated_span)
    if span is None:
        drop("span_not_uniquely_located")
        return None
    span_start, span_end = span

    # Negation veto: a negated completion verb asserts the event did NOT happen, so it is rejected
    # deterministically BEFORE any completion-marker exception can admit it.
    if _NEGATION_RE.search(stated_span):
        drop("negated_claim")
        return None

    # Possession veto: "I've got a notebook" states possession, not acquisition. Admitted only when a
    # distinctly eventive acquisition verb (bought/purchased/acquired) is present in the same quote.
    if _POSSESSION_RE.search(stated_span) and not _ACQUIRED_NON_GOT_RE.search(stated_span):
        drop("possession_not_acquisition")
        return None

    if _has_intent_cues(stated_span) and not _expresses_completed_acquisition(stated_span):
        drop("intent_or_hypothetical")
        return None

    # Completed-acquisition evidence gate: the quote itself must express a completed acquisition via
    # an explicit acquisition verb OR a conservative possessive-new construction. This rejects a
    # hallucinated row emitted over arbitrary prose that contains no acquisition evidence at all.
    if not _has_completed_acquisition_evidence(stated_span):
        drop("no_completed_acquisition_evidence")
        return None

    # Object grounding: the object phrase (after stripping an article/possessive determiner) must
    # occur in the exact stated_span, so an acquisition of one thing is never admitted as another.
    if not _object_grounded(stated_span, object_key):
        drop("object_not_grounded")
        return None

    return EventPerceptionProposal(
        subject_text=subject_text,
        predicate=canonical,
        object_key=object_key,
        object_display=object_display,
        stated_span=stated_span,
        episode_uuid=episode_uuid,
        span_start=span_start,
        span_end=span_end,
        domain=domain,
        when=when_parsed,
        claim_ordinal=0,
    )


def extract_events_once(
    episodes: list[Any], llm_complete: LlmComplete,
    *, on_drop: "Callable[[str], None] | None" = None,
) -> list[EventPerceptionProposal]:
    """One event-perception pass: prose -> validated, grounded ``EventPerceptionProposal``s.

    ``episodes`` are objects with ``.uuid`` and ``.content`` (e.g. ``perception.Episode``). A row
    survives ONLY through ``parse_event_row`` (fail-closed admission); a whole-response parse failure
    is reported once through ``on_drop`` as ``malformed_json`` / ``not_a_json_array``. Pure given the
    injected ``llm_complete``; writes nothing.

    ``on_drop`` is an OPTIONAL observability seam: called once per DISCARDED row with a short reason,
    and ONCE for a whole-response parse failure. It is injected rather than emitted from here so the
    function stays pure by default and so any audit emit lives with the other emits in the caller.
    """
    if not episodes:
        return []

    def _drop(reason: str) -> None:
        if on_drop is not None:
            on_drop(reason)

    log = "\n".join(f"[{i}] {getattr(e, 'content', '')}" for i, e in enumerate(episodes))
    raw_rows, parse_failure = _parse_json_array(llm_complete(EVENT_SYSTEM_PROMPT, log))
    if parse_failure is not None:
        _drop(parse_failure)
    else:
        raw_rows = _expand_episode_envelopes(raw_rows, len(episodes), _drop)

    out: list[EventPerceptionProposal] = []
    for row in raw_rows:
        proposal = parse_event_row(row, episodes, _drop)
        if proposal is not None:
            out.append(proposal)
    return out


def _resolve_event_valid_time(
    when: str | None, episode_reference_time: str | None,
) -> "tuple[str | None, str | None]":
    """Choose (valid_at, time_basis) for the assertion, precision-first and NEVER using ingest time:
      * an explicit validated ``when`` (from admission) -> (when, 'explicit');
      * else the episode's own reference time when it parses -> (that time, 'episode_reference');
      * else -> (None, None): abstention, because neither source nor world time is usable.

    ``learned_at`` is deliberately NOT a fallback — event authority orders by world/source time only,
    and an ungrounded ingest stamp must never masquerade as an occurrence time."""
    if when:
        return when, "explicit"
    if episode_reference_time:
        dt = parse_iso8601(episode_reference_time)
        if dt is not None:
            return episode_reference_time, "episode_reference"
    return None, None


@dataclass(frozen=True)
class EventAssertionBuildResult:
    """Pure builder receipt. ``assertion`` is None EXACTLY when the builder abstained (no usable
    world/source time); ``reason`` explains that abstention and is None on success, so a caller can
    inspect and distinguish ``no_valid_time`` from any future abstention reason without string-matching
    prose. ``built`` is True only when an assertion was produced."""

    assertion: TypedEventAssertion | None
    reason: str | None = None

    @property
    def built(self) -> bool:
        return self.assertion is not None


def build_event_assertion(
    proposal: EventPerceptionProposal, *,
    subject_uuid: str, namespace: str | None, learned_at: str,
    episode_reference_time: str | None, turn_evidence_uuid: str | None,
    perceiver_version: str,
) -> EventAssertionBuildResult:
    """Build the durable ``TypedEventAssertion`` from a proposal + its resolved subject and temporal
    context. Returns an ``EventAssertionBuildResult``: ``built`` True with the assertion on success, or
    ``built`` False with an inspectable ``reason`` (``no_valid_time``) on explicit abstention.

    ``valid_at`` uses the proposal's explicit world time when present (``explicit``), else the
    episode reference time (``episode_reference``). ``learned_at`` is NEVER used as ``valid_at``. If
    neither source nor world time is usable the builder abstains and returns no authority-eligible
    data rather than creating it. Evidence tier is forced to ``agent`` (the lowest, advisory tier) —
    probabilistic extraction can never grant itself authority. Pure; does not persist or project. The
    proposal's span/episode flow through unchanged, so the assertion's binding-stable ``source_key`` is
    IDENTICAL to the one the proposal predicted."""
    valid_at, time_basis = _resolve_event_valid_time(proposal.when, episode_reference_time)
    if valid_at is None:
        return EventAssertionBuildResult(None, "no_valid_time")
    assertion = TypedEventAssertion(
        subject_uuid=subject_uuid,
        subject_display=proposal.subject_text,
        predicate=proposal.predicate,
        object_key=proposal.object_key,
        object_display=proposal.object_display,
        valid_at=valid_at,
        learned_at=learned_at,
        stated_span=proposal.stated_span,
        episode_uuid=proposal.episode_uuid,
        span_start=proposal.span_start,
        span_end=proposal.span_end,
        claim_ordinal=proposal.claim_ordinal,
        domain=proposal.domain,
        namespace=namespace,
        turn_evidence_uuid=turn_evidence_uuid,
        time_basis=time_basis,
        evidence_tier="agent",
        perceiver_version=perceiver_version,
    )
    return EventAssertionBuildResult(assertion)
