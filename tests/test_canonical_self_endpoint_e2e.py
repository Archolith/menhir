"""Real-model E2E for the declared canonical-self endpoint.

Runs the public REST path against a disposable Docker Neo4j and a self-served test Menhir.  It is
online-only because extraction uses the configured OpenAI-compatible model; no VPS or production
service is contacted.
"""

from __future__ import annotations

import base64
from hashlib import sha256
import json
import math
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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from menhir.domain import merge_eligibility as merge_policy
from menhir.domain.self_authority import canonical_json_bytes
from menhir.domain.self_identity import SUBJECT_ENDPOINT_MARKER_PREFIX, self_uuid_for_namespace
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.self_authority import confirmation_filename
from scripts.dev import test_server


pytestmark = [pytest.mark.online, pytest.mark.timeout(600)]

_RELEASE_TEST_FLAG = "MENHIR_RELEASE_TEST"
_RELEASE_TEST_MODEL = "MENHIR_RELEASE_TEST_MODEL"
_RELEASE_TEST_IMAGE = "MENHIR_RELEASE_TEST_IMAGE"
_RELEASE_TEST_COMMIT = "MENHIR_RELEASE_TEST_COMMIT"


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
    release_image = _configured_value(_RELEASE_TEST_IMAGE, file_env)
    release_commit = _configured_value(_RELEASE_TEST_COMMIT, file_env)

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
        if release_image and not release_commit:
            pytest.fail(f"{_RELEASE_TEST_COMMIT} is required with {_RELEASE_TEST_IMAGE}")

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
        "image": release_image or "source",
        "image_commit": release_commit,
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


def _seed_counterpart_through_public_ingest(
    server: test_server.RunningServer,
    driver: Any,
    namespace: str,
    *,
    timeout_s: float = 180,
) -> str:
    """Create the counterpart through the tested app's real embedding/persistence path.

    A raw CREATE with only name/UUID is invisible to Graphiti's cosine-only candidate
    acquisition. Do not fabricate an embedding with a different client/model either:
    seed an ordinary episode through the candidate app and require its exact provenance,
    unique entity identity and persisted name embedding before attempting confirmation.
    The unsigned proposal below additionally proves that real resolution found this UUID.
    """
    memory = _request(server.base_url, "POST", "/api/memory", {
        "episode": "Project Cobalt is a software project.",
        "source": "claude-code",
        "session_id": "canonical-self-e2e-session",
        "user_id": "canonical-self-e2e",
        "namespace": namespace,
    })
    pending_uuid = str(memory["episode_id"])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with driver.session() as session:
            pending = session.run(
                "MATCH (p:Episodic {uuid:$uuid}) "
                "RETURN p.processing_state AS state, p.processing_error AS error",
                uuid=pending_uuid,
            ).single()
        state = str(pending["state"] if pending else "MISSING")
        if state == "READY":
            break
        if state in {"FAILED", "MANUAL_REVIEW"}:
            raise AssertionError(
                f"counterpart seed state={state!r}, error={pending['error']!r}\n"
                f"isolated server log:\n{server.tail_log()}"
            )
        time.sleep(1)
    else:
        raise TimeoutError("counterpart seed did not become READY")
    with driver.session() as session:
        candidates = list(session.run(
            "MATCH (p:Episodic {uuid:$pending}) "
            "MATCH (g:Episodic)-[:MENTIONS]->(n:Entity {name:$name, group_id:$group}) "
            "WHERE g.uuid = p.resolved_episode_uuid "
            "RETURN DISTINCT n.uuid AS uuid, n.name_embedding AS embedding",
            pending=pending_uuid, name="Project Cobalt", group=namespace,
        ))
    assert len(candidates) == 1, "counterpart seed must produce one exact episode-linked identity"
    candidate = candidates[0]
    vector = candidate["embedding"]
    assert isinstance(vector, list) and vector and all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in vector
    ), "counterpart seed must have a searchable name embedding from the candidate app"
    counterpart_uuid = str(candidate["uuid"] or "").strip()
    assert counterpart_uuid, "counterpart seed must have a persistent UUID"
    return counterpart_uuid


