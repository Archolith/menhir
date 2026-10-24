"""Who said a recall result: derived from the raw episode signals behind it (pure, no I/O).

The graph query returns raw signals (episode sources, first-seen time, turn-evidence ids);
the classification lives here. A source is human when the intake gate stamped it with one of
``GATE_APPROVED_HUMAN_SOURCES``; any other non-empty source is the agent side (agent inference,
tool names, document ingest, ...). Renderers label only non-user items so output for
user-stated items is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from menhir.domain.self_identity import GATE_APPROVED_HUMAN_SOURCES

_MAX_TURN_EVIDENCE_IDS = 5
# Values Graphiti writes to Episodic.source before Menhir stamps the real one.
_GRAPHITI_EPISODE_TYPES = frozenset({"message", "json", "text", "fact_triple"})

_LABELS = {
    "agent": "agent-recorded",
    "mixed": "user + agent",
    "unknown": "provenance unknown",
}


@dataclass(frozen=True)
class RecallProvenance:
    said_by: str  # "user" | "agent" | "mixed" | "unknown"
    sources: tuple[str, ...] = ()
    first_seen_at: str | None = None
    turn_evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "said_by": self.said_by,
            "sources": list(self.sources),
            "first_seen_at": self.first_seen_at,
            "turn_evidence_ids": list(self.turn_evidence_ids),
        }


def derive_recall_provenance(row: Mapping[str, object]) -> RecallProvenance:
    raw_sources = row.get("sources") or []
    # Graphiti's own episode-type default ('message', ...) is not a Menhir source stamp.
    sources = sorted(
        {
            str(s) for s in raw_sources
            if s is not None and str(s).strip()
            and str(s).lower().strip() not in _GRAPHITI_EPISODE_TYPES
        }
    )
    has_human = False
    has_agent = False
    for source in sources:
        if source.lower().strip() in GATE_APPROVED_HUMAN_SOURCES:
            has_human = True
        else:
            has_agent = True
    if has_human and has_agent:
        said_by = "mixed"
    elif has_human:
        said_by = "user"
    elif has_agent:
        said_by = "agent"
    else:
        said_by = "unknown"
    first_seen = row.get("first_seen_at")
    if first_seen is not None and not isinstance(first_seen, str):
        iso = getattr(first_seen, "isoformat", None)
        first_seen = iso() if callable(iso) else str(first_seen)
    turn_ids = sorted({str(t) for t in (row.get("turn_evidence_ids") or []) if t is not None})
    return RecallProvenance(
        said_by=said_by,
        sources=tuple(sources),
        first_seen_at=first_seen or None,
        turn_evidence_ids=tuple(turn_ids[:_MAX_TURN_EVIDENCE_IDS]),
    )


def provenance_label(p: RecallProvenance | Mapping[str, object] | None) -> str | None:
    """Short renderer label; None for no provenance or a user-stated item (render unchanged)."""
    if p is None:
        return None
    said_by = p.said_by if isinstance(p, RecallProvenance) else p.get("said_by")
    return _LABELS.get(str(said_by))
