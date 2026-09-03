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
from menhir.domain.todo_location import (
    DEFAULT_TODO_NAMESPACE,
    TODO_LINK_RELATIONS,
    code_ref_file_predicate,
    parse_code_ref,
)

_VALID_PRIORITIES = frozenset({"low", "normal", "high"})
_VALID_STATUSES = frozenset({"open", "closed"})

#: Canonical todo-age expression. THE single source of truth: every read that
#: reports an age, flags staleness, or selects todos by age MUST use this and
#: never inline its own duration call.
#:
#: `duration.between(a, b)` returns a STRUCTURED duration whose months are
#: extracted first, so its `.days` component is only the sub-month remainder --
#: a todo created 2026-05-28 and read on 2026-09-02 is "3 months 5 days" and
#: reports `.days == 5`. That silently capped every age at ~31, made the
#: `> TODO_STALE_AFTER_DAYS` flag near-unreachable, and turned
#: `close_stale_todos(older_than_days=60)` into a permanent no-op.
#: `duration.inDays(a, b)` returns a day-only duration, so `.days` is the true
#: total. Verified against the live graph: see tests in tests/test_todo.py.
#:
#: Binds the node to the alias `n`; every query using it must name its :Todo `n`.
TODO_AGE_DAYS_CYPHER = "duration.inDays(datetime(n.created_at), datetime()).days"

#: Days open before a todo is flagged stale in read output. Passed as the
#: `$stale_after` query parameter rather than inlined, so the threshold has one
#: definition too.
TODO_STALE_AFTER_DAYS = 30

#: Shared silo. Every :Todo carries a non-null namespace -- "unscoped" is not
#: representable. Reads that request a silo also see this bucket, so todos
#: written before namespacing (backfilled to 'default') stay visible.
#: Imported directly from domain.namespace (CF-76): it IS the canonical
#: constant, not a local rebind that could silently diverge from it.
#: Defined in the domain module so structural consumers can apply the same rule
#: without importing this repository.




#: Inbound semantic relations a memory may declare against a todo, mapped to their
#: distinct edge types. Slice 1 ships reference relations only -- RESOLVES_TODO and
#: REOPENS_TODO belong with the lifecycle transaction in slice 2, because creating
#: them without one would imply a status change the edge alone must never make.
#:
#: Cypher cannot parameterize a relationship type, so this whitelist is also the
#: injection guard: a relation outside it never reaches a query string.
#:
#: Imported from domain.todo_location (CF-150 shape): the `link_memory_to_todo` MCP
#: tool validates its `relation` argument against the same mapping, and a second
#: hand-written copy there would agree until the day someone adds a relation to one.
_TODO_LINK_RELATIONS = TODO_LINK_RELATIONS

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

