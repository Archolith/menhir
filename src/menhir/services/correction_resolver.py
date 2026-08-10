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

#: (compiled pattern, (old_group_index, new_group_index)). Only UNAMBIGUOUS corrective connectives —
#: `X, not Y`, `not X ... Y`, `from X to Y`, `X instead of Y`, `X -> Y` (ASCII arrow), `to X from Y`,
#: `X replaces/replacing Y`, `X replaced by Y`. Ordered; the first match wins. The value-match against
#: an existing View is the real safety net, so a loose detector can never write a wrong View (it can
#: only re-value a View that already holds exactly `old`).
_PATTERNS: list[tuple[re.Pattern[str], tuple[int, int]]] = [
    (re.compile(rf"{_NUM}\s*,?\s+not\s+{_NUM}", re.I), (2, 1)),                       # "NEW, not OLD"
    # reversed form REQUIRES a connective word between the numbers, so the two \d+ can never match
    # adjacent digits of one number ("25" -> "2","5") — that separator is what disambiguates them.
    (re.compile(rf"\bnot\s+{_NUM}\s*,?\s+(?:it'?s|it\s+is|but|rather|instead|make\s+it)\s+{_NUM}",
                re.I), (1, 2)),                                                       # "not OLD, it's NEW"
    # "not OLD anymore, it is NEW" — the cessation filler ("anymore"/"any longer") sits between OLD
    # and the connective, so the previous pattern (which joins OLD straight to the connective) misses
    # it. Same (old,new) semantics; still requires a connective before NEW, and the unique-value-match
    # safety net below means a wrong detection can only ever re-value a View that already holds `old`.
    (re.compile(
        rf"\bnot\s+{_NUM}\s+(?:anymore|any\s+longer)\s*[,.]?\s+"
        rf"(?:it'?s|it\s+is|now\s+it'?s|now\s+it\s+is|make\s+it)\s+{_NUM}",
        re.I), (1, 2)),                                                               # "not OLD anymore, it's NEW"
    (re.compile(rf"\bfrom\s+{_NUM}\s+to\s+{_NUM}", re.I), (1, 2)),                    # "from OLD to NEW"
    (re.compile(rf"{_NUM}\s+instead\s+of\s+{_NUM}", re.I), (2, 1)),                   # "NEW instead of OLD"
    # arrow form ("25 -> 20", "correction: 25 --> 20", "25 => 20"). The ASCII arrow is the required
    # connective between the two numbers (ASCII-only; no unicode arrow), so the two \d+ can never match
    # adjacent digits of one number. OLD -> NEW.
    (re.compile(rf"{_NUM}\s*(?:-+>|=>)\s*{_NUM}"), (1, 2)),                            # "OLD -> NEW"
    # reverse from/to ("changed it to 20 from 25", "bump to 20 from 25") — the mirror of "from OLD to
    # NEW"; the "to ... from" connective disambiguates. NEW first, OLD second.
    (re.compile(rf"\bto\s+{_NUM}\s+from\s+{_NUM}", re.I), (2, 1)),                     # "to NEW from OLD"
    # replacement phrasings — the connective word ("replacing"/"replaces"/"replaced by") is required.
    (re.compile(rf"{_NUM}\s+replac(?:es|ing)\s+{_NUM}", re.I), (2, 1)),               # "NEW replaces OLD"
    (re.compile(rf"{_NUM}\s+replaced\s+by\s+{_NUM}", re.I), (1, 2)),                  # "OLD replaced by NEW"
]


def detect_correction(text: str) -> tuple[float, float] | None:
    """Return (old_value, new_value) if `text` is a numeric correction, else None. Deterministic and
    precision-first: matches only the unambiguous corrective connectives, and requires old != new."""
    t = (text or "").replace("$", " ").strip()
    if not t:
        return None
    for rx, (old_g, new_g) in _PATTERNS:
        m = rx.search(t)
        if not m:
            continue
        try:
            old, new = float(m.group(old_g)), float(m.group(new_g))
        except (TypeError, ValueError):
            continue
        if old != new:
            return (old, new)
    return None


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
