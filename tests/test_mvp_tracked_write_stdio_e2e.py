"""Black-box local-stdio acceptance coverage for the #118 MVP contract.

The test deliberately starts Menhir through its documented package entry point and
talks to a separate ``python -m menhir.mcp.server`` child over MCP stdio.  Direct
graph access is used only for the duplicate-write safety assertion and cleanup; all
user-visible behavior is exercised through MCP.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

pytestmark = [
    pytest.mark.online,
    pytest.mark.needs_llm,
    pytest.mark.timeout(600),
]

_EPISODE_ID = re.compile(r"^episode_id:\s*([0-9a-f-]{36})$", re.MULTILINE)
_PROVIDER_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_CHAT_MODEL",
    "OPENAI_EMBED_MODEL",
    "LOCAL_LLM_BASE_URL",
    "LOCAL_LLM_API_KEY",
    "LOCAL_LLM_CHAT_MODEL",
    "LOCAL_LLM_EMBED_BASE_URL",
    "LOCAL_LLM_EMBED_MODEL",
    "LLM_MAX_TOKENS",
    "GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS",
    "GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS",
    "GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _provider_environment(repo_root: Path) -> dict[str, str]:
    configured = os.environ.get("MENHIR_E2E_ENV_FILE", "").strip()
    env_file = Path(configured) if configured else repo_root / ".env"
    file_env = {
        str(key): str(value)
        for key, value in dotenv_values(env_file).items()
        if value
    } if env_file.is_file() else {}

    def value(name: str) -> str:
        return str(os.environ.get(name) or file_env.get(name) or "").strip()

    provider_env = {
        key: value(key)
        for key in _PROVIDER_KEYS
        if value(key)
    }
    if not provider_env.get("OPENAI_API_KEY"):
        pytest.skip(
            "tracked-write stdio E2E requires OPENAI_API_KEY or "
            "MENHIR_E2E_ENV_FILE pointing to a provider environment"
        )
    provider_env.setdefault("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    provider_env.setdefault("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    provider_env.update(
        {
            "LLM_CHAT_PROVIDER": "openai",
            "GRAPHITI_LLM_PROVIDER": "openai",
            "GRAPHITI_EMBED_PROVIDER": "openai",
            "GRAPHITI_RERANKER_PROVIDER": "openai",
        }
    )
    return provider_env


def _base_process_environment(
    repo_root: Path, tmp_path: Path, provider_env: dict[str, str]
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(provider_env)
    env.update(
        {
            # Never permit a subprocess to inherit or load production graph/state settings.
            "ENV_FILE": str(tmp_path / "intentionally-absent.env"),
            "NEO4J_URI": os.environ.get(
                "MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"
            ),
            "NEO4J_USER": os.environ.get("MENHIR_TEST_NEO4J_USER", "neo4j"),
            "NEO4J_PASSWORD": os.environ.get(
                "MENHIR_TEST_NEO4J_PASSWORD", "testpassword"
            ),
            "NEO4J_DATABASE": os.environ.get(
                "MENHIR_TEST_NEO4J_DATABASE", "neo4j"
            ),
            "MENHIR_AGENT_KEY": "",
            "MENHIR_READONLY_KEY": "",
            "MENHIR_OPERATOR_KEY": "",
            "MENHIR_API_KEY": "",
            "MENHIR_CLIENT_NAMESPACES": "",
            "MENHIR_CLIENT_TOOLS": "",
            "MENHIR_KNOWN_CLIENTS": "",
            "MENHIR_BENCHMARK_MODE": "1",
            "MENHIR_SAGA_RECONCILE_STARTUP_MODE": "observe",
            "MENHIR_LOG_DIR": str(tmp_path / "logs"),
            "PYTHONPATH": str(repo_root / "src"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _readiness(base_url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}/api/ready", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _tail(path: Path, chars: int = 6000) -> str:
    if not path.is_file():
        return "<no process log>"
    return path.read_text(encoding="utf-8", errors="replace")[-chars:]


@contextmanager
def _backend_process(
    repo_root: Path,
    tmp_path: Path,
    env: dict[str, str],
    label: str,
) -> Iterator[tuple[str, dict[str, str]]]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    process_env = dict(env)
    process_env.update(
        {
            "MENHIR_API_HOST": "127.0.0.1",
            "MENHIR_API_PORT": str(port),
            "MENHIR_MCP_TELEMETRY_DB": str(tmp_path / f"{label}-backend.db"),
        }
    )
    log_path = tmp_path / f"{label}-backend.log"
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "menhir",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=repo_root,
        env=process_env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 180
        last_error = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    f"Menhir backend exited during startup with {process.returncode}\n"
                    + _tail(log_path)
                )
            try:
                ready = _readiness(base_url)
                if ready.get("status") == "ready" and ready.get(
                    "capabilities", {}
                ).get("enrichment_ready"):
                    break
            except (OSError, ValueError, urllib.error.URLError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1)
        else:
            pytest.fail(
                f"Menhir backend was not enrichment-ready: {last_error}\n"
                + _tail(log_path)
            )
        yield base_url, process_env
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        log_file.close()


def _stdio_transport(
    repo_root: Path,
    tmp_path: Path,
    backend_env: dict[str, str],
    base_url: str,
    label: str,
) -> StdioTransport:
    env = dict(backend_env)
    env.update(
        {
            "MENHIR_BACKEND_URL": base_url,
            "MENHIR_MCP_TELEMETRY_DB": str(tmp_path / f"{label}-stdio.db"),
        }
    )
    return StdioTransport(
        command=sys.executable,
        args=["-m", "menhir.mcp.server"],
        env=env,
        cwd=str(repo_root),
        log_file=tmp_path / f"{label}-stdio.log",
    )


def _tool_text(result: Any) -> str:
    return "\n".join(
        str(block.text)
        for block in result.content
        if getattr(block, "type", "") == "text"
    )


def _tool_json(result: Any) -> dict[str, Any]:
    return json.loads(_tool_text(result))


async def _proxy_call(
    client: Client,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = 300,
) -> Any:
    return await client.call_tool(
        "call_tool",
        {"name": name, "arguments": arguments},
        timeout=timeout,
    )


def _episode_id(text: str) -> str:
    match = _EPISODE_ID.search(text)
    assert match, text
    return match.group(1)


class _FailingProviderHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": "forced-failure"}]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        self._json(
            400,
            {
                "error": {
                    "message": "forced provider failure for stdio acceptance test",
                    "type": "invalid_request_error",
                    "code": "forced_test_failure",
                }
            },
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _failing_provider() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FailingProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.mark.asyncio
async def test_tracked_write_stdio_workflow_survives_restart_and_reports_failure(
    test_neo4j_repo: Any,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    provider_env = _provider_environment(repo_root)
    process_env = _base_process_environment(repo_root, tmp_path, provider_env)
    namespace = f"mvp-118-{uuid4().hex}"
    reader_id = f"reader-{uuid4().hex}"
    original = (
        "The Atlas Lantern service uses Stripe. Its refund approval threshold is "
        "500 dollars."
    )
    correction = (
        "Correction: the Atlas Lantern refund approval threshold is 750 dollars now; "
        "500 dollars is the historical value."
    )

    with _backend_process(repo_root, tmp_path, process_env, "success-1") as (
        base_url,
        backend_env,
    ):
        transport = _stdio_transport(
            repo_root, tmp_path, backend_env, base_url, "success-1"
        )
        async with Client(transport, timeout=360) as client:
            visible = {tool.name for tool in await client.list_tools()}
            assert {"search_tools", "call_tool", "recall_memories", "build_context"} <= visible
            discovered = _tool_text(
                await client.call_tool(
                    "search_tools", {"query": "queue memory and track enrichment"}
                )
            )
            assert "add_memory_and_track" in discovered
            assert "get_enrichment_status" in discovered

            accepted = _tool_text(
                await _proxy_call(
                    client,
                    "add_memory_and_track",
                    {
                        "text": original,
                        "source": "stdio-e2e",
                        "namespace": namespace,
                        "timeout_s": 0,
                        "poll_interval_s": 0.05,
                    },
                )
            )
            original_episode = _episode_id(accepted)
            assert "timed_out: True" in accepted
            assert "Do not submit this memory again" in accepted

            observed = _tool_text(
                await _proxy_call(
                    client,
                    "get_enrichment_status",
                    {
                        "episode_uuid": original_episode,
                        "namespace": namespace,
                        "wait": True,
                        "timeout_s": 240,
                        "poll_interval_s": 1,
                    },
                )
            )
            assert f"episode_id: {original_episode}" in observed
            assert "status: READY" in observed

            corrected = _tool_text(
                await _proxy_call(
                    client,
                    "add_memory_and_track",
                    {
                        "text": correction,
                        "source": "stdio-e2e",
                        "namespace": namespace,
                        "timeout_s": 240,
                        "poll_interval_s": 1,
                    },
                )
            )
            correction_episode = _episode_id(corrected)
            assert correction_episode != original_episode
            assert "status: READY" in corrected

            original_resolution = test_neo4j_repo.execute(
                "MATCH (e:Episodic {uuid: $uuid}) "
                "RETURN e.resolved_episode_uuid AS resolved_uuid",
                params={"uuid": original_episode},
            )
            assert original_resolution and original_resolution[0]["resolved_uuid"]
            original_resolved_episode = str(
                original_resolution[0]["resolved_uuid"]
            )

            direct = _tool_json(
                await client.call_tool(
                    "recall_memories",
                    {
                        "query": "What is the current Atlas Lantern refund approval threshold?",
                        "namespace": namespace,
                        "limit": 8,
                    },
                    timeout=180,
                )
            )
            direct_text = json.dumps(direct).lower()
            assert direct["count"] > 0
            assert "750" in direct_text

            paraphrase = _tool_json(
                await client.call_tool(
                    "recall_memories",
                    {
                        "query": "How large a refund needs a manager to approve it for Atlas Lantern?",
                        "namespace": namespace,
                        "limit": 8,
                    },
                    timeout=180,
                )
            )
            assert "750" in json.dumps(paraphrase).lower()

            history = _tool_json(
                await client.call_tool(
                    "recall_memories",
                    {
                        "query": "Atlas Lantern refund threshold history",
                        "namespace": namespace,
                        "limit": 10,
                        "include_invalidated": True,
                    },
                    timeout=180,
                )
            )
            history_receipts: dict[str, dict[str, Any]] = {}
            for item in history["items"]:
                item_provenance = _tool_json(
                    await _proxy_call(
                        client,
                        "get_provenance",
                        {
                            "node_uuid": item["uuid"],
                            "namespace": namespace,
                            "content_chars": 1000,
                        },
                    )
                )
                if item_provenance["ok"]:
                    history_receipts.update(
                        {
                            str(episode["uuid"]): episode
                            for episode in item_provenance["episodes"]
                        }
                    )
            assert original_resolved_episode in history_receipts
            assert (
                str(history_receipts[original_resolved_episode]["content"])
                == original
            )

            context = _tool_text(
                await client.call_tool(
                    "build_context",
                    {
                        "query": "What is the current Atlas Lantern refund approval threshold?",
                        "namespace": namespace,
                        "max_tokens": 1000,
                    },
                    timeout=180,
                )
            )
            assert "750" in context

            await client.call_tool(
                "read_flagged_memories",
                {"reader_id": reader_id, "namespace": namespace},
            )
            startup_context = _tool_json(
                await client.call_tool(
                    "recall_context_memories",
                    {
                        "reader_id": reader_id,
                        "query": "Atlas Lantern refund approval policy",
                        "namespace": namespace,
                        "limit": 8,
                        "recent_limit": 8,
                    },
                    timeout=180,
                )
            )
            assert startup_context["bootstrap_verified"] is True
            assert "750" in json.dumps(startup_context).lower()

            current_item = next(
                item for item in direct["items"] if "750" in json.dumps(item)
            )
            provenance = _tool_json(
                await _proxy_call(
                    client,
                    "get_provenance",
                    {
                        "node_uuid": current_item["uuid"],
                        "namespace": namespace,
                        "content_chars": 1000,
                    },
                )
            )
            assert provenance["ok"] is True
            receipts = {episode["uuid"]: episode for episode in provenance["episodes"]}
            resolution = test_neo4j_repo.execute(
                "MATCH (e:Episodic {uuid: $uuid}) "
                "RETURN e.resolved_episode_uuid AS resolved_uuid",
                params={"uuid": correction_episode},
            )
            assert resolution and resolution[0]["resolved_uuid"]
            resolved_episode = str(resolution[0]["resolved_uuid"])
            assert resolved_episode in receipts
            assert "750" in str(receipts[resolved_episode]["content"])

    # The process really stops and a fresh backend + fresh stdio bridge must recover
    # both status and recall from the durable graph.
    with _backend_process(repo_root, tmp_path, process_env, "success-2") as (
        base_url,
        backend_env,
    ):
        transport = _stdio_transport(
            repo_root, tmp_path, backend_env, base_url, "success-2"
        )
        async with Client(transport, timeout=360) as client:
            persisted_status = _tool_text(
                await _proxy_call(
                    client,
                    "get_enrichment_status",
                    {
                        "episode_uuid": correction_episode,
                        "namespace": namespace,
                    },
                )
            )
            assert "status: READY" in persisted_status
            persisted = _tool_json(
                await client.call_tool(
                    "recall_memories",
                    {
                        "query": "current Atlas Lantern refund approval threshold",
                        "namespace": namespace,
                        "limit": 8,
                    },
                    timeout=180,
                )
            )
            assert "750" in json.dumps(persisted).lower()

    accepted_roots = test_neo4j_repo.execute(
        "MATCH (e:Episodic) "
        "WHERE coalesce(e.namespace, 'default') = $namespace "
        "AND e.source = $source AND e.content IN $contents "
        "AND e.processing_state IS NOT NULL "
        "RETURN e.content AS content, collect(e.uuid) AS uuids, count(e) AS copies "
        "ORDER BY content",
        params={
            "namespace": namespace,
            "source": "stdio-e2e",
            "contents": [original, correction],
        },
    )
    assert {
        row["content"]: (row["copies"], set(row["uuids"]))
        for row in accepted_roots
    } == {
        original: (1, {original_episode}),
        correction: (1, {correction_episode}),
    }

    # A deterministic provider refusal proves FAILED is distinct from acceptance and
    # that continued observation still addresses the one accepted episode.
    with _failing_provider() as failing_base_url:
        failure_provider_env = dict(provider_env)
        failure_provider_env.update(
            {
                "LLM_CHAT_PROVIDER": "local",
                "GRAPHITI_LLM_PROVIDER": "local",
                "GRAPHITI_EMBED_PROVIDER": "openai",
                "GRAPHITI_RERANKER_PROVIDER": "openai",
                "LOCAL_LLM_BASE_URL": failing_base_url,
                "LOCAL_LLM_API_KEY": "test-only",
                "LOCAL_LLM_CHAT_MODEL": "forced-failure",
            }
        )
        failure_env = _base_process_environment(
            repo_root, tmp_path, failure_provider_env
        )
        with _backend_process(repo_root, tmp_path, failure_env, "failure") as (
            base_url,
            backend_env,
        ):
            transport = _stdio_transport(
                repo_root, tmp_path, backend_env, base_url, "failure"
            )
            async with Client(transport, timeout=180) as client:
                failed_write = _tool_text(
                    await _proxy_call(
                        client,
                        "add_memory_and_track",
                        {
                            "text": "Atlas Lantern failure-path fact.",
                            "source": "stdio-e2e",
                            "namespace": f"{namespace}-failure",
                            "timeout_s": 90,
                            "poll_interval_s": 0.5,
                        },
                        timeout=150,
                    )
                )
                failed_episode = _episode_id(failed_write)
                assert "status: FAILED" in failed_write
                assert "authorized retry/repair on the existing episode" in failed_write

                failed_status = _tool_text(
                    await _proxy_call(
                        client,
                        "get_enrichment_status",
                        {
                            "episode_uuid": failed_episode,
                            "namespace": f"{namespace}-failure",
                        },
                    )
                )
                assert f"episode_id: {failed_episode}" in failed_status
                assert "status: FAILED" in failed_status

    failed_count = test_neo4j_repo.execute(
        "MATCH (e:Episodic {uuid: $uuid}) RETURN count(e) AS copies",
        params={"uuid": failed_episode},
    )[0]["copies"]
    assert failed_count == 1
