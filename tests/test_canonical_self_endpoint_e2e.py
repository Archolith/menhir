"""Real-model E2E for the declared canonical-self endpoint.

Runs the public REST path against a disposable Docker Neo4j and a self-served test Menhir.  It is
online-only because extraction uses the configured OpenAI-compatible model; no VPS or production
service is contacted.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from neo4j import GraphDatabase

from menhir.domain import merge_eligibility as merge_policy
from menhir.domain.self_identity import SUBJECT_ENDPOINT_MARKER_PREFIX, self_uuid_for_namespace
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.neo4j import Neo4jRepository
from scripts.dev import test_server


pytestmark = [pytest.mark.online, pytest.mark.timeout(420)]


def _request(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None):
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": "Bearer test-agent-key",
            "Content-Type": "application/json",
            "X-Menhir-User-Id": "canonical-self-e2e",
            "X-Menhir-Session-Id": "canonical-self-e2e-session",
            "X-Menhir-Client-Name": "canonical-self-e2e",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


def _wait_ready(base_url: str, timeout_s: float = 180) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready = _request(base_url, "GET", "/api/ready")
        if ready.get("status") == "ready" and ready.get("capabilities", {}).get(
            "enrichment_ready"
        ):
            return
        time.sleep(1)
    raise TimeoutError("test Menhir did not become enrichment-ready")


def _contains_reserved_marker(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_reserved_marker(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_reserved_marker(item) for item in value)
    return SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in str(value or "").casefold()


def test_declared_self_survives_public_ingest_and_cannot_be_merged(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    file_env = {
        str(key): str(value)
        for key, value in dotenv_values(repo_root / ".env").items()
        if value
    }
    api_key = os.environ.get("OPENAI_API_KEY") or file_env.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY is required for the real-model E2E")

    allowed_llm = {
        "OPENAI_API_KEY", "OPENAI_CHAT_MODEL", "OPENAI_EMBED_MODEL", "OPENAI_BASE_URL",
        "LLM_CHAT_PROVIDER", "GRAPHITI_LLM_PROVIDER", "GRAPHITI_EMBED_PROVIDER",
        "GRAPHITI_RERANKER_PROVIDER", "GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS",
        "GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS", "LLM_MAX_TOKENS",
    }
    llm_env = {
        key: os.environ.get(key) or file_env.get(key, "")
        for key in allowed_llm
        if os.environ.get(key) or file_env.get(key)
    }
    original_shape_env = test_server._shape_env

    def _shape_env(*args, **kwargs):
        env = original_shape_env(*args, **kwargs)
        env.update(llm_env)
        env["PYTHONPATH"] = str(repo_root / "src")
        env["OPENAI_CHAT_MODEL"] = "gpt-4o-mini"
        env["LLM_CHAT_PROVIDER"] = "openai"
        env["GRAPHITI_LLM_PROVIDER"] = "openai"
        env["GRAPHITI_EMBED_PROVIDER"] = "openai"
        env["MENHIR_CANONICAL_SELF_BINDING_MODE"] = "enforce"
        env["MENHIR_MAX_INGEST_WORKERS"] = "1"
        env["SCHEDULER_TRACE_DISABLED"] = "1"
        return env

    monkeypatch.setattr(test_server, "_shape_env", _shape_env)
    namespace = f"canonical-self-e2e-{uuid4()}"
    turn_text = "I own exactly 37 cobalt postcards in the north cabinet."

    with test_server.launch(
        "static", backend="neo4j", python_executable=sys.executable,
        health_timeout_s=180, quiet=True,
    ) as server:
        _wait_ready(server.base_url)
        turn = _request(server.base_url, "POST", "/api/turn-evidence", {
            "text": turn_text,
            "role": "user",
            "declarant": "user",
            "session_id": "canonical-self-e2e-session",
            "namespace": namespace,
            "source_kind": "codex_e2e",
            "source_client": "codex",
            "hook_version": "canonical-self-e2e-v1",
            "triage_reason": ["first_person", "ownership"],
            "triage_version": "canonical-self-e2e-v1",
            "prompt_id": f"prompt-{namespace}",
        })
        turn_id = str(turn["turn_id"])
        memory = _request(server.base_url, "POST", "/api/memory", {
            "episode": "The cobalt postcard collection contains 37 items in the north cabinet.",
            "source": "claude-code",
            "session_id": "canonical-self-e2e-session",
            "user_id": "canonical-self-e2e",
            "namespace": namespace,
        })
        admission = _request(server.base_url, "POST", "/api/episode-admission", {
            "episode_uuid": str(memory["episode_id"]),
            "turn_evidence_uuid": turn_id,
        })
        assert admission["linked"] is True
        projection_uuid = str(admission["projection_uuid"])

        driver = GraphDatabase.driver(
            server.neo4j_uri, auth=(server.neo4j_user, server.neo4j_password)
        )
        try:
            deadline = time.monotonic() + 300
            state = ""
            while time.monotonic() < deadline:
                with driver.session() as session:
                    record = session.run(
                        "MATCH (p:Episodic {uuid:$uuid}) RETURN p.processing_state AS state",
                        uuid=projection_uuid,
                    ).single()
                state = str(record["state"] if record else "MISSING")
                if state in {"READY", "FAILED", "MANUAL_REVIEW"}:
                    break
                time.sleep(2)
            assert state == "READY"

            canonical_uuid = self_uuid_for_namespace(namespace)
            with driver.session() as session:
                canonical = session.run(
                    "MATCH (s:Entity {uuid:$uuid}) RETURN s.name AS name, s.group_id AS group_id",
                    uuid=canonical_uuid,
                ).single()
                resolved = session.run(
                    "MATCH (g:Episodic {name:$name}) WHERE g.uuid <> $pending "
                    "RETURN g.uuid AS uuid",
                    name=f"evidence-projection-{turn_id}", pending=projection_uuid,
                ).single()
                assert resolved is not None
                graphiti_episode_uuid = str(resolved["uuid"])
                relation_count = session.run(
                    "MATCH (:Entity {uuid:$self_uuid})-[r]-(:Entity) "
                    "WHERE $episode IN coalesce(r.episodes, []) RETURN count(r) AS count",
                    self_uuid=canonical_uuid, episode=graphiti_episode_uuid,
                ).single()["count"]
                mention_count = session.run(
                    "MATCH (:Episodic {uuid:$episode})-[:MENTIONS]->"
                    "(:Entity {uuid:$self_uuid}) "
                    "RETURN count(*) AS count",
                    episode=graphiti_episode_uuid, self_uuid=canonical_uuid,
                ).single()["count"]
                persisted_properties = [
                    record["props"]
                    for record in session.run(
                        "MATCH (n) RETURN properties(n) AS props "
                        "UNION ALL "
                        "MATCH ()-[r]->() RETURN properties(r) AS props"
                    )
                ]
                ordinary = session.run(
                    "MATCH (n:Entity {group_id:$group}) WHERE n.uuid <> $self_uuid "
                    "AND toLower(trim(n.name)) = 'user' RETURN n.uuid AS uuid LIMIT 1",
                    group=namespace, self_uuid=canonical_uuid,
                ).single()

            assert canonical and canonical["name"] == "user"
            assert canonical["group_id"] == namespace
            assert int(relation_count) >= 1
            assert int(mention_count) == 1
            assert not any(
                _contains_reserved_marker(item) for item in persisted_properties
            )
            assert ordinary is not None

            repository = Neo4jRepository(
                uri=server.neo4j_uri, database="neo4j",
                user=server.neo4j_user, password=server.neo4j_password,
            )
            try:
                correlation = CorrelationRepository(repository)
                ordinary_uuid = str(ordinary["uuid"])
                refusals = (
                    correlation.merge_entity(ordinary_uuid, canonical_uuid, similarity=0.999),
                    correlation.merge_entity(canonical_uuid, ordinary_uuid, similarity=0.999),
                )
            finally:
                repository.close()
            assert all(result["merged"] == 0 for result in refusals)
            assert all(result["reason"] == merge_policy.INELIGIBLE_ROLE for result in refusals)
        finally:
            driver.close()
