"""TodoRepository — direct Neo4j reads/writes for :Todo nodes.

:Todo nodes bypass the Graphiti enrichment pipeline entirely. They are
created and managed directly via Cypher, never queued for LLM processing.

Graph edges created at write time:
  (:Todo)-[:REFERENCES_FILE]->(:Entity)   — structural file node for code_ref
  (:Todo)-[:CREATED_FROM]->(:Episodic)    — episode that triggered the TODO
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from menhir.domain.todo_location import (
    DEFAULT_TODO_NAMESPACE,
    code_ref_file_predicate,
    parse_code_ref,
)

_VALID_PRIORITIES = frozenset({"low", "normal", "high"})
_VALID_STATUSES = frozenset({"open", "closed"})

#: Shared silo. Every :Todo carries a non-null namespace -- "unscoped" is not
#: representable. Reads that request a silo also see this bucket, so todos
#: written before namespacing (backfilled to 'default') stay visible.
#: Defined in the domain module so structural consumers can apply the same rule
#: without importing this repository.
DEFAULT_NAMESPACE = DEFAULT_TODO_NAMESPACE


def _safe_namespace(namespace: str | None) -> str:
    """Resolve a namespace argument to a non-null value."""
    return (namespace or "").strip() or DEFAULT_NAMESPACE


#: Inbound semantic relations a memory may declare against a todo, mapped to their
#: distinct edge types. Slice 1 ships reference relations only -- RESOLVES_TODO and
#: REOPENS_TODO belong with the lifecycle transaction in slice 2, because creating
#: them without one would imply a status change the edge alone must never make.
#:
#: Cypher cannot parameterize a relationship type, so this whitelist is also the
#: injection guard: a relation outside it never reaches a query string.
_TODO_LINK_RELATIONS: dict[str, str] = {
    "mentions": "MENTIONS_TODO",
    "addresses": "ADDRESSES_TODO",
}

#: Lifecycle relations. Deliberately absent from _TODO_LINK_RELATIONS so they can
#: never be created by a bare link call -- they exist only as the evidence half of
#: resolve_todo / reopen_todo, which create the edge and move status together.
#: They are still readable, so a todo can show why it closed or reopened.
_TODO_LIFECYCLE_RELATIONS: dict[str, str] = {
    "resolves": "RESOLVES_TODO",
    "reopens": "REOPENS_TODO",
}

#: Every inbound relation type, for reads.
_ALL_TODO_INBOUND_EDGES: list[str] = [
    *_TODO_LINK_RELATIONS.values(),
    *_TODO_LIFECYCLE_RELATIONS.values(),
]


# Words to skip when extracting keywords for entity matching
_STOPWORDS = frozenset({
    "about", "above", "after", "again", "against", "before", "being",
    "between", "could", "doing", "during", "having", "needs", "other",
    "their", "there", "these", "those", "under", "until", "using",
    "where", "which", "while", "would", "should", "shall", "might",
})


def _query_words(text: str) -> list[str]:
    """Extract meaningful words (>= 5 chars, not stopwords) for keyword search."""
    return [
        w for w in re.findall(r"\b[a-zA-Z]{5,}\b", text.lower())
        if w not in _STOPWORDS
    ]


class TodoRepository:
    """Direct Neo4j CRUD for :Todo nodes."""

    neo4j: Any  # Neo4jRepository

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j
        self._known_projects_cache: frozenset[str] | None = None

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------

    def _known_projects(self) -> frozenset[str]:
        """Project names the structure graph knows, for bare `<project>/<path>` refs.

        Cached per repository instance: the set changes only when a project is
        scanned, and normalization must not pay a graph round trip per segment.
        """
        if self._known_projects_cache is None:
            rows = self.neo4j.execute(
                """
                MATCH (e:Entity)
                WHERE e.structure_project IS NOT NULL
                RETURN DISTINCT e.structure_project AS p
                """,
                {},
            )
            self._known_projects_cache = frozenset(
                str(r["p"]) for r in rows if r.get("p")
            )
        return self._known_projects_cache

    def _write_locations(
        self,
        todo_uuid: str,
        code_ref: str | None,
        structure_project: str | None,
    ) -> list[dict[str, Any]]:
        """Normalize ``code_ref`` into owned :TodoLocation nodes.

        :TodoLocation carries its own label and never :Entity or :Episodic --
        the same containment the :TurnEvidence node uses -- so a location can
        never surface in semantic recall. It holds no namespace: visibility is
        inherited through the owning :Todo, so there is no copy to drift.
        """
        if not code_ref:
            return []

        locations = parse_code_ref(
            code_ref,
            structure_project=structure_project,
            known_projects=self._known_projects(),
        )
        if not locations:
            return []

        rows = [loc.as_properties() for loc in locations]
        self.neo4j.execute(
            """
            MATCH (t:Todo {uuid: $uuid})
            UNWIND $rows AS row
            CREATE (t)-[:HAS_LOCATION]->(l:TodoLocation)
            SET l += row
            """,
            {"uuid": todo_uuid, "rows": rows},
        )
        self._link_location_files(todo_uuid)
        return rows

    def _link_location_files(self, todo_uuid: str) -> int:
        """Resolve each location to a structural file entity, if one exists.

        The edge hangs off the :TodoLocation rather than the :Todo, so the trail
        reads raw declaration -> normalized location -> resolved entity. With a
        multi-location todo, a todo-level edge could not say which declaration
        produced which file.

        Matching mirrors blast radius: exact path, plus a basename-only fallback
        for a bare filename, which is underspecified but still a real
        declaration. Unresolvable locations simply get no edge -- the location
        itself is retained either way.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $uuid}})-[:HAS_LOCATION]->(l:TodoLocation)
            WHERE l.resolution_status = 'resolved' AND l.path IS NOT NULL
            MATCH (f:Entity)
            WHERE f.structure_role IN ['file', 'entrypoint', 'config', 'test']
              AND {code_ref_file_predicate('f', 'l.path')}
              AND (l.project IS NULL OR f.structure_project = l.project)
            MERGE (l)-[:REFERENCES_FILE]->(f)
            RETURN count(*) AS linked
            """,
            {"uuid": todo_uuid},
        )
        return int(rows[0].get("linked", 0)) if rows else 0

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
        safe_namespace = _safe_namespace(namespace)
        todo_uuid = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()

        self.neo4j.execute(
            """
            CREATE (n:Todo {
                uuid:       $uuid,
                content:    $content,
                code_ref:   $code_ref,
                priority:   $priority,
                status:     'open',
                source:     $source,
                created_at: $now,
                closed_at:  null,
                due_date:   $due_date,
                namespace:  $namespace
            })
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
                CREATE (r:Entity {
                    uuid:          $r_uuid,
                    name:          $r_name,
                    summary:       '',
                    content:       $content,
                    group_id:      '',
                    type:          'TEMPORAL',
                    target_date:   $due_date,
                    status:        'open',
                    source:        $source,
                    scope:         'PERSISTENT',
                    created_at:    $now,
                    last_accessed: $now,
                    freshness:     'ACTIVE',
                    edge_count:    0,
                    sharpness:     1.0
                })
                CREATE (t)-[:HAS_REMINDER]->(r)
                """,
                {
                    "todo_uuid": todo_uuid,
                    "r_uuid": reminder_uuid,
                    "r_name": reminder_name,
                    "content": content,
                    "due_date": due_date,
                    "source": source,
                    "now": now,
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
    # Inbound semantic links (Phase B slice 1)
    # ------------------------------------------------------------------

    def link_memory_to_todo(
        self, memory_uuid: str, todo_uuid: str, relation: str
    ) -> dict[str, Any]:
        """Point a durable semantic entity at a todo.

        Direction is always inward: a memory references the todo. The todo stays
        an operational object -- knowledge lives in memories and semantic
        objects, never accumulating inside the todo itself.

        Eligibility is deliberately narrow. Only PERSISTENT, non-structural
        entities may link. A file does not "address" a todo, and admitting
        structural nodes would recreate the CONCERNS noise problem in a typed
        costume.

        Returns a dict with ``linked`` and, when refused, a ``reason``. Refusing
        is not an error: callers get a reason rather than an exception, because
        a rejected link is a data-quality signal, not a crash.
        """
        edge_type = _TODO_LINK_RELATIONS.get(relation)
        if edge_type is None:
            return {"linked": False, "reason": "unsupported_relation", "relation": relation}

        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $todo_uuid}})
            MATCH (m:Entity {{uuid: $memory_uuid}})
            WHERE m.scope = 'PERSISTENT'
              AND m.structure_role IS NULL
              // Visibility mirrors the read rule: a memory may link to a todo in
              // its own silo or in the shared default bucket, never across silos.
              AND t.namespace IN [coalesce(m.namespace, $default_ns), $default_ns]
            MERGE (m)-[r:{edge_type}]->(t)
            RETURN count(r) AS linked
            """,
            {
                "todo_uuid": todo_uuid,
                "memory_uuid": memory_uuid,
                "default_ns": DEFAULT_TODO_NAMESPACE,
            },
        )
        if rows and int(rows[0].get("linked", 0)) > 0:
            return {"linked": True, "relation": relation, "edge_type": edge_type}
        return {"linked": False, "reason": "ineligible_or_not_found", "relation": relation}

    def todo_inbound_links(self, todo_uuid: str) -> list[dict[str, Any]]:
        """Semantic entities referencing this todo, newest first."""
        return self.neo4j.execute(
            """
            MATCH (m:Entity)-[r]->(t:Todo {uuid: $uuid})
            WHERE type(r) IN $edge_types
            RETURN type(r)  AS relation,
                   m.uuid   AS memory_uuid,
                   m.name   AS memory_name,
                   m.created_at AS created_at
            ORDER BY m.created_at DESC
            """,
            {"uuid": todo_uuid, "edge_types": _ALL_TODO_INBOUND_EDGES},
        )

    def resolve_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        """Close a todo and record the memory that resolved it, atomically.

        Deliberately one Cypher statement. ``Neo4jRepository.execute`` exposes no
        transaction scope, and Neo4j wraps a single statement in an implicit
        transaction, so the edge and the status move together or neither does.
        Splitting this into two calls would allow a RESOLVES_TODO edge pointing
        at a still-open todo, or a closed todo with no evidence of why.

        This is the only path that may create RESOLVES_TODO -- ``link_memory_to_todo``
        refuses the relation precisely so an edge can never imply a status change
        it did not make.

        Eligibility and namespace rules match slice 1. The todo must be open;
        resolving an already-closed todo is refused rather than silently
        re-closing it, so the recorded resolution stays the one that applied.
        """
        return self._lifecycle_transition(
            todo_uuid,
            memory_uuid,
            edge_type=_TODO_LIFECYCLE_RELATIONS["resolves"],
            from_status="open",
            to_status="closed",
            reminder_status="completed",
            set_closed_at=True,
        )

    def reopen_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        """Reopen a closed todo and record the memory that reopened it, atomically.

        Same single-statement guarantee as ``resolve_todo``. Clears ``closed_at``
        and returns any linked reminder to open, mirroring what closing did.
        """
        return self._lifecycle_transition(
            todo_uuid,
            memory_uuid,
            edge_type=_TODO_LIFECYCLE_RELATIONS["reopens"],
            from_status="closed",
            to_status="open",
            reminder_status="open",
            set_closed_at=False,
        )

    def _lifecycle_transition(
        self,
        todo_uuid: str,
        memory_uuid: str,
        *,
        edge_type: str,
        from_status: str,
        to_status: str,
        reminder_status: str,
        set_closed_at: bool,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        closed_at = "$now" if set_closed_at else "null"
        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $todo_uuid}})
            WHERE t.status = $from_status
            MATCH (m:Entity {{uuid: $memory_uuid}})
            WHERE m.scope = 'PERSISTENT'
              AND m.structure_role IS NULL
              AND t.namespace IN [coalesce(m.namespace, $default_ns), $default_ns]
            MERGE (m)-[:{edge_type}]->(t)
            SET t.status = $to_status, t.closed_at = {closed_at}
            WITH t
            OPTIONAL MATCH (t)-[:HAS_REMINDER]->(r:Entity {{type: 'TEMPORAL'}})
            SET r.status = $reminder_status, r.last_accessed = $now
            RETURN count(t) AS applied
            """,
            {
                "todo_uuid": todo_uuid,
                "memory_uuid": memory_uuid,
                "from_status": from_status,
                "to_status": to_status,
                "reminder_status": reminder_status,
                "default_ns": DEFAULT_TODO_NAMESPACE,
                "now": now,
            },
        )
        if rows and int(rows[0].get("applied", 0)) > 0:
            return {"applied": True, "status": to_status, "edge_type": edge_type}
        return {
            "applied": False,
            "reason": "todo_not_in_expected_status_or_memory_ineligible",
            "expected_status": from_status,
        }

    def close_todo(self, uuid: str) -> bool:
        """Mark a todo as closed. Returns True if it was open and got closed.

        Also completes any linked TEMPORAL reminder node via [:HAS_REMINDER].
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            """
            MATCH (n:Todo {uuid: $uuid})
            WHERE n.status = 'open'
            SET n.status = 'closed', n.closed_at = $now
            WITH n
            OPTIONAL MATCH (n)-[:HAS_REMINDER]->(r:Entity {type: 'TEMPORAL'})
            WHERE r.status = 'open'
            SET r.status = 'completed', r.last_accessed = $now
            RETURN count(n) AS updated
            """,
            {"uuid": uuid, "now": now},
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def delete_todo(self, uuid: str) -> bool:
        """Hard-delete a todo node regardless of status.

        Also detach-deletes any linked TEMPORAL reminder node via [:HAS_REMINDER]
        and every owned :TodoLocation. Locations are value objects owned by the
        todo, so they must not outlive it.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Todo {uuid: $uuid})
            OPTIONAL MATCH (n)-[:HAS_REMINDER]->(r:Entity {type: 'TEMPORAL'})
            OPTIONAL MATCH (n)-[:HAS_LOCATION]->(l:TodoLocation)
            WITH n, r, l, count(n) AS found
            DETACH DELETE n, r, l
            RETURN found
            """,
            {"uuid": uuid},
        )
        return bool(rows and int(rows[0].get("found", 0)) > 0)

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

        Each row includes an ``age_days`` field and a ``stale`` flag (True if
        the todo has been open for more than 30 days).

        ``namespace`` is opt-in: omitting it lists every silo (the historical
        behavior). Supplying one narrows to that silo plus ``DEFAULT_NAMESPACE``,
        so the shared bucket stays visible rather than a pinned client seeing
        nothing.
        """
        safe_status = status if status in _VALID_STATUSES else "open"
        safe_limit = max(1, min(limit, 200))
        namespaces = (
            [_safe_namespace(namespace), DEFAULT_NAMESPACE] if namespace else None
        )
        return self.neo4j.execute(
            """
            MATCH (n:Todo)
            WHERE n.status = $status
              AND ($namespaces IS NULL OR n.namespace IN $namespaces)
            WITH n, duration.between(
                datetime(n.created_at), datetime()
            ).days AS age_days
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
                CASE WHEN age_days > 30 THEN true ELSE false END AS stale
            ORDER BY
                CASE n.priority
                    WHEN 'high'   THEN 0
                    WHEN 'normal' THEN 1
                    ELSE               2
                END,
                n.created_at ASC
            LIMIT $limit
            """,
            {"status": safe_status, "limit": safe_limit, "namespaces": namespaces},
        )

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
            [_safe_namespace(namespace), DEFAULT_NAMESPACE] if namespace else None
        )
        rows = self.neo4j.execute(
            """
            MATCH (n:Todo {uuid: $uuid})
            WHERE $namespaces IS NULL OR n.namespace IN $namespaces
            OPTIONAL MATCH (n)-[:REFERENCES_FILE]->(f:Entity)
            OPTIONAL MATCH (n)-[:CREATED_FROM]->(ep:Episodic)
            WITH n, f, ep,
                 duration.between(datetime(n.created_at), datetime()).days AS age_days
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
                CASE WHEN age_days > 30 THEN true ELSE false END AS stale,
                f.structure_path    AS linked_file_path,
                f.structure_project AS linked_file_project,
                ep.uuid             AS episode_uuid,
                locations           AS locations
            LIMIT 1
            """,
            {"uuid": uuid, "namespaces": namespaces},
        )
        if not rows:
            return None

        # Inbound links are fetched separately rather than folded into the query
        # above: that chain already collects locations and concerns, and a third
        # collect over an independent relationship multiplies the intermediate
        # rows before aggregation.
        todo = dict(rows[0])
        todo["inbound_links"] = self.todo_inbound_links(uuid)
        return todo

    def search_by_query(
        self,
        query: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Find open todos whose content matches the query (keyword-based).

        Matches if the full query string appears in content, or if any
        significant word (>= 5 chars) from the query appears in content.
        Returns priority-sorted results.
        """
        safe_limit = max(1, min(limit, 50))
        words = _query_words(query)
        return self.neo4j.execute(
            """
            WITH toLower($query) AS q, $words AS words
            MATCH (t:Todo {status: 'open'})
            WHERE toLower(t.content) CONTAINS q
              OR any(word IN words WHERE toLower(t.content) CONTAINS word)
            RETURN
                t.uuid     AS uuid,
                t.content  AS content,
                t.code_ref AS code_ref,
                t.priority AS priority
            ORDER BY
                CASE t.priority
                    WHEN 'high'   THEN 0
                    WHEN 'normal' THEN 1
                    ELSE               2
                END
            LIMIT $limit
            """,
            {"query": query.lower(), "words": words, "limit": safe_limit},
        )

    def close_stale_todos(self, *, older_than_days: int = 60, dry_run: bool = True) -> dict[str, Any]:
        """Close todos older than N days.

        Returns a dict with ``closed_count`` and ``preview`` (uuids that would
        be or were closed).  With ``dry_run=True`` (default), no todos are
        actually closed.
        """
        now = datetime.now(timezone.utc).isoformat()
        safe_days = max(1, min(older_than_days, 365))

        # Find stale todos
        rows = self.neo4j.execute(
            """
            MATCH (n:Todo {status: 'open'})
            WHERE duration.between(datetime(n.created_at), datetime()).days >= $days
            RETURN n.uuid AS uuid, n.content AS content, n.created_at AS created_at
            ORDER BY n.created_at ASC
            """,
            {"days": safe_days},
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
