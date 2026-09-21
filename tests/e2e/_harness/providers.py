"""Fake OpenAI-compatible providers for the E2E campaign.

Ported verbatim from ``tests/test_mvp_tracked_write_stdio_e2e.py`` on branch
``mvp-118-stdio-e2e``, which built and proved them for #118's stdio acceptance
work. They are moved here rather than reimplemented: this is the piece the E2E
scaffold was missing, and a second implementation of a Graphiti-shaped
structured-extraction stub would be a liability, not a contribution.

Why they matter to the campaign:

- :class:`DeterministicProviderHandler` lets a lane reach READY enrichment with no
  live model and no spend, answering every Graphiti extraction schema
  (``CombinedExtraction``, ``ExtractedEntities``, ``ExtractedEdges``,
  ``NodeResolutions``, ``EdgeDuplicate``, summaries) plus embeddings and logprobs.
  Its embeddings are deterministic and non-zero with a shared anchor, so the tiny
  acceptance corpus stays mutually searchable.
- :class:`FailingProviderHandler` refuses every completion, which is how E2E-8's
  "provider failure does not silently pass" criterion is proven. Without it, a
  provider outage and a passing run look identical.

``real_provider_environment`` remains for lanes an operator deliberately points at
a live model; it skips rather than proceeding when no key is configured.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

__all__ = [
    "DETERMINISTIC_PROVIDER_FLAG",
    "REFUND_CORRECTED_AMOUNT",
    "REFUND_ORIGINAL_AMOUNT",
    "DeterministicProviderHandler",
    "FailingProviderHandler",
    "deterministic_provider",
    "failing_provider",
    "local_provider_environment",
    "real_provider_environment",
]

DETERMINISTIC_PROVIDER_FLAG = "MENHIR_E2E_DETERMINISTIC_PROVIDER"

#: The refund amounts the canned extraction answers with. E2E-2's corpus MUST use these
#: exact values: this provider does not read the episode, it replays a fixed fact, so a
#: lane that changed its own wording would be asserting against the fake's unchanged
#: answer. They live here, beside the responses, and the lane imports them -- single
#: sourced so the two cannot drift apart.
#:
#: Deliberately not 500. E2E-2 previously used 500 and asserted `"500" in context`; when
#: build_context faulted, the tool returned "500 Internal Server Error", the substring
#: matched the HTTP status code, and a crashed endpoint was recorded as a pass.
REFUND_ORIGINAL_AMOUNT = "1275"
REFUND_CORRECTED_AMOUNT = "3840"

#: Provider variables carried through to a child when a lane uses a live model.
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


def local_provider_environment(base_url: str) -> dict[str, str]:
    return {
        "LLM_CHAT_PROVIDER": "local",
        "GRAPHITI_LLM_PROVIDER": "local",
        "GRAPHITI_EMBED_PROVIDER": "local",
        "GRAPHITI_RERANKER_PROVIDER": "local",
        "LOCAL_LLM_BASE_URL": base_url,
        "LOCAL_LLM_EMBED_BASE_URL": base_url,
        "LOCAL_LLM_API_KEY": "test-only",
        "LOCAL_LLM_CHAT_MODEL": "deterministic-chat",
        "LOCAL_LLM_EMBED_MODEL": "text-embedding-3-small",
    }


def real_provider_environment(repo_root: Path) -> dict[str, str]:
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


class FailingProviderHandler(BaseHTTPRequestHandler):
    """Passes startup preflight, then refuses every completion and embedding.

    GET /v1/models lists the SAME model names ``local_provider_environment`` configures. It
    used to list ``forced-failure``, which the backend's preflight rejected ("does not list
    required model(s)"), so the backend started ``degraded_queue_only`` with enrichment OFF:
    the write under test sat PENDING with attempts=0 for the whole 360s wait, and the lane
    passed because "not READY" was trivially true. That proved nothing about a failing
    provider -- and cost six minutes per CI run. Failing at the call, not at preflight, is
    what makes the pipeline actually try.
    """

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "deterministic-chat", "object": "model"},
                        {"id": "text-embedding-3-small", "object": "model"},
                    ],
                },
            )
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


class DeterministicProviderHandler(BaseHTTPRequestHandler):
    """Small OpenAI-compatible provider for the required CI acceptance lane."""

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "deterministic-chat", "object": "model"},
                        {"id": "text-embedding-3-small", "object": "model"},
                    ],
                },
            )
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        payload = self._request_json()
        if self.path.rstrip("/") == "/v1/embeddings":
            values = payload.get("input", [])
            inputs = values if isinstance(values, list) else [values]
            self._json(
                200,
                {
                    "object": "list",
                    "model": payload.get("model") or "text-embedding-3-small",
                    "data": [
                        {
                            "object": "embedding",
                            "index": index,
                            "embedding": self._embedding(str(value)),
                        }
                        for index, value in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return

        messages = payload.get("messages") or []
        prompt = "\n".join(str(message.get("content") or "") for message in messages)
        response_format = payload.get("response_format") or {}
        schema = response_format.get("json_schema") or {}
        schema_name = str(schema.get("name") or "")
        if payload.get("logprobs"):
            self._json(200, self._chat_response("True", with_logprobs=True))
            return

        structured = self._structured_response(schema_name, prompt, schema.get("schema") or {})
        self._json(200, self._chat_response(json.dumps(structured)))

    @staticmethod
    def _embedding(value: str) -> list[float]:
        # Non-zero and deterministic. A shared anchor keeps this tiny acceptance corpus
        # mutually searchable; hashed token slots retain stable query-specific variation.
        vector = [0.0] * 1536
        vector[0] = 1.0
        for token in re.findall(r"[a-z0-9]+", value.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vector[1 + int.from_bytes(digest[:2], "big") % 1535] += 0.05
        return vector

    @staticmethod
    def _facts(prompt: str) -> tuple[str, list[dict[str, Any]]]:
        correction = f"{REFUND_CORRECTED_AMOUNT} dollars now" in prompt
        threshold = (
            f"The Atlas Lantern refund approval threshold is {REFUND_CORRECTED_AMOUNT} "
            f"dollars now; {REFUND_ORIGINAL_AMOUNT} dollars is the historical value."
            if correction
            else f"The Atlas Lantern refund approval threshold is "
            f"{REFUND_ORIGINAL_AMOUNT} dollars."
        )
        edges = [
            {
                "source_entity_name": "Atlas Lantern service",
                "target_entity_name": "Stripe",
                "relation_type": "USES",
                "fact": "The Atlas Lantern service uses Stripe.",
                "episode_indices": [0],
            },
            {
                "source_entity_name": "Atlas Lantern service",
                "target_entity_name": "refund approval threshold",
                "relation_type": "HAS_REFUND_APPROVAL_THRESHOLD",
                "fact": threshold,
                "episode_indices": [0],
            },
        ]
        return threshold, edges

    @classmethod
    def _structured_response(
        cls, schema_name: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        threshold, edges = cls._facts(prompt)
        entities = [
            {"name": "Atlas Lantern service", "entity_type_id": 0},
            {"name": "Stripe", "entity_type_id": 0},
            {"name": "refund approval threshold", "entity_type_id": 0},
        ]
        if schema_name in {"CombinedExtraction", "PatchedCombinedExtraction"}:
            return {"extracted_entities": entities, "edges": edges}
        if schema_name == "ExtractedEntities":
            return {
                "extracted_entities": [
                    {**entity, "episode_indices": [0]} for entity in entities
                ]
            }
        if schema_name == "ExtractedEdges":
            return {
                "edges": [
                    {**edge, "valid_at": None, "invalid_at": None}
                    for edge in edges
                ]
            }
        if schema_name == "BatchEdgeTimestamps":
            return {
                "timestamps": [
                    {"valid_at": None, "invalid_at": None} for _edge in edges
                ]
            }
        if schema_name == "EdgeTimestamps":
            return {"valid_at": None, "invalid_at": None}
        if schema_name == "NodeResolutions":
            return {"entity_resolutions": []}
        if schema_name == "EdgeDuplicate":
            return {
                "duplicate_facts": [],
                "contradicted_facts": [0] if f"{REFUND_CORRECTED_AMOUNT} dollars now" in prompt else [],
            }
        if schema_name == "SummarizedEntities":
            return {
                "summaries": [
                    {
                        "name": entity["name"],
                        "summary": (
                            threshold
                            if entity["name"] != "Stripe"
                            else "Stripe is used by the Atlas Lantern service."
                        ),
                    }
                    for entity in entities
                ]
            }
        if schema_name in {"Summary", "EntitySummary"}:
            return {"summary": threshold}
        return cls._schema_defaults(schema)

    @classmethod
    def _schema_defaults(
        cls, schema: dict[str, Any], root: dict[str, Any] | None = None
    ) -> Any:
        root = root or schema
        if "$ref" in schema:
            target: Any = root
            for part in str(schema["$ref"]).removeprefix("#/").split("/"):
                target = target[part]
            return cls._schema_defaults(target, root)
        if "anyOf" in schema:
            non_null = [item for item in schema["anyOf"] if item.get("type") != "null"]
            return cls._schema_defaults(non_null[0], root) if non_null else None
        schema_type = schema.get("type")
        if schema_type == "object" or "properties" in schema:
            return {
                name: cls._schema_defaults(field, root)
                for name, field in (schema.get("properties") or {}).items()
            }
        if schema_type == "array":
            return []
        if schema_type == "integer":
            return 0
        if schema_type == "number":
            return 0.0
        if schema_type == "boolean":
            return False
        if schema_type == "null":
            return None
        return ""

    @staticmethod
    def _chat_response(content: str, *, with_logprobs: bool = False) -> dict[str, Any]:
        choice: dict[str, Any] = {
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
            "logprobs": None,
        }
        if with_logprobs:
            choice["logprobs"] = {
                "content": [
                    {
                        "token": "True",
                        "logprob": -0.01,
                        "bytes": None,
                        "top_logprobs": [
                            {"token": "True", "logprob": -0.01, "bytes": None},
                            {"token": "False", "logprob": -4.6, "bytes": None},
                        ],
                    }
                ]
            }
        return {
            "id": "chatcmpl-deterministic",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "deterministic-chat",
            "choices": [choice],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    def _request_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8"))

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
def failing_provider() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FailingProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@contextmanager
def deterministic_provider() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DeterministicProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
