"""Hook Center tool/file event capture -> deterministic dirty/stale marking (Hook Center v0).

A hook observes a meaningful file/tool event (an edit/write/delete/rename) and POSTs a NORMALIZED event
here. This repository marks the AFFECTED structure-file node dirty and lets recall/diagnostics detect
memories anchored to a file that changed AFTER they were anchored. It reuses menhir's EXISTING
structural code graph (`:Entity {structure_role:'file', structure_path, structure_project}`) and the
`ANCHORED_TO {created_at}` edge — no new node architecture, no schema change (dirty markers are just
properties set on existing file nodes; we MATCH, never MERGE a new label).

Invariants (Hook Center):
- Observes file/tool EVENTS; never raw transcripts.
- Stores NO file content — only optional client-supplied hashes/mtime (provenance, not content).
- Idempotent + fail-safe: an event for a file not (yet) in the structure graph is ACCEPTED but marks
  nothing (documented v0 limit); it never errors.
- Deterministic: no LLM, no structure rebuild here. Dirty marking is a property write; a later scan /
  recall consumes it.

The "stale" signal (the whole point): a memory `(sem)-[a:ANCHORED_TO]->(f:file)` is STALE when
`f.structure_dirty` and `f.dirty_at > a.created_at` — the file changed after the memory was anchored.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params

logger = logging.getLogger(__name__)


def _parse_iso_utc(text: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string, returning None on failure."""
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return None
        return dt
    except (ValueError, TypeError):
        return None

#: file operations a v0 file_changed event may carry. rename also touches `old_path`.
FILE_OPERATIONS = ("write", "edit", "delete", "rename", "create")


def affected_paths(path: str | None, old_path: str | None, operation: str | None) -> list[str]:
    """The structure paths an event touches: always `path`; also `old_path` on a rename/move (both the
    source and destination file anchors are affected). Deterministic, pure — deduped, order-preserving,
    empties dropped."""
    out: list[str] = []
    for p in (path, old_path if (operation or "").lower() == "rename" else None):
        if p and p.strip() and p not in out:
            out.append(p.strip())
    return out


