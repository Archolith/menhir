"""Declared relationships for :class:`WorkArtifactRepository`: artifact,
entity and todo links plus relationship reads. Methods moved verbatim
from work_artifact_repository.py."""

from __future__ import annotations

from typing import Any

from menhir.domain.work_artifact import (
    ABOUT_EDGE,
    ARTIFACT_RELATIONS,
    NAMESPACE_COMPATIBILITY_PARAMS,
    REFERENCES_TODO_EDGE,
    SUPERSEDES_EDGE,
    namespace_compatibility_cypher,
    namespaces_are_compatible,
    relation_is_legal,
)


class WorkArtifactRelationsMixin:
    """See module docstring; combined into WorkArtifactRepository by the facade."""

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
            return {
                "linked": False,
                "reason": "unsupported_relation",
                "relation": relation,
            }

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
            return {
                "linked": False,
                "reason": "artifact_not_found",
                "relation": relation,
            }

        row = rows[0]
        ok, reason = relation_is_legal(relation, row["source_type"], row["target_type"])
        if not ok:
            return {"linked": False, "reason": reason, "relation": relation}

        # CF-48: the rule lives in the domain beside `supersession_cypher`, which enforces the
        # same namespace compatibility for the one relation this method refuses.
        if not namespaces_are_compatible(row["source_ns"], row["target_ns"]):
            return {
                "linked": False,
                "reason": "namespace_incompatible",
                "relation": relation,
            }

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
            WHERE {namespace_compatibility_cypher()}
            MERGE (a)-[:{REFERENCES_TODO_EDGE}]->(t)
            RETURN count(t) AS linked
            """,
            {
                "artifact_uuid": artifact_uuid,
                "todo_uuid": todo_uuid,
                # CF-48: bound by the domain's own `namespace_compatibility_cypher`.
                **NAMESPACE_COMPATIBILITY_PARAMS,
            },
        )
        if rows and int(rows[0].get("linked", 0)) > 0:
            return {"linked": True, "edge_type": REFERENCES_TODO_EDGE}
        return {"linked": False, "reason": "todo_not_found_or_namespace_incompatible"}

    def artifact_relationships(
        self, artifact_uuid: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Declared relationships in both directions, plus subjects and todos."""
        edge_types = [spec[0] for spec in ARTIFACT_RELATIONS.values()] + [
            SUPERSEDES_EDGE
        ]
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
