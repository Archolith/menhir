"""WorkArtifactRepository — direct Neo4j reads/writes for :WorkArtifact nodes.

Like :Todo, these bypass the Graphiti enrichment pipeline and are managed
directly via Cypher. Unlike :Todo, a :WorkArtifact is a *semantic* object: it
carries meaning and is referenced by other semantic objects.

Graph shape:
  (:WorkArtifact)-[:EMBODIED_IN]->(:ArtifactSource)     — where the doc itself lives
  (:WorkArtifact)-[:HAS_LOCATION]->(:ArtifactLocation)  — code the doc discusses
  (:ArtifactLocation)-[:RESOLVES_TO]->(:Entity)         — structural file entity

EMBODIED_IN and HAS_LOCATION answer different questions and must never be
merged: the first is what the artifact IS, the second is what it talks about.

Both subordinates are Owned Records (`model.owned_record`): own label, never
:Entity, no namespace copy, per-record resolution, deleted with the owner.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from menhir.domain.artifact_shape import ShapeReport, ShapeStatus, validate_shape
from menhir.domain.todo_location import parse_code_ref
from menhir.domain.work_artifact import (
    ABOUT_EDGE,
    ANSWERS_QUESTION_EDGE,
    ARTIFACT_MEDIA,
    ARTIFACT_RELATIONS,
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_TYPES,
    DEFAULT_ARTIFACT_NAMESPACE,
    INITIAL_STATUS,
    REFERENCES_TODO_EDGE,
    SUPERSEDES_EDGE,
    TERMINAL_ANY,
    ArtifactSourceSpec,
    ArtifactStatus,
    DeclarationKind,
    DeclarationStatus,
    QuestionStatus,
    can_transition,
    legal_next_statuses,
    normalize_declarations,
    relation_is_legal,
    valid_statuses,
)


def _safe_namespace(namespace: str | None) -> str:
    return (namespace or "").strip() or DEFAULT_ARTIFACT_NAMESPACE


class WorkArtifactRepository:
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
        if artifact_type not in ARTIFACT_TYPES:
            raise ValueError(f"unknown artifact_type: {artifact_type!r}")

        resolved_status = status or INITIAL_STATUS[artifact_type]
        if resolved_status not in valid_statuses(artifact_type):
            raise ValueError(
                f"status {resolved_status!r} is not valid for {artifact_type!r}"
            )

        artifact_uuid = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        safe_namespace = _safe_namespace(namespace)

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
        if source.medium not in ARTIFACT_MEDIA:
            raise ValueError(f"unknown medium: {source.medium!r}")

        props = source.as_properties()
        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            CREATE (a)-[:EMBODIED_IN]->(s:ArtifactSource)
            SET s += $props, s.first_seen_at = $now, s.last_seen_at = $now
            """,
            {"uuid": artifact_uuid, "props": props, "now": datetime.now(timezone.utc).isoformat()},
        )
        return props

    def relocate_source(
        self, artifact_uuid: str, medium: str, locator: dict[str, str], version: str | None = None
    ) -> bool:
        """Update an embodiment's locator after a rename, move, or archive.

        This is an *update*, never a new embodiment: identity and embodiment are
        unchanged, only the locator moved (`model.embodiment_invariant`). Every
        relationship pointing at the artifact survives, which is the entire
        reason identity is not the path.
        """
        props = {f"locator_{k}": v for k, v in (locator or {}).items()}
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[:EMBODIED_IN]->(s:ArtifactSource)
            WHERE s.medium = $medium
            SET s += $props, s.last_seen_at = $now,
                s.version = coalesce($version, s.version)
            RETURN count(s) AS updated
            """,
            {
                "uuid": artifact_uuid,
                "medium": medium,
                "props": props,
                "version": version,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

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
        self._link_location_files(artifact_uuid)
        return rows

    def _link_location_files(self, artifact_uuid: str) -> int:
        """Resolve each location to a structural file entity where one exists."""
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[:HAS_LOCATION]->(l:ArtifactLocation)
            WHERE l.resolution_status = 'resolved' AND l.path IS NOT NULL
            MATCH (f:Entity)
            WHERE f.structure_role IN ['file', 'entrypoint', 'config', 'test']
              AND (
                    f.structure_path = l.path
                    OR (NOT l.path CONTAINS '/' AND f.structure_path ENDS WITH ('/' + l.path))
                  )
              AND (l.project IS NULL OR f.structure_project = l.project)
            MERGE (l)-[:RESOLVES_TO]->(f)
            RETURN count(*) AS linked
            """,
            {"uuid": artifact_uuid},
        )
        return int(rows[0].get("linked", 0)) if rows else 0

    def transition_status(self, artifact_uuid: str, to_status: str) -> dict[str, Any]:
        """Move an artifact to a new status, if the transition is legal.

        Legality is decided in the domain against the artifact's *current*
        stored status, so the check cannot be bypassed by a caller asserting
        what it thinks the current state is. Refusals report a reason rather
        than raising -- an illegal transition is a caller mistake to surface,
        not a crash.
        """
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            RETURN a.artifact_type AS artifact_type, a.status AS status
            """,
            {"uuid": artifact_uuid},
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
        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            SET a.status = $to_status, a.status_changed_at = $now, a.updated_at = $now
            """,
            {"uuid": artifact_uuid, "to_status": to_status, "now": now},
        )
        return {"applied": True, "from_status": from_status, "to_status": to_status}

    # ------------------------------------------------------------------
    # Declared relationships
    # ------------------------------------------------------------------

    def link_artifacts(
        self, source_uuid: str, target_uuid: str, relation: str
    ) -> dict[str, Any]:
        """Declare an artifact-to-artifact relationship.

        Never inferred. Type legality is decided against the *stored* types of
        both artifacts, so a caller cannot assert its way past the constraint.
        Refusals return a reason rather than raising -- a rejected declaration
        is a data-quality signal about the caller's input.
        """
        if relation not in ARTIFACT_RELATIONS:
            return {"linked": False, "reason": "unsupported_relation", "relation": relation}

        rows = self.neo4j.execute(
            """
            MATCH (s:WorkArtifact {artifact_uuid: $source_uuid})
            MATCH (t:WorkArtifact {artifact_uuid: $target_uuid})
            RETURN s.artifact_type AS source_type, t.artifact_type AS target_type,
                   s.namespace AS source_ns, t.namespace AS target_ns
            """,
            {"source_uuid": source_uuid, "target_uuid": target_uuid},
        )
        if not rows:
            return {"linked": False, "reason": "artifact_not_found", "relation": relation}

        row = rows[0]
        ok, reason = relation_is_legal(relation, row["source_type"], row["target_type"])
        if not ok:
            return {"linked": False, "reason": reason, "relation": relation}

        if row["target_ns"] not in (row["source_ns"], DEFAULT_ARTIFACT_NAMESPACE):
            return {"linked": False, "reason": "namespace_incompatible", "relation": relation}

        edge_type = ARTIFACT_RELATIONS[relation][0]
        self.neo4j.execute(
            f"""
            MATCH (s:WorkArtifact {{artifact_uuid: $source_uuid}})
            MATCH (t:WorkArtifact {{artifact_uuid: $target_uuid}})
            MERGE (s)-[:{edge_type}]->(t)
            """,
            {"source_uuid": source_uuid, "target_uuid": target_uuid},
        )
        return {"linked": True, "relation": relation, "edge_type": edge_type}

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
            WHERE new.artifact_type = old.artifact_type
              AND new.artifact_uuid <> old.artifact_uuid
              AND NOT old.status IN $terminal
              AND old.namespace IN [new.namespace, $default_ns]
            MERGE (new)-[:{SUPERSEDES_EDGE}]->(old)
            SET old.status = $superseded, old.status_changed_at = $now, old.updated_at = $now
            RETURN count(old) AS applied
            """,
            {
                "new_uuid": new_uuid,
                "old_uuid": old_uuid,
                "terminal": sorted(TERMINAL_ANY),
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

    def link_subject(self, artifact_uuid: str, entity_uuid: str) -> dict[str, Any]:
        """Declare what an artifact is ABOUT.

        Targets an existing durable semantic :Entity rather than a bespoke
        subject label: "OAuth" already exists as one, and a second identity for
        the same concept is exactly the duplication the modeling primitives
        forbid. Structural nodes are ineligible -- a file is not a subject.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (a:WorkArtifact {{artifact_uuid: $artifact_uuid}})
            MATCH (e:Entity {{uuid: $entity_uuid}})
            WHERE e.scope = 'PERSISTENT' AND e.structure_role IS NULL
            MERGE (a)-[:{ABOUT_EDGE}]->(e)
            RETURN count(e) AS linked
            """,
            {"artifact_uuid": artifact_uuid, "entity_uuid": entity_uuid},
        )
        if rows and int(rows[0].get("linked", 0)) > 0:
            return {"linked": True, "edge_type": ABOUT_EDGE}
        return {"linked": False, "reason": "entity_ineligible_or_not_found"}

    def link_todo(self, artifact_uuid: str, todo_uuid: str) -> dict[str, Any]:
        """Declare that an artifact references an operational todo.

        Named distinctly from a generic REFERENCES because a graph accumulating
        several referent classes should not overload one edge type, and
        traversals are type-specific.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (a:WorkArtifact {{artifact_uuid: $artifact_uuid}})
            MATCH (t:Todo {{uuid: $todo_uuid}})
            WHERE t.namespace IN [a.namespace, $default_ns]
            MERGE (a)-[:{REFERENCES_TODO_EDGE}]->(t)
            RETURN count(t) AS linked
            """,
            {
                "artifact_uuid": artifact_uuid,
                "todo_uuid": todo_uuid,
                "default_ns": DEFAULT_ARTIFACT_NAMESPACE,
            },
        )
        if rows and int(rows[0].get("linked", 0)) > 0:
            return {"linked": True, "edge_type": REFERENCES_TODO_EDGE}
        return {"linked": False, "reason": "todo_not_found_or_namespace_incompatible"}

    def artifact_relationships(self, artifact_uuid: str) -> dict[str, list[dict[str, Any]]]:
        """Declared relationships in both directions, plus subjects and todos."""
        edge_types = [spec[0] for spec in ARTIFACT_RELATIONS.values()] + [SUPERSEDES_EDGE]
        outgoing = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[r]->(t:WorkArtifact)
            WHERE type(r) IN $edge_types
            RETURN type(r) AS relation, t.artifact_uuid AS target_uuid,
                   t.title AS target_title, t.artifact_type AS target_type
            """,
            {"uuid": artifact_uuid, "edge_types": edge_types},
        )
        incoming = self.neo4j.execute(
            """
            MATCH (s:WorkArtifact)-[r]->(a:WorkArtifact {artifact_uuid: $uuid})
            WHERE type(r) IN $edge_types
            RETURN type(r) AS relation, s.artifact_uuid AS source_uuid,
                   s.title AS source_title, s.artifact_type AS source_type
            """,
            {"uuid": artifact_uuid, "edge_types": edge_types},
        )
        subjects = self.neo4j.execute(
            f"""
            MATCH (a:WorkArtifact {{artifact_uuid: $uuid}})-[:{ABOUT_EDGE}]->(e:Entity)
            RETURN e.uuid AS entity_uuid, e.name AS name
            """,
            {"uuid": artifact_uuid},
        )
        todos = self.neo4j.execute(
            f"""
            MATCH (a:WorkArtifact {{artifact_uuid: $uuid}})-[:{REFERENCES_TODO_EDGE}]->(t:Todo)
            RETURN t.uuid AS todo_uuid, t.status AS status
            """,
            {"uuid": artifact_uuid},
        )
        return {
            "outgoing": [r for r in outgoing if r.get("relation")],
            "incoming": incoming,
            "subjects": subjects,
            "todos": todos,
        }

    # ------------------------------------------------------------------
    # Open questions
    # ------------------------------------------------------------------

    def add_open_questions(self, artifact_uuid: str, texts: list[str]) -> list[dict[str, Any]]:
        """Attach declared open questions, preserving the author's ordering.

        Every artifact in this corpus ends with an "Open Questions" list. As
        prose it is re-parsed by every reader; as owned records it answers
        "what design questions remain?" without reading markdown.

        Each question gets a uuid so it can be *addressed* -- a review must be
        able to say which question it answered. That does not promote it to a
        semantic object: it still never recalls and still dies with its artifact.
        """
        rows: list[dict[str, Any]] = []
        for ordinal, text in enumerate(t for t in texts if t and t.strip()):
            rows.append({
                "question_uuid": str(uuid4()),
                "ordinal": ordinal,
                "text": text.strip(),
                "raw_segment": text,
                "status": QuestionStatus.OPEN,
                "schema_version": ARTIFACT_SCHEMA_VERSION,
            })
        if not rows:
            return []

        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            UNWIND $rows AS row
            CREATE (a)-[:HAS_OPEN_QUESTION]->(q:OpenQuestion)
            SET q += row
            """,
            {"uuid": artifact_uuid, "rows": rows},
        )
        return rows

    def answer_question(self, question_uuid: str, answering_artifact_uuid: str) -> dict[str, Any]:
        """Mark a question answered and record what answered it, atomically.

        One statement, for the reason ``resolve_todo`` is: an answered question
        with no answering artifact is a claim without evidence, and a dangling
        ANSWERS_QUESTION edge beside a still-open question is equally wrong.

        Only an open question may be answered -- re-answering would overwrite
        which artifact actually resolved it.
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            f"""
            MATCH (q:OpenQuestion {{question_uuid: $question_uuid}})
            WHERE q.status = $open
            MATCH (a:WorkArtifact {{artifact_uuid: $answering_uuid}})
            MERGE (a)-[:{ANSWERS_QUESTION_EDGE}]->(q)
            SET q.status = $answered, q.answered_at = $now
            RETURN count(q) AS applied
            """,
            {
                "question_uuid": question_uuid,
                "answering_uuid": answering_artifact_uuid,
                "open": QuestionStatus.OPEN,
                "answered": QuestionStatus.ANSWERED,
                "now": now,
            },
        )
        if rows and int(rows[0].get("applied", 0)) > 0:
            return {"applied": True, "status": QuestionStatus.ANSWERED}
        return {"applied": False, "reason": "question_not_open_or_artifact_missing"}

    def defer_question(self, question_uuid: str) -> dict[str, Any]:
        """Mark a question deliberately deferred.

        Needs no answering artifact: deferring is a decision, not an answer, so
        requiring evidence would be requiring evidence of a non-event.
        """
        rows = self.neo4j.execute(
            """
            MATCH (q:OpenQuestion {question_uuid: $question_uuid})
            WHERE q.status = $open
            SET q.status = $deferred, q.deferred_at = $now
            RETURN count(q) AS applied
            """,
            {
                "question_uuid": question_uuid,
                "open": QuestionStatus.OPEN,
                "deferred": QuestionStatus.DEFERRED,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
        if rows and int(rows[0].get("applied", 0)) > 0:
            return {"applied": True, "status": QuestionStatus.DEFERRED}
        return {"applied": False, "reason": "question_not_open"}

    def open_questions(
        self, *, artifact_uuid: str | None = None, status: str | None = QuestionStatus.OPEN,
        namespace: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Questions across artifacts, or within one.

        Answers "what design questions remain?" and "which plans are blocked?"
        without parsing markdown. Namespace is read off the owning artifact --
        questions carry no copy of it.
        """
        namespaces = (
            [_safe_namespace(namespace), DEFAULT_ARTIFACT_NAMESPACE] if namespace else None
        )
        return self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)-[:HAS_OPEN_QUESTION]->(q:OpenQuestion)
            WHERE ($artifact_uuid IS NULL OR a.artifact_uuid = $artifact_uuid)
              AND ($status IS NULL OR q.status = $status)
              AND ($namespaces IS NULL OR a.namespace IN $namespaces)
            OPTIONAL MATCH (answering:WorkArtifact)-[:ANSWERS_QUESTION]->(q)
            RETURN q.question_uuid AS question_uuid,
                   q.ordinal       AS ordinal,
                   q.text          AS text,
                   q.status        AS status,
                   a.artifact_uuid AS artifact_uuid,
                   a.title         AS artifact_title,
                   answering.artifact_uuid AS answered_by
            ORDER BY a.title ASC, q.ordinal ASC
            LIMIT $limit
            """,
            {
                "artifact_uuid": artifact_uuid,
                "status": status,
                "namespaces": namespaces,
                "limit": max(1, min(limit, 500)),
            },
        )

    # ------------------------------------------------------------------
    # Shape validation
    # ------------------------------------------------------------------

    def record_shape(
        self, artifact_uuid: str, artifact_type: str, document: str | None
    ) -> ShapeReport:
        """Validate a document against its type's shape and store the verdict.

        Called on ingest and on update, so the stored verdict always describes
        the document as it is now rather than as it was when first seen.

        A failing document is recorded, never rejected. Refusing the write
        would leave the graph unaware of a document that exists on disk, which
        is worse than knowing about it and knowing it is malformed -- the same
        reasoning that keeps an unresolved declaration instead of dropping it.
        """
        report = validate_shape(artifact_type, document)
        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            SET a += $props, a.shape_checked_at = $now, a.updated_at = $now
            """,
            {
                "uuid": artifact_uuid,
                "props": report.as_properties(),
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
        return report

    def nonconforming_artifacts(
        self, *, namespace: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Artifacts whose documents failed their shape contract.

        Separated from 'never checked': an artifact with no verdict is a gap in
        coverage, not a clean bill of health, and conflating them would let
        unchecked documents pass for valid ones.
        """
        namespaces = (
            [_safe_namespace(namespace), DEFAULT_ARTIFACT_NAMESPACE] if namespace else None
        )
        return self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)
            WHERE a.shape_status = $nonconforming
              AND ($namespaces IS NULL OR a.namespace IN $namespaces)
            RETURN a.artifact_uuid AS artifact_uuid, a.title AS title,
                   a.artifact_type AS artifact_type, a.namespace AS namespace,
                   a.shape_violations AS violations
            ORDER BY size(a.shape_violations) DESC
            LIMIT $limit
            """,
            {
                "nonconforming": ShapeStatus.NONCONFORMING,
                "namespaces": namespaces,
                "limit": max(1, min(limit, 500)),
            },
        )

    # ------------------------------------------------------------------
    # Declarations
    # ------------------------------------------------------------------

    def declare_from_frontmatter(
        self, artifact_uuid: str, declarations: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Record structured frontmatter declarations and try to honor them.

        The declaration is the durable record; the edge is a *consequence* of a
        successful resolution. Frontmatter names targets by human-readable name
        while identity is a uuid, so resolution can fail -- a renamed target, a
        typo, a plan not yet ingested. Every declaration is stored verbatim
        first and resolved second, so a failure leaves evidence instead of
        silently deleting a relationship the author asked for.

        Declarations only ever *add*. A target dropped from frontmatter does not
        retract an existing edge: removal is an explicit act, and a document
        losing a line should not quietly rewrite the graph. (This settles the
        design's open question 2 in the only direction that is safe to
        automate.)
        """
        references, ignored = normalize_declarations(declarations)
        if not references:
            return {"declared": 0, "ignored": ignored, "results": []}

        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "declaration_uuid": str(uuid4()),
                "key": ref.key,
                "kind": ref.kind,
                "raw_target": ref.raw_target,
                "ordinal": ref.ordinal,
                "declared_at": now,
                "schema_version": ARTIFACT_SCHEMA_VERSION,
            }
            for ref in references
        ]

        # MERGE on (owner, key, raw_target): re-ingesting unchanged frontmatter
        # must not accumulate duplicate declarations, and an existing record
        # keeps its uuid so anything referring to it stays valid.
        self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            UNWIND $rows AS row
            MERGE (a)-[:DECLARES]->(d:ArtifactDeclaration {
                key: row.key, raw_target: row.raw_target
            })
            ON CREATE SET d.declaration_uuid = row.declaration_uuid,
                          d.kind             = row.kind,
                          d.declared_at      = row.declared_at,
                          d.resolution_status = $unresolved,
                          d.unresolved_reason = 'not_yet_resolved',
                          d.schema_version   = row.schema_version
            SET d.ordinal = row.ordinal, d.last_seen_at = row.declared_at
            """,
            {
                "uuid": artifact_uuid,
                "rows": rows,
                "unresolved": DeclarationStatus.UNRESOLVED,
            },
        )

        results = self.resolve_declarations(artifact_uuid)
        return {"declared": len(rows), "ignored": ignored, "results": results}

    def resolve_declarations(self, artifact_uuid: str) -> list[dict[str, Any]]:
        """Attempt resolution for every unresolved declaration on an artifact.

        Re-runnable by design: a declaration naming a plan that had not been
        ingested yet resolves on a later pass without the author touching the
        document. Already-resolved declarations are left alone so a successful
        resolution is never re-litigated against a graph that has since changed.
        """
        pending = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[:DECLARES]->(d:ArtifactDeclaration)
            WHERE d.resolution_status <> $resolved
            RETURN d.declaration_uuid AS declaration_uuid, d.key AS key,
                   d.kind AS kind, d.raw_target AS raw_target
            ORDER BY d.ordinal ASC
            """,
            {"uuid": artifact_uuid, "resolved": DeclarationStatus.RESOLVED},
        )

        results: list[dict[str, Any]] = []
        for row in pending:
            outcome = self._resolve_one(artifact_uuid, row)
            self._stamp_declaration(row["declaration_uuid"], outcome)
            results.append({**outcome, "raw_target": row["raw_target"], "key": row["key"]})
        return results

    def _resolve_one(self, artifact_uuid: str, row: dict[str, Any]) -> dict[str, Any]:
        kind = row["kind"]
        raw_target = row["raw_target"]

        if kind == DeclarationKind.SUBJECT:
            return self._resolve_subject(artifact_uuid, raw_target)
        if kind == DeclarationKind.TODO:
            return self._resolve_todo(artifact_uuid, raw_target)

        target_uuid, reason = self._match_artifact(artifact_uuid, raw_target)
        if target_uuid is None:
            return {"status": DeclarationStatus.UNRESOLVED, "reason": reason}

        if kind == DeclarationKind.SUPERSESSION:
            # Routed through supersede_artifact, never link_artifacts: the
            # document declaring "supersedes X" is the authority for X being
            # superseded, and the edge and status must move together.
            applied = self.supersede_artifact(artifact_uuid, target_uuid)
            if applied.get("applied"):
                return {"status": DeclarationStatus.RESOLVED, "target_uuid": target_uuid}
            return {
                "status": DeclarationStatus.REJECTED,
                "reason": applied.get("reason"),
                "target_uuid": target_uuid,
            }

        linked = self.link_artifacts(artifact_uuid, target_uuid, row["key"])
        if linked.get("linked"):
            return {"status": DeclarationStatus.RESOLVED, "target_uuid": target_uuid}
        return {
            "status": DeclarationStatus.REJECTED,
            "reason": linked.get("reason"),
            "target_uuid": target_uuid,
        }

    def _match_artifact(
        self, artifact_uuid: str, raw_target: str
    ) -> tuple[str | None, str | None]:
        """Resolve a declared name to exactly one artifact uuid.

        Authors write either a title or the filename stem, so both are matched,
        case-insensitively. Ambiguity is a refusal rather than a first-match
        pick: choosing arbitrarily between two candidates would write a
        confident edge from an uncertain input, and the author is the only one
        who can say which was meant.
        """
        needle = raw_target.strip().lower()
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            MATCH (t:WorkArtifact)
            WHERE t.artifact_uuid <> $uuid
              AND t.namespace IN [a.namespace, $default_ns]
            OPTIONAL MATCH (t)-[:EMBODIED_IN]->(s:ArtifactSource)
            WITH t, collect(toLower(coalesce(s.locator_path, ''))) AS paths
            WHERE toLower(t.title) = $needle
               OR any(p IN paths WHERE p = $needle
                      OR p ENDS WITH ('/' + $needle + '.md')
                      OR p = ($needle + '.md'))
            RETURN DISTINCT t.artifact_uuid AS artifact_uuid
            LIMIT 5
            """,
            {"uuid": artifact_uuid, "needle": needle, "default_ns": DEFAULT_ARTIFACT_NAMESPACE},
        )
        if not rows:
            return None, "target_not_found"
        if len(rows) > 1:
            return None, "ambiguous_target"
        return rows[0]["artifact_uuid"], None

    def _resolve_subject(self, artifact_uuid: str, raw_target: str) -> dict[str, Any]:
        """Resolve an ``about:`` declaration to an existing semantic entity.

        Deliberately does not mint a missing entity, despite the design allowing
        it. An :Entity created here would carry no name embedding and would be
        silently invisible to recall -- a worse outcome than an honest
        unresolved declaration, because it looks like it worked. Entity creation
        belongs to the ingest path that stamps embeddings.
        """
        needle = raw_target.strip().lower()
        rows = self.neo4j.execute(
            """
            MATCH (e:Entity)
            WHERE toLower(e.name) = $needle
              AND e.scope = 'PERSISTENT'
              AND e.structure_role IS NULL
            RETURN e.uuid AS entity_uuid
            LIMIT 5
            """,
            {"needle": needle},
        )
        if not rows:
            return {"status": DeclarationStatus.UNRESOLVED, "reason": "subject_not_found"}
        if len(rows) > 1:
            return {"status": DeclarationStatus.UNRESOLVED, "reason": "ambiguous_subject"}

        entity_uuid = rows[0]["entity_uuid"]
        linked = self.link_subject(artifact_uuid, entity_uuid)
        if linked.get("linked"):
            return {"status": DeclarationStatus.RESOLVED, "target_uuid": entity_uuid}
        return {
            "status": DeclarationStatus.REJECTED,
            "reason": linked.get("reason"),
            "target_uuid": entity_uuid,
        }

    def _resolve_todo(self, artifact_uuid: str, raw_target: str) -> dict[str, Any]:
        """Resolve a ``todos:`` declaration.

        Todos have no titles, only content, so the declared value must be a uuid
        or an unambiguous uuid prefix -- the short form that already appears in
        every todo listing. Matching on content would be inference.
        """
        needle = raw_target.strip()
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            MATCH (t:Todo)
            WHERE (t.uuid = $needle OR t.uuid STARTS WITH $needle)
              AND t.namespace IN [a.namespace, $default_ns]
            RETURN t.uuid AS todo_uuid
            LIMIT 5
            """,
            {"uuid": artifact_uuid, "needle": needle, "default_ns": DEFAULT_ARTIFACT_NAMESPACE},
        )
        if not rows:
            return {"status": DeclarationStatus.UNRESOLVED, "reason": "todo_not_found"}
        if len(rows) > 1:
            return {"status": DeclarationStatus.UNRESOLVED, "reason": "ambiguous_todo"}

        todo_uuid = rows[0]["todo_uuid"]
        linked = self.link_todo(artifact_uuid, todo_uuid)
        if linked.get("linked"):
            return {"status": DeclarationStatus.RESOLVED, "target_uuid": todo_uuid}
        return {
            "status": DeclarationStatus.REJECTED,
            "reason": linked.get("reason"),
            "target_uuid": todo_uuid,
        }

    def _stamp_declaration(self, declaration_uuid: str, outcome: dict[str, Any]) -> None:
        self.neo4j.execute(
            """
            MATCH (d:ArtifactDeclaration {declaration_uuid: $uuid})
            SET d.resolution_status = $status,
                d.unresolved_reason = $reason,
                d.resolved_uuid     = $target_uuid,
                d.resolved_at       = $now
            """,
            {
                "uuid": declaration_uuid,
                "status": outcome["status"],
                "reason": outcome.get("reason"),
                "target_uuid": outcome.get("target_uuid"),
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )

    def declarations(self, artifact_uuid: str) -> list[dict[str, Any]]:
        """Every declaration on an artifact, resolved or not, in author order."""
        return self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})-[:DECLARES]->(d:ArtifactDeclaration)
            RETURN d.declaration_uuid  AS declaration_uuid,
                   d.key               AS key,
                   d.kind              AS kind,
                   d.raw_target        AS raw_target,
                   d.ordinal           AS ordinal,
                   d.resolution_status AS resolution_status,
                   d.unresolved_reason AS unresolved_reason,
                   d.resolved_uuid     AS resolved_uuid
            ORDER BY d.ordinal ASC
            """,
            {"uuid": artifact_uuid},
        )

    def delete_artifact(self, artifact_uuid: str) -> bool:
        """Hard-delete an artifact and every subordinate it owns."""
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            OPTIONAL MATCH (a)-[:EMBODIED_IN]->(s:ArtifactSource)
            OPTIONAL MATCH (a)-[:HAS_LOCATION]->(l:ArtifactLocation)
            OPTIONAL MATCH (a)-[:HAS_OPEN_QUESTION]->(q:OpenQuestion)
            OPTIONAL MATCH (a)-[:DECLARES]->(d:ArtifactDeclaration)
            WITH a, s, l, q, d, count(a) AS found
            DETACH DELETE a, s, l, q, d
            RETURN found
            """,
            {"uuid": artifact_uuid},
        )
        return bool(rows and int(rows[0].get("found", 0)) > 0)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_artifact(
        self, artifact_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch one artifact with its embodiments and locations."""
        namespaces = (
            [_safe_namespace(namespace), DEFAULT_ARTIFACT_NAMESPACE] if namespace else None
        )
        rows = self.neo4j.execute(
            """
            MATCH (a:WorkArtifact {artifact_uuid: $uuid})
            WHERE $namespaces IS NULL OR a.namespace IN $namespaces
            OPTIONAL MATCH (a)-[:EMBODIED_IN]->(s:ArtifactSource)
            WITH a, collect(s {.*}) AS embodiments
            OPTIONAL MATCH (a)-[:HAS_LOCATION]->(l:ArtifactLocation)
            WITH a, embodiments, l ORDER BY l.ordinal ASC
            RETURN
                a.artifact_uuid     AS artifact_uuid,
                a.artifact_type     AS artifact_type,
                a.title             AS title,
                a.status            AS status,
                a.namespace         AS namespace,
                a.created_at        AS created_at,
                a.updated_at        AS updated_at,
                a.status_changed_at AS status_changed_at,
                a.status_raw        AS status_raw,
                a.status_unresolved_reason AS status_unresolved_reason,
                a.shape_status      AS shape_status,
                a.shape_violations  AS shape_violations,
                a.shape_checked_at  AS shape_checked_at,
                embodiments         AS embodiments,
                collect(l {.project, .path, .kind, .line_start, .line_end,
                           .symbol, .ordinal, .resolution_status,
                           .unresolved_reason}) AS locations
            """,
            {"uuid": artifact_uuid, "namespaces": namespaces},
        )
        return rows[0] if rows else None

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List artifacts, newest first, with optional type/status/silo filters.

        ``namespace`` is opt-in and follows the :Todo rule: omitted spans every
        silo, supplied narrows to that silo plus the shared default bucket, so a
        pinned client sees shared artifacts rather than nothing.
        """
        namespaces = (
            [_safe_namespace(namespace), DEFAULT_ARTIFACT_NAMESPACE] if namespace else None
        )
        return self.neo4j.execute(
            """
            MATCH (a:WorkArtifact)
            WHERE ($artifact_type IS NULL OR a.artifact_type = $artifact_type)
              AND ($status IS NULL OR a.status = $status)
              AND ($namespaces IS NULL OR a.namespace IN $namespaces)
            RETURN
                a.artifact_uuid AS artifact_uuid,
                a.artifact_type AS artifact_type,
                a.title         AS title,
                a.status        AS status,
                a.namespace     AS namespace,
                a.updated_at    AS updated_at,
                a.status_raw    AS status_raw,
                a.status_unresolved_reason AS status_unresolved_reason
            ORDER BY a.updated_at DESC
            LIMIT $limit
            """,
            {
                "artifact_type": artifact_type,
                "status": status,
                "namespaces": namespaces,
                "limit": max(1, min(limit, 200)),
            },
        )
