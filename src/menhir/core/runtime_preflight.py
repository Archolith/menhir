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
    failures: tuple[str, ...] = field(default_factory=tuple)
    #: Outcome of the free GET /v1/models credential probe for a cloud provider:
    #: "verified" (200), "rejected" (401/403), "unverified" (network/other -- startup
    #: proceeds), or "n/a" when no cloud provider is configured.
    cloud_credential: str = "n/a"
    #: Neo4j probe outcome: "ok", "unauthorized" (server reached, credentials refused),
    #: "unreachable" (no server answered), or "unknown".
    neo4j_status: str = "unknown"

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


_TRUTHY = {"1", "true", "yes", "on"}


def venv_guard_applies() -> bool:
    """Return whether the project-venv interpreter guard should be enforced.

    The guard exists for the source-checkout case: a stray global interpreter picked up
    instead of the project ``.venv``. It is only meaningful when that ``.venv`` exists.
    A pip, pipx, or container install has no project ``.venv`` next to the package, so the
    guard is skipped there and ``check_graphiti_dependency`` carries the real concern.
    ``MENHIR_ALLOW_SYSTEM_PYTHON=1`` remains an explicit opt-out for a checkout that does
    carry a ``.venv`` but is deliberately run from another interpreter.
    """

    if os.getenv("MENHIR_ALLOW_SYSTEM_PYTHON", "").strip().lower() in _TRUTHY:
        return False
    return expected_venv_python().exists()


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


NEO4J_OK = "ok"
NEO4J_UNAUTHORIZED = "unauthorized"
NEO4J_UNREACHABLE = "unreachable"
NEO4J_UNKNOWN = "unknown"

#: Outcome of the most recent :func:`probe_neo4j` in this process. `collect_runtime_capabilities`
#: reads it instead of opening a second connection to learn *why* a check failed.
_last_neo4j_status: str = NEO4J_UNKNOWN


def probe_neo4j(uri: str, user: str, password: str) -> str:
    """Return ``ok`` / ``unauthorized`` / ``unreachable`` for one connection attempt.

    The distinction matters for a first run: an unreachable server is worth waiting for (it
    may still be booting), a refused password never becomes right by waiting.
    """

    global _last_neo4j_status
    logger.info("Checking Neo4j at %s ...", uri)
    if _NEO4J_IMPORT_ERROR is not None:
        logger.error("Neo4j connectivity failed: %s", _NEO4J_IMPORT_ERROR)
        _last_neo4j_status = NEO4J_UNREACHABLE
        return NEO4J_UNREACHABLE
    driver = None
    try:
        from menhir.infrastructure.neo4j import DRIVER_NOTIFICATION_CONFIG

        driver = GraphDatabase.driver(uri, auth=(user, password), **DRIVER_NOTIFICATION_CONFIG)
        with driver.session() as session:
            result = session.run("RETURN 1 AS ok").single()
        if not result or result.get("ok") != 1:
            logger.error("Neo4j response invalid: %s", result)
            _last_neo4j_status = NEO4J_UNREACHABLE
            return NEO4J_UNREACHABLE
        logger.info("Neo4j connectivity verified.")
        _last_neo4j_status = NEO4J_OK
        return NEO4J_OK
    except Exception as exc:  # pragma: no cover - external dependency behavior
        code = str(getattr(exc, "code", "") or "")
        if "Security.Unauthorized" in code or "AuthError" in type(exc).__name__:
            logger.error("Neo4j rejected the configured credentials: %s", exc)
            _last_neo4j_status = NEO4J_UNAUTHORIZED
            return NEO4J_UNAUTHORIZED
        logger.error("Neo4j connectivity failed: %s", exc)
        _last_neo4j_status = NEO4J_UNREACHABLE
        return NEO4J_UNREACHABLE
    finally:
        if driver is not None:
            driver.close()


def check_neo4j_connectivity(uri: str, user: str, password: str) -> bool:
    """Validate Neo4j reachable and queryable (one connection; see :func:`probe_neo4j`)."""

    return probe_neo4j(uri, user, password) == NEO4J_OK


