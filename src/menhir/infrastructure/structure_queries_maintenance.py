"""Identity-keyed maintenance for the structure graph: mtime reads and stale prunes."""

from __future__ import annotations

from typing import Any


class StructureGraphMaintenanceMixin:
    """Mixin part of ``StructureGraphWriter`` carrying prune/delete and heat helpers."""

    def _owner_arms(
        self, alias: str, project_id: str | None
    ) -> tuple[str, ...]:
        """WHERE fragments that together address exactly the rows this scan owns (#99).

        Every prune below used to match `{structure_project: $project}` -- the caller-supplied
        DISPLAY NAME -- while the identity gate validated `project_id`, `root_key`, generation and
        host. The gate guarded the front door and the deletes used a different address, so nothing
        proved that the rows about to be DETACH DELETEd were the rows the gate authorised. Renaming
        a project made the same gap visible from the other side: `get_file_mtimes(new_name)`
        returned nothing, the writer took the first-scan branch, and every entity under the old
        name was orphaned while the name-keyed prunes matched nothing at all.

        Two arms rather than one `OR`, because each is a separate indexed lookup -- the composite
        constraint `(structure_project_id, structure_path)` backs the first and
        `entity_structure_project_path_idx` the second. An `OR` across both would plan as a label
        scan over every :Entity in the graph, six times per scan.

        The arms are disjoint by construction, so a row is never deleted twice:

        1. rows stamped with THIS identity, whatever they are currently called;
        2. rows with NO identity stamp that carry this display name -- written before CF-257
           existed. They are matched only while unstamped: a row bearing ANOTHER project's id can
           never be reached by either arm, which is the property the old predicate lacked.

        **When `project_id` is absent this degrades to the old name-only behaviour** rather than
        refusing. An id-less scan never passed an identity gate, so there is no validated identity
        to diverge from and nothing here can invent one; making it refuse would change behaviour
        on a path CF-257 is separately closing. That remains the residual hole in #99 and it shuts
        when `project_id` becomes mandatory on the write path.

        **Consequence worth stating: `identity_action="new"` no longer prunes the old silo.** Rows
        stamped with a superseded id are matched by neither arm, so minting a fresh identity at a
        root that already had one leaves the previous project's rows in place instead of deleting
        them under the shared name. That is the intended direction -- those rows belong to an
        identity this scan was not authorised to touch, which is the whole point -- but it means
        `adopt` remains the way to continue a project, and a deliberate `new` leaves a silo behind
        for the operator to remove explicitly.
        """
        if not project_id:
            return (f"{alias}.structure_project = $project",)
        return (
            f"{alias}.structure_project_id = $project_id",
            f"{alias}.structure_project_id IS NULL AND {alias}.structure_project = $project",
        )

    def _owner_params(self, project_name: str, project_id: str | None) -> dict[str, Any]:
        """The parameters both arms read. Values stay parameterised; only the predicate is spliced."""
        return {"project": project_name, "project_id": project_id}

    def get_file_mtimes(
        self, project_name: str, project_id: str | None = None
    ) -> dict[str, float]:
        """Return stored file mtimes keyed by rel_path for a project.

        Only returns rows where ``file_mtime`` is set and non-zero — i.e.
        file/entrypoint/config/test entities written with scanner v2+.
        Returns empty dict for first-ever scan.

        Keyed on identity (see `_owner_arms`): addressing this by name meant a renamed project
        read back zero mtimes and was treated as a first-ever scan, which is how the rename
        orphaned its own file entities (#99).
        """
        mtimes: dict[str, float] = {}
        for owner in self._owner_arms("n", project_id):
            rows = self.neo4j.execute(
                f"""
                MATCH (n:Entity)
                WHERE {owner}
                  AND n.structure_role IN ['file', 'entrypoint', 'config', 'test']
                  AND n.file_mtime IS NOT NULL AND n.file_mtime > 0
                RETURN n.structure_path AS path, n.file_mtime AS mtime
                """,
                self._owner_params(project_name, project_id),
            )
            for r in rows:
                if r.get("path"):
                    mtimes[str(r["path"])] = float(r["mtime"])
        return mtimes

    def _delete_file_entities(
        self, project_name: str, rel_paths: list[str], project_id: str | None = None
    ) -> None:
        """Delete file Entity nodes (and their Symbol children) for files removed from the project."""
        for owner in self._owner_arms("f", project_id):
            self.neo4j.execute(
                f"""
                UNWIND $paths AS path
                MATCH (f:Entity {{structure_path: path}})
                WHERE {owner}
                OPTIONAL MATCH (f)-[:DEFINES]->(sym:Entity {{structure_role: 'symbol'}})
                DETACH DELETE f, sym
                """,
                {**self._owner_params(project_name, project_id), "paths": rel_paths},
            )

    def _delete_stale_role_entities(
        self, project_name: str, role: str, keep_paths: list[str],
        project_id: str | None = None, *, source: str | None = None,
    ) -> int:
        """Delete entities of *role* whose `structure_path` is absent from the current scan.

        An EMPTY `keep_paths` deletes every entity of *role*, and that is deliberate. Callers
        must first establish that the scan was complete (`not scan.partial_index`); given a
        complete scan, "found none" is a real answer and the only way to represent a project
        that stopped exposing anything. Guarding on emptiness instead made zero permanently
        unreachable -- see the archolith endpoint accumulation in `write_project` step 5b.

        *source* narrows the prune to entities with that `source` label. The `document` role is
        shared by scanner-written orientation docs (`project-scan`, repo-relative paths) and
        `ingest_document` docs (`document-ingest`, absolute-path keys); only the former are the
        scan's to prune.
        """
        source_clause = " AND n.source = $source" if source is not None else ""
        deleted = 0
        for owner in self._owner_arms("n", project_id):
            rows = self.neo4j.execute(
                f"""
                MATCH (n:Entity {{structure_role: $role}})
                WHERE {owner}
                  AND NOT n.structure_path IN $keep{source_clause}
                DETACH DELETE n
                RETURN count(*) AS deleted
                """,
                {
                    **self._owner_params(project_name, project_id),
                    "role": role,
                    "keep": keep_paths,
                    **({"source": source} if source is not None else {}),
                },
            )
            deleted += int(rows[0].get("deleted", 0)) if rows else 0
        return deleted

    def _delete_stale_role_entities_multi(
        self, project_name: str, roles: list[str], keep_paths: list[str],
        project_id: str | None = None,
    ) -> int:
        """Delete entities across several roles whose path is absent from the current scan.

        Files span four roles (`file`/`entrypoint`/`config`/`test`) and a single scan path
        may be classified into any of them, so the keep-set must be applied across all four
        at once -- pruning role by role would delete a file that merely changed role between
        scans (e.g. `app.py` reclassified from `file` to `entrypoint`).

        Same anti-footgun: an empty keep-list deletes nothing.
        """
        if not keep_paths:
            return 0
        deleted = 0
        for owner in self._owner_arms("n", project_id):
            rows = self.neo4j.execute(
                f"""
                MATCH (n:Entity)
                WHERE {owner}
                  AND n.structure_role IN $roles
                  AND NOT n.structure_path IN $keep
                OPTIONAL MATCH (n)-[:DEFINES]->(sym:Entity {{structure_role: 'symbol'}})
                DETACH DELETE n, sym
                RETURN count(*) AS deleted
                """,
                {
                    **self._owner_params(project_name, project_id),
                    "roles": roles,
                    "keep": keep_paths,
                },
            )
            deleted += int(rows[0].get("deleted", 0)) if rows else 0
        return deleted

    def _delete_stale_contains_repo_edges(
        self, project_name: str, keep_rel_paths: list[str],
        project_id: str | None = None,
    ) -> int:
        """Delete CONTAINS_REPO edges whose `rel_path` is absent from the current scan.

        Deletes the RELATIONSHIP only, never the child project entity: the child is its own
        project with its own ingest, and an umbrella that stops containing it says nothing
        about whether it still exists. An empty keep-list is meaningful on a complete scan --
        an umbrella whose sub-repos were all removed contains none.
        """
        deleted = 0
        for owner in self._owner_arms("p", project_id):
            rows = self.neo4j.execute(
                f"""
                MATCH (p:Entity {{structure_path: '.', structure_role: 'project'}})
                      -[r:CONTAINS_REPO]->()
                WHERE {owner}
                  AND NOT coalesce(r.rel_path, '') IN $keep
                DELETE r
                RETURN count(*) AS deleted
                """,
                {**self._owner_params(project_name, project_id), "keep": keep_rel_paths},
            )
            deleted += int(rows[0].get("deleted", 0)) if rows else 0
        return deleted

    def _delete_stale_directories(
        self, project_name: str, keep_paths: list[str],
        project_id: str | None = None,
    ) -> int:
        """Delete directory entities no longer present in the scan.

        File entities are pruned via `_delete_file_entities` off the stored-mtime diff, but
        directories carry no mtime and so had no pruning path at all -- they accumulated
        forever. Making nested repos a scan boundary exposed how bad that is: the `archolith`
        umbrella dropped from 1,977 files to 8 while keeping 7,103 directory entities and
        7,066 CONTAINS edges describing subtrees it no longer indexes.

        Anti-footgun: an EMPTY `keep_paths` deletes nothing. A scan that legitimately finds no
        directories is indistinguishable here from a scan that failed to populate them, and
        wiping a project's entire directory tree on the strength of an empty list is not a
        risk worth taking for a case that saves nothing.
        """
        if not keep_paths:
            return 0
        deleted = 0
        for owner in self._owner_arms("d", project_id):
            rows = self.neo4j.execute(
                f"""
                MATCH (d:Entity {{structure_role: 'directory'}})
                WHERE {owner}
                  AND NOT d.structure_path IN $keep
                WITH d, count(*) AS _
                DETACH DELETE d
                RETURN count(*) AS deleted
                """,
                {**self._owner_params(project_name, project_id), "keep": keep_paths},
            )
            deleted += int(rows[0].get("deleted", 0)) if rows else 0
        return deleted

    def _increment_heat(self, project_name: str, rel_paths: list[str]) -> None:
        """Increment hot_count on file entities that changed in this scan."""
        self.neo4j.execute(
            """
            UNWIND $paths AS path
            MATCH (n:Entity {structure_project: $project, structure_path: path})
            WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test']
            SET n.hot_count = coalesce(n.hot_count, 0) + 1
            """,
            {"project": project_name, "paths": rel_paths},
        )
