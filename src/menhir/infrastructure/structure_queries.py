"""Cypher writer for structural project entities and edges.

Writes deterministic structural data directly to Neo4j — not through
Graphiti — so the project skeleton is always complete and reliable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4, uuid5

from menhir.domain.namespace import namespace_to_group_ids
from menhir.domain.todo_location import (
    DEFAULT_TODO_NAMESPACE as _DEFAULT_TODO_NAMESPACE,
)
from menhir.domain.truth.kinds import SOURCE_CONFIDENCE_AGENT
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.structure_queries_common import (
    _ENTITY_DEFAULTS,
    STRUCTURE_SOURCE,
    STRUCTURE_SOURCE_CONFIDENCE,
)
from menhir.infrastructure.structure_queries_content import (
    _set_properties,
    StructureGraphContentMixin,
)
from menhir.infrastructure.structure_queries_internals import (
    _symbol_path,
    StructureGraphInternalsMixin,
)
from menhir.infrastructure.structure_queries_maintenance import StructureGraphMaintenanceMixin
from menhir.infrastructure.structure_queries_metadata import (
    _normalize_structure_path,
    StructureGraphMetadataMixin,
)
from menhir.infrastructure.structure_queries_reads import StructureGraphReadsMixin
from menhir.infrastructure.structure_queries_writes import StructureGraphWritesMixin

# Frozen namespace for project identities that exist only because another project's scan named
# them. These ids are placeholders until the project is scanned directly and its settled identity
# overwrites the inferred one. A namespace constant (rather than NAMESPACE_URL at each call site)
# makes the allocation contract explicit and prevents the CALLS and CONTAINS_REPO writers from
# drifting into independent identity spaces.
_INFERRED_PROJECT_ID_NAMESPACE = UUID("b872ff47-dd11-5845-9702-6e0eef2730fd")


def _inferred_project_id(project_name: str) -> str:
    """Return the stable placeholder identity shared by every inferred-project writer."""
    return str(uuid5(_INFERRED_PROJECT_ID_NAMESPACE, project_name))

# Both inferred edge writers splice in this exact prefix. New targets MERGE on the live composite
# uniqueness constraint, so concurrent writers all contend for one (project_id, path) key. The
# legacy lookup is a compatibility bridge: once a direct scan has replaced the placeholder id with
# the settled id, later inferred edges must reuse that row rather than recreate the placeholder.
_INFERRED_PROJECT_TARGET_CYPHER = """
            OPTIONAL MATCH (legacy:Entity {
                structure_project: $target_name,
                structure_path: '.',
                structure_role: 'project'
            })
            WITH head(collect(legacy)) AS existing
            CALL {
                WITH existing
                WITH existing WHERE existing IS NOT NULL
                RETURN existing AS target
                UNION
                WITH existing
                WITH existing WHERE existing IS NULL
                MERGE (target:Entity {
                    structure_project_id: $target_project_id,
                    structure_path: '.'
                })
                ON CREATE SET
                    target.uuid = $uuid,
                    target.identity_source = 'inferred',
                    target.content = $target_name,
                    target.type = 'SEMANTIC',
                    target.scope = 'PERSISTENT',
                    target.source = 'project-scan',
                    target.source_confidence = $sc_inferred,
                    target.user_flagged = false,
                    target.group_id = '',
                    target.session_id = $session_id,
                    target.user_id = $user_id,
                    target.created_at = $now,
                    target.last_accessed = $now
                RETURN target
            }
            SET target.structure_project = $target_name,
                target.structure_role = 'project',
                target.name = $target_name,
                target.structure_project_id = coalesce(
                    target.structure_project_id, $target_project_id
                ),
                target.identity_source = coalesce(target.identity_source, 'inferred')
            WITH target
