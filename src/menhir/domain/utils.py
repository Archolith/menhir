"""Shared utilities used across multiple menhir modules."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from menhir.domain.truth.kinds import (
    SOURCE_CONFIDENCE_AGENT,
    SOURCE_CONFIDENCE_AGENT_REVIEWED,
    SOURCE_CONFIDENCE_STRUCTURAL,
    SOURCE_CONFIDENCE_USER,
)

__all__ = [
    "days_ago",
    "excerpt",
    "decode_json_value",
    "source_confidence_for",
    "symbol_structure_path",
]


def days_ago(dt: object | None, *, default: float = 30.0) -> float:
    """Convert a datetime (or None) to days-since-now.

    Returns *default* when *dt* is None or not a recognised datetime type.
    Handles Neo4j ``DateTime`` objects via ``.to_native()``.
    """
    if dt is None:
        return default
    if hasattr(dt, "to_native"):
        dt = dt.to_native()
    if isinstance(dt, datetime):
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    return default


def excerpt(value: object | None, *, limit: int = 160) -> str | None:
    """Collapse whitespace and truncate *value* to *limit* characters."""
    if value is None:
        return None
    rendered = " ".join(str(value).split())
    if not rendered:
        return None
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(0, limit - 3)] + "..."


def decode_json_value(value: object | None) -> object | None:
    """Try to JSON-parse a string value; return as-is otherwise."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


#: THE source-label -> trust-tier table. Single source of truth (SSOT-14); every writer derives from
#: it and nothing re-derives its own.
#:
#: WHY IT EXISTS. `project-scan` was absent, so this function fell through to the agent default while
#: `structure_queries.py` deliberately stamped SOURCE_CONFIDENCE_STRUCTURAL for the same label. Each
#: file was self-consistent and they disagreed with each other. Measured in production 2026-07-28:
#: 48,781 :Entity carried 0.9 and 76 :Episodic carried 0.5 for the identical source string. A merge
#: fix that "recomputes confidence from source" -- the obvious repair for the entity-merge inflation
#: bug -- would have silently demoted all 48,781 to agent tier while fixing a 2% problem.
#:
#: Keys are matched against the stripped, lowercased label.
_SOURCE_TIERS: dict[str, float] = {
    "manual": SOURCE_CONFIDENCE_USER,
    "user": SOURCE_CONFIDENCE_USER,
    # Deterministic ground truth: verifiable without a human reviewer. kinds.py names the category as
    # "git, file, test, project-scan", but ONLY project-scan is listed here, because it is the only
    # one any writer actually emits (infrastructure/structure_queries.py). `test` in particular must
    # NOT be added: throughout this suite `source="test"` is a fixture placeholder, not a claim that
    # a test produced the fact, so promoting it to structural tier would be both wrong and disruptive.
    # Add a label here when a writer starts emitting it -- not on the strength of the prose above.
    "project-scan": SOURCE_CONFIDENCE_STRUCTURAL,
    "claude-code": SOURCE_CONFIDENCE_AGENT_REVIEWED,
}

#: Substring rules, consulted only when no exact key matches. These are model-family downweights, not
#: rungs on the ladder -- 0.3 has no constant in kinds.py because it is not a tier, it is a judgement
#: that one model family is less reliable than a generic agent. Preserved as-is; revisiting the value
#: is a product decision separate from unifying the mapping.
_SOURCE_SUBSTRING_TIERS: tuple[tuple[str, float], ...] = (
    ("gemini", 0.3),
    ("gpt", SOURCE_CONFIDENCE_AGENT),
)


