"""Structure graph write/query delegates and the structure query allowlist.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphStructureMixin:
    """Structure graph write/query delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # Structure graph delegates → StructureGraphWriter
    # -------------------------------------------------------------------------

    def write_project_structure(
        self,
        scan: Any,
        session_id: str,
        user_id: str,
    ) -> dict[str, int]:
        """Write a project's structure graph, subject to the migration fence.

        THE choke point, and that is why the fence sits here rather than at the four call sites:
        the REST scan path, the deprecated raw-payload path, the background symbol rescan and the
        unattended watcher all arrive through this one method. Guarding the callers instead would
        mean four places to keep in step, and the next writer added would silently miss it.
        """
        from menhir.infrastructure.project_identity_binding import binding_host, root_key_for
        from menhir.infrastructure.structure_write_fence import (
            IdentityClaim, admit_structure_writer, release_structure_writer,
        )

        # CF-257. The identity invariant belongs HERE, beside the fence, for the same reason the
        # fence does: this is the one method every structure writer funnels through. Settling
        # identity only in `scan_and_write_project` left the watcher and the deprecated raw path
        # writing id-less nodes -- and a NULL key does not violate a uniqueness constraint, so the
        # invariant eroded silently from zero to 1,816 nodes with both constraints live and no
        # error anywhere. Refusing at the choke point is what makes "every structure node carries
        # an id" a property of the system rather than of one call path.
        #
        # **A populated id is not an authorisation.** This check used to be exactly
        # `if not scan.project_id`, and that admits the stale-transfer race: X settles, Y
        # supersedes X, X's scan finishes minutes later and writes under an identity that no
        # longer owns the directory -- carrying the per-project stale prune into another
        # project's silo. So the id travels as a CLAIM (identity, directory, generation) and is
        # re-validated inside `admit_structure_writer`, in the same statement that registers the
        # writer and under a lock a concurrent transfer must wait for.
        if not getattr(scan, "project_id", None):
            raise ValueError(
                f"Refusing to write structure for {getattr(scan, 'name', '<unknown>')!r} with no "
                "structure_project_id. Callers must settle identity first "
                "(services.project_identity_service.settle_project_identity). An id-less node is "
                "invisible to the composite uniqueness constraint, so it erodes the invariant "
                "without failing."
            )

        # The directory comes from the SCAN, not from the claim. The claim must authorise the
        # directory this payload actually describes, so a settlement that bound some other root
        # fails here rather than being taken on trust.
        claim = IdentityClaim(
            project_id=str(scan.project_id),
            root_key=root_key_for(str(getattr(scan, "root_path", "") or "")),
            generation=int(getattr(scan, "identity_generation", 0) or 0),
            host=binding_host(),
        )
        handle = admit_structure_writer(
            self.neo4j, label=str(getattr(scan, "name", "") or ""), claim=claim
        )
        try:
            return self._structure.write_project(scan, session_id, user_id)
        finally:
            release_structure_writer(self.neo4j, handle)

    def write_document(
        self,
        file_path: str,
        content: str,
        *,
        project: str,
        structure_path: str,
        project_id: str,
        identity_generation: int,
        identity_root: str,
        session_id: str,
        user_id: str,
        document_type: str = "generic",
    ) -> None:
        """Write one document under the same durable identity fence as a project scan."""
        from menhir.infrastructure.project_identity_binding import binding_host, root_key_for
        from menhir.infrastructure.structure_write_fence import (
            IdentityClaim, admit_structure_writer, release_structure_writer,
        )

        if not project_id:
            raise ValueError(
                f"Refusing to write document structure for {project!r} with no "
                "structure_project_id. Callers must settle identity first."
            )
        if not identity_root:
            raise ValueError(
                f"Refusing to write document structure for {project!r} with no identity root."
            )
        if identity_generation is None:
            raise ValueError(
                f"Refusing to write document structure for {project!r} with no identity "
                "generation."
            )

        claim = IdentityClaim(
            project_id=str(project_id),
            root_key=root_key_for(str(identity_root)),
            generation=int(identity_generation),
            host=binding_host(),
        )
        handle = admit_structure_writer(self.neo4j, label=project, claim=claim)
        try:
            self._structure.write_document(
                file_path,
                content,
                project=project,
                structure_path=structure_path,
                structure_project_id=str(project_id),
                session_id=session_id,
                user_id=user_id,
                document_type=document_type,
            )
        finally:
            release_structure_writer(self.neo4j, handle)

    def get_scan_fingerprint(self, project_name: str) -> str | None:
        return self._structure.get_scan_fingerprint(project_name)

    def get_project_root_path(self, project_name: str) -> str | None:
        return self._structure.get_project_root_path(project_name)

    def query_structure(self, project: str, query_type: str, **kwargs: Any) -> Any:
        if query_type not in self.STRUCTURE_QUERY_TYPES:
            # Same message and type as before: an unknown type was already a ValueError, and a
            # now-refused-but-existing method must be indistinguishable from a typo, or the error
            # itself enumerates the private surface.
            raise ValueError(f"Unknown structure query type: {query_type}")
        method = getattr(self._structure, f"query_{query_type}", None)
        if method is None:
            raise ValueError(f"Unknown structure query type: {query_type}")
        return method(project, **kwargs)

    def query_documents(
        self, project: str, path_filter: str = "", document_type: str | None = None
    ) -> list[dict[str, str]]:
        """List document entities for a project."""
        return self._structure.query_documents(project, path_filter, document_type)

    def link_episode_to_documents(
        self,
        episode_uuid: str,
        entity_names: list[str],
        project: str,
        max_links: int = 5,
    ) -> int:
        """Link an episode to wiki/reference documents by name match."""
        return self._structure.link_episode_to_documents(
            episode_uuid, entity_names, project, max_links
        )

    def get_linked_documents(self, episode_uuids: list[str]) -> list[dict[str, str]]:
        """Get documents linked to episodes via RELATES_TO."""
        return self._structure.get_linked_documents(episode_uuids)

    def list_structure_projects(self) -> list[dict[str, str]]:
        return self._structure.list_projects()

    def list_orphan_structure_projects(self) -> list[dict[str, Any]]:
        return self._structure.list_orphan_structure_projects()