"""


@dataclass
class StructureGraphWriter(
    StructureGraphWritesMixin,
    StructureGraphMaintenanceMixin,
    StructureGraphMetadataMixin,
    StructureGraphReadsMixin,
    StructureGraphContentMixin,
    StructureGraphInternalsMixin,
):
    """Structure graph facade.

    The method surface is unchanged: it is assembled verbatim from the
    ``structure_queries_*`` sibling mixins. This module keeps the inferred-project
    identity unit plus the methods other modules pin by source (blast radius,
    linked memories, inferred edge writers) and re-exports the moved constants and
    helpers so every existing import keeps working.
    """

    neo4j: Neo4jRepository

    def query_blast_radius(
        self,
        project: str,
        file_paths: list[str],
        max_depth: int = 5,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Trace transitive reverse-imports from changed files + map to affected tests.

        Returns {
            "changed": [...],
            "directly_affected": [...],
            "transitively_affected": [...],
            "affected_tests": [...],
            "cross_project_refs": [...],
        }
        """
        # Transitive reverse-import traversal with depth tracking
        # depth=1 → direct importers, depth>1 → transitive
        rows = self.neo4j.execute(
            """
            UNWIND $paths AS changed_path
            MATCH (changed:Entity {structure_project: $p, structure_path: changed_path})
            OPTIONAL MATCH path = (importer:Entity)-[:IMPORTS*1..]->(changed)
            WHERE ALL(n IN nodes(path) WHERE n.structure_project = $p)
              AND length(path) <= $depth
            RETURN DISTINCT importer.structure_path AS importer_path,
                   length(path) AS hop_distance
            """,
            {"p": project, "paths": file_paths, "depth": max_depth},
        )

        direct: set[str] = set()
        transitive: set[str] = set()
        changed_set = set(file_paths)

        for row in rows:
            imp = row.get("importer_path")
            if not imp or imp in changed_set:
                continue
            dist = row.get("hop_distance", 1)
            if dist == 1:
                direct.add(imp)
            else:
                transitive.add(imp)

        # Don't double-count: if a file is both direct and transitive, keep it as direct
        transitive -= direct

        # Find affected tests: tests that cover any changed/affected file
        all_impacted = list(changed_set | direct | transitive)
        test_rows = self.neo4j.execute(
            """
            UNWIND $paths AS src_path
            MATCH (t:Entity)-[:TESTS]->(s:Entity {structure_project: $p, structure_path: src_path})
            RETURN DISTINCT t.structure_path AS test_file, s.structure_path AS covers
            ORDER BY t.structure_path
            """,
            {"p": project, "paths": all_impacted},
        )
        affected_tests = [
            {"test": str(r["test_file"]), "covers": str(r["covers"])}
            for r in test_rows
            if r.get("test_file")
        ]

        # Cross-project impact: check if any changed file is an endpoint or has CALLS edges
        xref_rows = self.neo4j.execute(
            """
            MATCH (src:Entity {structure_project: $p, structure_role: 'project'})-[r:CALLS]->(tgt:Entity)
            RETURN tgt.name AS target, r.mechanism AS mechanism
            """,
            {"p": project},
        )
        cross_refs = [
            {"target": str(r["target"]), "mechanism": str(r.get("mechanism", ""))}
            for r in xref_rows
        ]

        # Related memories: semantic memories anchored to impacted files
        related_memories = self.query_linked_memories(
            project, all_impacted, limit=10, namespace=namespace
        )

        # Open TODOs at any impacted location. Matched on the todo's own
        # normalized :TodoLocation (exact project + path) rather than the
        # REFERENCES_FILE edge, which resolved only 13 of 77 todos because
        # code_ref mixes workspace- and project-relative forms while the old
        # resolver required the stored path to END WITH the ref.
        #
        # Line and symbol are narrowing detail, never identity: a todo naming a
        # file with no line still surfaces for that file.
        #
        # Namespace is read off the owning :Todo -- locations deliberately carry
        # no copy of it -- and follows the same requested-plus-default rule as
        # list_todos/get_todo so this path cannot leak across silos.
        todo_namespaces = [namespace, _DEFAULT_TODO_NAMESPACE] if namespace else None
        todo_rows = self.neo4j.execute(
            """
            UNWIND $paths AS src_path
            MATCH (t:Todo {status: 'open'})-[:HAS_LOCATION]->(l:TodoLocation)
            WHERE l.resolution_status = 'resolved'
              AND (
                    l.path = src_path
                    // A bare filename ("CardPrintingImportService.java") is a
                    // legitimate but underspecified declaration. Match it on
                    // basename only -- narrower than the old blanket ENDS WITH,
                    // which is what made an unrelated ref match any suffix.
                    OR (NOT l.path CONTAINS '/' AND src_path ENDS WITH ('/' + l.path))
                  )
              AND (l.project IS NULL OR l.project = $p)
              AND ($namespaces IS NULL OR t.namespace IN $namespaces)
            RETURN DISTINCT
                t.uuid     AS uuid,
                t.content  AS content,
                t.priority AS priority,
                t.code_ref AS code_ref,
                src_path   AS file_path
            ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END
            """,
            {"paths": all_impacted, "p": project, "namespaces": todo_namespaces},
        )
        open_todos = [
            {
                "uuid": str(r["uuid"]),
                "content": str(r["content"]),
                "priority": str(r["priority"]),
                "code_ref": r.get("code_ref"),
                "file_path": str(r["file_path"]),
            }
            for r in todo_rows
            if r.get("uuid")
        ]

        # Function-level callers via intra-project CALLS edges
        fn_caller_rows = self.neo4j.execute(
            """
            UNWIND $paths AS changed_path
            MATCH (f:Entity {structure_project: $p, structure_path: changed_path})
            MATCH (f)-[:DEFINES]->(callee_sym:Entity {structure_role: 'symbol'})
            MATCH (caller_sym:Entity {structure_role: 'symbol', structure_project: $p})-[:CALLS]->(callee_sym)
            MATCH (caller_file:Entity {structure_project: $p})-[:DEFINES]->(caller_sym)
            WHERE caller_file.structure_path <> changed_path
              AND NOT caller_file.structure_path IN $paths
            RETURN DISTINCT
                caller_sym.name AS caller_name,
                caller_sym.symbol_parent AS caller_class,
                caller_file.structure_path AS caller_file,
                callee_sym.name AS callee_name,
                callee_sym.symbol_parent AS callee_class
            ORDER BY caller_file.structure_path, caller_sym.name
            LIMIT 30
            """,
            {"p": project, "paths": file_paths},
        )
        function_callers = [
            {
                "caller": f"{r['caller_class']}.{r['caller_name']}"
                if r.get("caller_class")
                else str(r["caller_name"]),
                "caller_file": str(r["caller_file"]),
                "callee": f"{r['callee_class']}.{r['callee_name']}"
                if r.get("callee_class")
                else str(r["callee_name"]),
            }
            for r in fn_caller_rows
            if r.get("caller_file")
        ]

        # Coverage qualification: which requested paths are actually in the index, and is the
        # project's index complete? Without this the caller cannot distinguish "nothing
        # depends on this file" from "this file was never scanned".
        indexed = self.which_paths_indexed(project, list(file_paths))
        return {
            "changed": sorted(changed_set),
            "directly_affected": sorted(direct),
            "transitively_affected": sorted(transitive),
            "affected_tests": affected_tests,
            "cross_project_refs": cross_refs,
            "related_memories": related_memories,
            "open_todos": open_todos,
            "function_callers": function_callers,
            "unindexed_paths": sorted(p for p in file_paths if p not in indexed),
            "coverage": self.get_project_coverage(project),
        }

    def query_linked_memories(
        self,
        project: str,
        file_paths: list[str],
        limit: int = 10,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, str]]:
        """Find semantic memories anchored to the given structural file paths.

        The result is restricted to the given namespace when one is supplied, and
        unfiltered when it is None.
        """
        if not file_paths:
            return []
        # Isolation is opt-in (domain/namespace.py): an unspecified namespace must not filter,
        # so the predicate is omitted entirely rather than passed as a null-guarded no-op.
        # Predicated on group_id, the load-bearing isolation boundary -- `namespace` on a node
        # is only the defense-in-depth stamp. NULL group_id therefore does NOT match a scoped
        # read: this is a leak fix, so an unstamped node fails closed rather than open.
        group_ids = namespace_to_group_ids(namespace)
        params: dict[str, Any] = {"p": project, "paths": file_paths, "limit": limit}
        tenancy_filter = ""
        if group_ids is not None:
            tenancy_filter = "AND sem.group_id IN $group_ids"
            params["group_ids"] = group_ids
        rows = self.neo4j.execute(
            f"""
            UNWIND $paths AS fp
            MATCH (struct:Entity {{structure_project: $p, structure_path: fp}})
                  <-[r:ANCHORED_TO]-(sem:Entity)
            WHERE sem.structure_role IS NULL
              AND coalesce(sem.freshness, 'ACTIVE') <> 'GONE'
              {tenancy_filter}
            WITH sem, struct, r
            ORDER BY sem.last_accessed DESC
            RETURN sem.uuid AS uuid,
                   sem.name AS name,
                   left(coalesce(sem.content, sem.summary, ''), 120) AS preview,
                   collect(DISTINCT struct.structure_path)[0] AS linked_file,
                   collect(DISTINCT r.anchor_source)[0] AS anchor_source,
                   max(sem.last_accessed) AS last_accessed
            ORDER BY last_accessed DESC
            LIMIT $limit
            """,
            params,
        )
        return [
            {
                "uuid": str(r["uuid"]),
                "name": str(r.get("name", "")),
                "preview": str(r.get("preview", "")),
                "linked_file": str(r.get("linked_file", "")),
                "anchor_source": str(r.get("anchor_source", "")),
            }
            for r in rows
            if r.get("uuid")
        ]

    def _write_calls_edge(
        self,
        source_project: str,
        ref: Any,
        session_id: str,
        user_id: str,
        now: str,
    ) -> None:
        """Write a CALLS edge between two project entities.

        Creates the target project entity if it doesn't exist yet.
        """
        self.neo4j.execute(
            _INFERRED_PROJECT_TARGET_CYPHER
            + """
            MATCH (source:Entity {structure_project: $source_name, structure_path: '.', structure_role: 'project'})
            MERGE (source)-[r:CALLS]->(target)
            ON CREATE SET r.source = 'project-scan', r.mechanism = $mechanism, r.evidence = $evidence, r.created_at = datetime()
            ON MATCH SET r.mechanism = $mechanism, r.evidence = $evidence
            """,
            {
                "source_name": source_project,
                "target_name": ref.target_project,
                "uuid": str(uuid4()),
                "target_project_id": _inferred_project_id(ref.target_project),
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
                "mechanism": ref.mechanism,
                "evidence": ref.evidence,
                "sc_inferred": SOURCE_CONFIDENCE_AGENT,
            },
        )

    def _write_contains_repo_edge(
        self,
        umbrella_project: str,
        nested: Any,
        session_id: str,
        user_id: str,
        now: str,
    ) -> None:
        """Write a CONTAINS_REPO edge from an umbrella project to a nested repository.

        Deliberately NOT `CONTAINS`: that is the within-repo project→directory→file chain that
        `blast_radius` traverses, and reusing it would let impact analysis cross a repository
        boundary -- reintroducing in edge form the coupling the scan boundary removes. Also not
        `CALLS`, which means a runtime/import dependency; containing a repo implies no such
        thing.

        Like `_write_calls_edge`, the target project entity is MERGEd, so an umbrella may be
        scanned before its children and the edges still land; each child fills in its own
        detail when scanned directly.
        """
        self.neo4j.execute(
            _INFERRED_PROJECT_TARGET_CYPHER
            + """
            MATCH (source:Entity {structure_project: $source_name, structure_path: '.', structure_role: 'project'})
            MERGE (source)-[r:CONTAINS_REPO]->(target)
            ON CREATE SET r.source = 'project-scan', r.rel_path = $rel_path, r.created_at = datetime()
            ON MATCH SET r.rel_path = $rel_path
            """,
            {
                "source_name": umbrella_project,
                "target_name": nested.name,
                "rel_path": nested.rel_path,
                "uuid": str(uuid4()),
                "target_project_id": _inferred_project_id(nested.name),
                "session_id": session_id,
                "user_id": user_id,
                "now": now,
                "sc_inferred": SOURCE_CONFIDENCE_AGENT,
            },
        )
