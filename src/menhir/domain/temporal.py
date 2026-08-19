"""Bitemporal fact metadata (R3 rung B / Chronostratum Rung 1).

menhir facts (graphiti :RELATES_TO edges) carry four timestamps that must NOT be
collapsed into "fact + timestamp":

    world time  -- when the fact was true:        valid_at / invalid_at
    belief time -- when menhir knew it:           created_at / expired_at

The clock model (menhir-temporal-chronostratum-plan.md), as deterministic filters:

    current belief recall:   expired_at IS NULL
    history / belief-drift:  include expired_at IS NOT NULL
    as-of (world time):      valid_at <= as_of   AND (invalid_at IS NULL OR invalid_at > as_of)
    what-did-we-know-then:   created_at <= known_as_of AND (expired_at IS NULL OR expired_at > known_as_of)

This is the deterministic temporal substrate. It is the single PRODUCER of the
current/superseded/validity signal; the belief currentness policy (belief.py) is the
CONSUMER that turns it into an assertion decision. Keeping production here (deterministic,
git-groundable) and policy there avoids two copies of supersession logic drifting apart.

Pure functions over ISO-8601 timestamps; no graph access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def parse_iso8601(value: Any) -> datetime | None:
    """Canonical tolerant parse for a timestamp read back out of Neo4j. Returns None if absent or
    unparseable (never raises); a naive result is treated as UTC so callers may always compare.

    `toString(n.valid_at)` renders as ``2026-07-21T19:13:04.572Z[UTC]`` -- a trailing zone-id
    suffix that ``datetime.fromisoformat`` rejects. Stripping it is the whole point of this
    function: without it every read-back timestamp parses to None and each caller then fails in
    its own way (the fold collapsed to its _EPOCH fallback and abstained; the LWW guard fell open
    and stopped firing). That bug was found and fixed three separate times in three copies of this
    parser, so it lives here once now. Anything parsing a Neo4j timestamp MUST use this."""
    if not value:
        return None
    text = str(value).strip()
    bracket = text.find("[")
    if bracket != -1:                       # drop a trailing zone-id suffix like "[UTC]"
        text = text[:bracket]
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


#: CF-55: the bitemporal filters below now use the canonical parser, as its own docstring says
#: everything reading a Neo4j timestamp must.
#:
#: There WAS a 4th copy here that did not strip the zone-id suffix, kept separate on the
#: reasoning that switching it "would change what the bitemporal filters consider parseable,
#: i.e. real recall-behaviour change -- so it is a decision, not a cleanup." That was right about
#: the risk and silent about the current behaviour already being wrong: `toString()` is exactly
#: the format recall projects these fields in, the old parser returned None for it, and an
#: unparseable bound is SKIPPED rather than raised (`if va is not None and va > pivot`). So every
#: bound silently stopped constraining and every filter returned True. Fail-open by construction,
#: on the one format production actually produces.
#:
#: Results on any enabled path will change: inputs that were passing incorrectly will start being
#: excluded. That is the fix, not a regression.
#:
#: A second defect goes with it. The old parser returned NAIVE datetimes for unsuffixed input,
#: while a pivot parsed from an offset-bearing string is aware -- comparing the two raises
#: TypeError. `parse_iso8601` treats a naive result as UTC, so every comparison below is now
#: between two aware values.
_parse = parse_iso8601


@dataclass(frozen=True)
class FactTemporal:
    """The four bitemporal stamps on a fact (any may be absent)."""

    valid_at: str | None = None      # world: became true
    invalid_at: str | None = None    # world: stopped being true
    created_at: str | None = None    # belief: menhir learned it
    expired_at: str | None = None    # belief: menhir stopped believing it


class TemporalRole(str, Enum):
    """How a fact relates to the query's time — surfaced to the agent, not collapsed."""

    CURRENT = "current"              # current belief, valid at the reference time
    HISTORICAL = "historical"       # belief expired (superseded in belief time)
    NOT_YET_VALID = "not_yet_valid" # world-validity starts after the reference time
    EXPIRED_WORLD = "expired_world" # no longer true in the world (invalid_at passed)
    NOT_YET_KNOWN = "not_yet_known" # learned AFTER the as-of point (temporal leakage)
    UNKNOWN = "unknown"


class TemporalQuery(str, Enum):
    """The temporal lens a recall applies."""

    CURRENT_BELIEF = "current_belief"  # what menhir currently believes (expired_at IS NULL)
    AS_OF_WORLD = "as_of_world"        # what was true in the world at as_of
    AS_KNOWN_AT = "as_known_at"        # what menhir knew at known_as_of


def is_current_belief(fact: FactTemporal) -> bool:
    """A fact menhir still believes: its belief has not expired."""
    return fact.expired_at is None


