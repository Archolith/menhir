"""Reusable runtime preflight checks for menhir."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from menhir.config import MemorySettings
from menhir.infrastructure.embedding_dimensions import (
    embedding_dimension_health,
    expected_graphiti_embedding_dimension,
)
from menhir.infrastructure.llama_endpoint import (
    acquire_llama_url_sync,
    scheduler_url_from_env,
    should_use_scheduler,
)
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.providers import ProviderConfig, ProviderKind

try:
    from neo4j import GraphDatabase
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    GraphDatabase = None  # type: ignore[assignment]
    _NEO4J_IMPORT_ERROR = exc
else:
    _NEO4J_IMPORT_ERROR = None

DEFAULT_TIMEOUT_SECONDS = 5
_STARTUP_POLL_INTERVAL = 3
_STARTUP_WAIT_SECONDS = 90

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Runtime dependency snapshot used for startup and readiness reporting."""

    venv_ready: bool
    graphiti_dependency_ready: bool
    neo4j_ready: bool
    graphiti_llm_ready: bool
    embedder_ready: bool
    reranker_ready: bool
    scheduler_required: bool
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def llm_ready(self) -> bool:
        return self.graphiti_llm_ready

    @property
    def graphiti_ready(self) -> bool:
        return self.neo4j_ready and self.embedder_ready

    @property
    def reads_ready(self) -> bool:
        return self.neo4j_ready and self.embedder_ready

    @property
    def queue_writes_ready(self) -> bool:
        return self.neo4j_ready

    @property
    def enrichment_ready(self) -> bool:
        return self.neo4j_ready and self.embedder_ready and self.graphiti_llm_ready

    @property
    def scheduler_ready(self) -> bool:
        return (not self.scheduler_required) or self.enrichment_ready

    @property
    def startup_mode(self) -> str:
        if self.enrichment_ready:
            return "full"
        if self.reads_ready:
            return "degraded_reads_only"
        if self.queue_writes_ready:
            return "degraded_queue_only"
        return "unavailable"

    @property
    def is_strictly_startable(self) -> bool:
        return not self.failures


def _should_bypass_local_auth(base_url: str) -> bool:
    normalized = (base_url or "").strip().lower()
    return normalized.startswith("http://127.0.0.1") or normalized.startswith("http://localhost")


def expected_venv_python() -> Path:
    """Return the canonical interpreter path for the project virtual environment."""

    repo_root = Path(__file__).resolve().parents[3]
    if os.name == "nt":
        return repo_root / ".venv" / "Scripts" / "python.exe"
    return repo_root / ".venv" / "bin" / "python"


def check_expected_python_runtime(executable: str | None = None) -> bool:
    """Require the MCP server to run from the project virtualenv interpreter."""

    active = Path(executable or sys.executable).resolve()
    expected = expected_venv_python().resolve()
    if active != expected:
        logger.error(
            "Expected menhir MCP to run from %s but active interpreter is %s",
            expected,
            active,
        )
        return False
    return True


def check_graphiti_dependency() -> bool:
    """Validate that graphiti_core is importable in the active interpreter."""

    if importlib.util.find_spec("graphiti_core") is None:
        logger.error(
            "graphiti_core is not installed in the active interpreter: %s",
            Path(sys.executable).resolve(),
        )
        return False
    return True


def check_neo4j_connectivity(uri: str, user: str, password: str) -> bool:
    """Validate Neo4j reachable and queryable."""

    logger.info("Checking Neo4j at %s ...", uri)
    driver = None
    if _NEO4J_IMPORT_ERROR is not None:
        logger.error("Neo4j connectivity failed: %s", _NEO4J_IMPORT_ERROR)
        return False
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session() as session:
            result = session.run("RETURN 1 AS ok").single()
            if not result or result.get("ok") != 1:
                logger.error("Neo4j response invalid: %s", result)
                return False
        logger.info("Neo4j connectivity verified.")
        return True
    except Exception as exc:  # pragma: no cover - external dependency behavior
        logger.error("Neo4j connectivity failed: %s", exc)
        return False
    finally:
        if driver is not None:
            driver.close()


