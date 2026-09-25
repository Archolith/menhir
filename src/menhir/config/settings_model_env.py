"""Environment loading for ``MemorySettings``.

Moved verbatim from ``menhir.config.settings_model``: the module-level env
parsing helpers plus the body of ``MemorySettings.from_env``. The facade module
re-exports ``ARTIFACT_RECONCILE_MODES`` and delegates ``from_env`` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .settings_helpers import (
    _getenv,
    _parse_float,
    _parse_int,
    parse_bool_env,
    parse_client_namespaces,
    parse_client_tools,
    parse_csv_env,
)

if TYPE_CHECKING:
    from menhir.config.settings_model import MemorySettings


#: Startup reconciliation modes. An unrecognized value falls back to `audit`
#: rather than raising or silently disabling: a typo in an env var must not turn
#: drift detection off, and must not turn graph writes on.
ARTIFACT_RECONCILE_MODES: frozenset[str] = frozenset({"off", "audit", "safe_apply"})


def _normalize_reconcile_mode(raw: str) -> str:
    mode = (raw or "").strip().lower()
    return mode if mode in ARTIFACT_RECONCILE_MODES else "audit"


def _parse_scalar_threshold(raw: str) -> float:
    """Parse a decimal or ratio such as ``2/3`` without the 0.67 rounding trap."""
    text = str(raw).strip()
    if "/" not in text:
        return _parse_float(text, env_var="MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD")
    numerator, separator, denominator = text.partition("/")
    if not separator or not numerator.strip() or not denominator.strip():
        raise ValueError(
            "MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD must be a decimal or ratio"
        )
    try:
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(
            "MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD must be a decimal or ratio"
        ) from exc


def memory_settings_from_env(cls: type["MemorySettings"]) -> "MemorySettings":
    """Load settings from environment variables."""
    return cls(
        # Neo4j
        neo4j_uri=_getenv("NEO4J_URI", default=cls.neo4j_uri),
        neo4j_database=_getenv("NEO4J_DATABASE", default=cls.neo4j_database),
        neo4j_user=_getenv("NEO4J_USER", default=cls.neo4j_user),
        neo4j_password=_getenv("NEO4J_PASSWORD", default=cls.neo4j_password),
        # Local LLM — LOCAL_LLM_* is canonical; LLAMA_* accepted for backward compat
        local_llm_base_url=_getenv("LOCAL_LLM_BASE_URL", "LLAMA_BASE_URL", default=cls.local_llm_base_url),
        local_llm_api_key=_getenv("LOCAL_LLM_API_KEY", "LLAMA_API_KEY", default=cls.local_llm_api_key),
        local_llm_chat_model=_getenv("LOCAL_LLM_CHAT_MODEL", "LLAMA_CHAT_MODEL", default=cls.local_llm_chat_model),
        local_llm_embed_model=_getenv("LOCAL_LLM_EMBED_MODEL", "LLAMA_EMBED_MODEL", default=cls.local_llm_embed_model),
        local_llm_embed_base_url=_getenv("LOCAL_LLM_EMBED_BASE_URL", "LLAMA_EMBED_BASE_URL", default=cls.local_llm_embed_base_url),
        # OpenAI
        openai_api_key=_getenv("OPENAI_API_KEY", default=cls.openai_api_key),
        openai_chat_model=_getenv("OPENAI_CHAT_MODEL", default=cls.openai_chat_model),
        openai_embed_model=_getenv("OPENAI_EMBED_MODEL", default=cls.openai_embed_model),
        # Provider selection
        chat_provider=_getenv("LLM_CHAT_PROVIDER", "MEMORY_CHAT_PROVIDER", default=cls.chat_provider),
        # GRAPHITI_PROVIDER is accepted as a final alias: it is the intuitive name
        # people reach for, and silently ignoring it (the canonical var is
        # GRAPHITI_LLM_PROVIDER) caused extraction to run on the wrong model.
        # graphiti_embed_provider / graphiti_reranker_provider inherit this when blank.
        graphiti_provider=_getenv("GRAPHITI_LLM_PROVIDER", "MEMORY_GRAPHITI_PROVIDER", "GRAPHITI_PROVIDER", default=cls.graphiti_provider),
        graphiti_embed_provider=_getenv("GRAPHITI_EMBED_PROVIDER", "MEMORY_GRAPHITI_EMBED_PROVIDER", default=cls.graphiti_embed_provider),
        embed_version_override=_getenv("MENHIR_EMBED_VERSION", default=cls.embed_version_override),
        graphiti_reranker_provider=_getenv("GRAPHITI_RERANKER_PROVIDER", "MEMORY_GRAPHITI_RERANKER_PROVIDER", default=cls.graphiti_reranker_provider),
        # Graphiti tuning
        graphiti_add_episode_timeout_seconds=_parse_float(
            _getenv("MEMORY_GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS", "GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS", default=str(cls.graphiti_add_episode_timeout_seconds)),
            env_var="MEMORY_GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS",
        ),
        graphiti_episode_max_estimated_tokens=_parse_int(
            _getenv("MEMORY_GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS", "GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS", default=str(cls.graphiti_episode_max_estimated_tokens)),
            env_var="MEMORY_GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS",
        ),
        graphiti_request_max_estimated_tokens=_parse_int(
            _getenv("MEMORY_GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS", "GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS", default=str(cls.graphiti_request_max_estimated_tokens)),
            env_var="MEMORY_GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS",
        ),
        # LLM generation
        llm_max_tokens=_parse_int(
            _getenv("LLM_MAX_TOKENS", default=str(cls.llm_max_tokens)),
            env_var="LLM_MAX_TOKENS",
        ),
        # Langfuse
        langfuse_host=_getenv("LANGFUSE_HOST", "LANGFUSE_BASE_URL", default=cls.langfuse_host),
        langfuse_public_key=_getenv("LANGFUSE_PUBLIC_KEY", default=cls.langfuse_public_key),
        langfuse_secret_key=_getenv("LANGFUSE_SECRET_KEY", default=cls.langfuse_secret_key),
        # M6 sidecar expansion
        record_detailed_revisions=parse_bool_env(_getenv("MENHIR_RECORD_DETAILED_REVISIONS", default=str(cls.record_detailed_revisions))),
        revision_retention_days=_parse_int(
            _getenv("MENHIR_REVISION_RETENTION_DAYS", default=str(cls.revision_retention_days)),
            env_var="MENHIR_REVISION_RETENTION_DAYS",
        ),
        # M6 LLM budget caps
        max_llm_calls_per_session_window=_parse_int(
            _getenv("MENHIR_MAX_LLM_CALLS_PER_SESSION_WINDOW", default=str(cls.max_llm_calls_per_session_window)),
            env_var="MENHIR_MAX_LLM_CALLS_PER_SESSION_WINDOW",
            minimum=1,
        ),
        llm_session_window_seconds=_parse_int(
            _getenv("MENHIR_LLM_SESSION_WINDOW_SECONDS", default=str(cls.llm_session_window_seconds)),
            env_var="MENHIR_LLM_SESSION_WINDOW_SECONDS",
        ),
        max_llm_calls_per_enrichment_job=_parse_int(
            _getenv("MENHIR_MAX_LLM_CALLS_PER_JOB", default=str(cls.max_llm_calls_per_enrichment_job)),
            env_var="MENHIR_MAX_LLM_CALLS_PER_JOB",
            minimum=1,
        ),
        ingest_concurrency=_parse_int(
            _getenv("MENHIR_INGEST_CONCURRENCY", default=str(cls.ingest_concurrency)),
            env_var="MENHIR_INGEST_CONCURRENCY",
            minimum=1,
        ),
        shadow_context_composition=parse_bool_env(_getenv("MENHIR_SHADOW_CONTEXT_COMPOSITION", default=str(cls.shadow_context_composition))),
        shadow_composition_timeout_s=_parse_float(
            _getenv("MENHIR_SHADOW_COMPOSITION_TIMEOUT_S", default=str(cls.shadow_composition_timeout_s)),
            env_var="MENHIR_SHADOW_COMPOSITION_TIMEOUT_S",
        ),
        # Structure watcher
        structure_watcher_interval_s=_parse_float(
            _getenv("MENHIR_STRUCTURE_WATCHER_INTERVAL_S", default=str(cls.structure_watcher_interval_s)),
            env_var="MENHIR_STRUCTURE_WATCHER_INTERVAL_S",
            minimum=1.0,
        ),
        structure_watcher_enabled=parse_bool_env(_getenv("MENHIR_STRUCTURE_WATCHER_ENABLED", default=str(cls.structure_watcher_enabled))),
        artifact_reconcile_mode=_normalize_reconcile_mode(
            _getenv("MENHIR_ARTIFACT_RECONCILE_MODE", default=cls.artifact_reconcile_mode)
        ),
        artifact_reconcile_repo=_getenv("MENHIR_ARTIFACT_RECONCILE_REPO", default="") or "",
        artifact_reconcile_repository=_getenv(
            "MENHIR_ARTIFACT_RECONCILE_REPOSITORY", default=""
        ) or "",
        experience_counter_enabled=parse_bool_env(_getenv("MENHIR_EXPERIENCE_COUNTER_ENABLED", default=str(cls.experience_counter_enabled))),
        verifier_sync_enabled=parse_bool_env(_getenv("MENHIR_VERIFIER_SYNC_ENABLED", default=str(cls.verifier_sync_enabled))),
        verifier_sync_interval_s=_parse_float(
            _getenv("MENHIR_VERIFIER_SYNC_INTERVAL_S", default=str(cls.verifier_sync_interval_s)),
            env_var="MENHIR_VERIFIER_SYNC_INTERVAL_S",
            minimum=1.0,
        ),
        canonical_self_binding_mode=_getenv("MENHIR_CANONICAL_SELF_BINDING_MODE", default=cls.canonical_self_binding_mode),
        personal_memory_consolidation_enabled=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED", default=str(cls.personal_memory_consolidation_enabled))),
        personal_memory_consolidation_interval_s=_parse_float(
            _getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S", default=str(cls.personal_memory_consolidation_interval_s)),
            env_var="MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S",
            minimum=1.0,
        ),
        personal_memory_consolidation_k=_parse_int(
            _getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_K", default=str(cls.personal_memory_consolidation_k)),
            env_var="MENHIR_PERSONAL_MEMORY_CONSOLIDATION_K",
            minimum=1,
        ),
        personal_memory_consolidation_call_budget=_parse_int(
            _getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_CALL_BUDGET", default=str(cls.personal_memory_consolidation_call_budget)),
            env_var="MENHIR_PERSONAL_MEMORY_CONSOLIDATION_CALL_BUDGET",
            minimum=1,
        ),
        personal_memory_consolidation_max_tokens=_parse_int(
            _getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_MAX_TOKENS", default=str(cls.personal_memory_consolidation_max_tokens)),
            env_var="MENHIR_PERSONAL_MEMORY_CONSOLIDATION_MAX_TOKENS",
        ),
        personal_memory_consolidation_disable_reasoning=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_DISABLE_REASONING", default=str(cls.personal_memory_consolidation_disable_reasoning))),
        personal_memory_consolidation_chat_model=_getenv("MENHIR_PERSONAL_MEMORY_CHAT_MODEL", default=cls.personal_memory_consolidation_chat_model),
        personal_memory_consolidation_verify_retries=int(_getenv("MENHIR_PERSONAL_MEMORY_VERIFY_RETRIES", default=str(cls.personal_memory_consolidation_verify_retries))),
        personal_memory_consolidation_sum_grounding=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_SUM_GROUNDING", default=str(cls.personal_memory_consolidation_sum_grounding))),
        personal_memory_scalar_state_enabled=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED", default=str(cls.personal_memory_scalar_state_enabled))),
        personal_memory_scalar_state_perceiver_version=_getenv("MENHIR_PERSONAL_MEMORY_SCALAR_STATE_PERCEIVER_VERSION", default=cls.personal_memory_scalar_state_perceiver_version),
        personal_memory_scalar_view_authority_enabled=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED", default=str(cls.personal_memory_scalar_view_authority_enabled))),
        personal_memory_scalar_reconcile_attribute=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_ATTRIBUTE", default=str(cls.personal_memory_scalar_reconcile_attribute))),
        personal_memory_scalar_reconcile_scope=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_SCOPE", default=str(cls.personal_memory_scalar_reconcile_scope))),
        personal_memory_scalar_reconcile_subject=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_SUBJECT", default=str(cls.personal_memory_scalar_reconcile_subject))),
        personal_memory_scalar_canonical_self=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_SCALAR_CANONICAL_SELF", default=str(cls.personal_memory_scalar_canonical_self))),
        personal_memory_scalar_threshold=_parse_scalar_threshold(
            _getenv(
                "MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD",
                default=str(cls.personal_memory_scalar_threshold),
            )
        ),
        personal_memory_consolidation_audit_enabled=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED", default=str(cls.personal_memory_consolidation_audit_enabled))),
        personal_memory_recall_audit_enabled=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_RECALL_AUDIT_ENABLED", default=str(cls.personal_memory_recall_audit_enabled))),
        personal_memory_scalar_history_enabled=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED", default=str(cls.personal_memory_scalar_history_enabled))),
        personal_memory_scalar_deterministic_shadow=parse_bool_env(_getenv(
            "MENHIR_SCALAR_DETERMINISTIC_SHADOW",
            default=str(cls.personal_memory_scalar_deterministic_shadow),
        )),
        personal_memory_scalar_deterministic_router=parse_bool_env(_getenv(
            "MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_ROUTER",
            default=str(cls.personal_memory_scalar_deterministic_router),
        )),
        personal_memory_scalar_deterministic_classes=tuple(dict.fromkeys(
            item.strip().lower()
            for item in parse_csv_env(_getenv(
                "MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_CLASSES",
                default="",
            ))
            if item.strip()
        )),
        personal_memory_event_history_enabled=parse_bool_env(_getenv(
            "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED",
            default=str(cls.personal_memory_event_history_enabled),
        )),
        personal_memory_event_history_perceiver_version=_getenv(
            "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_PERCEIVER_VERSION",
            default=cls.personal_memory_event_history_perceiver_version,
        ),
        personal_memory_event_history_authority_enabled=parse_bool_env(_getenv(
            "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED",
            default=str(cls.personal_memory_event_history_authority_enabled),
        )),
        # Benchmark mode (LongMemEval Mode B isolation)
        benchmark_mode=parse_bool_env(_getenv("MENHIR_BENCHMARK_MODE", default=str(cls.benchmark_mode))),
        # Frontier portions (default off)
        frontier_bm25=parse_bool_env(_getenv("MENHIR_FRONTIER_BM25", default=str(cls.frontier_bm25))),
        frontier_content_vector=parse_bool_env(_getenv("MENHIR_FRONTIER_CONTENT_VECTOR", default=str(cls.frontier_content_vector))),
        frontier_content_vector_replace_name=parse_bool_env(_getenv("MENHIR_FRONTIER_CONTENT_VECTOR_REPLACE_NAME", default=str(cls.frontier_content_vector_replace_name))),
        frontier_content_vector_k=_parse_int(
            _getenv("MENHIR_FRONTIER_CONTENT_VECTOR_K", default=str(cls.frontier_content_vector_k)),
            env_var="MENHIR_FRONTIER_CONTENT_VECTOR_K",
            minimum=1,
        ),
        frontier_content_vector_weight=_parse_float(
            _getenv("MENHIR_FRONTIER_CONTENT_VECTOR_WEIGHT", default=str(cls.frontier_content_vector_weight)),
            env_var="MENHIR_FRONTIER_CONTENT_VECTOR_WEIGHT",
        ),
        frontier_fusion_admission_policy=_getenv(
            "MENHIR_FRONTIER_FUSION_ADMISSION_POLICY",
            default=cls.frontier_fusion_admission_policy,
        ).strip().lower(),
        frontier_oracle_ranking=parse_bool_env(_getenv("MENHIR_FRONTIER_ORACLE_RANKING", default=str(cls.frontier_oracle_ranking))),
        frontier_intent_lens=parse_bool_env(_getenv("MENHIR_FRONTIER_INTENT_LENS", default=str(cls.frontier_intent_lens))),
        frontier_warden_gate=parse_bool_env(_getenv("MENHIR_FRONTIER_WARDEN_GATE", default=str(cls.frontier_warden_gate))),
        frontier_diversity_gate=parse_bool_env(_getenv("MENHIR_FRONTIER_DIVERSITY_GATE", default=str(cls.frontier_diversity_gate))),
        frontier_contradiction_interrupt=parse_bool_env(_getenv("MENHIR_FRONTIER_CONTRADICTION_INTERRUPT", default=str(cls.frontier_contradiction_interrupt))),
        frontier_belief_gate=parse_bool_env(_getenv("MENHIR_FRONTIER_BELIEF_GATE", default=str(cls.frontier_belief_gate))),
        frontier_evidence_anchor=parse_bool_env(_getenv("MENHIR_FRONTIER_EVIDENCE_ANCHOR", default=str(cls.frontier_evidence_anchor))),
        frontier_fact_edges=parse_bool_env(_getenv("MENHIR_FRONTIER_FACT_EDGES", default=str(cls.frontier_fact_edges))),
        frontier_fact_edge_mode=_getenv("MENHIR_FRONTIER_FACT_EDGE_MODE", default=cls.frontier_fact_edge_mode).strip().lower(),
        frontier_similarity_scale=_getenv("MENHIR_FRONTIER_SIMILARITY_SCALE", default=cls.frontier_similarity_scale).strip().lower(),
        frontier_shadow=parse_bool_env(_getenv("MENHIR_FRONTIER_SHADOW", default=str(cls.frontier_shadow))),
        frontier_brief_builder=parse_bool_env(_getenv("MENHIR_FRONTIER_BRIEF_BUILDER", default=str(cls.frontier_brief_builder))),
        # HTTP server
        api_host=_getenv("MENHIR_API_HOST", default=cls.api_host),
        api_port=_parse_int(
            _getenv("MENHIR_API_PORT", default=str(cls.api_port)),
            env_var="MENHIR_API_PORT",
        ),
        api_key=_getenv("MENHIR_API_KEY", default=cls.api_key),
        operator_key=_getenv("MENHIR_OPERATOR_KEY", default=cls.operator_key),
        agent_key=_getenv("MENHIR_AGENT_KEY", default=cls.agent_key),
        readonly_key=_getenv("MENHIR_READONLY_KEY", default=cls.readonly_key),
        allow_insecure_remote_no_auth=parse_bool_env(_getenv("MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH", default=str(cls.allow_insecure_remote_no_auth))),
        client_tokens_enabled=parse_bool_env(_getenv("MENHIR_CLIENT_TOKENS_ENABLED", default=str(cls.client_tokens_enabled))),
        telemetry_observability_retention_days=int(
            _getenv("MENHIR_TELEMETRY_OBSERVABILITY_RETENTION_DAYS", default="30") or 30
        ),
        telemetry_diagnostic_retention_days=int(
            _getenv("MENHIR_TELEMETRY_DIAGNOSTIC_RETENTION_DAYS", default="90") or 90
        ),
        client_namespaces=parse_client_namespaces(_getenv("MENHIR_CLIENT_NAMESPACES", default="")),
        client_tools=parse_client_tools(_getenv("MENHIR_CLIENT_TOOLS", default="")),
        known_clients=frozenset(
            part.strip().lower()
            for part in (_getenv("MENHIR_KNOWN_CLIENTS", default="") or "").split(",")
            if part.strip()
        ),
        startup_scope=_getenv("MENHIR_STARTUP_SCOPE", default=cls.startup_scope).strip().lower(),
        runtime_mode=_getenv("MENHIR_RUNTIME_MODE", default=cls.runtime_mode).strip().lower(),
        client_policy_path=_getenv(
            "MENHIR_CLIENT_POLICY_PATH", default=cls.client_policy_path
        ).strip(),
        client_policy_digest=_getenv(
            "MENHIR_CLIENT_POLICY_DIGEST", default=cls.client_policy_digest
        ).strip().lower(),
        cors_origins=parse_csv_env(_getenv("MENHIR_CORS_ORIGINS", default="")),
        instance_id=_getenv("MENHIR_INSTANCE_ID", default=cls.instance_id).strip(),
        release_id=_getenv("MENHIR_RELEASE_ID", default=cls.release_id).strip(),
        source_fence_key_id=_getenv(
            "MENHIR_SOURCE_FENCE_KEY_ID", default=cls.source_fence_key_id
        ).strip(),
        source_fence_token=_getenv(
            "MENHIR_SOURCE_FENCE_TOKEN", default=cls.source_fence_token
        ),
        source_fence_private_key_path=_getenv(
            "MENHIR_SOURCE_FENCE_PRIVATE_KEY_PATH",
            default=cls.source_fence_private_key_path,
        ).strip(),
        explorer_enabled=parse_bool_env(_getenv("MENHIR_EXPLORER_ENABLED", default=str(cls.explorer_enabled))),
        privacy_redact=parse_bool_env(_getenv("MENHIR_PRIVACY_REDACT", default=str(cls.privacy_redact))),
        saga_reconcile_startup_mode=_getenv(
            "MENHIR_SAGA_RECONCILE_STARTUP_MODE",
            default=cls.saga_reconcile_startup_mode,
        ).strip().lower(),
        oauth_enabled=parse_bool_env(_getenv("MENHIR_OAUTH_ENABLED", default=str(cls.oauth_enabled))),
        oauth_public_base_url=_getenv("MENHIR_PUBLIC_BASE_URL", default=cls.oauth_public_base_url).rstrip("/"),
        oauth_resource=_getenv("MENHIR_OAUTH_RESOURCE", "MENHIR_MCP_RESOURCE", default=cls.oauth_resource).strip(),
        oauth_audiences=parse_csv_env(
            _getenv("MENHIR_OAUTH_AUDIENCE", "MENHIR_OAUTH_AUDIENCES", default="")
        ),
        oauth_issuer=_getenv("MENHIR_OAUTH_ISSUER", default=cls.oauth_issuer).strip(),
        oauth_jwks_uri=_getenv("MENHIR_OAUTH_JWKS_URI", default=cls.oauth_jwks_uri).strip(),
        oauth_authorization_servers=parse_csv_env(
            _getenv("MENHIR_AUTHORIZATION_SERVERS", default="")
        ),
        oauth_as_enabled=parse_bool_env(
            _getenv("MENHIR_OAUTH_AS_ENABLED", default=str(cls.oauth_as_enabled))
        ),
        oauth_scopes_supported=parse_csv_env(
            _getenv("MENHIR_OAUTH_SCOPES_SUPPORTED", default=",".join(cls.oauth_scopes_supported))
        ),
        oauth_read_scopes=parse_csv_env(
            _getenv("MENHIR_OAUTH_READ_SCOPES", default=",".join(cls.oauth_read_scopes))
        ),
        oauth_write_scopes=parse_csv_env(
            _getenv("MENHIR_OAUTH_WRITE_SCOPES", default=",".join(cls.oauth_write_scopes))
        ),
        oauth_admin_scopes=parse_csv_env(
            _getenv("MENHIR_OAUTH_ADMIN_SCOPES", default=",".join(cls.oauth_admin_scopes))
        ),
        oauth_jwks_cache_ttl_s=_parse_int(
            _getenv("MENHIR_OAUTH_JWKS_CACHE_TTL_S", default=str(cls.oauth_jwks_cache_ttl_s)),
            env_var="MENHIR_OAUTH_JWKS_CACHE_TTL_S",
        ),
        oauth_http_timeout_s=_parse_float(
            _getenv("MENHIR_OAUTH_HTTP_TIMEOUT_S", default=str(cls.oauth_http_timeout_s)),
            env_var="MENHIR_OAUTH_HTTP_TIMEOUT_S",
        ),
        oauth_clock_skew_s=_parse_int(
            _getenv("MENHIR_OAUTH_CLOCK_SKEW_S", default=str(cls.oauth_clock_skew_s)),
            env_var="MENHIR_OAUTH_CLOCK_SKEW_S",
        ),
        oauth_allowed_algorithms=parse_csv_env(
            _getenv("MENHIR_OAUTH_ALLOWED_ALGORITHMS", default=",".join(cls.oauth_allowed_algorithms))
        ),
        oauth_as_dir=_getenv("MENHIR_OAUTH_AS_DIR", default=cls.oauth_as_dir).strip(),
        oauth_signing_key_path=_getenv(
            "MENHIR_OAUTH_SIGNING_KEY_PATH",
            default=cls.oauth_signing_key_path,
        ).strip(),
        oauth_refresh_retry_keyring_path=_getenv(
            "MENHIR_OAUTH_REFRESH_RETRY_KEYRING_PATH",
            default=cls.oauth_refresh_retry_keyring_path,
        ).strip(),
        oauth_as_code_ttl_s=_parse_float(
            _getenv("MENHIR_OAUTH_AS_CODE_TTL_S", default=str(cls.oauth_as_code_ttl_s)),
            env_var="MENHIR_OAUTH_AS_CODE_TTL_S",
        ),
        oauth_as_access_ttl_s=_parse_int(
            _getenv("MENHIR_OAUTH_AS_ACCESS_TTL_S", default=str(cls.oauth_as_access_ttl_s)),
            env_var="MENHIR_OAUTH_AS_ACCESS_TTL_S",
        ),
        oauth_as_consent_secret=_getenv(
            "MENHIR_OAUTH_AS_CONSENT_SECRET", default=cls.oauth_as_consent_secret
        ),
        oauth_as_consent_ttl_s=_parse_float(
            _getenv("MENHIR_OAUTH_AS_CONSENT_TTL_S", default=str(cls.oauth_as_consent_ttl_s)),
            env_var="MENHIR_OAUTH_AS_CONSENT_TTL_S",
        ),
        oauth_as_session_ttl_s=_parse_float(
            _getenv("MENHIR_OAUTH_AS_SESSION_TTL_S", default=str(cls.oauth_as_session_ttl_s)),
            env_var="MENHIR_OAUTH_AS_SESSION_TTL_S",
        ),
        oauth_as_register_rate=_parse_int(
            _getenv("MENHIR_OAUTH_AS_REGISTER_RATE", default=str(cls.oauth_as_register_rate)),
            env_var="MENHIR_OAUTH_AS_REGISTER_RATE",
        ),
        oauth_as_register_window_s=_parse_int(
            _getenv("MENHIR_OAUTH_AS_REGISTER_WINDOW_S", default=str(cls.oauth_as_register_window_s)),
            env_var="MENHIR_OAUTH_AS_REGISTER_WINDOW_S",
        ),
        oauth_as_approve_rate=_parse_int(
            _getenv("MENHIR_OAUTH_AS_APPROVE_RATE", default=str(cls.oauth_as_approve_rate)),
            env_var="MENHIR_OAUTH_AS_APPROVE_RATE",
        ),
        oauth_as_approve_window_s=_parse_int(
            _getenv("MENHIR_OAUTH_AS_APPROVE_WINDOW_S", default=str(cls.oauth_as_approve_window_s)),
            env_var="MENHIR_OAUTH_AS_APPROVE_WINDOW_S",
        ),
        oauth_as_max_clients=_parse_int(
            _getenv("MENHIR_OAUTH_AS_MAX_CLIENTS", default=str(cls.oauth_as_max_clients)),
            env_var="MENHIR_OAUTH_AS_MAX_CLIENTS",
        ),
        oauth_as_stale_client_max_age_s=_parse_int(
            _getenv(
                "MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S",
                default=str(cls.oauth_as_stale_client_max_age_s),
            ),
            env_var="MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S",
        ),
        oauth_as_refresh_tokens_enabled=parse_bool_env(_getenv(
            "MENHIR_OAUTH_AS_REFRESH_TOKENS_ENABLED",
            default=str(cls.oauth_as_refresh_tokens_enabled),
        )),
        oauth_as_refresh_without_offline_access_enabled=parse_bool_env(_getenv(
            "MENHIR_OAUTH_AS_REFRESH_WITHOUT_OFFLINE_ACCESS_ENABLED",
            default=str(cls.oauth_as_refresh_without_offline_access_enabled),
        )),
        oauth_as_refresh_ttl_s=_parse_int(
            _getenv(
                "MENHIR_OAUTH_AS_REFRESH_TTL_S",
                default=str(cls.oauth_as_refresh_ttl_s),
            ),
            env_var="MENHIR_OAUTH_AS_REFRESH_TTL_S",
        ),
        oauth_as_refresh_retry_grace_s=_parse_float(
            _getenv(
                "MENHIR_OAUTH_AS_REFRESH_RETRY_GRACE_S",
                default=str(cls.oauth_as_refresh_retry_grace_s),
            ),
            env_var="MENHIR_OAUTH_AS_REFRESH_RETRY_GRACE_S",
        ),
        trusted_proxy=parse_bool_env(
            _getenv("MENHIR_TRUSTED_PROXY", default=str(cls.trusted_proxy))
        ),
        trusted_proxy_peers=parse_csv_env(
            _getenv("MENHIR_TRUSTED_PROXY_PEERS", default=",".join(cls.trusted_proxy_peers))
        ),
        backend_url=_getenv("MENHIR_BACKEND_URL", default=cls.backend_url),
        mcp_client_user_id=_getenv("MENHIR_MCP_CLIENT_USER_ID", default=cls.mcp_client_user_id),
        mcp_client_id=_getenv("MENHIR_CLIENT_ID", default=cls.mcp_client_id),
        mcp_client_name=_getenv("MENHIR_CLIENT_NAME", default=cls.mcp_client_name),
        # Conflict suppression
        conflict_cooldown_days=_parse_int(
            _getenv("MENHIR_CONFLICT_COOLDOWN_DAYS", default=str(cls.conflict_cooldown_days)),
            env_var="MENHIR_CONFLICT_COOLDOWN_DAYS",
        ),
    )
