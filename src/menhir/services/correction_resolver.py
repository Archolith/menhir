"""F2 — anaphoric numeric correction binding.

A bare correction ("Actually it is 20, not 25.") carries no measure noun, so the perception
extractor emits nothing for it and the stale View (movies = 25) survives. This resolver closes that
gap WITHOUT loosening the extractor: it detects the correction's (old -> new) numbers
deterministically and binds them to the ONE recent current View in the SAME namespace whose value
equals `old`, writing a superseding value.

Tight by design (precision-first):
  * same namespace only — never binds across the graph;
  * a UNIQUE current counter View whose value == old — 0 matches -> no target, >1 -> abstain;
  * the correction never invents a measure; it can only re-value an EXISTING one.
So an ambiguous or unmatched correction touches nothing (`no unrelated 25-valued View gets touched`).

The superseding write reuses the deterministic fold sink (`fold_events_to_counter`) with a single
assertion event whose world-time is the correction turn's `recorded_at` — strictly later than the
original assertion's date — so the LWW register (fold-algebra Law 1) makes the correction win and
survive batch re-folds: a later pass re-derives the stale 25 at the original (earlier) date, which
the LWW guard skips as stale, and the resolver then finds no 25-valued View to correct (idempotent).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from menhir.domain.fold_algebra import Event

logger = logging.getLogger(__name__)

Embed = Callable[[str], "list[float] | None"]

_NUM = r"(\d+(?:\.\d+)?)"

# --- correction intent (stage 2 authorization) ------------------------------------------------
# Imported rather than duplicated so the cue vocabulary has ONE definition. typed_scalar_rules
# does not import this module, so there is no cycle.
from menhir.services.typed_scalar_rules import _CORRECTION_CUES, _CORRECTION_NOT_RE

#: Edit verbs. Note these are NOT sufficient on their own — see `correction_intent_is_established`.
_EDIT_VERB = r"(?:chang(?:e|ed|ing)|bump(?:ed)?|updat(?:e|ed)|set|mak(?:e|ing)|switch(?:ed)?|revis(?:e|ed)|correct(?:ed)?|fix(?:ed)?)"
#: A reference to the STORED thing rather than a thing in the world.
_STORED_REF = r"(?:it|that|this|the\s+(?:count|number|value|total|figure|tally))"
#: "change it from 25 to 20", "update the count to 20" — edit action grounded on stored state.
_GROUNDED_EDIT_RE = re.compile(rf"\b{_EDIT_VERB}\s+{_STORED_REF}\b", re.I)
#: Subjectless imperative addressed to the assistant: "bump to 20 from 25".
#: BASE FORMS ONLY — an imperative is never past tense. Using the full `_EDIT_VERB` here would
#: match sentence-initial "changed from 25 to 20", authorizing a bare fragment that establishes
#: no intent and could equally be an elided "[the temperature] changed from 25 to 20". Treating
#: past-tense "changed" as an imperative is an implementation artifact, not evidence.
_IMPERATIVE_VERB = r"(?:change|bump|update|set|make|switch|revise|correct|fix)"
_IMPERATIVE_EDIT_RE = re.compile(rf"^\s*(?:please\s+)?{_IMPERATIVE_VERB}\b", re.I)
#: A description of the world: "The temperature changed ...", "My dosage changed ...",
#: "The schedule was updated ...", "I switched ...", "I ended up buying ...". These carry edit
#: verbs but report an event; they must never authorize a write.
_WORLD_SUBJECT_RE = re.compile(
    rf"\b(?:the|my|our|his|her|their|its)\s+[\w\s]{{0,20}}?(?:was\s+|were\s+|has\s+been\s+)?{_EDIT_VERB}\b"
    rf"|^\s*(?:i|we|they|he|she)\s+[\w\s]{{0,20}}?{_EDIT_VERB}\b",
    re.I,
)

#: (compiled pattern, (old_group_index, new_group_index), needs_authorization).
#:
#: `needs_authorization=False` — INTRINSICALLY SELF-CUEING. The connective itself expresses
#: correction and cannot appear in innocent prose about two different quantities:
#:   `X, not Y`, `not X ... Y`, `X -> Y` (ASCII arrow), `X replaces/replacing Y`, `X replaced by Y`.
#:
#: `needs_authorization=True` — PROSE-SHAPED. Ordinary English that happens to contain two
#: numbers: `from X to Y`, `X instead of Y`, `to X from Y`. Verified by execution to fire on
#: "I work from 9 to 5", "My blood pressure went from 120 to 118", "I bought 6 instead of 4".
#: These extract a candidate but may NOT mutate memory without correction intent.
#:
#: The numeric value-match against an existing View (below) is a NECESSARY condition, not a
#: sufficient one: it requires only that SOME unrelated View in the namespace happens to hold
#: `old`, and subject/measure are then taken from that unrelated View. It is not a safety net
#: for a loose detector, and the earlier comment claiming otherwise was wrong (CF-129).
_PATTERNS: list[tuple[re.Pattern[str], tuple[int, int], bool]] = [
    (re.compile(rf"{_NUM}\s*,?\s+not\s+{_NUM}", re.I), (2, 1), False),                # "NEW, not OLD"
    # reversed form REQUIRES a connective word between the numbers, so the two \d+ can never match
    # adjacent digits of one number ("25" -> "2","5") — that separator is what disambiguates them.
    (re.compile(rf"\bnot\s+{_NUM}\s*,?\s+(?:it'?s|it\s+is|but|rather|instead|make\s+it)\s+{_NUM}",
                re.I), (1, 2), False),                                                # "not OLD, it's NEW"
    # "not OLD anymore, it is NEW" — the cessation filler ("anymore"/"any longer") sits between OLD
    # and the connective, so the previous pattern (which joins OLD straight to the connective) misses
    # it. Same (old,new) semantics; still requires a connective before NEW, and the unique-value-match
    # safety net below means a wrong detection can only ever re-value a View that already holds `old`.
    (re.compile(
        rf"\bnot\s+{_NUM}\s+(?:anymore|any\s+longer)\s*[,.]?\s+"
        rf"(?:it'?s|it\s+is|now\s+it'?s|now\s+it\s+is|make\s+it)\s+{_NUM}",
        re.I), (1, 2), False),                                                        # "not OLD anymore, it's NEW"
    # PROSE-SHAPED (needs_authorization=True): ordinary English. "I work from 9 to 5",
    # "My blood pressure went from 120 to 118", "I bought 6 instead of 4" all match these.
    (re.compile(rf"\bfrom\s+{_NUM}\s+to\s+{_NUM}", re.I), (1, 2), True),              # "from OLD to NEW"
    (re.compile(rf"{_NUM}\s+instead\s+of\s+{_NUM}", re.I), (2, 1), True),             # "NEW instead of OLD"
    # arrow form ("25 -> 20", "correction: 25 --> 20", "25 => 20"). The ASCII arrow is the required
    # connective between the two numbers (ASCII-only; no unicode arrow), so the two \d+ can never match
    # adjacent digits of one number. OLD -> NEW.
    (re.compile(rf"{_NUM}\s*(?:-+>|=>)\s*{_NUM}"), (1, 2), False),                     # "OLD -> NEW"
    # reverse from/to ("changed it to 20 from 25", "bump to 20 from 25") — the mirror of "from OLD to
    # NEW"; the "to ... from" connective disambiguates. NEW first, OLD second.
    (re.compile(rf"\bto\s+{_NUM}\s+from\s+{_NUM}", re.I), (2, 1), True),               # "to NEW from OLD"  PROSE-SHAPED
    # replacement phrasings — the connective word ("replacing"/"replaces"/"replaced by") is required.
    (re.compile(rf"{_NUM}\s+replac(?:es|ing)\s+{_NUM}", re.I), (2, 1), False),        # "NEW replaces OLD"
    (re.compile(rf"{_NUM}\s+replaced\s+by\s+{_NUM}", re.I), (1, 2), False),           # "OLD replaced by NEW"
]


def extract_correction_candidate(text: str) -> tuple[float, float, bool] | None:
    """STAGE 1 — permissive extraction. Return (old, new, needs_authorization) or None.

    A candidate is NOT permission to mutate memory. `needs_authorization` is True for the
    prose-shaped connectives, which occur constantly in ordinary speech; False for the
    intrinsically self-cueing ones, which carry their own correction intent.

    Exposed separately from `detect_correction` on purpose. A future conversational-context
    layer can authorize a bare candidate when the SURROUNDING turns supply the grounding this
    utterance lacks — e.g. after the assistant asks "Did you mean 25 or 20?", a bare
    "20 instead of 25" becomes safe. That layer should call this, apply its own evidence, and
    never widen the patterns below.
    """
    t = (text or "").replace("$", " ").strip()
    if not t:
        return None
    for rx, (old_g, new_g), needs_auth in _PATTERNS:
        m = rx.search(t)
        if not m:
            continue
        try:
            old, new = float(m.group(old_g)), float(m.group(new_g))
        except (TypeError, ValueError):
            continue
        if old != new:
            return (old, new, needs_auth)
    return None


def correction_intent_is_established(text: str) -> bool:
    """STAGE 2 — may this candidate authorize a destructive memory mutation?

    True only on evidence that the speaker is editing what MENHIR STORED, rather than
    describing a change in the world. Two admissible forms of evidence:

      * an explicit correction cue ("actually", "I meant", "correction:", "not X, ...");
      * an edit action whose target is grounded in the stored thing ("change IT from 25 to
        20", "update THE COUNT to 20"), including a subjectless imperative addressed to the
        assistant ("bump to 20 from 25").

    A change-verb whitelist is deliberately NOT used, and would be wrong: "The temperature
    changed from 25 to 20", "My dosage changed from 25 to 20", "I switched from 25 to 20 last
    month" and "The schedule was updated from 9 to 5" all carry change verbs and are ordinary
    descriptions. The verb is identical on both sides of the distinction; only the TARGET
    separates them, so that is what this tests.

    Precision-first by construction: missing a correction is recoverable (the stale value
    survives and the user can restate), whereas a false authorization silently overwrites an
    unrelated value AND stamps it `view_audit_gate="correction"`, which is indistinguishable
    from a real correction in the audit trail. Ambiguity therefore abstains.
    """
    t = (text or "").strip()
    if not t:
        return False
    lowered = t.lower()
    if any(cue in lowered for cue in _CORRECTION_CUES) or _CORRECTION_NOT_RE.search(lowered):
        return True
    # A description of the world is never an instruction to edit memory, even when it uses an
    # edit verb. Checked BEFORE the grounded-target test so "The temperature changed ..." can
    # never be rescued by an incidental pronoun later in the sentence.
    if _WORLD_SUBJECT_RE.search(t):
        return False
    return bool(_GROUNDED_EDIT_RE.search(t) or _IMPERATIVE_EDIT_RE.match(t))


def detect_correction(text: str) -> tuple[float, float] | None:
    """Return (old, new) when `text` authorizes a correction of a stored value, else None.

    Extraction + authorization. Callers treat a non-None result as permission to mutate, so
    this stays the authorized entry point; `extract_correction_candidate` is the unauthorized
    half for callers that supply their own evidence.
    """
    candidate = extract_correction_candidate(text)
    if candidate is None:
        return None
    old, new, needs_auth = candidate
    if needs_auth and not correction_intent_is_established(text):
        return None
    return (old, new)


def _num_eq(a: Any, b: float) -> bool:
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return False


def resolve_corrections(
    rows: list[dict[str, Any]],
    graph_adapter: Any,
    *,
    namespace: str,
    source: str = "perception",
    embed: Embed | None = None,
    run_tally: Any | None = None,
) -> dict[str, Any]:
    """Bind numeric corrections in `rows` (user turns, oldest first, with full source/fallback `valid_at`)
    to the unique value-matching current View in `namespace`, superseding it. Returns applied/ambiguous
    /no-target counts. Records `correction_applied` / `correction_ambiguous` receipts (out of recall)
    for observability, mirroring the F1 abstention receipts."""
    from menhir.services.event_fold import fold_events_to_counter

    applied = ambiguous = no_target = detected = 0
    details: list[dict[str, Any]] = []

    def _current_user_views() -> list[dict[str, Any]]:
        try:
            views = graph_adapter.list_counters(namespace=namespace, limit=200)
        except Exception:
            logger.warning("correction resolver: list_counters failed for %s", namespace, exc_info=True)
            return []
        return [v for v in views if str(v.get("subject")) != "perception"]

    for r in rows:
        text = str(r.get("content") or "")
        if text[:5].lower() == "user:":       # tolerate the legacy Episodic `user:` prefix
            text = text[5:]
        corr = detect_correction(text)
        if corr is None:
            continue
        old, new = corr
        detected += 1

        candidates = [v for v in _current_user_views() if _num_eq(v.get("value"), old)]
        if len(candidates) == 0:
            no_target += 1
            continue
        if len(candidates) > 1:
            ambiguous += 1
            logger.info("correction '%s'->'%s' ambiguous in %s: %d value-matching Views; abstaining",
                        old, new, namespace, len(candidates))
            continue

        cand = candidates[0]
        subject, measure = str(cand.get("subject") or "user"), str(cand.get("counter") or "")
        when = str(r.get("valid_at") or "")           # correction turn world time: strictly newer
        uid = str(r.get("uuid") or "") or None
        ev = Event(when=when, kind="assertion", value=new, episode_uuid=uid)
        audit = {
            "view_audit_gate": "correction",
            "view_audit_correction_from": float(old),
            "view_audit_reason": f"anaphoric correction {old:g} -> {new:g} bound to '{measure}'",
        }
        try:
            fold_events_to_counter(
                graph_adapter=graph_adapter, subject=subject, measure=measure, events=[ev],
                reducer="sum", namespace=namespace, source=source, embed=embed, audit=audit,
            )
        except Exception:
            logger.warning("correction resolver: write failed for (%s, %s)", subject, measure,
                           exc_info=True)
            continue
        applied += 1
        details.append({"measure": measure, "old": old, "new": new})
        logger.info("correction applied in %s: %s %g -> %g", namespace, measure, old, new)

    for label, n in (("correction_applied", applied), ("correction_ambiguous", ambiguous)):
        if n:
            # Route the run-level tally to the :Metric saga when wired; fall back to the legacy
            # :Entity receipt otherwise. Never raise on a diagnostic tally.
            try:
                if run_tally is not None:
                    run_tally.record_run_tally(
                        subject="perception", counter=label, value=float(n), namespace=namespace
                    )
                else:
                    graph_adapter.record_counter(
                        subject="perception", counter=label, value=float(n), namespace=namespace,
                        valid_at=None, source=source, name_embedding=None,
                    )
            except Exception:
                logger.warning("correction resolver: receipt %s failed", label, exc_info=True)

    return {
        "corrections_detected": detected,
        "corrections_applied": applied,
        "corrections_ambiguous": ambiguous,
        "corrections_no_target": no_target,
        "details": details,
    }
