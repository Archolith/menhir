"""BriefBuilder — cluster recalled memories into compact, provenance-preserving evidence
bundles instead of a flat score-ordered list.

Motivation (measured, not vibes): the ProvenanceClassifier showed retrieval is strong for
entity-answers (~90% in top-k) but the benchmark's two real levers are (a) answers that
require assembling a TEMPORAL CHAIN (temporal-reasoning ~80% supersession/date-chain,
knowledge-update supersession) and (b) knowledge-update "burying" where a superseded value
out-ranks the current one. A flat "[Memory i] name: content" brief scrambles chains and does
not mark currency, so the answer model has to reconstruct the timeline itself.

BriefBuilder groups on the TEMPORAL signal carried by each ScoredMemory.temporal_facts
(populated post-rank in recall, unconditionally):
  - dated facts (>=1 temporal_fact with valid_at) -> a single chronological Timeline bundle,
    ordered by world-time, each line marked (current) when it is the current belief.
  - undated memories -> compact per-memory evidence bundles, score-ordered.

The result is 3-6 bundles that keep MSC token efficiency while fixing the "many tiny
fragments / no chronology / no currency" problem. Pure domain logic: no I/O, fully testable.

v2 (deferred): render superseded->current pairs ("current: X (was: Y until <date>)"), which
needs recall called with include_invalidated=True so the superseded beliefs survive the
current-belief filter (recall_service.py:1137).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from menhir.domain.recall_visibility import memory_lifecycle_note

from menhir.domain.recall import ScoredMemory, TemporalFact

# Merge-shape labels (mirror the ProvenanceClassifier taxonomy). When build_context recalls with
# include_invalidated=True (brief-builder path), superseded beliefs are present and the Timeline
# is labelled a supersession chain; otherwise it is a plain date chain.
SHAPE_SUPERSESSION = "supersession_belief_chain"
SHAPE_CHRONOLOGICAL = "chronological_date_chain"
SHAPE_FLAT = "flat"

_MAX_LINE_CHARS = 240


@dataclass(frozen=True)
class EvidenceBundle:
    """A compact cluster of related evidence, provenance preserved via ``memory_uuids``."""

    heading: str
    shape: str
    lines: tuple[str, ...]
    memory_uuids: tuple[str, ...]
    score: float                 # max member final_score, for budget-order among bundles
    sort_key: str | None         # earliest valid_at (ISO) when dated, else None


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _MAX_LINE_CHARS else text[: _MAX_LINE_CHARS - 1] + "…"


def _dated_facts(m: ScoredMemory) -> list[TemporalFact]:
    return [tf for tf in m.temporal_facts if tf.valid_at]


def _has_superseded(memories: list[ScoredMemory]) -> bool:
    return any(not tf.is_current_belief for m in memories for tf in _dated_facts(m))


def _earliest_valid_at(m: ScoredMemory) -> str | None:
    vs = [tf.valid_at for tf in m.temporal_facts if tf.valid_at]
    return min(vs) if vs else None


def _memory_text(m: ScoredMemory) -> str:
    """Prefer the entity summary/content; fall back to the entity name."""
    return _lifecycle_text(m, m.content or m.name or "")


def _lifecycle_text(m: ScoredMemory, text: str) -> str:
    note = memory_lifecycle_note(
        status=m.status, artifact_status=m.artifact_status, superseded_by=m.superseded_by,
    )
    return f"{note} {_clip(text)}" if note else _clip(text)


def _parse_instant(valid_at: str) -> datetime | None:
    """Parse an ISO-8601 instant (trailing ``Z`` and an optional ``[Zone]`` suffix such
    as ``2026-01-01T00:01:00Z[UTC]``) into a naive-UTC datetime. None when unparseable."""
    raw = (valid_at or "").strip()
    text = re.sub(r"\[[^\]]*\]$", "", raw)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _instant_key(valid_at: str) -> tuple[int, object]:
    """Sort key over valid_at instants: parseable ones first (by instant), then
    unparseable ones by their raw string. Never raises."""
    raw = (valid_at or "").strip()
    dt = _parse_instant(raw)
    if dt is None:
        return (1, raw)
    return (0, dt)


def _label_formatter(instants: list[datetime]) -> Callable[[datetime], str]:
    """Pick the label precision for a timeline: day-only unless two DISTINCT instants
    share a calendar day (then minute precision), with second precision when two
    distinct instants share the same minute."""
    distinct = sorted(set(instants))
    if len(distinct) >= 2:
        if len({d.date() for d in distinct}) < len(distinct):
            if len({d.replace(second=0, microsecond=0) for d in distinct}) < len(distinct):
                return lambda dt: f"{dt:%Y-%m-%d %H:%M:%S}"
            return lambda dt: f"{dt:%Y-%m-%d %H:%M}"
    return lambda dt: f"{dt:%Y-%m-%d}"


def _timeline_lines(dated: list[ScoredMemory]) -> tuple[list[str], list[str]]:
    """Return (lines, uuids) for the chronological bundle: one line per (fact|memory),
    ordered by world-time (full instant, stable for ties), current beliefs marked.
    Undated-but-related facts are skipped here (they flow through the flat path)."""
    # (valid_at, is_current_belief, invalid_at, text, uuid, historical_memory)
    events: list[tuple[str, bool, str | None, str, str, bool]] = []
    for m in dated:
        facts = _dated_facts(m)
        historical = bool(memory_lifecycle_note(
            status=m.status, artifact_status=m.artifact_status, superseded_by=m.superseded_by,
        ))
        if facts:
            for tf in facts:
                text = tf.fact or m.content or m.name or ""
                events.append((
                    tf.valid_at or "", tf.is_current_belief, tf.invalid_at,
                    _lifecycle_text(m, text), m.uuid, historical,
                ))
        else:  # dated at the memory level but no fact string
            events.append((
                _earliest_valid_at(m) or "", True, None, _memory_text(m), m.uuid, historical,
            ))
    events.sort(key=lambda e: _instant_key(e[0]))
    parsed = [dt for dt in (_parse_instant(e[0]) for e in events) if dt is not None]
    fmt = _label_formatter(parsed)
    lines: list[str] = []
    uuids: list[str] = []
    seen: set[tuple[tuple[int, object], str]] = set()
    for valid_at, is_current, invalid_at, text, uuid, historical in events:
        key = (_instant_key(valid_at), text)
        if key in seen:
            continue
        seen.add(key)
        if is_current:
            tag = " (current belief)" if historical else " (current)"
        else:
            end_dt = _parse_instant(invalid_at) if invalid_at else None
            if end_dt is not None:
                end = fmt(end_dt)
            else:
                end = (invalid_at or "")[:10]
            tag = f" (superseded until {end})" if end else " (superseded)"
        dt = _parse_instant(valid_at)
        label = fmt(dt) if dt is not None else (valid_at or "")[:10]
        lines.append(f"- [{label}] {text}{tag}")
        uuids.append(uuid)
    return lines, uuids


def build_timeline_bundle(
    memories: list[ScoredMemory], *, min_points: int = 1,
) -> EvidenceBundle | None:
    """Build a single chronological Timeline bundle from the dated facts in `memories`
    (world-time ordered, currency-marked). Returns None when nothing is dated, or when the
    number of DISTINCT valid_at instants among the rendered lines is below `min_points`
    (the long-running gate: a timeline needs a real history to be worth its tokens).
    `min_points=1` preserves the previous behavior for direct callers.

    This is a SUPPLEMENTARY view: an A/B showed that leading the brief with the Timeline
    (displacing recall's relevance order) is net-negative — the answer is usually the
    top-relevance, often-undated memory, which the Timeline demoted. So the context
    builder now packs the relevance-ranked list first and APPENDS this bundle below, giving
    the model the ordered chain for genuine temporal questions without burying the answer.
    """
    dated = [m for m in memories if _dated_facts(m)]
    if not dated:
        return None
    distinct_instants = {
        _instant_key(tf.valid_at or "") for m in dated for tf in _dated_facts(m)
    }
    if len(distinct_instants) < max(1, min_points):
        return None
    lines, uuids = _timeline_lines(dated)
    if not lines:
        return None
    return EvidenceBundle(
        heading="Timeline",
        shape=SHAPE_SUPERSESSION if _has_superseded(dated) else SHAPE_CHRONOLOGICAL,
        lines=tuple(lines),
        memory_uuids=tuple(uuids),
        score=max(m.final_score for m in dated),
        sort_key=min((_earliest_valid_at(m) for m in dated if _earliest_valid_at(m)), default=None),
    )


def build_bundles(memories: list[ScoredMemory], *, max_bundles: int = 6) -> list[EvidenceBundle]:
    """Cluster recalled memories into <= max_bundles evidence bundles (Timeline-first).

    NOTE: retained for tests/experiments. The shipped context-builder path uses
    build_timeline_bundle as an APPENDED view instead, because Timeline-first measured
    net-negative (it displaces relevance order). See build_timeline_bundle.
    """
    if not memories:
        return []

    undated = [m for m in memories if not _dated_facts(m)]
    bundles: list[EvidenceBundle] = []
    tl = build_timeline_bundle(memories)
    if tl is not None:
        bundles.append(tl)

    for m in sorted(undated, key=lambda x: x.final_score, reverse=True):
        if len(bundles) >= max_bundles:
            break
        bundles.append(EvidenceBundle(
            heading=_clip(m.name or "Memory"),
            shape=SHAPE_FLAT,
            lines=(f"- {_memory_text(m)}",),
            memory_uuids=(m.uuid,),
            score=m.final_score,
            sort_key=None,
        ))
    return bundles[:max_bundles]


def render_bundles(bundles: list[EvidenceBundle], *, include_provenance: bool = False) -> str:
    """Render bundles to a compact brief string. Provenance uuids are appended per bundle
    only when include_provenance (debug/eval); the default brief stays token-lean."""
    blocks: list[str] = []
    for b in bundles:
        head = f"=== {b.heading} ==="
        body = "\n".join(b.lines)
        if include_provenance:
            body += f"\n  ({', '.join(b.memory_uuids)})"
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks)