def test_owner_confirmation_controls_public_self_fact_lifecycle(
    monkeypatch, record_property, tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    llm_env, llm_evidence = _release_llm_environment(repo_root)
    release_image = llm_evidence["image"]
    container_image = None
    if release_image != "source":
        try:
            container_image, revision = test_server.resolve_container_image(
                release_image, expected_revision=llm_evidence["image_commit"],
            )
        except RuntimeError as exc:
            pytest.fail(str(exc))
        llm_evidence["image_id"] = container_image
        llm_evidence["image_revision"] = revision
    for key, value in llm_evidence.items():
        record_property(f"release_llm_{key}", value)
    print("canonical-self E2E LLM: " + json.dumps(llm_evidence, sort_keys=True))
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_raw = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    public_key_path = tmp_path / "owner-public.pem"
    public_key_path.write_bytes(public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    confirmation_directory = tmp_path / "confirmations"
    confirmation_directory.mkdir()
    original_shape_env = test_server._shape_env

    def _shape_env(*args, **kwargs):
        env = original_shape_env(*args, **kwargs)
        env.update(llm_env)
        if release_image == "source":
            env["PYTHONPATH"] = str(repo_root / "src")
        env["MENHIR_CANONICAL_SELF_BINDING_MODE"] = "enforce"
        env["MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_PATH"] = str(public_key_path)
        env["MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_SHA256"] = sha256(
            public_raw
        ).hexdigest()
        env["MENHIR_CANONICAL_SELF_CONFIRMATION_DIRECTORY"] = str(
            confirmation_directory
        )
        env["MENHIR_MAX_INGEST_WORKERS"] = "1"
        env["SCHEDULER_TRACE_DISABLED"] = "1"
        return env

    monkeypatch.setattr(test_server, "_shape_env", _shape_env)
    namespace = f"canonical-self-e2e-{uuid4()}"
    turn_text = "I own Project Cobalt."

    with test_server.launch(
        "static", backend="neo4j",
        python_executable=sys.executable if release_image == "source" else None,
        container_image=container_image,
        health_timeout_s=180, quiet=True,
    ) as server:
        _wait_ready(server.base_url)
        seed_driver = GraphDatabase.driver(
            server.neo4j_uri, auth=(server.neo4j_user, server.neo4j_password)
        )
        try:
            counterpart_uuid = _seed_counterpart_through_public_ingest(
                server, seed_driver, namespace
            )
        finally:
            seed_driver.close()
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
            "episode": "Project Cobalt is an existing project.",
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
                provenance = session.run(
                    "MATCH (m:Episodic {uuid:$memory})-[a:ADMITTED_ON]->"
                    "(t:TurnEvidence {turn_id:$turn}) "
                    "MATCH (p:Episodic {uuid:$projection}) "
                    "RETURN count(a) AS admission_count, p.content AS content, "
                    "p.evidence_projection_of AS projection_of, "
                    "p.is_evidence_projection AS is_projection, "
                    "p.resolved_episode_uuid AS resolved_episode_uuid",
                    memory=str(memory["episode_id"]), turn=turn_id,
                    projection=projection_uuid,
                ).single()
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
                proposal_row = session.run(
                    "MATCH (p:Episodic {uuid:$projection}) "
                    "RETURN p.self_assertion_proposals_json AS proposals, "
                    "p.self_assertion_proposal_count AS proposal_count, "
                    "p.self_assertion_authorized_count AS authorized_count",
                    projection=projection_uuid,
                ).single()
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
            assert provenance is not None
            assert int(provenance["admission_count"]) == 1
            assert provenance["content"] == turn_text
            assert provenance["projection_of"] == turn_id
            assert provenance["is_projection"] is True
            assert provenance["resolved_episode_uuid"] == graphiti_episode_uuid
            assert int(relation_count) == 0
            assert int(mention_count) == 1
            assert proposal_row is not None
            assert int(proposal_row["proposal_count"]) >= 1
            assert int(proposal_row["authorized_count"]) == 0
            assert not any(
                _contains_reserved_marker(item) for item in persisted_properties
            )
            proposals = json.loads(str(proposal_row["proposals"] or "[]"))
            proposal = next(
                item
                for item in proposals
                if isinstance(item.get("assertion"), dict)
                and item["assertion"].get("counterpart", {}).get("uuid")
                == counterpart_uuid
            )
            assert proposal["authorization"]["authorized"] is False
            assert proposal["authorization"]["reason"] != "counterpart_identity_not_persistent"
            payload = {
                key: value for key, value in proposal.items() if key != "authorization"
            }
            signature = private_key.sign(canonical_json_bytes(payload))
            confirmation_path = confirmation_directory / confirmation_filename(
                str(payload["episode_uuid"])
            )
            confirmation_path.write_text(
                json.dumps({
                    "confirmations": [{
                        "payload": payload,
                        "signature": base64.b64encode(signature).decode("ascii"),
                    }]
                }),
                encoding="utf-8",
            )

            repository = Neo4jRepository(
                uri=server.neo4j_uri, database="neo4j",
                user=server.neo4j_user, password=server.neo4j_password,
            )
            try:
                assert EpisodeRepository(repository).force_reset_failed_episode(
                    projection_uuid
                ) is True
            finally:
                repository.close()

            deadline = time.monotonic() + 300
            authorized_count = 0
            while time.monotonic() < deadline:
                with driver.session() as session:
                    rerun = session.run(
                        "MATCH (p:Episodic {uuid:$uuid}) "
                        "RETURN p.processing_state AS state, "
                        "p.processing_error AS error, "
                        "p.self_assertion_authorized_count AS authorized_count",
                        uuid=projection_uuid,
                    ).single()
                state = str(rerun["state"] if rerun else "MISSING")
                authorized_count = int(
                    rerun["authorized_count"] if rerun and rerun["authorized_count"] else 0
                )
                if state == "READY" and authorized_count >= 1:
                    break
                if state in {"FAILED", "MANUAL_REVIEW"}:
                    processing_error = str(rerun["error"] if rerun else "")
                    break
                time.sleep(2)
            assert state == "READY" and authorized_count >= 1, (
                f"confirmed rerun state={state!r}, authorized={authorized_count}, "
                f"error={processing_error!r}\nisolated server log:\n{server.tail_log()}"
            )

            with driver.session() as session:
                authorized_edge = session.run(
                    "MATCH (:Entity {uuid:$self_uuid})-[r]-"
                    "(:Entity {uuid:$counterpart_uuid}) "
                    "WHERE r.menhir_self_authority_payload_json IS NOT NULL "
                    "RETURN count(r) AS count, collect(r.fact) AS facts",
                    self_uuid=canonical_uuid,
                    counterpart_uuid=counterpart_uuid,
                ).single()
            assert authorized_edge is not None
            assert int(authorized_edge["count"]) == 1
            signed_fact = str(authorized_edge["facts"][0])
            confirmed_recall = _request(server.base_url, "POST", "/api/recall", {
                "query": "What do I own?",
                "namespace": namespace,
                "limit": 10,
            })
            assert signed_fact in {
                str(item.get("content") or "")
                for item in confirmed_recall["results"]
            }

            confirmation_path.unlink()
            revoked_recall = _request(server.base_url, "POST", "/api/recall", {
                "query": "What do I own?",
                "namespace": namespace,
                "limit": 10,
            })
            assert signed_fact not in {
                str(item.get("content") or "")
                for item in revoked_recall["results"]
            }

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
