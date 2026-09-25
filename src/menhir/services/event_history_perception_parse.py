"""Event-history perception — LLM extraction pass and fail-closed admission parsing.

Turns prose episodes into unbound, source-grounded ``EventPerceptionProposal`` objects: the system
prompt, the deterministic response-shape parser (episode envelopes only), and ``parse_event_row``,
which admits a raw model row only when every required field is present, the predicate canonicalizes
through the acquisition registry, the stated span is uniquely located in the episode, and the quote
itself carries completed-acquisition evidence. Pure given the injected ``llm_complete``; writes
nothing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import build_source_key
from menhir.services.event_history_perception_registry import (
    _ACQUIRED_NON_GOT_RE,
    _NEGATION_RE,
    _POSSESSION_RE,
    _canon_object_key,
    _expresses_completed_acquisition,
    _has_completed_acquisition_evidence,
    _has_intent_cues,
    _object_grounded,
    canonicalize_predicate,
)
from menhir.services.seam_types import LlmComplete
from menhir.services.typed_scalar_rules import (
    _ground_span,
    _opt_str,
    _req_episode_index,
    _req_str,
)

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
