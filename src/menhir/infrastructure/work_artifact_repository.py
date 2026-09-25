"""WorkArtifactRepository — direct Neo4j reads/writes for :WorkArtifact nodes.

Like :Todo, these bypass the Graphiti enrichment pipeline and are managed
directly via Cypher. Unlike :Todo, a :WorkArtifact is a *semantic* object: it
carries meaning and is referenced by other semantic objects.

Graph shape:
  (:WorkArtifact)-[:EMBODIED_IN]->(:ArtifactSource)     — where the doc itself lives
  (:WorkArtifact)-[:HAS_LOCATION]->(:ArtifactLocation)  — code the doc discusses

There is deliberately NO location->file edge. `(:ArtifactLocation)-[:RESOLVES_TO]->(:Entity)` was
written on every create and read by nothing -- removed in CF-143. Consumers reach files through
`:HAS_LOCATION` and match on `l.path` / `l.project`, which is what `structure_queries` already
does; that is also why it resolves todos the old edge missed.

EMBODIED_IN and HAS_LOCATION answer different questions and must never be
merged: the first is what the artifact IS, the second is what it talks about.

Both subordinates are Owned Records (`model.owned_record`): own label, never
:Entity, no namespace copy, per-record resolution, deleted with the owner.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from menhir.domain.artifact_shape import ShapeReport, ShapeStatus, validate_shape
from menhir.domain.namespace import normalize_namespace
from menhir.domain.todo_location import parse_code_ref
from menhir.infrastructure.paths import default_workspace_marker
from menhir.domain.artifact_reconciliation import (
    ARTIFACT_SOURCE_SCHEMA_VERSION,
    ArtifactSourceSnapshot,
    INTEGRITY_ALGORITHM,
    ResolutionStatus,
    SourceObservation,
    VersionKind,
    WorkArtifactIdentitySnapshot,
    locator_key,
)
from menhir.domain.work_artifact import (
    ABOUT_EDGE,
    ANSWERS_QUESTION_EDGE,
    ARTIFACT_MEDIA,
    ARTIFACT_RELATIONS,
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_TYPES,
    DEFAULT_ARTIFACT_NAMESPACE,
    INITIAL_STATUS,
    NAMESPACE_COMPATIBILITY_PARAMS,
    REFERENCES_TODO_EDGE,
    SUPERSEDES_EDGE,
    SUPERSESSION_PARAMS,
    TERMINAL_ANY,
    ArtifactMedium,
    ArtifactSourceSpec,
    ArtifactStatus,
    DeclarationKind,
    DeclarationStatus,
    QuestionStatus,
    can_transition,
    legal_next_statuses,
    namespace_compatibility_cypher,
    namespaces_are_compatible,
    question_statuses_allowing,
    require_known_medium,
    resolve_registration,
    supersession_cypher,
    normalize_declarations,
    relation_is_legal,
    valid_statuses,
)
from menhir.infrastructure.schema import (
    ARTIFACT_RECONCILIATION_REQUIRED_CONSTRAINTS,
    get_artifact_reconciliation_schema_queries,
)


from menhir.infrastructure.work_artifact_repository_support import (
    _Neo4jConstraintError,
    _safe_namespace_filter,
)
from menhir.infrastructure.work_artifact_repository_declarations import WorkArtifactDeclarationsMixin
from menhir.infrastructure.work_artifact_repository_questions import WorkArtifactQuestionsMixin
from menhir.infrastructure.work_artifact_repository_reads import WorkArtifactReadsMixin
from menhir.infrastructure.work_artifact_repository_reconciliation import WorkArtifactReconciliationMixin
from menhir.infrastructure.work_artifact_repository_register import WorkArtifactRegistrationMixin
from menhir.infrastructure.work_artifact_repository_relations import WorkArtifactRelationsMixin
from menhir.infrastructure.work_artifact_repository_source_ops import WorkArtifactSourceOpsMixin


# Facade module: the remaining method groups live in the sibling
# `work_artifact_repository_*.py` mixin modules imported above and are combined
# into this class. Kept in this module on purpose: `create_artifact`,
# `_write_source` and `supersede_artifact` are source-text-pinned by
# tests/test_high_wave8_domain_authority.py; `transition_status` holds the
# hand-written tenancy predicate ratcheted by
# tests/test_cf127_tenancy_scope_predicate.py; `_known_projects` and
# `_write_locations` are create_artifact's helpers.
class WorkArtifactRepository(
    WorkArtifactReconciliationMixin,
    WorkArtifactSourceOpsMixin,
    WorkArtifactRegistrationMixin,
    WorkArtifactRelationsMixin,
    WorkArtifactQuestionsMixin,
    WorkArtifactDeclarationsMixin,
    WorkArtifactReadsMixin,
):
    """Direct Neo4j CRUD for :WorkArtifact nodes and their owned subordinates."""

    neo4j: Any  # Neo4jRepository

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j
        self._known_projects_cache: frozenset[str] | None = None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_artifact(
        self,
        *,
        artifact_type: str,
        title: str,
        source: ArtifactSourceSpec | None = None,
        code_refs: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        status_raw: str | None = None,
        status_unresolved_reason: str | None = None,
        document: str | None = None,
        structure_project: str | None = None,
        artifact_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Create a :WorkArtifact with its embodiment and any code references.

        ``status`` defaults to the type's initial state. An explicit status is
        accepted (migration needs to land an artifact already IMPLEMENTED) but
        must be legal for the type -- an illegal one is refused rather than
        stored, since a bad state would then be transitioned *from*.

        ``status_raw`` keeps whatever the document actually said, and
        ``status_unresolved_reason`` records why it could not be mapped. The
        same discipline as a declaration: an artifact sitting in its initial
        state because nobody could read its header is distinguishable from one
        that genuinely is in that state, which makes the gap findable instead of
        invisible.
        """
        # CF-48: the domain decides registration legality, matching how `can_transition` is
        # already delegated a few methods down. The repository gathers and writes; it does not rule.
        resolved_status = resolve_registration(artifact_type, status)

        # A caller may supply the UUID the document already declares, so a
        # clean-clone registration reuses the author's identity instead of
        # minting a second one for the same record.
        artifact_uuid = artifact_uuid or str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        safe_namespace = normalize_namespace(namespace)

        self.neo4j.execute(
            """
            CREATE (a:WorkArtifact {
                artifact_uuid:     $uuid,
                artifact_type:     $artifact_type,
                title:             $title,
                status:            $status,
                status_raw:        $status_raw,
                status_unresolved_reason: $status_unresolved_reason,
                namespace:         $namespace,
                created_at:        $now,
                updated_at:        $now,
                status_changed_at: $now,
                schema_version:    $schema_version
            })
            """,
            {
                "uuid": artifact_uuid,
                "artifact_type": artifact_type,
                "title": title,
                "status": resolved_status,
                "status_raw": status_raw,
                "status_unresolved_reason": status_unresolved_reason,
                "namespace": safe_namespace,
                "now": now,
                "schema_version": ARTIFACT_SCHEMA_VERSION,
            },
        )

        embodiment = self._write_source(artifact_uuid, source)
        locations = self._write_locations(artifact_uuid, code_refs, structure_project)

        # Ingest validates when it was given the bytes. Passing no document
        # leaves shape_status absent rather than 'conforming', so "not checked"
        # never masquerades as "checked and fine".
        shape = (
            self.record_shape(artifact_uuid, artifact_type, document)
            if document is not None
            else None
        )

        return {
            "artifact_uuid": artifact_uuid,
            "artifact_type": artifact_type,
            "title": title,
            "status": resolved_status,
            "namespace": safe_namespace,
            "created_at": now,
            "embodiment": embodiment,
            "locations": locations,
            "shape": shape.as_properties() if shape else None,
        }

    def _write_source(
        self, artifact_uuid: str, source: ArtifactSourceSpec | None
    ) -> dict[str, Any] | None:
        """Attach one embodiment. Several may be attached over time (md, pdf)."""
        if source is None:
            return None
        require_known_medium(source.medium)  # CF-48

        props = source.as_properties()
        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            CREATE (a)-[:EMBODIED_IN]->(s:ArtifactSource)
            SET s += $props, s.first_seen_at = $now, s.last_seen_at = $now
            """,
            {
                "uuid": artifact_uuid,
                "props": props,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
        return props

    def _known_projects(self) -> frozenset[str]:
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
        self, artifact_uuid: str, code_refs: str | None, structure_project: str | None
    ) -> list[dict[str, Any]]:
        """Normalize code references the artifact discusses into :ArtifactLocation.

        Reuses ``parse_code_ref`` rather than reimplementing it -- the same
        normalizer that backs :TodoLocation, so multi-path declarations, line
        ranges, symbols and unresolvable prose all behave identically here.
        """
        if not code_refs:
            return []

        locations = parse_code_ref(
            code_refs,
            structure_project=structure_project,
            known_projects=self._known_projects(),
            workspace_marker=default_workspace_marker(),
        )
        if not locations:
            return []

        rows = [loc.as_properties() for loc in locations]
        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            UNWIND $rows AS row
            CREATE (a)-[:HAS_LOCATION]->(l:ArtifactLocation)
            SET l += row
            """,
            {"uuid": artifact_uuid, "rows": rows},
        )
        return rows

    def transition_status(
        self, artifact_uuid: str, to_status: str, *, namespace: str | None = None
    ) -> dict[str, Any]:
        """Move an artifact to a new status, if the transition is legal.

        Legality is decided in the domain against the artifact's *current*
        stored status, so the check cannot be bypassed by a caller asserting
        what it thinks the current state is. Refusals report a reason rather
        than raising -- an illegal transition is a caller mistake to surface,
        not a crash.
        """
        # Scoped EXACTLY, not requested-plus-default. The read idiom elsewhere in this file
        # widens to the default namespace as a convenience; this is a lifecycle MUTATION, so a
        # caller scoped to one silo must not move an artifact in the shared bucket.
        scoped = bool((namespace or "").strip())
        ns_filter = "AND a.namespace = $namespace" if scoped else ""
        params: dict[str, Any] = {"uuid": artifact_uuid}
        if scoped:
            params["namespace"] = normalize_namespace(namespace)

        rows = self.neo4j.execute(
            f"""
            MATCH (a:WorkArtifact {{artifact_uuid: $uuid}})
            WHERE true {ns_filter}
            RETURN a.artifact_type AS artifact_type, a.status AS status
            """,
            params,
        )
        if not rows:
            return {"applied": False, "reason": "artifact_not_found"}

        artifact_type = rows[0].get("artifact_type")
        from_status = rows[0].get("status")
        if not can_transition(artifact_type, from_status, to_status):
            return {
                "applied": False,
                "reason": "illegal_transition",
                "from_status": from_status,
                "to_status": to_status,
                "artifact_type": artifact_type,
                # Naming what IS legal, so a refusal does not send the caller
                # probing states one at a time.
                "valid_transitions": sorted(
                    legal_next_statuses(artifact_type, from_status)
                ),
            }

        now = datetime.now(timezone.utc).isoformat()
        # The predicate is REPEATED inside the mutation, not just in the legality read above.
        # Scoping only the preflight leaves the read-to-write window unguarded; the merge path
        # in correlation_queries closes the same race the same way.
        write_params: dict[str, Any] = {
            "uuid": artifact_uuid, "to_status": to_status, "now": now
        }
        if scoped:
            write_params["namespace"] = normalize_namespace(namespace)
        self.neo4j.execute(
            f"""
            MATCH (a:WorkArtifact {{artifact_uuid: $uuid}})
            WHERE true {ns_filter}
            SET a.status = $to_status, a.status_changed_at = $now, a.updated_at = $now
            """,
            write_params,
        )
        return {"applied": True, "from_status": from_status, "to_status": to_status}

    # Declared-relationships group lives in work_artifact_repository_relations;
    # this method stays in the facade (see module-top note).
    def supersede_artifact(self, new_uuid: str, old_uuid: str) -> dict[str, Any]:
        """Supersede one artifact with another, atomically.

        One Cypher statement, for the same reason ``resolve_todo`` is: the edge
        and the status move together or neither does. Two calls would allow a
        SUPERSEDES edge pointing at an artifact still marked APPROVED, or a
        SUPERSEDED artifact with no record of what replaced it.

        This is the only path that creates SUPERSEDES; ``link_artifacts``
        refuses the relation so an edge can never imply a status change it did
        not make. Same-type only, and an already-superseded artifact is refused
        rather than re-superseded, so the recorded replacement stays the one
        that applied.
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            f"""
            MATCH (new:WorkArtifact {{artifact_uuid: $new_uuid}})
            MATCH (old:WorkArtifact {{artifact_uuid: $old_uuid}})
            WHERE """
            + supersession_cypher()
            + f"""
            MERGE (new)-[:{SUPERSEDES_EDGE}]->(old)
            SET old.status = $superseded, old.status_changed_at = $now, old.updated_at = $now
            RETURN count(old) AS applied
            """,
            {
                "new_uuid": new_uuid,
                "old_uuid": old_uuid,
                # CF-48: bound by the domain's own `supersession_cypher`, from `TERMINAL_ANY`.
                **SUPERSESSION_PARAMS,
                "superseded": ArtifactStatus.SUPERSEDED,
                "default_ns": DEFAULT_ARTIFACT_NAMESPACE,
                "now": now,
            },
        )
        if rows and int(rows[0].get("applied", 0)) > 0:
            return {"applied": True, "status": ArtifactStatus.SUPERSEDED}
        return {
            "applied": False,
            "reason": "type_mismatch_already_terminal_or_namespace_incompatible",
        }