#: The one todo-to-todo edge. Every other edge on a :Todo points INWARD from a semantic
#: object, because knowledge belongs in memories and never accumulates inside the todo.
#: This one is deliberately different, and the exception is narrow enough to state exactly:
#: supersession is an IDENTITY fact ("this node replaced that node"), not a knowledge claim.
#: It is the same category as RESOLVES_TODO -- lifecycle, not meaning -- and it makes neither
#: todo describe the other. Admitting it was an owner decision on 2026-09-03, taken because
#: the inward-only alternative (both todos hung off the refiling memory) cannot express which
#: replacement belongs to which original when one memory refiles N todos into M.
#:
#: A todo still never becomes a semantic object: this edge is not recallable, and CF-247's
#: ADJACENCY_EDGE_TYPES allowlist (RELATES_TO, MENTIONS) excludes it from ranking entirely.
_TODO_SUPERSESSION_EDGE = "SUPERSEDED_BY"


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
        return rows

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
    # Inbound semantic links (Phase B slice 1)
    # ------------------------------------------------------------------

    def link_memory_to_todo(
        self, memory_uuid: str, todo_uuid: str, relation: str
    ) -> dict[str, Any]:
        """Point a durable semantic entity at a todo.

        Direction is always inward: a memory references the todo. The todo stays
        an operational object -- knowledge lives in memories and semantic
        objects, never accumulating inside the todo itself.

        The single exception is SUPERSEDED_BY (see ``supersede_todo``), which is
        todo-to-todo because it states an identity fact rather than a knowledge
        claim. It is not reachable from here: this method only ever writes the
        relations in ``_TODO_LINK_RELATIONS``.

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

        Refuses a SUPERSEDED todo. Reopening one produced an open node still carrying
        an outgoing SUPERSEDED_BY -- the state ``supersede_todo`` exists to prevent --
        and made cycles reachable via ``supersede(A, B)`` / ``reopen(A)`` /
        ``supersede(B, A)``. Refusing is the conservative half of the fix: it destroys
        nothing, where deleting the edge on reopen would silently discard the lineage.
        A superseded todo that genuinely needs to come back is reopened by superseding
        or reopening its successor, not by resurrecting the predecessor underneath it.
        """
        return self._lifecycle_transition(
            todo_uuid,
            memory_uuid,
            edge_type=_TODO_LIFECYCLE_RELATIONS["reopens"],
            from_status="closed",
            to_status="open",
            reminder_status="open",
            set_closed_at=False,
            require_no_successor=True,
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
        require_no_successor: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        closed_at = "$now" if set_closed_at else "null"
        # A fixed fragment built from a module constant, never from caller input --
        # the same injection rule the relation whitelists above exist to enforce.
        successor_guard = (
            f"\n              AND NOT (t)-[:{_TODO_SUPERSESSION_EDGE}]->(:Todo)"
            if require_no_successor
            else ""
        )
        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $todo_uuid}})
            WHERE t.status = $from_status{successor_guard}
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

    # ------------------------------------------------------------------
    # Supersession (todo -> todo)
    # ------------------------------------------------------------------

    def supersede_todo(self, old_uuid: str, new_uuid: str) -> dict[str, Any]:
        """Close ``old`` and record that ``new`` replaced it, atomically.

        Menhir has no update path: editing a todo means closing it and adding a
        replacement, which until now dropped the link between the two. This is that
        link, written as the same kind of single statement ``resolve_todo`` is -- a
        SUPERSEDED_BY edge pointing at a still-open todo, or a closed todo with no
        record of what replaced it, are both states the graph must never hold.

        Deliberately NOT built on ``_lifecycle_transition``: that helper hardcodes the
        memory side of the edge (``:Entity`` with ``scope = 'PERSISTENT'`` and no
        ``structure_role``), and this edge has a ``:Todo`` on both ends. The two
        eligibility rules have nothing in common, so they stay separate rather than
        growing a parameter that means "which of two unrelated predicates to apply".

        Refusals, all returned as a reason rather than raised:

        - ``old`` must be open.
        - ``new`` must exist, be open, and not be ``old`` itself.
        - ``old`` must not already have a successor, and ``new`` must not have one
          either. The guard on ``new`` is what blocks cycles, and it is NOT redundant
          with the status guards. An earlier version of this docstring argued that
          closing ``old`` made cycles impossible; that was wrong, because ``reopen_todo``
          returns a superseded todo to open and does not remove its edge. The concrete
          sequence was: ``supersede(A, B)`` -> ``reopen(A)`` -> ``supersede(B, A)``,
          leaving A->B and B->A. Reopen now refuses a superseded todo, and this guard
          closes the same hole independently: every ancestor in a chain carries an
          outgoing SUPERSEDED_BY, so a ``new`` with no successor cannot be an ancestor
          of ``old``. Two guards for one invariant is deliberate -- this one is local to
          the write and holds even if the reopen rule is later relaxed.
        - Namespace, mirroring the slice-1 rule exactly: ``new`` must be in ``old``'s
          silo or in the shared default bucket. The asymmetry is inherited on purpose --
          a default-bucket todo cannot be superseded by a siloed one, the same way a
          memory cannot link across silos.

          **This is the inverse of `supersede_artifact`.** That path uses
          ``namespace_compatibility_cypher(owner=new, subordinate=old)``, i.e.
          ``old.namespace IN [new.namespace, default]`` -- owner and subordinate are
          swapped for the same verb, so the two tools refuse opposite cases. Kept rather
          than aligned because todos inherit the slice-1 link rule, where the memory doing
          the pointing is the owner, and changing it would silently alter which existing
          links are legal. Recorded here because an agent that learned one rule will get
          the opposite answer from the other, and that is a documentation problem, not a
          bug in either.
        """
        if old_uuid == new_uuid:
            return {"applied": False, "reason": "cannot_supersede_itself"}

        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            f"""
            MATCH (t_old:Todo {{uuid: $old_uuid}})
            WHERE t_old.status = 'open'
              AND NOT (t_old)-[:{_TODO_SUPERSESSION_EDGE}]->(:Todo)
            MATCH (t_new:Todo {{uuid: $new_uuid}})
            WHERE t_new.status = 'open'
              AND NOT (t_new)-[:{_TODO_SUPERSESSION_EDGE}]->(:Todo)
              AND coalesce(t_new.namespace, $default_ns)
                  IN [coalesce(t_old.namespace, $default_ns), $default_ns]
            MERGE (t_old)-[:{_TODO_SUPERSESSION_EDGE}]->(t_new)
            SET t_old.status = 'closed', t_old.closed_at = $now
            WITH t_old
            OPTIONAL MATCH (t_old)-[:HAS_REMINDER]->(r:Entity {{type: 'TEMPORAL'}})
            SET r.status = 'completed', r.last_accessed = $now
            RETURN count(t_old) AS applied
            """,
            {
                "old_uuid": old_uuid,
                "new_uuid": new_uuid,
                "default_ns": DEFAULT_TODO_NAMESPACE,
                "now": now,
            },
        )
        if rows and int(rows[0].get("applied", 0)) > 0:
            return {
                "applied": True,
                "status": "closed",
                "edge_type": _TODO_SUPERSESSION_EDGE,
                "superseded_by": new_uuid,
            }
        return {"applied": False, "reason": self._supersede_refusal_reason(old_uuid)}

    def _supersede_refusal_reason(self, old_uuid: str) -> str:
        """Name which precondition failed, for a refusal the caller can act on.

        A second read, deliberately: the write is one statement for atomicity, so it
        cannot also report WHY it matched nothing. Diagnosing after the fact costs a
        round trip only on the refusal path. It is advisory -- state may have moved
        between the two reads -- so it never gates anything, it only explains.

        Worth the round trip because `old_already_superseded` is now reachable and a
        caller told only "refused" cannot tell it apart from a typo'd uuid.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $uuid}})
            RETURN t.status AS status,
                   exists((t)-[:{_TODO_SUPERSESSION_EDGE}]->(:Todo)) AS has_successor
            LIMIT 1
            """,
            {"uuid": old_uuid},
        )
        if not rows:
            return "old_todo_not_found"
        if rows[0].get("has_successor"):
            return "old_todo_already_superseded"
        if rows[0].get("status") != "open":
            return "old_todo_not_open"
        # Old is fine, so the failure is on the new side: absent, closed, already a
        # predecessor itself, or in a namespace the old todo cannot reach.
        return "new_todo_ineligible"

    def todo_supersession(
        self, todo_uuid: str, namespaces: list[str] | None = None
    ) -> dict[str, Any]:
        """Both directions of this todo's refile lineage.

        The reader half of ``supersede_todo``. CF-143 removed two edge types that were
        written on every create and read by nothing; this exists so SUPERSEDED_BY never
        becomes the third. It is surfaced through ``get_todo``, which renders it.

        Both sides are collected, and neither is a grouping key. An earlier version
        returned ``succ.uuid`` bare with ``LIMIT 1``: a todo with two successors -- which
        concurrent supersessions can still produce, since the write's successor guard is
        a read in the same statement rather than a constraint -- then yielded two rows and
        the unordered LIMIT dropped one at random. Collecting reports the anomaly instead
        of hiding it, so ``superseded_by`` is a LIST. Callers treat >1 as a repair signal,
        not as a normal outcome.

        ``namespaces`` scopes both sides to the caller's silo. Without it a shared
        default-bucket todo hands every reader the uuids of siloed todos on either end
        of its lineage.

        That filter is applied in PYTHON, not in the query, and deliberately. The rule
        todos use -- "the caller's silo OR the shared default bucket" -- is not the rule
        ``tenant_scope_cypher`` implements, which is "the caller's silo", with
        ``namespace_spellings`` covering only the ``''``/``'default'`` spelling
        equivalence. Using the shared builder here would hide default-bucket lineage
        from a siloed caller who can nonetheless read the default-bucket todo itself.
        Filtering in Python against the SAME ``namespaces`` list ``get_todo`` matched on
        keeps the two rules identical by construction, and adds no fourth hand-written
        tenancy predicate to this file (CF-127's ratchet).
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (t:Todo {{uuid: $uuid}})
            OPTIONAL MATCH (t)-[:{_TODO_SUPERSESSION_EDGE}]->(succ:Todo)
            WITH t, collect(DISTINCT {{uuid: succ.uuid, ns: succ.namespace}}) AS successors
            OPTIONAL MATCH (pred:Todo)-[:{_TODO_SUPERSESSION_EDGE}]->(t)
            RETURN successors AS superseded_by,
                   collect(DISTINCT {{uuid: pred.uuid, ns: pred.namespace}}) AS supersedes
            """,
            {"uuid": todo_uuid},
        )
        if not rows:
            return {"superseded_by": [], "supersedes": []}

        def _visible(entries: Any) -> list[str]:
            out = []
            for entry in entries or []:
                uuid = (entry or {}).get("uuid")
                if not uuid:
                    continue  # OPTIONAL MATCH miss collects a null-valued map
                if namespaces is not None and (entry.get("ns") or DEFAULT_TODO_NAMESPACE) not in namespaces:
                    continue
                out.append(uuid)
            return out

        return {
            "superseded_by": _visible(rows[0].get("superseded_by")),
            "supersedes": _visible(rows[0].get("supersedes")),
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
            [normalize_namespace(namespace), DEFAULT_NAMESPACE] if namespace else None
        )
        return self.neo4j.execute(
            """
            MATCH (n:Todo)
            WHERE n.status = $status
              AND ($namespaces IS NULL OR n.namespace IN $namespaces)
            WITH n, """ + TODO_AGE_DAYS_CYPHER + """ AS age_days
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
