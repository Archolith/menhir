"""Read side of the graph-operations saga journal.

Split out of ``graph_operations.py`` (file-size refactor): single-row and paged state reads,
the exhaustive state cursor, and the committed merge/unmerge lineage extraction consumed by
the ScalarStateView repair passes. ``GraphOperationsJournal`` composes this mixin; every
method runs against the facade's ``self.db_path``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any

from menhir.infrastructure.graph_operations_model import (
    OPERATION_STATES,
    GraphOperationError,
)
from menhir.infrastructure.telemetry import connect_telemetry_db


class _GraphOperationsReadsMixin:
    """Read-only journal queries and committed-pair lineage extraction."""

    # ------------------------------------------------------------------ reads
    def get(self, op_id: str) -> dict[str, Any] | None:
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM graph_operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            return dict(row) if row else None

    def iter_by_state(
        self, state: str, *, batch_size: int = 500
    ) -> Iterator[dict[str, Any]]:
        """Yield EVERY row in ``state``, oldest first, with no horizon that can hide rows.

        ``list_by_state(limit=500)`` cannot be made exhaustive by calling it repeatedly: it always
        returns the same oldest page, so any row that never leaves the state makes the caller loop
        on it forever while newer rows are never seen. That is the deterministic starvation CF-20
        has to remove before recovery can be trusted, and it is why this is a cursor, not a bigger
        limit.

        The keyset is ``(created_at, op_id)``, and the op_id half is load-bearing. ``created_at`` is
        NOT unique -- operations prepared in the same instant share it -- so ordering by it alone is
        not a total order, and a page boundary falling inside a group of ties would silently skip or
        repeat rows. op_id is the PRIMARY KEY, so it breaks every tie.

        Each page is a separate connection and the scan holds no snapshot: rows that leave ``state``
        mid-scan simply stop appearing, and rows added with a later ``created_at`` will be picked up.
        For an observation pass that is correct. A pass that must see a FIXED backlog has to close
        the PREPARE gate first (CF-20c) -- the cursor guarantees progress, not isolation.
        """
        if state not in OPERATION_STATES:
            raise GraphOperationError(f"unknown state {state!r}")
        self._ensure_ready()
        page_size = max(1, int(batch_size))
        cursor_created_at: str | None = None
        cursor_op_id: str | None = None
        while True:
            with connect_telemetry_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if cursor_created_at is None:
                    rows = conn.execute(
                        "SELECT * FROM graph_operations WHERE state = ? "
                        "ORDER BY created_at ASC, op_id ASC LIMIT ?",
                        (state, page_size),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM graph_operations WHERE state = ? "
                        "AND (created_at > ? OR (created_at = ? AND op_id > ?)) "
                        "ORDER BY created_at ASC, op_id ASC LIMIT ?",
                        (state, cursor_created_at, cursor_created_at, cursor_op_id, page_size),
                    ).fetchall()
            if not rows:
                return
            for row in rows:
                yield dict(row)
            cursor_created_at = rows[-1]["created_at"]
            cursor_op_id = rows[-1]["op_id"]
            if len(rows) < page_size:
                return

    def list_by_state(self, state: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if state not in OPERATION_STATES:
            raise GraphOperationError(f"unknown state {state!r}")
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM graph_operations WHERE state = ? ORDER BY created_at ASC LIMIT ?",
                (state, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_by_batch(self, batch_id: str) -> list[dict[str, Any]]:
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM graph_operations WHERE batch_id = ? ORDER BY created_at ASC",
                (batch_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_committed_merges(self, *, page_size: int = 5000) -> list[dict[str, Any]]:
        """Read-only merge-lineage source for the ScalarStateView C.4.4 repair passes: ALL COMMITTED
        ENTITY_MERGE ops as ``[{op_id, absorbed_uuid, survivor_uuid}]``, oldest first (chain order).
        Filters ``state='COMMITTED' AND operation_kind='ENTITY_MERGE'`` IN SQL and PAGES through the
        whole history (`page_size` is a batch size, NOT a total cap), so a later merge or a downstream
        chain crossing an arbitrary cutoff is never permanently hidden, and malformed rows cannot
        consume a limit ahead of valid lineage. A row missing a survivor/absorbed pair is skipped.
        Does not touch the write/fence path."""
        return self._committed_pairs("ENTITY_MERGE", page_size=page_size)

    def list_committed_unmerges(self, *, page_size: int = 5000) -> list[dict[str, Any]]:
        """Read-only lineage source for ALL committed ENTITY_UNMERGE ops as
        ``[{op_id, merge_op_id, absorbed_uuid, survivor_uuid}]`` (the shape
        `repair_incomplete_reconciliations` consumes). `merge_op_id` comes from the row's
        ``reverses_op_id``. Filtered in SQL and PAGED through the whole history; rows missing the pair
        or the reversed op are skipped. Read-only."""
        return self._committed_pairs("ENTITY_UNMERGE", page_size=page_size, with_reverses=True)

    def _committed_pairs(
        self, operation_kind: str, *, page_size: int, with_reverses: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_ready()
        out: list[dict[str, Any]] = []
        offset = 0
        batch = max(1, int(page_size))
        # Page to EXHAUSTION — there is no total cutoff. A ceiling here would permanently hide the
        # rows beyond it (every call restarts at offset 0), so a beyond-ceiling receiptless op would be
        # invisible forever and a beyond-ceiling orphan would be marked unresolved on every run.
        while True:
            with connect_telemetry_db(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT op_id, request_json, reverses_op_id FROM graph_operations "
                    "WHERE state = 'COMMITTED' AND operation_kind = ? "
                    "ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?",
                    (operation_kind, batch, offset),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                try:
                    request = json.loads(row["request_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue                    # malformed row skipped; it did NOT consume valid budget
                absorbed = request.get("absorbed_uuid")
                survivor = request.get("survivor_uuid")
                if not (absorbed and survivor):
                    continue
                item = {"op_id": str(row["op_id"]),
                        "absorbed_uuid": str(absorbed), "survivor_uuid": str(survivor)}
                if with_reverses:
                    merge_op_id = row["reverses_op_id"] or request.get("merge_op_id")
                    if not merge_op_id:
                        continue      # an unmerge with no forward-merge lineage cannot be reconciled
                    item["merge_op_id"] = str(merge_op_id)
                out.append(item)
            if len(rows) < batch:
                break                           # last page
            offset += batch
        return out
