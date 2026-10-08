"""Persist the anchored-time contract on the RELATES_TO edges it was computed for (P2).

Graphiti's edge resolution clears hook-time edge attributes, so the contract is written by a
Menhir-owned step after ``add_episode`` returns. Every write is bound to the call that produced it:

- the report must have been created during this call (a reused receipt can carry an older one);
- an edge uuid must appear in this call's returned edges (a duplicate resolves to an existing
  edge with another uuid, so the extracted uuid is absent and nothing is written);
- the edge must list this call's episode and carry no contract yet (write-once; a replay is a
  no-op), and belong to this call's tenant.

Failure never fails ingest: missing properties mean exactly the pre-P2 behavior.

P3 (MENHIR_ANCHORED_TIME_EXPIRY): when a row carries ``time_world_end``, the same locked write
also clears the ``expired_at`` Graphiti set because the fact had its own end, but only while
``invalid_at`` is still that end. A contradiction that changed it keeps its expiry.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import date, datetime, timezone
import json
import logging
from typing import Any

from menhir.domain.namespace import (
    DEFAULT_NAMESPACE,
    namespace_spellings,
    tenant_scope_cypher,
)

logger = logging.getLogger(__name__)

CONTRACT_SCHEMA = "at1"
EXPRESSION_MAX_CHARS = 120
PERSIST_TIMEOUT_S = 5.0

#: Every property this step may set. Erasure needs no registry: they live on the edge.
CONTRACT_PROPERTIES = (
    "time_basis",
    "time_expression",
    "time_kind",
    "time_granularity",
    "time_window_start",
    "time_window_end",
    "time_anchor_ref",
    "time_anchor_offset",
    "time_planned_start",
    "time_planned_end",
    "time_outcome",
    "time_speech_date",
    "time_ambiguity",
    "time_contract",
)

_ELIGIBLE = (
    tenant_scope_cypher("r") + " "
    "AND r.group_id = $group_id "
    "AND r.time_contract IS NULL "
    "AND $episode_uuid IN coalesce(r.episodes, [])"
)

#: Read-committed reads do not lock, so two concurrent writers could both see
#: ``time_contract IS NULL``. The dummy SET/REMOVE takes the edge's write lock (held to commit)
#: and every condition is re-read under it; tests/test_anchored_time_persist_lock_live.py races it.
LOCK_PROPERTY = "_menhir_time_lock"

PERSIST_CYPHER = (
    "UNWIND $rows AS row "
    "MATCH ()-[r:RELATES_TO {uuid: row.uuid}]->() "
    "WHERE " + _ELIGIBLE + " "
    f"SET r.{LOCK_PROPERTY} = true "
    f"REMOVE r.{LOCK_PROPERTY} "
    "WITH r, row "
    "WHERE " + _ELIGIBLE + " "
    "SET " + ", ".join(f"r.{name} = row.{name}" for name in CONTRACT_PROPERTIES) + " "
    "RETURN count(r) AS written"
)

#: P3 (MENHIR_ANCHORED_TIME_EXPIRY): same lock and re-check, then clear the ``expired_at`` that
#: Graphiti set only because the edge carried its own end (edge_operations.py path 1). Cleared
#: only while ``invalid_at`` is still the instant the hook recorded: a contradiction changes
#: ``invalid_at``, so it keeps its expiry (tests/test_anchored_time_expiry_live.py races this).
EXPIRY_PERSIST_CYPHER = (
    "UNWIND $rows AS row "
    "MATCH ()-[r:RELATES_TO {uuid: row.uuid}]->() "
    "WHERE " + _ELIGIBLE + " "
    f"SET r.{LOCK_PROPERTY} = true "
    f"REMOVE r.{LOCK_PROPERTY} "
    "WITH r, row "
    "WHERE " + _ELIGIBLE + " "
    "SET " + ", ".join(f"r.{name} = row.{name}" for name in CONTRACT_PROPERTIES) + " "
    "WITH r, row, (row.time_world_end_ms IS NOT NULL AND r.expired_at IS NOT NULL "
    "AND r.invalid_at IS NOT NULL "
    "AND datetime(r.invalid_at).epochMillis = row.time_world_end_ms) AS unexpire "
    "FOREACH (_ IN CASE WHEN unexpire THEN [1] ELSE [] END | "
    "SET r.expired_at = null, r.time_expiry = 'world_end', r.time_world_end = row.time_world_end) "
    "RETURN count(r) AS written, sum(CASE WHEN unexpire THEN 1 ELSE 0 END) AS unexpired"
)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def contract_rows(report: Any, edge_uuids: Iterable[str], speech_date: date | None) -> list[dict]:
    """One parameter row per result whose edge is among ``edge_uuids``. Pure."""
    allowed = {str(u) for u in edge_uuids if u}
    contract = f"{CONTRACT_SCHEMA}|{report.prompt_version}|{report.model}"
    rows = []
    for result in report.results:
        if result.edge_uuid not in allowed:
            continue
        planned = result.planned_window or (None, None)
        expression = result.expression[:EXPRESSION_MAX_CHARS] if result.expression else None
        offset = (json.dumps(result.anchor_offset, sort_keys=True, separators=(",", ":"))
                  if result.anchor_offset else None)
        rows.append({
            "uuid": result.edge_uuid,
            "time_basis": result.basis,
            "time_expression": expression,
            "time_kind": result.kind,
            "time_granularity": result.granularity,
            "time_window_start": _iso(result.window_start),
            "time_window_end": _iso(result.window_end),
            "time_anchor_ref": result.anchor_ref,
            "time_anchor_offset": offset,
            "time_planned_start": _iso(planned[0]),
            "time_planned_end": _iso(planned[1]),
            "time_outcome": result.reason,
            "time_speech_date": _iso(speech_date),
            "time_ambiguity": getattr(result, "ambiguity", None),
            "time_contract": contract,
        })
        world_end = getattr(result, "world_end", None)
        if world_end is not None and getattr(report, "expiry", False):
            # Only P3 rows carry these keys, so a flag-off row is exactly the P2 row.
            rows[-1]["time_world_end"] = world_end.isoformat()
            rows[-1]["time_world_end_ms"] = _epoch_ms(world_end)
    return rows


def _epoch_ms(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(aware.timestamp() * 1000)


def _namespace_of(group_id: str) -> str:
    return group_id or DEFAULT_NAMESPACE


async def persist_anchored_time(
    driver: Any,
    report: Any,
    *,
    episode_uuid: str | None,
    group_id: str,
    edge_uuids: Iterable[str],
    speech_date: date | None,
) -> None:
    """Write the contract for this call's new edges. Sets ``report.persist``; never raises
    except on cancellation."""
    if not episode_uuid:
        report.persist = "no_episode"
        return
    rows = contract_rows(report, edge_uuids, speech_date)
    if not rows:
        report.persist = "no_rows"
        return
    params = {
        "rows": rows,
        "group_id": group_id,
        "episode_uuid": episode_uuid,
        "tenant_namespaces": namespace_spellings(_namespace_of(group_id)),
    }
    expiry = any("time_world_end_ms" in row for row in rows)
    statement = EXPIRY_PERSIST_CYPHER if expiry else PERSIST_CYPHER
    try:
        result = await asyncio.wait_for(
            driver.execute_query(statement, params=params, routing_="w"),
            timeout=PERSIST_TIMEOUT_S,
        )
        records = getattr(result, "records", None) or []
        report.persisted = int(records[0]["written"]) if records else 0
        if expiry and records:
            report.unexpired = int(records[0]["unexpired"] or 0)
        report.persist = "ok"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # never fail ingest over telemetry-grade metadata
        report.persist = "error"
        logger.warning(
            "Anchored-time contract not persisted episode=%s rows=%d error=%s",
            episode_uuid, len(rows), type(exc).__name__,
        )