def check_llama_connectivity(
    base_url: str,
    api_key: str,
    chat_model: str,
    embed_model: str,
) -> bool:
    """Validate an OpenAI-compatible endpoint and, where authoritative, its model list.

    ``GET /models`` is authoritative for a local server (llama.cpp, Ollama, LM Studio, vLLM):
    a model it does not list cannot be served, so a typo is caught here. Hosted gateways
    reachable at a non-loopback URL (OpenRouter, a proxy) often do not enumerate everything
    they route -- OpenRouter serves ``openai/text-embedding-3-small`` without listing it -- so
    there a missing name is a warning and the first real call is the verification.
    """

    import time as _time

    logger.info("Checking OpenAI-compatible endpoint at %s ...", base_url)
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

            if missing and _should_bypass_local_auth(base_url):
                logger.error("Local endpoint does not list required model(s): %s", ", ".join(missing))
                return False
            if missing:
                logger.warning(
                    "Endpoint %s does not list %s at GET /models; hosted gateways often omit models "
                    "they still serve, so this is verified on first call.",
                    base_url, ", ".join(missing),
                )
            else:
                logger.info("Endpoint connectivity and model checks passed.")
            return True

        except HTTPError as exc:
            if exc.code == 503 and _time.monotonic() < deadline:
                logger.info("Endpoint model still loading (503), retrying in %ds...", _STARTUP_POLL_INTERVAL)
                _time.sleep(_STARTUP_POLL_INTERVAL)
                continue
            logger.error("Endpoint connectivity failed: %s", exc)
            return False
        except (URLError, ValueError, json.JSONDecodeError) as exc:
            logger.error("Endpoint connectivity failed: %s", exc)
            return False


CREDENTIAL_VERIFIED = "verified"
CREDENTIAL_REJECTED = "rejected"
CREDENTIAL_UNVERIFIED = "unverified"


