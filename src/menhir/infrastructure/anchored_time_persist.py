"""Persist the anchored-time contract on the RELATES_TO edges it was computed for (P2).

Graphiti's edge resolution clears hook-time edge attributes, so the contract is written by a
Menhir-owned step after ``add_episode`` returns. Every write is bound to the call that produced it:

- the report must have been created during this call (a reused receipt can carry an older one);
- an edge uuid must appear in this call's returned edges (a duplicate resolves to an existing
  edge with another uuid, so the extracted uuid is absent and nothing is written);
- the edge must list this call's episode and carry no contract yet (write-once; a replay is a
  no-op), and belong to this call's tenant.

Failure never fails ingest: missing properties mean exactly the pre-P2 behavior.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import date
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
    "time_contract",
)

PERSIST_CYPHER = (
    "UNWIND $rows AS row "
    "MATCH ()-[r:RELATES_TO {uuid: row.uuid}]->() "
    "WHERE " + tenant_scope_cypher("r") + " "
    "AND r.group_id = $group_id "
    "AND r.time_contract IS NULL "
    "AND $episode_uuid IN coalesce(r.episodes, []) "
    "SET " + ", ".join(f"r.{name} = row.{name}" for name in CONTRACT_PROPERTIES) + " "
    "RETURN count(r) AS written"
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
            "time_contract": contract,
        })
    return rows


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
    try:
        result = await asyncio.wait_for(
            driver.execute_query(PERSIST_CYPHER, params=params, routing_="w"),
            timeout=PERSIST_TIMEOUT_S,
        )
        records = getattr(result, "records", None) or []
        report.persisted = int(records[0]["written"]) if records else 0
        report.persist = "ok"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # never fail ingest over telemetry-grade metadata
        report.persist = "error"
        logger.warning(
            "Anchored-time contract not persisted episode=%s rows=%d error=%s",
            episode_uuid, len(rows), type(exc).__name__,
        )
