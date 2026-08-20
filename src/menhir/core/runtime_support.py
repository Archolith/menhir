"""Shared runtime state and helper utilities."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

from menhir.config import MemorySettings, redact_uri_credentials
from menhir.core.reader_identity import normalize_reader_id
from menhir.core.runtime_preflight import RuntimeCapabilities
from menhir.domain.bootstrap_scope import bootstrap_selection
from menhir.infrastructure.llama_endpoint import should_use_scheduler
from menhir.infrastructure.providers import ProviderConfig
from menhir.services import MaintenanceScheduler

if TYPE_CHECKING:
    from menhir.core.bootstrap import BuildArtifacts
    from menhir.domain.session import MemorySession


@dataclass
class RuntimeState:
    """Typed runtime state container."""

    built: object | None = None
    session: object | None = None
    scheduler: MaintenanceScheduler | None = None
    init_task: asyncio.Task[tuple[object, object]] | None = None
    startup_runtime_task: asyncio.Task[None] | None = None
    orphan_recovery_task: asyncio.Task[None] | None = None
    shutdown_task: asyncio.Task[None] | None = None
    flagged_bootstrap_reads: dict[str, str] = field(default_factory=dict)
    _flagged_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    capabilities: RuntimeCapabilities | None = None

    def clear_all(self) -> None:
        self.built = None
        self.session = None
        self.scheduler = None
        self.init_task = None
        self.startup_runtime_task = None
        self.orphan_recovery_task = None
        self.shutdown_task = None
        with self._flagged_lock:
            self.flagged_bootstrap_reads = {}
        self.capabilities = None

    def __getitem__(self, key: str) -> object:
        if not hasattr(self, key):
            raise KeyError(key)
        value = getattr(self, key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: object) -> None:
        if not hasattr(self, key):
            raise KeyError(key)
        setattr(self, key, value)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key) and getattr(self, key) is not None

    def get(self, key: str, default: object = None) -> object:
        if not hasattr(self, key):
            return default
        value = getattr(self, key)
        return default if value is None else value

    def clear(self) -> None:
        self.clear_all()

    def items(self) -> list[tuple[str, object]]:
        return [(f.name, getattr(self, f.name)) for f in fields(self)]

    def __iter__(self):
        return iter(self.items())

    def update(self, values: object) -> None:
        items = values.items() if hasattr(values, "items") else values
        for key, value in items:
            self[key] = value


@dataclass
class RuntimeContext:
    """Holds all runtime state for a menhir process."""

    built: BuildArtifacts
    session: MemorySession
    scheduler: MaintenanceScheduler | None = None
    capabilities: RuntimeCapabilities | None = None


_state = RuntimeState()
_init_lock = asyncio.Lock()


def _graphiti_scheduler_probe_urls(settings: object) -> list[str]:
    required = (
        "graphiti_provider",
        "graphiti_embed_provider",
        "graphiti_reranker_provider",
        "chat_provider",
        "local_llm_base_url",
        "local_llm_api_key",
        "local_llm_chat_model",
        "local_llm_embed_model",
        "local_llm_embed_base_url",
        "openai_api_key",
        "openai_chat_model",
        "openai_embed_model",
        "gemini_base_url",
        "gemini_api_key",
        "gemini_chat_model",
    )
    if all(hasattr(settings, attr) for attr in required):
        providers = (
            ProviderConfig.for_graphiti_llm(settings),
            ProviderConfig.for_graphiti_embedder(settings),
            ProviderConfig.for_graphiti_reranker(settings),
        )
        return [provider.base_url for provider in providers if provider.base_url]

    fallback_base_url = getattr(settings, "local_llm_base_url", "")
    return [fallback_base_url] if fallback_base_url else []


def _uses_scheduler_managed_graphiti(settings: object) -> bool:
    return any(should_use_scheduler(base_url) for base_url in _graphiti_scheduler_probe_urls(settings))


def _annotate_runtime_failures(failures: list[str], settings: MemorySettings) -> list[str]:
    annotated: list[str] = []
    for failure in failures:
        if failure == "Neo4j connectivity check failed.":
            annotated.append(
                f"{failure} Expected {redact_uri_credentials(settings.neo4j_uri)}. "
                "Start Docker Desktop and the yawn-neo4j container."
            )
            continue
        annotated.append(failure)
    return annotated


def _bootstrap_receipt_key(reader_id: str, workspace: str | None) -> str:
    normalized_reader_id = normalize_reader_id(reader_id)
    selection_key, _ = bootstrap_selection(workspace)
    return f"{normalized_reader_id}|{selection_key}"


def _remember_flagged_bootstrap_read(
    reader_id: str, flagged_version: str, workspace: str | None = None
) -> None:
    receipt_key = _bootstrap_receipt_key(reader_id, workspace)
    with _state._flagged_lock:
        _state.flagged_bootstrap_reads[receipt_key] = str(flagged_version)


def _has_recent_flagged_bootstrap_read(
    reader_id: str, flagged_version: str, workspace: str | None = None
) -> bool:
    receipt_key = _bootstrap_receipt_key(reader_id, workspace)
    with _state._flagged_lock:
        last_seen_version = _state.flagged_bootstrap_reads.get(receipt_key)
    return str(last_seen_version or "") == str(flagged_version)
