"""Open questions and shape validation for :class:`WorkArtifactRepository`.
Methods moved verbatim from work_artifact_repository.py."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from menhir.domain.artifact_shape import ShapeReport, ShapeStatus, validate_shape
from menhir.domain.namespace import normalize_namespace
from menhir.domain.work_artifact import (
    ANSWERS_QUESTION_EDGE,
    ARTIFACT_SCHEMA_VERSION,
    DEFAULT_ARTIFACT_NAMESPACE,
    QuestionStatus,
    question_statuses_allowing,
)


class WorkArtifactQuestionsMixin:
    """See module docstring; combined into WorkArtifactRepository by the facade."""

    # ------------------------------------------------------------------
    # Open questions
    # ------------------------------------------------------------------

    def add_open_questions(
        self, artifact_uuid: str, texts: list[str]
    ) -> list[dict[str, Any]]:
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
            rows.append(
                {
                    "question_uuid": str(uuid4()),
                    "ordinal": ordinal,
                    "text": text.strip(),
                    "raw_segment": text,
                    "status": QuestionStatus.OPEN,
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                }
            )
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

    def answer_question(
        self, question_uuid: str, answering_artifact_uuid: str
    ) -> dict[str, Any]:
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
            WHERE q.status IN $answerable
            MATCH (a:WorkArtifact {{artifact_uuid: $answering_uuid}})
            MERGE (a)-[:{ANSWERS_QUESTION_EDGE}]->(q)
            SET q.status = $answered, q.answered_at = $now
            RETURN count(q) AS applied
            """,
            {
                "question_uuid": question_uuid,
                "answering_uuid": answering_artifact_uuid,
                # CF-48: which statuses may be answered is the domain's question, not this
                # statement's. The compare-and-set stays here because the guard and the edge
                # must land together; the RULE does not.
                "answerable": sorted(question_statuses_allowing(QuestionStatus.ANSWERED)),
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
            WHERE q.status IN $deferrable
            SET q.status = $deferred, q.deferred_at = $now
            RETURN count(q) AS applied
            """,
            {
                "question_uuid": question_uuid,
                # CF-48: see `answer_question`.
                "deferrable": sorted(question_statuses_allowing(QuestionStatus.DEFERRED)),
                "deferred": QuestionStatus.DEFERRED,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
        if rows and int(rows[0].get("applied", 0)) > 0:
            return {"applied": True, "status": QuestionStatus.DEFERRED}
        return {"applied": False, "reason": "question_not_open"}

    def open_questions(
        self,
        *,
        artifact_uuid: str | None = None,
        status: str | None = QuestionStatus.OPEN,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Questions across artifacts, or within one.

        Answers "what design questions remain?" and "which plans are blocked?"
        without parsing markdown. Namespace is read off the owning artifact --
        questions carry no copy of it.
        """
        namespaces = (
            [normalize_namespace(namespace), DEFAULT_ARTIFACT_NAMESPACE]
            if namespace
            else None
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
            [normalize_namespace(namespace), DEFAULT_ARTIFACT_NAMESPACE]
            if namespace
            else None
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