def check_llama_connectivity(
    base_url: str,
    api_key: str,
    chat_model: str,
    embed_model: str,
) -> bool:
    """Validate llama.cpp OpenAI-compatible API and required models."""

    import time as _time

    logger.info("Checking llama.cpp endpoint at %s ...", base_url)
    headers = {"Content-Type": "application/json"}
    if api_key and not _should_bypass_local_auth(base_url):
        headers["Authorization"] = f"Bearer {api_key}"
    models_url = base_url.rstrip("/") + "/models"
    deadline = _time.monotonic() + _STARTUP_WAIT_SECONDS

    while True:
        try:
            request = Request(models_url, headers=headers, method="GET")
            with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))

            available = {
                str(item.get("id"))
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            }

            missing = []
            for required in (chat_model, embed_model):
                if required and required not in available:
                    missing.append(required)

            if missing:
                logger.error("llama.cpp endpoint missing required model(s): %s", ", ".join(missing))
                return False

            logger.info("llama.cpp connectivity and model checks passed.")
            return True

        except HTTPError as exc:
            if exc.code == 503 and _time.monotonic() < deadline:
                logger.info("llama.cpp model still loading (503), retrying in %ds...", _STARTUP_POLL_INTERVAL)
                _time.sleep(_STARTUP_POLL_INTERVAL)
                continue
            logger.error("llama.cpp connectivity failed: %s", exc)
            return False
        except (URLError, ValueError, json.JSONDecodeError) as exc:
            logger.error("llama.cpp connectivity failed: %s", exc)
            return False


def check_openai_provider_configuration(
    *,
    api_key: str,
    chat_model: str,
    embed_model: str,
) -> bool:
    """Validate cloud OpenAI provider configuration without requiring a startup probe.

    Startup should not hard-fail or degrade just because a cloud provider blocks
    a `/models` probe, is momentarily slow, or the current runtime environment
    restricts outbound sockets. Actual request failures are handled at call time.
    """

    if not api_key.strip():
        logger.error("OpenAI provider is configured but OPENAI_API_KEY is missing.")
        return False
    if not chat_model.strip() and not embed_model.strip():
        logger.error("OpenAI provider is configured but no chat or embedding model is set.")
        return False
    logger.info(
        "OpenAI provider configuration verified for startup (chat=%s, embed=%s).",
        chat_model or "(none)",
        embed_model or "(none)",
    )
    return True


def _resolve_connectivity_base_url(
    fallback_base_url: str,
    *,
    task: str | None = None,
    acquire_scheduler_endpoint: bool,
) -> str:
    normalized_fallback = (fallback_base_url or "").strip()
    if not should_use_scheduler(normalized_fallback):
        return normalized_fallback
    if not acquire_scheduler_endpoint:
        return normalized_fallback
    return acquire_llama_url_sync(
        fallback=normalized_fallback,
        task=task,
        timeout_s=120.0,
    )


def collect_runtime_failures(
    settings: MemorySettings,
    *,
    require_venv: bool = False,
    acquire_scheduler_endpoints: bool = False,
) -> list[str]:
    """Run runtime preflight checks and return human-readable failure messages."""

    return list(
        collect_runtime_capabilities(
            settings,
            require_venv=require_venv,
            acquire_scheduler_endpoints=acquire_scheduler_endpoints,
        ).failures
    )