def probe_openai_credential(
    *,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Ask the provider whether ``api_key`` is accepted, using the free ``GET /models`` call.

    Bills no tokens. Three outcomes, so a network problem never masquerades as a bad key:
    ``verified`` (2xx), ``rejected`` (401/403 -- the key itself is wrong), ``unverified``
    (timeout, DNS, TLS, 5xx, 429 -- nothing is known; startup proceeds as before).
    """

    if not api_key.strip():
        return CREDENTIAL_REJECTED
    request = Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            status = getattr(response, "status", 200)
        return CREDENTIAL_VERIFIED if 200 <= int(status) < 300 else CREDENTIAL_UNVERIFIED
    except HTTPError as exc:
        if exc.code in (401, 403):
            return CREDENTIAL_REJECTED
        logger.warning("Cloud credential probe inconclusive (HTTP %s); continuing.", exc.code)
        return CREDENTIAL_UNVERIFIED
    except (URLError, OSError, ValueError) as exc:
        logger.warning("Cloud credential probe inconclusive (%s); continuing.", exc)
        return CREDENTIAL_UNVERIFIED


def check_openai_provider_configuration(
    *,
    api_key: str,
    chat_model: str,
    embed_model: str,
    credential_status: str | None = None,
) -> bool:
    """Validate cloud OpenAI provider configuration.

    ``credential_status`` is the shared result of :func:`probe_openai_credential`. Only a
    definite rejection fails the check: a probe that could not reach the provider (blocked
    outbound sockets, a slow network) leaves startup exactly as permissive as before, and
    real request failures are still handled at call time.
    """

    if not api_key.strip():
        logger.error("OpenAI provider is configured but OPENAI_API_KEY is missing.")
        return False
    if not chat_model.strip() and not embed_model.strip():
        logger.error("OpenAI provider is configured but no chat or embedding model is set.")
        return False
    if credential_status == CREDENTIAL_REJECTED:
        logger.error("OpenAI rejected OPENAI_API_KEY (401/403) on GET /models.")
        return False
    logger.info(
        "OpenAI provider configuration %s for startup (chat=%s, embed=%s).",
        "verified" if credential_status == CREDENTIAL_VERIFIED else "accepted (credential not verified)",
        chat_model or "(none)",
        embed_model or "(none)",
    )
    return True


def collect_runtime_failures(
    settings: MemorySettings,
    *,
    require_venv: bool | None = None,
) -> list[str]:
    """Run runtime preflight checks and return human-readable failure messages.

    ``require_venv=None`` resolves via :func:`venv_guard_applies`.
    """

    return list(
        collect_runtime_capabilities(
            settings,
            require_venv=require_venv,
        ).failures
    )


def collect_runtime_capabilities(
    settings: MemorySettings,
    *,
    require_venv: bool | None = None,
) -> RuntimeCapabilities:
    """Run runtime preflight checks and return a capability snapshot.

    ``require_venv=None`` resolves via :func:`venv_guard_applies`, so ``serve`` and
    ``check`` make the same decision without each re-deriving it.
    """

    failures: list[str] = []

    if require_venv is None:
        require_venv = venv_guard_applies()
    venv_ready = True
    if require_venv:
        venv_ready = check_expected_python_runtime()
        if not venv_ready:
            failures.append("menhir must run from the project .venv interpreter.")

    graphiti_dependency_ready = check_graphiti_dependency()
    if not graphiti_dependency_ready:
        failures.append("graphiti_core is not installed in the active interpreter.")

    global _last_neo4j_status
    _last_neo4j_status = NEO4J_UNKNOWN
    neo4j_ready = check_neo4j_connectivity(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
    )
    # One connection only: the probe behind check_neo4j_connectivity recorded why it failed.
    # A substituted check (tests) leaves this "unknown", which renders as the generic message.
    neo4j_status = NEO4J_OK if neo4j_ready else _last_neo4j_status
    if not neo4j_ready and neo4j_status == NEO4J_UNAUTHORIZED:
        failures.append("Neo4j rejected NEO4J_USER/NEO4J_PASSWORD (authentication failure).")
    elif not neo4j_ready:
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
                    # CF-173: the `serve` guard ran this identical six-scan sweep moments ago
                    # against the same graph. Share its result rather than paying for it twice.
                    use_cache=True,
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
            failures=tuple(failures),
            neo4j_status=neo4j_status,
        )

    llama_base_url = (graphiti_llm.base_url or "").strip()
    embed_base_url = (graphiti_embed.base_url or "").strip()
    cloud_credential = "n/a"
    cloud_provider = next(
        (p for p in (graphiti_llm, graphiti_embed, graphiti_reranker) if p.kind is ProviderKind.OPENAI),
        None,
    )
    if cloud_provider is not None:
        cloud_credential = probe_openai_credential(
            api_key=cloud_provider.api_key, base_url=cloud_provider.base_url
        )
    if graphiti_llm.kind is ProviderKind.OPENAI:
        graphiti_llm_ready = check_openai_provider_configuration(
            api_key=graphiti_llm.api_key,
            chat_model=graphiti_llm.chat_model,
            embed_model="" if embed_base_url != llama_base_url else graphiti_embed.embed_model,
            credential_status=cloud_credential,
        )
    else:
        graphiti_llm_ready = check_llama_connectivity(
            base_url=llama_base_url,
            api_key=graphiti_llm.api_key,
            chat_model=graphiti_llm.chat_model,
            embed_model="" if embed_base_url != llama_base_url else graphiti_embed.embed_model,
        )
    if not graphiti_llm_ready and cloud_credential == CREDENTIAL_REJECTED:
        failures.append("OpenAI rejected OPENAI_API_KEY (401/403) on GET /v1/models; set a valid key.")
    elif not graphiti_llm_ready:
        failures.append(
            "Graphiti extraction connectivity/model check failed "
            f"(provider={graphiti_llm.kind.value}, "
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
                credential_status=cloud_credential,
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
    reranker_base_url = (graphiti_reranker.base_url or "").strip()
    reranker_ready = True
    if reranker_base_url != llama_base_url:
        if graphiti_reranker.kind is ProviderKind.OPENAI:
            reranker_ready = check_openai_provider_configuration(
                api_key=graphiti_reranker.api_key,
                chat_model=graphiti_reranker.chat_model,
                embed_model="",
                credential_status=cloud_credential,
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
        failures=tuple(failures),
        cloud_credential=cloud_credential,
        neo4j_status=neo4j_status,
    )
