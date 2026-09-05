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

_RELEASE_TEST_FLAG = "MENHIR_RELEASE_TEST"
_RELEASE_TEST_MODEL = "MENHIR_RELEASE_TEST_MODEL"


def _configured_value(name: str, file_env: dict[str, str]) -> str:
    return str(os.environ.get(name) or file_env.get(name) or "").strip()


def _release_llm_environment(repo_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    file_env = {
        str(key): str(value)
        for key, value in dotenv_values(repo_root / ".env").items()
        if value
    }
    release_mode = _configured_value(_RELEASE_TEST_FLAG, file_env).casefold() in {
        "1", "true", "yes", "on",
    }
    chat_provider = _configured_value("LLM_CHAT_PROVIDER", file_env)
    graphiti_provider = _configured_value("GRAPHITI_LLM_PROVIDER", file_env)
    embed_provider = _configured_value("GRAPHITI_EMBED_PROVIDER", file_env)
    release_model = _configured_value(_RELEASE_TEST_MODEL, file_env)

    if release_mode:
        missing = [
            name for name, value in (
                ("LLM_CHAT_PROVIDER", chat_provider),
                ("GRAPHITI_LLM_PROVIDER", graphiti_provider),
                ("GRAPHITI_EMBED_PROVIDER", embed_provider),
                (_RELEASE_TEST_MODEL, release_model),
            ) if not value
        ]
        if missing:
            pytest.fail(
                "release E2E requires explicit provider/model settings: "
                + ", ".join(missing)
            )

    chat_provider = chat_provider or "openai"
    graphiti_provider = graphiti_provider or chat_provider
    embed_provider = embed_provider or graphiti_provider
    if chat_provider not in {"local", "openai"} or graphiti_provider not in {
        "local", "openai",
    }:
        pytest.fail("canonical-self E2E requires OpenAI-compatible chat providers")

    model_env = "LOCAL_LLM_CHAT_MODEL" if chat_provider == "local" else "OPENAI_CHAT_MODEL"
    model = release_model or _configured_value(model_env, file_env)
    if not model and not release_mode:
        model = "gpt-4o-mini"
    if not model:
        pytest.fail(f"{model_env} is required for the configured release provider")

    allowed_llm = {
        "OPENAI_API_KEY", "OPENAI_CHAT_MODEL", "OPENAI_EMBED_MODEL",
        "LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_CHAT_MODEL",
        "LLM_CHAT_PROVIDER", "GRAPHITI_LLM_PROVIDER", "GRAPHITI_EMBED_PROVIDER",
        "GRAPHITI_RERANKER_PROVIDER", "GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS",
        "GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS", "LLM_MAX_TOKENS",
    }
    llm_env = {
        key: _configured_value(key, file_env)
        for key in allowed_llm
        if _configured_value(key, file_env)
    }
    llm_env["LLM_CHAT_PROVIDER"] = chat_provider
    llm_env["GRAPHITI_LLM_PROVIDER"] = graphiti_provider
    llm_env["GRAPHITI_EMBED_PROVIDER"] = embed_provider
    llm_env[model_env] = model

    selected_providers = {chat_provider, graphiti_provider, embed_provider}
    required_credentials = []
    if "local" in selected_providers:
        required_credentials.extend(("LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY"))
    if "openai" in selected_providers:
        required_credentials.append("OPENAI_API_KEY")
    missing_credentials = [name for name in required_credentials if not llm_env.get(name)]
    if missing_credentials:
        message = "real-model E2E requires: " + ", ".join(missing_credentials)
        if release_mode:
            pytest.fail(message)
        pytest.skip(message)

    evidence = {
        "chat_provider": chat_provider,
        "graphiti_provider": graphiti_provider,
        "embed_provider": embed_provider,
        "chat_model": model,
        "embed_model": llm_env.get("OPENAI_EMBED_MODEL", ""),
        "base_url": llm_env.get("LOCAL_LLM_BASE_URL", "https://api.openai.com/v1"),
        "release_mode": str(release_mode).lower(),
    }
    return llm_env, evidence


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


def test_declared_self_survives_public_ingest_and_cannot_be_merged(
    monkeypatch, record_property,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    llm_env, llm_evidence = _release_llm_environment(repo_root)
    for key, value in llm_evidence.items():
        record_property(f"release_llm_{key}", value)
    print("canonical-self E2E LLM: " + json.dumps(llm_evidence, sort_keys=True))
    original_shape_env = test_server._shape_env

    def _shape_env(*args, **kwargs):
        env = original_shape_env(*args, **kwargs)
        env.update(llm_env)
        env["PYTHONPATH"] = str(repo_root / "src")
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
            processing_error = ""
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
            if state == "FAILED":
                with driver.session() as session:
                    failed = session.run(
                        "MATCH (p:Episodic {uuid:$uuid}) "
                        "RETURN p.processing_error AS processing_error",
                        uuid=projection_uuid,
                    ).single()
                processing_error = str(
                    failed["processing_error"]
                    if failed and failed["processing_error"] else ""
                )
            assert state == "READY", (
                f"projection state={state!r}, processing_error={processing_error!r}\n"
                f"isolated server log:\n{server.tail_log()}"
            )

            canonical_uuid = self_uuid_for_namespace(namespace)
            ordinary_uuid = f"ordinary-{uuid4()}"
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
                session.run(
                    "CREATE (:Entity {uuid:$uuid, name:'ordinary merge target', "
                    "group_id:$group, scope:'PERSISTENT', freshness:'ACTIVE'})",
                    uuid=ordinary_uuid, group=namespace,
                ).consume()

            assert canonical and canonical["name"] == "user"
            assert canonical["group_id"] == namespace
            assert int(relation_count) >= 1
            assert int(mention_count) == 1
            assert not any(
                _contains_reserved_marker(item) for item in persisted_properties
            )
            repository = Neo4jRepository(
                uri=server.neo4j_uri, database="neo4j",
                user=server.neo4j_user, password=server.neo4j_password,
            )
            try:
                correlation = CorrelationRepository(repository)
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
