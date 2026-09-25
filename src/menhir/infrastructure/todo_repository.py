"""TodoRepository — direct Neo4j reads/writes for :Todo nodes.

:Todo nodes bypass the Graphiti enrichment pipeline entirely. They are
created and managed directly via Cypher, never queued for LLM processing.

Graph edges created at write time:
  (:Todo)-[:REFERENCES_FILE]->(:Entity)   — structural file node for code_ref
  (:Todo)-[:CREATED_FROM]->(:Episodic)    — episode that triggered the TODO
  (:Todo)-[:SUPERSEDED_BY]->(:Todo)       — refile lineage, see `supersede_todo`
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from menhir.domain.namespace import (
    DEFAULT_NAMESPACE,
    namespace_to_group_id,
    normalize_namespace,
    stamped_namespace,
)
from menhir.infrastructure.paths import default_workspace_marker
from menhir.domain.todo_location import (
    DEFAULT_TODO_NAMESPACE,
    TODO_LINK_RELATIONS,
    code_ref_file_predicate,
    parse_code_ref,
)

from menhir.infrastructure.todo_repository_constants import (
    _ALL_TODO_INBOUND_EDGES,
    _STOPWORDS,
    _TODO_LIFECYCLE_RELATIONS,
    _TODO_LINK_RELATIONS,
    _TODO_SUPERSESSION_EDGE,
    _VALID_PRIORITIES,
    _VALID_STATUSES,
    _query_words,
    TODO_AGE_DAYS_CYPHER,
    TODO_STALE_AFTER_DAYS,
)
from menhir.infrastructure.todo_repository_links import TodoLinksMixin
from menhir.infrastructure.todo_repository_locations import TodoLocationsMixin
from menhir.infrastructure.todo_repository_maintenance import TodoMaintenanceMixin
from menhir.infrastructure.todo_repository_search import TodoSearchMixin
from menhir.infrastructure.todo_repository_supersession import TodoSupersessionMixin

#: Shared silo. Every :Todo carries a non-null namespace -- "unscoped" is not
#: representable. Reads that request a silo also see this bucket, so todos
#: written before namespacing (backfilled to 'default') stay visible.
#: Imported directly from domain.namespace (CF-76): it IS the canonical
#: constant, not a local rebind that could silently diverge from it.
#: Defined in the domain module so structural consumers can apply the same rule
#: without importing this repository.


class TodoRepository(
    TodoLocationsMixin,
    TodoLinksMixin,
    TodoSupersessionMixin,
    TodoSearchMixin,
    TodoMaintenanceMixin,
):
    """Direct Neo4j CRUD for :Todo nodes.

    Facade over the ``todo_repository_*`` sibling modules (file-size refactor):
    the location, link, supersession, search, and maintenance methods live on
    the mixin bases above. Every moved symbol is re-exported from this module,
    so existing import sites keep working unchanged.
    """

    neo4j: Any  # Neo4jRepository

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j
        self._known_projects_cache: frozenset[str] | None = None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_todo(
        self,
        *,
        content: str,
        code_ref: str | None = None,
        priority: str = "normal",
        source: str = "claude-code",
        episode_uuid: str | None = None,
        structure_project: str | None = None,
        due_date: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Create an open :Todo node and wire graph edges.

        Edges created (best-effort, silent on miss):
          - REFERENCES_FILE → structural :Entity matching code_ref file path
          - CREATED_FROM    → :Episodic matching episode_uuid
        """
        safe_priority = priority if priority in _VALID_PRIORITIES else "normal"
        safe_namespace = normalize_namespace(namespace)
        todo_uuid = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()

        self.neo4j.execute(
            """
            // CF-158 criterion 1. `:Todo.uuid` carries no uniqueness constraint either (verified
            // against the disposable instance: 13 constraints, none on this label), so this is the
            // same hazard as the :Entity and :Episodic writes. It also matters to the reminder
            // statement below: that one MERGEs its HAS_REMINDER edge, and an edge MERGE cannot
            // stay single if the :Todo it hangs off duplicates first.
            MERGE (n:Todo {uuid: $uuid})
            ON CREATE SET
                n.content    = $content,
                n.code_ref   = $code_ref,
                n.priority   = $priority,
                n.status     = 'open',
                n.source     = $source,
                n.created_at = $now,
                n.closed_at  = null,
                n.due_date   = $due_date,
                n.namespace  = $namespace
            """,
            {
                "uuid": todo_uuid,
                "content": content,
                "code_ref": code_ref,
                "priority": safe_priority,
                "source": source,
                "now": now,
                "due_date": due_date,
                "namespace": safe_namespace,
            },
        )

        # --- HAS_REMINDER edge → TEMPORAL :Entity (if due_date provided) ---
        reminder_uuid: str | None = None
        if due_date:
            reminder_uuid = str(uuid4())
            reminder_name = (content[:60])
            self.neo4j.execute(
                """
                MATCH (t:Todo {uuid: $todo_uuid})
                // CF-158 criterion 1: see `temporal_repository.create_temporal_memory`. This
                // reminder is the same TEMPORAL :Entity shape and inherits the same hazard -- a
                // re-executed CREATE would leave two nodes under one uuid, on a property that
                // cannot carry a uniqueness constraint. The edge MERGEs for the same reason:
                // re-execution must not leave a second HAS_REMINDER between the same two nodes.
                MERGE (r:Entity {uuid: $r_uuid})
                ON CREATE SET
                    r.name          = $r_name,
                    r.summary       = '',
                    r.content       = $content,
                    r.group_id      = $r_group_id,
                    r.type          = 'TEMPORAL',
                    r.target_date   = $due_date,
                    r.status        = 'open',
                    r.source        = $source,
                    r.scope         = 'PERSISTENT',
                    r.namespace     = $r_namespace,
                    r.created_at    = $now,
                    r.last_accessed = $now,
                    r.freshness     = 'ACTIVE',
                    r.edge_count    = 0,
                    r.sharpness     = 1.0
                MERGE (t)-[:HAS_REMINDER]->(r)
                """,
                {
                    "todo_uuid": todo_uuid,
                    "r_uuid": reminder_uuid,
                    "r_name": reminder_name,
                    "content": content,
                    "due_date": due_date,
                    "source": source,
                    "now": now,
                    "r_group_id": namespace_to_group_id(safe_namespace),
                    "r_namespace": stamped_namespace(safe_namespace),
                },
            )

        # --- REFERENCES_FILE edge ---
        linked_file_path: str | None = None
        if code_ref:
            file_path = code_ref.split(":")[0] if ":" in code_ref else code_ref
            rows = self.neo4j.execute(
                f"""
                MATCH (todo:Todo {{uuid: $uuid}})
                OPTIONAL MATCH (f:Entity)
                WHERE f.structure_role IN ['file', 'entrypoint', 'config', 'test']
                  AND {code_ref_file_predicate('f', '$file_path')}
                  AND ($structure_project IS NULL OR f.structure_project = $structure_project)
                WITH todo, f WHERE f IS NOT NULL
                CREATE (todo)-[:REFERENCES_FILE]->(f)
                RETURN f.structure_path AS linked_path
                LIMIT 1
                """,
                {"uuid": todo_uuid, "file_path": file_path, "structure_project": structure_project},
            )
            if rows:
                linked_file_path = rows[0].get("linked_path")

        # --- CREATED_FROM edge ---
        if episode_uuid:
            self.neo4j.execute(
                """
                MATCH (todo:Todo {uuid: $todo_uuid})
                OPTIONAL MATCH (ep:Episodic {uuid: $episode_uuid})
                WITH todo, ep WHERE ep IS NOT NULL
                CREATE (todo)-[:CREATED_FROM]->(ep)
                """,
                {"todo_uuid": todo_uuid, "episode_uuid": episode_uuid},
            )

        # --- HAS_LOCATION nodes (normalized from the author's code_ref) ---
        locations = self._write_locations(todo_uuid, code_ref, structure_project)

        return {
            "uuid": todo_uuid,
            "content": content,
            "code_ref": code_ref,
            "priority": safe_priority,
            "status": "open",
            "source": source,
            "created_at": now,
            "closed_at": None,
            "due_date": due_date,
            "namespace": safe_namespace,
            "reminder_uuid": reminder_uuid,
            "episode_uuid": episode_uuid,
            "structure_project": structure_project,
            "linked_file_path": linked_file_path,
            "locations": locations,
        }

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_todos(
        self,
        *,
        status: str = "open",
        limit: int = 50,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """List todos filtered by status, sorted by priority then created_at.

        Each row includes an ``age_days`` field, a ``stale`` flag (True if the todo has
        been open for more than 30 days), and ``supersedes_count``.

        ``supersedes_count`` is the discovery half of supersession. `get_todo` renders
        the full lineage, but nothing reaches `get_todo` unless something first says
        there is a lineage to look up -- and this listing (plus the session-start hook
        and the bootstrap recall, which both call it) is where an agent actually meets a
        todo. A todo that is a refile carries prior context under a uuid the agent would
        otherwise never ask for. A count rather than the uuids: listings are token-
        sensitive, and the marker only needs to be enough to prompt the drill-down.

        ``namespace`` is opt-in: omitting it lists every silo (the historical
        behavior). Supplying one narrows to that silo plus ``DEFAULT_NAMESPACE``,
        so the shared bucket stays visible rather than a pinned client seeing
        nothing.
        """
        safe_status = status if status in _VALID_STATUSES else "open"
        safe_limit = max(1, min(limit, 200))
        namespaces = (
            [normalize_namespace(namespace), DEFAULT_NAMESPACE] if namespace else None
        )
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Todo)
            WHERE n.status = $status
              AND ($namespaces IS NULL OR n.namespace IN $namespaces)
            WITH n, {TODO_AGE_DAYS_CYPHER} AS age_days
            OPTIONAL MATCH (pred:Todo)-[:{_TODO_SUPERSESSION_EDGE}]->(n)
            WITH n, age_days, collect(pred.namespace) AS pred_namespaces
            RETURN
                pred_namespaces AS pred_namespaces,
                n.uuid       AS uuid,
                n.content    AS content,
                n.code_ref   AS code_ref,
                n.priority   AS priority,
                n.status     AS status,
                n.source     AS source,
                n.created_at AS created_at,
                n.closed_at  AS closed_at,
                n.due_date   AS due_date,
                n.namespace  AS namespace,
                age_days     AS age_days,
                CASE WHEN age_days > $stale_after THEN true ELSE false END AS stale
            ORDER BY
                CASE n.priority
                    WHEN 'high'   THEN 0
                    WHEN 'normal' THEN 1
                    ELSE               2
                END,
                n.created_at ASC
            LIMIT $limit
            """,
            {
                "status": safe_status,
                "limit": safe_limit,
                "namespaces": namespaces,
                "stale_after": TODO_STALE_AFTER_DAYS,
            },
        )
        # Collapse the predecessor namespaces into a count the renderers can show.
        # Counted in Python, and scoped, for the reason `todo_supersession` gives: the
        # todo visibility rule is "own silo OR default bucket", which is not the rule
        # `tenant_scope_cypher` implements, and hand-writing a fourth tenancy predicate
        # in this file is what CF-127's ratchet exists to stop.
        out = []
        for row in rows:
            record = dict(row)
            pred_namespaces = record.pop("pred_namespaces", None) or []
            record["supersedes_count"] = sum(
                1
                for ns in pred_namespaces
                if namespaces is None or (ns or DEFAULT_TODO_NAMESPACE) in namespaces
            )
            out.append(record)
        return out

    def get_todo(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        """Fetch one todo by uuid with its full, untruncated content and edges.

        ``list_todos`` truncates content to a snippet, so long multi-part todos
        are unreadable through it. This is the read that returns the whole
        record, plus the graph context written at create time: the linked file
        (REFERENCES_FILE), the originating episode (CREATED_FROM), and the
        and the entities that reference it.

        Returns None when no :Todo has that uuid.

        This is a direct uuid lookup, so ``namespace`` is enforced only when
        supplied -- passing one refuses a todo outside that silo and the shared
        ``DEFAULT_NAMESPACE`` bucket. The namespace is always reported.
        """
        namespaces = (
            [normalize_namespace(namespace), DEFAULT_NAMESPACE] if namespace else None
        )
        rows = self.neo4j.execute(
            """
            MATCH (n:Todo {uuid: $uuid})
            WHERE $namespaces IS NULL OR n.namespace IN $namespaces
            OPTIONAL MATCH (n)-[:REFERENCES_FILE]->(f:Entity)
            OPTIONAL MATCH (n)-[:CREATED_FROM]->(ep:Episodic)
            WITH n, f, ep,
                 """ + TODO_AGE_DAYS_CYPHER + """ AS age_days
            OPTIONAL MATCH (n)-[:HAS_LOCATION]->(loc:TodoLocation)
            WITH n, f, ep, age_days, loc ORDER BY loc.ordinal ASC
            WITH n, f, ep, age_days,
                 collect(loc {.project, .path, .kind, .line_start, .line_end,
                              .symbol, .ordinal, .resolution_status,
                              .unresolved_reason}) AS locations
            RETURN
                n.uuid       AS uuid,
                n.content    AS content,
                n.code_ref   AS code_ref,
                n.priority   AS priority,
                n.status     AS status,
                n.source     AS source,
                n.created_at AS created_at,
                n.closed_at  AS closed_at,
                n.due_date   AS due_date,
                n.namespace  AS namespace,
                age_days     AS age_days,
                CASE WHEN age_days > $stale_after THEN true ELSE false END AS stale,
                f.structure_path    AS linked_file_path,
                f.structure_project AS linked_file_project,
                ep.uuid             AS episode_uuid,
                locations           AS locations
            LIMIT 1
            """,
            {
                "uuid": uuid,
                "namespaces": namespaces,
                "stale_after": TODO_STALE_AFTER_DAYS,
            },
        )
        if not rows:
            return None

        # Inbound links are fetched separately rather than folded into the query
        # above: that chain already collects locations and concerns, and a third
        # collect over an independent relationship multiplies the intermediate
        # rows before aggregation.
        todo = dict(rows[0])
        todo["inbound_links"] = self.todo_inbound_links(uuid)
        # Fetched separately for the same reason inbound links are: a third and fourth
        # collect over independent relationships would multiply intermediate rows.
        # Same silo filter the todo itself was matched under: a lineage uuid is still
        # an identifier from another namespace, and the shared default bucket is
        # readable from every silo.
        todo["supersession"] = self.todo_supersession(uuid, namespaces)
        return todo

    def close_stale_todos(
        self, *, older_than_days: int = 60, dry_run: bool = True, namespace: str | None = None
    ) -> dict[str, Any]:
        """Close todos older than N days, optionally restricted to one namespace.

        Returns a dict with ``closed_count`` and ``preview`` (uuids that would
        be or were closed).  With ``dry_run=True`` (default), no todos are
        actually closed.

        Scoping deliberately differs from this file's READ idiom. ``list_todos`` and
        ``get_todo`` use the requested-plus-default rule
        (``[normalize_namespace(namespace), DEFAULT_NAMESPACE]``), which is a convenience: seeing
        the shared bucket alongside your own costs nothing. This is a BULK MUTATION, so it
        matches the requested namespace EXACTLY. A client scoped to one silo -- or pinned to
        it server-side -- must not close todos in the shared default bucket as a side effect
        of tidying its own.

        With no namespace supplied the behaviour is unchanged and unscoped, per the opt-in
        isolation contract in ``domain/namespace.py``.
        """
        now = datetime.now(timezone.utc).isoformat()
        safe_days = max(1, min(older_than_days, 365))

        scoped = bool((namespace or "").strip())
        ns_filter = "AND n.namespace = $namespace" if scoped else ""
        params: dict[str, Any] = {"days": safe_days}
        if scoped:
            params["namespace"] = normalize_namespace(namespace)

        # Find stale todos
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Todo {{status: 'open'}})
            WHERE {TODO_AGE_DAYS_CYPHER} >= $days
              {ns_filter}
            RETURN n.uuid AS uuid, n.content AS content, n.created_at AS created_at
            ORDER BY n.created_at ASC
            """,
            params,
        )

        stale_uuids = [str(r.get("uuid")) for r in rows]

        if dry_run or not stale_uuids:
            return {
                "closed_count": 0,
                "preview_count": len(stale_uuids),
                "preview": stale_uuids,
                "older_than_days": safe_days,
                "dry_run": dry_run,
            }

        # Actually close them
        closed = self.neo4j.execute(
            """
            MATCH (n:Todo {status: 'open'})
            WHERE n.uuid IN $uuids
            SET n.status = 'closed', n.closed_at = $now
            WITH n
            OPTIONAL MATCH (n)-[:HAS_REMINDER]->(r:Entity {type: 'TEMPORAL', status: 'open'})
            SET r.status = 'completed', r.last_accessed = $now
            RETURN count(n) AS closed
            """,
            {"uuids": stale_uuids, "now": now},
        )
        closed_count = int(closed[0].get("closed", 0)) if closed else 0

        return {
            "closed_count": closed_count,
            "preview_count": len(stale_uuids),
            "preview": stale_uuids,
            "older_than_days": safe_days,
            "dry_run": False,
        }