def collect_runtime_capabilities(
    settings: MemorySettings,
    *,
    require_venv: bool = False,
    acquire_scheduler_endpoints: bool = False,
) -> RuntimeCapabilities:
    """Run runtime preflight checks and return a capability snapshot."""

    failures: list[str] = []

    venv_ready = True
    if require_venv:
        venv_ready = check_expected_python_runtime()
        if not venv_ready:
            failures.append("menhir must run from the project .venv interpreter.")

    graphiti_dependency_ready = check_graphiti_dependency()
    if not graphiti_dependency_ready:
        failures.append("graphiti_core is not installed in the active interpreter.")

    neo4j_ready = check_neo4j_connectivity(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
    )
    if not neo4j_ready:
        failures.append("Neo4j connectivity check failed.")
    else:
        expected_dim = expected_graphiti_embedding_dimension(settings)
        if expected_dim is not None:
            repo = Neo4jRepository(
                uri=settings.neo4j_uri,
                database=settings.neo4j_database,
                user=settings.neo4j_user,
                password=settings.neo4j_password,
            )
            health: dict[str, object] | None = None
            try:
                health = embedding_dimension_health(
                    neo4j=repo,
                    expected_dim=expected_dim,
                )
            except Exception as exc:
                logger.warning("Embedding dimension compatibility check failed: %s", exc)
            finally:
                repo.close()
            if health is not None:
                if not health["ok"]:
                    failures.append(
                        "Stored graph embeddings do not match the current embedder "
                        f"(expected_dim={expected_dim}, wrong_entities={health['wrong_entity_count']}, "
                        f"wrong_communities={health['wrong_community_count']}, wrong_relationships={health['wrong_edge_count']}). "
                        f"Missing vectors: entities={health.get('null_entity_count', 0)}, "
                        f"communities={health.get('null_community_count', 0)}, "
                        f"relationships={health.get('null_edge_count', 0)}; "
                        f"mixed_dimensions={health.get('mixed', False)}. "
                        "Run scripts/repair_embedding_dimensions.py --apply before relying on semantic retrieval."
                    )

    graphiti_llm = ProviderConfig.for_graphiti_llm(settings)
    graphiti_embed = ProviderConfig.for_graphiti_embedder(settings)
    graphiti_reranker = ProviderConfig.for_graphiti_reranker(settings)
    scheduler_required = any(
        should_use_scheduler(base_url)
        for base_url in (graphiti_llm.base_url, graphiti_embed.base_url, graphiti_reranker.base_url)
        if base_url
    )

    preflight_len = len(failures)
    if not graphiti_llm.supports_graphiti_openai_contract():
        failures.append("Graphiti extraction provider must be openai or openai_compat.")
    if not graphiti_embed.supports_graphiti_openai_contract():
        failures.append("Graphiti embed provider must be openai or openai_compat.")
    if not graphiti_reranker.supports_graphiti_openai_contract():
        failures.append("Graphiti reranker provider must be openai or openai_compat.")
    if len(failures) > preflight_len:
        return RuntimeCapabilities(
            venv_ready=venv_ready,
            graphiti_dependency_ready=graphiti_dependency_ready,
            neo4j_ready=neo4j_ready,
            graphiti_llm_ready=False,
            embedder_ready=False,
            reranker_ready=False,
            scheduler_required=scheduler_required,
            failures=tuple(failures),
        )

    fallback_base_url = graphiti_llm.base_url
    llama_base_url = _resolve_connectivity_base_url(
        fallback_base_url,
        acquire_scheduler_endpoint=acquire_scheduler_endpoints,
    )
    embed_fallback_base_url = graphiti_embed.base_url
    embed_base_url = _resolve_connectivity_base_url(
        embed_fallback_base_url,
        task="memory: graphiti embed bootstrap",
        acquire_scheduler_endpoint=acquire_scheduler_endpoints,
    )
    if graphiti_llm.kind is ProviderKind.OPENAI:
        graphiti_llm_ready = check_openai_provider_configuration(
            api_key=graphiti_llm.api_key,
            chat_model=graphiti_llm.chat_model,
            embed_model="" if embed_base_url != llama_base_url else graphiti_embed.embed_model,
        )
    else:
        graphiti_llm_ready = check_llama_connectivity(
            base_url=llama_base_url,
            api_key=graphiti_llm.api_key,
            chat_model=graphiti_llm.chat_model,
            embed_model="" if embed_base_url != llama_base_url else graphiti_embed.embed_model,
        )
    if not graphiti_llm_ready:
        failures.append(
            "Graphiti extraction connectivity/model check failed "
            f"(provider={graphiti_llm.kind.value}, scheduler={scheduler_url_from_env()}, "
            f"base_url={llama_base_url}, chat={graphiti_llm.chat_model}, "
            f"embed={graphiti_embed.embed_model})."
        )
    # NOTE: embedder_ready must never be optimistically True. A value meaning
    # "checked and healthy" must only be produced by a path that actually
    # performed a check; the old optimistic default reported ready with no check.
    if embed_base_url != llama_base_url:
        if graphiti_embed.kind is ProviderKind.OPENAI:
            embedder_ready = check_openai_provider_configuration(
                api_key=graphiti_embed.api_key,
                chat_model="",
                embed_model=graphiti_embed.embed_model,
            )
        else:
            embedder_ready = check_llama_connectivity(
                base_url=embed_base_url,
                api_key=graphiti_embed.api_key,
                chat_model="",
                embed_model=graphiti_embed.embed_model,
            )
    elif graphiti_embed.embed_model:
        embedder_ready = graphiti_llm_ready
    else:
        # No embed model is configured (`local_llm_embed_model` defaults to ""), so there is
        # nothing to check and nothing to connect to. Report NOT ready -- that is the honest
        # answer and it is what `reads_ready` should consume, since semantic reads cannot work
        # without an embedder.
        #
        # Deliberately NOT a `failures` entry. An unconfigured embedder is a deployment choice,
        # not a broken check, and `failures` gates startup: appending here would refuse to start
        # for configurations that run today. The defect in CF-138 was a flag that meant "checked
        # and healthy" being produced by a path that checked nothing -- not the absence of a
        # configured embedder.
        embedder_ready = False
    if not embedder_ready and graphiti_embed.embed_model:
        failures.append(
            "Graphiti embed connectivity/model check failed "
            f"(provider={graphiti_embed.kind.value}, embed_base_url={embed_base_url}, "
            f"embed={graphiti_embed.embed_model})."
        )
    reranker_fallback_base_url = graphiti_reranker.base_url
    reranker_base_url = _resolve_connectivity_base_url(
        reranker_fallback_base_url,
        task="memory: graphiti reranker bootstrap",
        acquire_scheduler_endpoint=acquire_scheduler_endpoints,
    )
    reranker_ready = True
    if reranker_base_url != llama_base_url:
        if graphiti_reranker.kind is ProviderKind.OPENAI:
            reranker_ready = check_openai_provider_configuration(
                api_key=graphiti_reranker.api_key,
                chat_model=graphiti_reranker.chat_model,
                embed_model="",
            )
        else:
            reranker_ready = check_llama_connectivity(
                base_url=reranker_base_url,
                api_key=graphiti_reranker.api_key,
                chat_model=graphiti_reranker.chat_model,
                embed_model="",
            )
    elif graphiti_reranker.chat_model:
        reranker_ready = graphiti_llm_ready
    if not reranker_ready:
        failures.append(
            "Graphiti reranker connectivity/model check failed "
            f"(provider={graphiti_reranker.kind.value}, reranker_base_url={reranker_base_url}, "
            f"model={graphiti_reranker.chat_model})."
        )

    return RuntimeCapabilities(
        venv_ready=venv_ready,
        graphiti_dependency_ready=graphiti_dependency_ready,
        neo4j_ready=neo4j_ready,
        graphiti_llm_ready=graphiti_llm_ready,
        embedder_ready=embedder_ready,
        reranker_ready=reranker_ready,
        scheduler_required=scheduler_required,
        failures=tuple(failures),
    )