class ToolEventRepository:
    """Mark structure-file nodes dirty from normalized tool/file events, and detect stale anchors.

    Mirrors `TurnEvidenceRepository`'s shape: a narrow, deterministic repo over the shared neo4j
    handle. No content, no LLM, no schema change."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    # ---- write (dirty marking) ---------------------------------------------------------------

    def _match_and_mark_dirty(
        self,
        paths: list[str],
        project: str | None,
        op: str,
        after_hash: str | None,
        mtime: str | None,
        source_client: str | None,
    ) -> int:
        rows = self._neo4j.execute(
            """
            UNWIND $paths AS p
            MATCH (f:Entity {structure_role: 'file', structure_path: p})
            WHERE $project IS NULL OR f.structure_project = $project
            SET f.structure_dirty = true,
                f.dirty_at = datetime(),
                f.last_event_op = $op,
                f.last_event_after_hash = $after_hash,
                f.last_event_mtime = $mtime,
                f.last_event_source_client = $source_client
            RETURN count(f) AS matched
            """,
            params={
                "paths": paths,
                "project": project,
                "op": op,
                "after_hash": after_hash,
                "mtime": mtime,
                "source_client": source_client,
            },
        )
        return int(rows[0]["matched"]) if rows else 0

    def record_file_event(
        self,
        *,
        path: str,
        operation: str = "edit",
        old_path: str | None = None,
        project: str | None = None,
        after_hash: str | None = None,
        mtime: str | None = None,
        source_client: str | None = None,
    ) -> dict[str, Any]:
        """Mark the affected file `:Entity` node(s) dirty. Returns
        `{accepted, matched, marked_dirty, paths}`. `matched` = how many existing file nodes were
        found; a file not in the structure graph yet -> accepted, matched 0 (no error). NO content is
        stored — only the optional `after_hash`/`mtime` provenance. When `project` is given the match
        is scoped to that structure_project; else it matches the relative `path` across projects
        (conservative: over-marks rather than misses). If a project-scoped match finds nothing, a
        path-only fallback is tried automatically so a mismatched structure_project is not a silent
        miss."""
        text = (path or "").strip()
        if not text:
            raise ValueError("record_file_event requires a non-empty path")
        op = (operation or "edit").strip().lower()
        if op not in FILE_OPERATIONS:
            raise ValueError(f"operation must be one of {FILE_OPERATIONS}, got {operation!r}")
        paths = affected_paths(text, old_path, op)
        matched = self._match_and_mark_dirty(paths, project, op, after_hash, mtime, source_client)
        fallback_used = False
        if matched == 0 and project is not None:
            matched = self._match_and_mark_dirty(paths, None, op, after_hash, mtime, source_client)
            fallback_used = True
        return {
            "accepted": True,
            "matched": matched,
            "marked_dirty": matched > 0,
            "paths": paths,
            "project_fallback_used": fallback_used,
        }

    def clear_file_dirty(self, *, path: str, old_path: str | None = None,
                         operation: str = "edit", project: str | None = None) -> int:
        """Remove the dirty markers from the affected file node(s); returns the count cleared. Used
        after a structure refresh reconciles the change, and by throwaway-eval teardown/idempotence."""
        paths = affected_paths((path or "").strip(), old_path, (operation or "").lower())
        if not paths:
            return 0
        rows = self._neo4j.execute(
            """
            UNWIND $paths AS p
            MATCH (f:Entity {structure_role: 'file', structure_path: p})
            WHERE ($project IS NULL OR f.structure_project = $project) AND f.structure_dirty = true
            REMOVE f.structure_dirty, f.dirty_at, f.last_event_op,
                   f.last_event_after_hash, f.last_event_mtime, f.last_event_source_client
            RETURN count(f) AS cleared
            """,
            params={"paths": paths, "project": project},
        )
        return int(rows[0]["cleared"]) if rows else 0

    # ---- read (diagnostics / stale detection) ------------------------------------------------

    def list_dirty_files(self, *, project: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Files currently marked dirty by an event — minimal recall/diagnostic visibility."""
        rows = self._neo4j.execute(
            """
            MATCH (f:Entity {structure_role: 'file'})
            WHERE f.structure_dirty = true AND ($project IS NULL OR f.structure_project = $project)
            RETURN f.structure_project AS project, f.structure_path AS path,
                   toString(f.dirty_at) AS dirty_at, f.last_event_op AS operation,
                   f.last_event_source_client AS source_client
            ORDER BY f.dirty_at DESC
            LIMIT $limit
            """,
            params={"project": project, "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    def stale_anchored_memories(
        self, *, project: str | None = None, limit: int = 200, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Memories anchored to a file that changed AFTER they were anchored — the stale-reference
        signal. `(sem)-[a:ANCHORED_TO]->(f:file)` where `f.structure_dirty` and
        `f.dirty_at > a.created_at`. Read-only detection; recall can down-rank / label these.

        CF-106: this returns `sem.name` — a memory's own name — so an unscoped read hands one
        tenant's memory names to another. Two readonly HTTP routes return it directly and recall
        uses it to label results.

        THE PREDICATE GOES ON `sem`, NOT ON `f`, and that is the whole subtlety of this join. `f`
        is a STRUCTURE entity: single shared silo, keyed on `(structure_project, structure_path)`,
        written with `group_id = ''` and no `namespace` at all (owner ruling 2026-08-21, encoded in
        `tenant_scope_cypher`, which refuses to emit a predicate for that scheme). Scoping the file
        side would match nothing and look like a working filter. Only the memory side carries a
        tenant.

        `namespace` is opt-in (``None`` reads every silo, matching `list_in_window` and
        `list_todos`), and the predicate comes from `tenant_scope_cypher` rather than being written
        by hand. **That is not a style preference.** The hand-written version tried first here
        coalesced the namespace property against the literal default, which matches a node whose
        property is ABSENT but not one stored as the empty string -- and those two are the same
        legacy silo by owner ruling, so such memories would have been invisible to the tenant that
        owns them. That is the identical blind spot CF-127 measured at 33,442 rows. The builder
        expands both spellings and coalesces through ``group_id``; CF-127's ratchet caught the
        hand-written version on its first suite run, which is what that ratchet is for.

        (The bad predicate is described rather than quoted on purpose: the ratchet scans source
        text and cannot tell a docstring from a query, so quoting it re-trips the guard.)
        """
        rows = self._neo4j.execute(
            f"""
            MATCH (sem:Entity)-[a:ANCHORED_TO]->(f:Entity {{structure_role: 'file'}})
            WHERE f.structure_dirty = true
                  AND a.created_at IS NOT NULL AND f.dirty_at > a.created_at
                  AND ($project IS NULL OR f.structure_project = $project)
                  AND {tenant_scope_cypher("sem")}
            RETURN sem.uuid AS memory_uuid, sem.name AS name,
                   f.structure_project AS project, f.structure_path AS path,
                   toString(f.dirty_at) AS dirty_at, toString(a.created_at) AS anchored_at,
                   f.last_event_op AS operation
            ORDER BY f.dirty_at DESC
            LIMIT $limit
            """,
            params={
                "project": project,
                "limit": int(limit),
                **tenant_scope_params(namespace),
            },
        )
        return [dict(r) for r in rows]

    # ---- stale anchor verification receipts -------------------------------------------------

    ALLOWED_VERIFICATION_OUTCOMES = ("still_valid", "outdated", "needs_review", "superseded")

    @staticmethod
    def _validate_verified_at(value: str | None) -> str | None:
        """Validate ``verified_at`` as strict ISO-8601 UTC.
        Returns the value unchanged if valid, raises ``ValueError`` if
        the format is unparseable. ``None`` is accepted (server-assigned)."""
        if value is None:
            return None
        dt = _parse_iso_utc(value)
        if dt is None:
            raise ValueError(
                f"verified_at must be a valid ISO-8601 UTC timestamp, got {value!r}"
            )
        return value

    def record_stale_anchor_verification(
        self,
        *,
        memory_uuid: str,
        project: str | None = None,
        path: str,
        outcome: str,
        verified_by: str | None = None,
        basis: str | None = None,
        current_file_hash: str | None = None,
        notes: str | None = None,
        verified_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a durable StaleAnchorVerification receipt. Validates outcome
        and verified_at. Returns ``{accepted, verification}``."""
        outcome = (outcome or "").strip().lower()
        if outcome not in self.ALLOWED_VERIFICATION_OUTCOMES:
            raise ValueError(
                f"outcome must be one of {self.ALLOWED_VERIFICATION_OUTCOMES}, got {outcome!r}"
            )
        text = (path or "").strip()
        if not text:
            raise ValueError("path is required for a stale-anchor verification")
        uid = (memory_uuid or "").strip()
        if not uid:
            raise ValueError("memory_uuid is required for a stale-anchor verification")
        verified_at = self._validate_verified_at(verified_at)
        rows = self._neo4j.execute(
            """
            CREATE (v:StaleAnchorVerification {
                uuid: randomUUID(),
                memory_uuid: $memory_uuid,
                project: $project,
                path: $path,
                outcome: $outcome,
                verified_by: $verified_by,
                basis: $basis,
                current_file_hash: $current_file_hash,
                notes: $notes,
                verified_at: $verified_at,
                created_at: datetime()
            })
            RETURN v.uuid AS uuid, v.memory_uuid AS memory_uuid,
                   v.project AS project, v.path AS path,
                   v.outcome AS outcome, v.verified_by AS verified_by,
                   v.basis AS basis, v.verified_at AS verified_at
            """,
            params={
                "memory_uuid": uid,
                "project": project,
                "path": text,
                "outcome": outcome,
                "verified_by": verified_by,
                "basis": basis,
                "current_file_hash": current_file_hash,
                "notes": notes,
                "verified_at": verified_at,
            },
        )
        row = dict(rows[0]) if rows else {}
        return {"accepted": True, "verification": row}

    def list_stale_anchor_verifications(
        self,
        *,
        memory_uuid: str | None = None,
        project: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return verification receipts, optionally filtered."""
        conditions = ["1 = 1"]
        params: dict[str, Any] = {
            "limit": int(limit),
            "verification_label": "StaleAnchorVerification",
            "uuid_key": "uuid",
            "memory_uuid_key": "memory_uuid",
            "project_key": "project",
            "path_key": "path",
            "outcome_key": "outcome",
            "verified_by_key": "verified_by",
            "basis_key": "basis",
            "current_file_hash_key": "current_file_hash",
            "notes_key": "notes",
            "verified_at_key": "verified_at",
            "created_at_key": "created_at",
        }
        if memory_uuid:
            conditions.append("v[$memory_uuid_key] = $memory_uuid")
            params["memory_uuid"] = memory_uuid
        if project:
            conditions.append("v[$project_key] = $project")
            params["project"] = project
        if path:
            conditions.append("v[$path_key] = $path")
            params["path"] = path
        where_clause = " AND ".join(conditions)
        rows = self._neo4j.execute(
            f"""
            MATCH (v:$($verification_label))
            WHERE {where_clause}
            RETURN v[$uuid_key] AS uuid, v[$memory_uuid_key] AS memory_uuid,
                   v[$project_key] AS project, v[$path_key] AS path,
                   v[$outcome_key] AS outcome, v[$verified_by_key] AS verified_by,
                   v[$basis_key] AS basis,
                   v[$current_file_hash_key] AS current_file_hash,
                   v[$notes_key] AS notes, v[$verified_at_key] AS verified_at,
                   toString(v[$created_at_key]) AS created_at
            ORDER BY v[$verified_at_key] DESC
            LIMIT $limit
            """,
            params=params,
        )
        return [dict(r) for r in rows]

    def latest_stale_anchor_verifications(
        self,
        *,
        stale_anchors: list[dict[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Return the latest post-dirty verification per (memory_uuid, path).

        For each stale anchor spec in ``stale_anchors`` (each must contain
        ``memory_uuid``, ``path``, and ``dirty_at``), return the most recent
        ``StaleAnchorVerification`` where ``v.memory_uuid = memory_uuid``
        AND ``v.path = path`` AND ``verified_at >= dirty_at``.

        A verification receipt only applies when it matches both the memory
        AND the file path — a ``still_valid`` receipt for ``src/a.py`` cannot
        reassure about a stale anchor on ``src/b.py``.

        Returns ``{(memory_uuid, path): receipt_dict}``.  Only anchors with a
        matching post-dirty verification are included.
        """
        if not stale_anchors:
            return {}
        memory_uuids = list({sa.get("memory_uuid", "") for sa in stale_anchors if sa.get("memory_uuid")})
        if not memory_uuids:
            return {}
        rows = self._neo4j.execute(
            """
            MATCH (v:$($verification_label))
            WHERE v[$memory_uuid_key] IN $memory_uuids
            RETURN v[$memory_uuid_key] AS memory_uuid, v[$path_key] AS path,
                   v[$outcome_key] AS outcome, v[$verified_by_key] AS verified_by,
                   v[$basis_key] AS basis, v[$verified_at_key] AS verified_at,
                   v[$current_file_hash_key] AS current_file_hash,
                   v[$notes_key] AS notes
            ORDER BY v[$verified_at_key] DESC
            """,
            params={
                "memory_uuids": memory_uuids,
                "verification_label": "StaleAnchorVerification",
                "memory_uuid_key": "memory_uuid",
                "path_key": "path",
                "outcome_key": "outcome",
                "verified_by_key": "verified_by",
                "basis_key": "basis",
                "verified_at_key": "verified_at",
                "current_file_hash_key": "current_file_hash",
                "notes_key": "notes",
            },
        )
        # Build a map from (memory_uuid, path) → latest receipt
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            row = dict(r)
            muid = str(row.get("memory_uuid") or "")
            vpath = str(row.get("path") or "")
            key = (muid, vpath)
            if not muid or not vpath or key in latest:
                continue  # already have the latest (sorted DESC)
            latest[key] = row

        # Match each stale anchor spec against verified receipts
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for sa in stale_anchors:
            muid = str(sa.get("memory_uuid") or "")
            spath = str(sa.get("path") or "")
            dirty_at = str(sa.get("dirty_at") or "")
            if not muid or not spath:
                continue
            key = (muid, spath)
            row = latest.get(key)
            if row is None:
                continue
            verified_at = str(row.get("verified_at") or "")
            # Skip if we cannot compare timestamps conservatively
            if not verified_at or not dirty_at:
                continue
            # Parse both timestamps for safe comparison
            try:
                v_dt = _parse_iso_utc(verified_at)
                d_dt = _parse_iso_utc(dirty_at)
            except Exception:
                continue  # unparseable → skip (conservative)
            if v_dt is None or d_dt is None:
                continue
            if v_dt < d_dt:
                continue  # pre-dirty verification is not reassuring
            result[key] = row
        return result

    def dirty_stats(self, *, project: str | None = None) -> dict[str, Any]:
        """Counts for a diagnostic response: dirty files + stale anchored memories."""
        return {
            "dirty_files": len(self.list_dirty_files(project=project, limit=10000)),
            "stale_anchors": len(self.stale_anchored_memories(project=project, limit=10000)),
        }
