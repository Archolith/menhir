"""Episode post-ingest metadata stamping and usage tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.domain.namespace import stamped_namespace
from menhir.infrastructure.cypher import Cypher


@dataclass
class PolicyStampResult:
    """Counts for records updated during post-ingest policy stamping."""

    nodes_touched: int
    edges_touched: int


class EpisodeStampingRepository:
    """Post-ingest stamping and live processing metadata updates."""

    neo4j: Any

    def stamp_ingest_metadata(
        self,
        *,
        node_uuids: list[str],
        edge_uuids: list[str],
        session_id: str,
        user_id: str,
        source: str,
        source_confidence: float,
        namespace: str | None = None,
        bootstrap_scope: str | None = None,
        belief_commit: str | None = None,
        belief_branch: str | None = None,
    ) -> PolicyStampResult:
        ns = stamped_namespace(namespace)
        unique_node_uuids = list(dict.fromkeys(node_uuids))
        unique_edge_uuids = list(dict.fromkeys(edge_uuids))

        nodes_touched = 0
        edges_touched = 0

        if unique_node_uuids:
            episodic_sets = [
                "n.scope = 'SESSION'",
                "n.source = $source",
                "n.source_confidence = $source_confidence",
                "n.user_flagged = coalesce(n.user_flagged, false)",
                "n.bootstrap_scope = CASE WHEN $bootstrap_scope IS NULL"
                " THEN n.bootstrap_scope ELSE $bootstrap_scope END",
                "n.session_id = $session_id",
                "n.user_id = $user_id",
                "n.namespace = $namespace",
                "n.created_at = coalesce(n.created_at, datetime())",
                "n.last_accessed = coalesce(n.last_accessed, n.created_at)",
            ]
            episodic_params = {
                "uuids": unique_node_uuids,
                "source": source,
                "source_confidence": source_confidence,
                "session_id": session_id,
                "user_id": user_id,
                "namespace": ns,
                "bootstrap_scope": bootstrap_scope,
            }
            if belief_commit is not None:
                episodic_sets.extend([
                    "n.belief_commit = coalesce(n.belief_commit, $belief_commit)",
                    "n.belief_branch = coalesce(n.belief_branch, $belief_branch)",
                ])
                episodic_params["belief_commit"] = belief_commit
                episodic_params["belief_branch"] = belief_branch

            episodic_query = (
                Cypher()
                .match("(n:Episodic)")
                .where("n.uuid IN $uuids")
                .set(episodic_sets)
                .return_raw("count(DISTINCT n) AS nodes_touched")
                .build()
            )
            episodic_rows = self.neo4j.execute(episodic_query, params=episodic_params)
            if episodic_rows:
                nodes_touched += int(episodic_rows[0].get("nodes_touched", 0))

            entity_sets = [
                "n.scope = coalesce(n.scope, 'SESSION')",
                "n.source = CASE WHEN locked THEN n.source"
                " ELSE coalesce(n.source, $source) END",
                "n.source_confidence = CASE WHEN locked"
                " THEN toFloat(n.source_confidence)"
                " ELSE coalesce(toFloat(n.source_confidence), $source_confidence) END",
                "n.user_flagged = coalesce(n.user_flagged, false)",
                "n.session_id = CASE WHEN locked THEN n.session_id"
                " ELSE coalesce(n.session_id, $session_id) END",
                "n.user_id = CASE WHEN locked THEN n.user_id"
                " ELSE coalesce(n.user_id, $user_id) END",
                "n.namespace = CASE WHEN locked THEN n.namespace"
                " ELSE coalesce(n.namespace, $namespace) END",
                "n.created_at = coalesce(n.created_at, datetime())",
                "n.last_accessed = coalesce(n.last_accessed, n.created_at)",
            ]
            entity_params = {
                "uuids": unique_node_uuids,
                "source": source,
                "source_confidence": source_confidence,
                "session_id": session_id,
                "user_id": user_id,
                "namespace": ns,
            }
            if belief_commit is not None:
                entity_sets.extend([
                    "n.belief_commit = CASE WHEN locked THEN n.belief_commit ELSE coalesce(n.belief_commit, $belief_commit) END",
                    "n.belief_branch = CASE WHEN locked THEN n.belief_branch ELSE coalesce(n.belief_branch, $belief_branch) END",
                ])
                entity_params["belief_commit"] = belief_commit
                entity_params["belief_branch"] = belief_branch

            entity_query = (
                Cypher()
                .match("(n:Entity)")
                .where("n.uuid IN $uuids")
                .with_clause("n, n.scope IN ['PERSISTENT', 'PROMOTED'] AS locked")
                .set(entity_sets)
                .return_raw("count(DISTINCT n) AS nodes_touched")
                .build()
            )
            entity_rows = self.neo4j.execute(entity_query, params=entity_params)
            if entity_rows:
                nodes_touched += int(entity_rows[0].get("nodes_touched", 0))

        if unique_edge_uuids:
            edge_query = (
                Cypher()
                .match("()-[r]-()")
                .where("r.uuid IN $uuids")
                .set(
                    (
                        "r.scope = coalesce(r.scope, 'SESSION')",
                        "r.weight = coalesce(toFloat(r.weight), 1.0)",
                        "r.source = coalesce(r.source, $source)",
                        "r.created_at = coalesce(r.created_at, datetime())",
                    )
                )
                .return_raw("count(DISTINCT r) AS edges_touched")
                .build()
            )
            edge_rows = self.neo4j.execute(
                edge_query,
                params={
                    "uuids": unique_edge_uuids,
                    "source": source,
                },
            )
            if edge_rows:
                edges_touched = int(edge_rows[0].get("edges_touched", 0))

        return PolicyStampResult(nodes_touched=nodes_touched, edges_touched=edges_touched)

    def update_episode_processing(
        self,
        episode_uuid: str,
        *,
        worker_id: str | None = None,
        stage: str | None = None,
        substage: str | None = None,
        progress: float | None = None,
        steps_completed: int | None = None,
        steps_total: int | None = None,
        llm_active_task: str | None = None,
        llm_active_kind: str | None = None,
        llm_active_model: str | None = None,
        llm_active_endpoint: str | None = None,
        clear_llm_active: bool = False,
        heartbeat: bool = True,
    ) -> bool:
        sets: list[str] = []
        params: dict[str, Any] = {"episode_uuid": episode_uuid, "worker_id": worker_id}

        if stage is not None:
            sets.append("n.processing_stage = $stage")
            params["stage"] = stage
        if substage is not None:
            sets.append("n.processing_substage = $substage")
            sets.append(
                "n.processing_substage_started_at = CASE"
                " WHEN $substage = n.processing_substage"
                " THEN n.processing_substage_started_at"
                " ELSE datetime() END"
            )
            params["substage"] = substage
        if progress is not None:
            params["progress"] = max(0.0, min(100.0, float(progress)))
            sets.append("n.processing_progress = $progress")
        if steps_total is not None:
            params["steps_total"] = max(1, int(steps_total))
            sets.append("n.processing_steps_total = $steps_total")
        if steps_completed is not None:
            clamped = max(0, int(steps_completed))
            if steps_total is not None:
                params["steps_completed"] = min(clamped, params["steps_total"])
                sets.append("n.processing_steps_completed = $steps_completed")
            else:
                params["steps_completed"] = clamped
                sets.append(
                    "n.processing_steps_completed = CASE"
                    " WHEN $steps_completed > coalesce(toInteger(n.processing_steps_total), 5)"
                    " THEN coalesce(toInteger(n.processing_steps_total), 5)"
                    " ELSE $steps_completed END"
                )
        if clear_llm_active:
            sets.extend(
                [
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                ]
            )
        else:
            if llm_active_task is not None:
                sets.append("n.processing_llm_active_task = $llm_active_task")
                params["llm_active_task"] = llm_active_task
            if llm_active_kind is not None:
                sets.append("n.processing_llm_active_kind = $llm_active_kind")
                params["llm_active_kind"] = llm_active_kind
            if llm_active_model is not None:
                sets.append("n.processing_llm_active_model = $llm_active_model")
                params["llm_active_model"] = llm_active_model
            if llm_active_endpoint is not None:
                sets.append("n.processing_llm_active_endpoint = $llm_active_endpoint")
                params["llm_active_endpoint"] = llm_active_endpoint
        if heartbeat:
            sets.append("n.processing_heartbeat_at = datetime()")
        if not sets:
            return False

        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "($worker_id IS NULL OR (n.processing_state = 'ENRICHING' AND n.processing_owner = $worker_id))",
            )
            .set(sets)
            .return_raw("count(n) AS updated")
            .build()
        )
        rows = self.neo4j.execute(query, params=params)
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def touch_episode_processing_heartbeat(self, episode_uuid: str, *, worker_id: str | None = None) -> bool:
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "($worker_id IS NULL OR (n.processing_state = 'ENRICHING' AND n.processing_owner = $worker_id))",
            )
            .set("n.processing_heartbeat_at = datetime()")
            .return_raw("count(n) AS updated")
            .build()
        )
        rows = self.neo4j.execute(query, params={"episode_uuid": episode_uuid, "worker_id": worker_id})
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def increment_episode_llm_usage(
        self,
        episode_uuid: str,
        *,
        task_delta: int = 1,
        phase: str = "started",
        kind: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        task: str | None = None,
        error: str | None = None,
    ) -> bool:
        delta = max(0, int(task_delta))
        phase_normalized = (phase or "started").strip().lower()
        if delta == 0 and phase_normalized == "started":
            return False

        substage_map = {
            "started": "llm_request_started",
            "completed": "llm_response_received",
            "failed": "llm_request_failed",
        }
        sets: list[str] = [
            "n.processing_llm_tasks_total = coalesce(toInteger(n.processing_llm_tasks_total), 0) + $task_delta",
            "n.processing_substage_started_at = datetime()",
        ]
        params: dict[str, Any] = {"episode_uuid": episode_uuid, "task_delta": delta}
        if phase_normalized == "started":
            sets.append(
                "n.processing_llm_tasks_attempt ="
                " coalesce(toInteger(n.processing_llm_tasks_attempt), 0) + $task_delta"
            )

        params["substage"] = substage_map.get(phase_normalized, "llm_activity_observed")
        sets.append("n.processing_substage = $substage")
        if phase_normalized in ("started", "completed", "failed"):
            sets.append("n.processing_llm_last_task_at = datetime()")
        if phase_normalized == "failed" and error is not None:
            params["error"] = error
            sets.append("n.processing_error = $error")
        if kind is not None:
            params["kind"] = kind
            sets.append("n.processing_llm_active_kind = $kind")
        if model is not None:
            params["model"] = model
            sets.append("n.processing_llm_active_model = $model")
        if endpoint is not None:
            params["endpoint"] = endpoint
            sets.append("n.processing_llm_active_endpoint = $endpoint")
        if task is not None:
            params["task"] = task
            sets.append("n.processing_llm_active_task = $task")

        query = Cypher().match("(n:Episodic)").where("n.uuid = $episode_uuid").set(sets).return_raw("count(n) AS updated").build()
        rows = self.neo4j.execute(query, params=params)
        return bool(rows and int(rows[0].get("updated", 0)) > 0)