def source_confidence_for(source: str) -> float:
    """Map a source label to its trust tier. THE authority -- do not re-derive this mapping.

    Returns the tier CEILING for the label. A writer with a weaker observation may deliberately stamp
    lower (e.g. structure_queries stamps an *inferred* import target `source='project-scan'` but
    SOURCE_CONFIDENCE_AGENT, because the node's existence is inferred rather than scanned). That is a
    legitimate, visible downgrade. What is NOT legitimate is a writer independently deciding a label's
    tier is HIGHER than this table says -- that is the divergence this table was created to end.

    "user" is the apex trust tier (SOURCE_CONFIDENCE_USER, matching domain.truth.kinds and the Trusted
    Memory Admission design doc's T3-T5 ladder) -- previously hardcoded to 0.9 here, which was drift
    from that documented intent, not an alternate valid design (SSOT-09).
    """
    source_key = (source or "").strip().lower()
    if source_key in _SOURCE_TIERS:
        return _SOURCE_TIERS[source_key]
    for needle, tier in _SOURCE_SUBSTRING_TIERS:
        if needle in source_key:
            return tier
    return SOURCE_CONFIDENCE_AGENT


#: Source-label families. Two labels in the same family are ONE writer wearing different hats, so
#: corroboration must count the family once. `claude-code` and `claude-chat` are the same agent;
#: `codex` and `codex-gpt-5` are the same tool. Counting raw labels would let one writer corroborate
#: itself by being renamed -- a weaker version of the very defect this replaces (merge count inflating
#: trust). Longest-prefix-wins is NOT implemented: order here is significance order, and `codex`
#: precedes `gpt` so `codex-gpt-5` resolves to codex rather than to the model it runs on.
_SOURCE_FAMILY_PREFIXES: tuple[str, ...] = (
    "project-scan",
    "claude",
    "codex",
    "opencode",
    "qwen",
    "gemini",
    "gpt",
    "painscan",
    "document-ingest",
    "perception",
)


def source_family(source: str) -> str:
    """The writer family a source label belongs to. Unknown labels are their own family.

    Deliberately coarse. A label that matches no known prefix is returned as-is rather than bucketed
    into an 'other' family, because collapsing unrelated one-off sources together would UNDERCOUNT
    corroboration -- and undercounting is the safe direction only until it merges genuinely
    independent writers into one.
    """
    key = (source or "").strip().lower()
    if not key:
        return "unknown"
    for prefix in _SOURCE_FAMILY_PREFIXES:
        if key == prefix or key.startswith(f"{prefix}-") or key.startswith(f"{prefix} "):
            return prefix
    return key


#: The label `primary_source_for` stamps on a node that has NO contributors at all. It is a
#: placeholder for the `source` slot, NOT a writer -- `node_contributors` must never read it back as
#: one. Without that exclusion a chain of source-less merges manufactures provenance out of nothing:
#: the first merge writes source='merged', the second reads it as a contributor, and the node ends up
#: claiming one corroborating writer that never existed.
SYNTHETIC_PRIMARY_SOURCE = "merged"


def normalize_contributors(raw: object) -> list[str]:
    """Ordered, de-duplicated contributor labels from either representation.

    Accepts the `sources` list OR the legacy comma-joined `source` string, so a node written before
    the list existed is readable without migrating it first. Order is preserved (first contributor
    stays first) because it is the only surviving trace of merge order.
    """
    if isinstance(raw, str):
        items: list[str] = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        label = str(item).strip()
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def node_contributors(sources: object, source: object) -> list[str]:
    """A node's real contributor labels, read from whichever representation it carries.

    Two distinctions that a plain ``sources or source`` fallback gets wrong:

    * A MISSING/`null` `sources` means "written before the list existed" -- fall back to the legacy
      comma-joined `source`. An EXPLICITLY EMPTY `sources` means "this node has no contributors",
      which is a derived fact the merge itself wrote; falling back there would resurrect the
      placeholder `source` as if it were provenance.
    * `merged` is that placeholder, never a writer (see SYNTHETIC_PRIMARY_SOURCE).
    """
    raw = sources if sources is not None else source
    return [s for s in normalize_contributors(raw) if s != SYNTHETIC_PRIMARY_SOURCE]