def is_valid_at(fact: FactTemporal, as_of: str) -> bool:
    """World-time: the fact was true at ``as_of`` (valid_at <= as_of < invalid_at)."""
    pivot = _parse(as_of)
    if pivot is None:
        return False
    va, iv = _parse(fact.valid_at), _parse(fact.invalid_at)
    if va is not None and va > pivot:
        return False
    if iv is not None and iv <= pivot:
        return False
    return True


def was_known_at(fact: FactTemporal, known_as_of: str) -> bool:
    """Belief-time: menhir knew the fact at ``known_as_of`` (created_at <= t < expired_at)."""
    pivot = _parse(known_as_of)
    if pivot is None:
        return False
    ca, ex = _parse(fact.created_at), _parse(fact.expired_at)
    if ca is not None and ca > pivot:
        return False
    if ex is not None and ex <= pivot:
        return False
    return True


def matches_query(fact: FactTemporal, query: TemporalQuery, *, as_of: str | None = None) -> bool:
    """Deterministic recall filter for one temporal lens.

    CURRENT_BELIEF needs no as_of. AS_OF_WORLD / AS_KNOWN_AT require an as_of pivot;
    without one they fall back to current-belief (safe default — never leak history)."""
    if query is TemporalQuery.CURRENT_BELIEF:
        return is_current_belief(fact)
    if as_of is None:
        return is_current_belief(fact)
    if query is TemporalQuery.AS_OF_WORLD:
        return is_valid_at(fact, as_of)
    if query is TemporalQuery.AS_KNOWN_AT:
        return was_known_at(fact, as_of)
    return is_current_belief(fact)


def temporal_role(fact: FactTemporal, *, as_of: str | None = None) -> TemporalRole:
    """Classify a fact's relation to the (optional) reference time, for display.

    Belief expiry dominates (HISTORICAL) — a superseded belief is historical regardless of
    world validity. With an as_of pivot, world-time edges (not-yet-valid / expired-world)
    and the not-yet-known anachronism are distinguished."""
    if fact.expired_at is not None:
        return TemporalRole.HISTORICAL
    if as_of is not None:
        pivot = _parse(as_of)
        if pivot is not None:
            ca = _parse(fact.created_at)
            if ca is not None and ca > pivot:
                return TemporalRole.NOT_YET_KNOWN  # learned after the as-of point
            va = _parse(fact.valid_at)
            if va is not None and va > pivot:
                return TemporalRole.NOT_YET_VALID
            iv = _parse(fact.invalid_at)
            if iv is not None and iv <= pivot:
                return TemporalRole.EXPIRED_WORLD
    if fact.valid_at is None and fact.created_at is None:
        return TemporalRole.UNKNOWN
    return TemporalRole.CURRENT


_FAR_FUTURE = "9999-12-31"


def world_time_key(fact: FactTemporal) -> tuple[str, str]:
    """Sort key by world time (valid_at), then belief time (created_at); missing sorts last.

    Ordering keys on when a fact was TRUE, not when menhir learned it — so a memory
    ingested late still threads into its correct chronological place (Chronostratum Rung 3,
    ingestion-order independence)."""
    return (fact.valid_at or _FAR_FUTURE, fact.created_at or _FAR_FUTURE)


def order_by_world_time(facts: list[tuple[str, FactTemporal]]) -> list[str]:
    """Return fact ids ordered by world time, independent of the input (ingestion) order.

    The keystone of ingestion-order independence: feed facts in any order (scrambled
    ingestion) and get the true chronological timeline back, because ordering uses
    valid_at, not created_at/insertion order. Deterministic (id breaks ties)."""
    return [fid for fid, _ in sorted(facts, key=lambda pair: (*world_time_key(pair[1]), pair[0]))]


def format_temporal_provenance(fact: FactTemporal, *, as_of: str | None = None) -> str:
    """Render a fact's happened-time vs learned-time for the agent (Chronostratum Rung 1C).

    The point is to NOT flatten world-time and belief-time into one date:
        happened  = valid_at   (when it was true in the world)
        learned   = created_at  (when menhir came to believe it)
        no longer believed since = expired_at (belief invalidation)
        stopped being true at    = invalid_at (world invalidation)

    Produces a compact, deterministic label, e.g.
        "happened 2026-01-05, learned 2026-01-07"
        "happened 2026-01-07, learned 2026-01-07, no longer believed since 2026-01-08".
    """
    parts: list[str] = []
    if fact.valid_at is not None:
        parts.append(f"happened {fact.valid_at}")
    if fact.created_at is not None:
        parts.append(f"learned {fact.created_at}")
    if fact.invalid_at is not None:
        parts.append(f"stopped being true {fact.invalid_at}")
    if fact.expired_at is not None:
        parts.append(f"no longer believed since {fact.expired_at}")
    if not parts:
        return "no temporal information"
    label = ", ".join(parts)
    role = temporal_role(fact, as_of=as_of)
    return f"{label} [{role.value}]"