def authority_for(sources: list[str]) -> float:
    """The NOMINAL trust ceiling of a set of contributors: the LOWEST tier any of them carries.

    Lowest, not highest or averaged, so that combining evidence can never manufacture authority the
    weakest contributor did not have. `min` makes it structurally impossible for a merge to raise
    trust -- the property the previous `+0.1` violated, guaranteed by construction rather than by a
    cap that merely bounded how far it could be violated.

    This is a CEILING, not the node's confidence. `source_confidence_for` documents that a writer may
    deliberately stamp lower than its label allows; use `effective_authority` to combine that observed
    value with this bound.
    """
    return min((source_confidence_for(s) for s in sources), default=SOURCE_CONFIDENCE_AGENT)


def effective_authority(sources: list[str], observed: object) -> float:
    """What a node's authority ACTUALLY is: its stored confidence, bounded by its label ceiling.

    Deriving authority from labels alone was wrong in both directions. A node explicitly downgraded
    below its label -- `structure_queries` stamps an *inferred* import target `source='project-scan'`
    at agent tier, not the structural tier the label allows -- would be RAISED back to the ceiling by
    a merge, silently promoting an inference to scanned ground truth. A legacy node carrying an
    inflated confidence (the pre-`01a10e4` `+0.1` accumulation left `claude-code` nodes at 1.0) must
    still be capped by what its labels can justify.

    So: take the observed value when it is a usable number, and never let it exceed the ceiling. A
    missing or malformed `source_confidence` falls back to the ceiling, which for a node with no
    contributors is the agent default -- the same conservative answer the tier table gives.

    USABLE means inside the tier ladder's domain, ``0.0 .. SOURCE_CONFIDENCE_USER``. A finite value
    outside it is corruption, not a downgrade, and must not be propagated just because `min` happens
    to accept it: a stored ``-0.25`` would otherwise become the merged node's authority and, being
    below every tier, would sit under any threshold the ladder defines. Out-of-range therefore takes
    the same path as a non-numeric value -- the observation carries no information, so the label
    ceiling answers instead. That is a repair of a corrupt read, NOT a promotion: the ceiling is still
    a real bound on the node, and a value ABOVE the domain is capped by it in the same step.
    """
    ceiling = authority_for(sources)
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return ceiling
    value = float(observed)
    if not math.isfinite(value) or not (0.0 <= value <= SOURCE_CONFIDENCE_USER):
        return ceiling
    return min(value, ceiling)


def primary_source_for(sources: list[str]) -> str:
    """The single label that carries the authority -- the lowest-tier contributor.

    It names the CEILING that binds the node, so `node.source_confidence <= source_confidence_for(
    node.source)` always holds. The stronger EQUALITY does not: a node may be explicitly stamped
    below its label's tier, and preserving that downgrade across a merge is the whole point of
    `effective_authority`. Ties break on contributor order, so the result is deterministic.
    """
    if not sources:
        return SYNTHETIC_PRIMARY_SOURCE
    return min(sources, key=lambda s: (source_confidence_for(s), sources.index(s)))


def corroboration_for(sources: list[str]) -> int:
    """How many INDEPENDENT writers assert this -- distinct families, not distinct labels.

    Separate from authority on purpose: authority is who said it, corroboration is how much
    independent observation agrees. Smearing the second onto the first is what made
    `source_confidence` incoherent. One writer repeating itself scores 1, however many duplicates it
    produced.
    """
    return len({source_family(s) for s in sources})


def symbol_structure_path(file_path: str, name: str, parent: str) -> str:
    """Compute the fully qualified structure_path for a code symbol.

    Single source of truth (SSOT-12): `infrastructure/project_scanner.py`'s
    `_sym_path` and `infrastructure/structure_queries.py`'s `_symbol_path`
    were two independent copies of this exact logic (one computed at scan
    time, the other at query/write time) that could silently diverge if
    edited separately. Takes plain strings rather than a `SymbolEntry`
    instance so this stays a domain-layer utility with no dependency on the
    infrastructure-layer scanner dataclass.
    """
    if parent:
        return f"{file_path}::{parent}.{name}"
    return f"{file_path}::{name}"
