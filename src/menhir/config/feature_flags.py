"""Declarative feature/control inventory. Metadata only; settings remain the runtime authority.

Settings entries snapshot code defaults. Env-only defaults describe the raw read's absent
value, before consumer-specific fallbacks; their descriptions name those fallbacks.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureFlag:
    setting: str
    env_var: str
    default: bool | str | int | float | tuple[str, ...] | None
    category: str
    description: str
    recall_affecting: bool
    env_var_aliases: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    requirement_scope: str = ""
    emission_requires: tuple[str, ...] = ()
    version_bump: bool = False
    observation_only: bool = False
    documented: bool = True


# Boolean and mode snapshots, plus settings also read by legacy environment wrappers.
_SETTINGS = (
    ('record_detailed_revisions', 'MENHIR_RECORD_DETAILED_REVISIONS', True, 'runtime',
        'Retain detailed revision provenance.', False, ()),
    ('shadow_context_composition', 'MENHIR_SHADOW_CONTEXT_COMPOSITION', False, 'runtime',
        'Observe experimental context composition.', False, ()),
    ('structure_watcher_enabled', 'MENHIR_STRUCTURE_WATCHER_ENABLED', True, 'runtime',
        'Enable background structural watching.', False, ()),
    ('artifact_reconcile_mode', 'MENHIR_ARTIFACT_RECONCILE_MODE', 'audit', 'runtime',
        'Configure artifact reconcile mode.', False, ()),
    ('experience_counter_enabled', 'MENHIR_EXPERIENCE_COUNTER_ENABLED', True, 'runtime',
        'Enable background experience counters.', False, ()),
    ('verifier_sync_enabled', 'MENHIR_VERIFIER_SYNC_ENABLED', False, 'runtime', 'Enable verifier sync enabled.',
        False, ()),
    ('canonical_self_binding_mode', 'MENHIR_CANONICAL_SELF_BINDING_MODE', 'off', 'runtime',
        'Configure canonical self binding mode.', True, ()),
    ('anchored_time_resolver_enabled', 'MENHIR_ANCHORED_TIME_RESOLVER', False, 'runtime',
        'Overlay anchored-time valid_at on edges from user turns with temporal cues.', True, ()),
    ('personal_memory_consolidation_enabled', 'MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED', False,
        'personal_memory', 'Enable personal-memory consolidation.', True, ()),
    ('personal_memory_consolidation_disable_reasoning', 'MENHIR_PERSONAL_MEMORY_CONSOLIDATION_DISABLE_REASONING',
        False, 'personal_memory', 'Request consolidation reasoning suppression where supported.', False, ()),
    ('personal_memory_consolidation_sum_grounding', 'MENHIR_PERSONAL_MEMORY_SUM_GROUNDING', True,
        'personal_memory', 'Enable sum/count evidence grounding.', True, ()),
    ('personal_memory_scalar_state_enabled', 'MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED', False,
        'personal_memory', 'Enable personal memory scalar state enabled.', True, ()),
    ('personal_memory_scalar_view_authority_enabled', 'MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED',
        False, 'personal_memory', 'Enable personal memory scalar view authority enabled.', True, ()),
    ('personal_memory_scalar_reconcile_attribute', 'MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_ATTRIBUTE', True,
        'personal_memory', 'Enable personal memory scalar reconcile attribute.', True, ()),
    ('personal_memory_scalar_reconcile_scope', 'MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_SCOPE', True,
        'personal_memory', 'Enable personal memory scalar reconcile scope.', True, ()),
    ('personal_memory_scalar_reconcile_subject', 'MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_SUBJECT', True,
        'personal_memory', 'Enable personal memory scalar reconcile subject.', True, ()),
    ('personal_memory_scalar_canonical_self', 'MENHIR_PERSONAL_MEMORY_SCALAR_CANONICAL_SELF', False,
        'personal_memory', 'Enable canonical-self scalar subject resolution.', True, ()),
    ('personal_memory_consolidation_audit_enabled', 'MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED', False,
        'personal_memory', 'Enable personal memory consolidation audit enabled.', False, ()),
    ('personal_memory_recall_audit_enabled', 'MENHIR_PERSONAL_MEMORY_RECALL_AUDIT_ENABLED', False,
        'personal_memory', 'Record recall audit decisions.', False, ()),
    ('personal_memory_scalar_history_enabled', 'MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED', False,
        'personal_memory', 'Enable personal memory scalar history enabled.', True, ()),
    ('personal_memory_scalar_deterministic_shadow', 'MENHIR_SCALAR_DETERMINISTIC_SHADOW', False, 'personal_memory',
        'Enable personal memory scalar deterministic shadow.', False, ()),
    ('personal_memory_scalar_deterministic_router', 'MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_ROUTER', False,
        'personal_memory', 'Enable the optional deterministic scalar router.', True, ()),
    ('personal_memory_event_history_enabled', 'MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED', False,
        'personal_memory', 'Enable personal memory event history enabled.', True, ()),
    ('personal_memory_event_history_authority_enabled', 'MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED',
        False, 'personal_memory', 'Enable personal memory event history authority enabled.', True, ()),
    ('benchmark_mode', 'MENHIR_BENCHMARK_MODE', False, 'runtime', 'Enable benchmark mode.', False, ()),
    ('frontier_bm25', 'MENHIR_FRONTIER_BM25', False, 'frontier', 'Enable frontier bm25.', True, ()),
    ('frontier_content_vector', 'MENHIR_FRONTIER_CONTENT_VECTOR', False, 'frontier',
        'Enable frontier content vector.', True, ()),
    ('frontier_content_vector_replace_name', 'MENHIR_FRONTIER_CONTENT_VECTOR_REPLACE_NAME', False, 'frontier',
        'Enable frontier content vector replace name.', True, ()),
    ('frontier_fusion_admission_policy', 'MENHIR_FRONTIER_FUSION_ADMISSION_POLICY', 'attributed', 'frontier',
        'Configure frontier fusion admission policy.', True, ()),
    ('frontier_oracle_ranking', 'MENHIR_FRONTIER_ORACLE_RANKING', False, 'frontier',
        'Enable frontier oracle ranking.', True, ()),
    ('frontier_intent_lens', 'MENHIR_FRONTIER_INTENT_LENS', False, 'frontier', 'Enable frontier intent lens.',
        True, ()),
    ('frontier_warden_gate', 'MENHIR_FRONTIER_WARDEN_GATE', False, 'frontier', 'Enable frontier warden gate.',
        True, ()),
    ('frontier_diversity_gate', 'MENHIR_FRONTIER_DIVERSITY_GATE', False, 'frontier',
        'Enable frontier diversity gate.', True, ()),
    ('frontier_contradiction_interrupt', 'MENHIR_FRONTIER_CONTRADICTION_INTERRUPT', False, 'frontier',
        'Enable frontier contradiction interrupt.', True, ()),
    ('frontier_belief_gate', 'MENHIR_FRONTIER_BELIEF_GATE', False, 'frontier', 'Enable frontier belief gate.',
        True, ()),
    ('frontier_evidence_anchor', 'MENHIR_FRONTIER_EVIDENCE_ANCHOR', False, 'frontier',
        'Enable frontier evidence anchor.', True, ()),
    ('frontier_fact_edges', 'MENHIR_FRONTIER_FACT_EDGES', False, 'frontier', 'Enable frontier fact edges.', True, ()),
    ('frontier_fact_edge_mode', 'MENHIR_FRONTIER_FACT_EDGE_MODE', 'standalone', 'frontier',
        'Configure frontier fact edge mode.', True, ()),
    ('frontier_similarity_scale', 'MENHIR_FRONTIER_SIMILARITY_SCALE', 'rrf', 'frontier',
        'Configure frontier similarity scale.', True, ()),
    ('frontier_shadow', 'MENHIR_FRONTIER_SHADOW', False, 'frontier', 'Enable frontier shadow.', False, ()),
    ('frontier_source_memories', 'MENHIR_FRONTIER_SOURCE_MEMORIES', True, 'frontier',
        'Enable frontier source memories.', True, ()),
    ('frontier_source_memory_pools', 'MENHIR_FRONTIER_SOURCE_MEMORY_POOLS', False, 'frontier',
        'Enable frontier source memory pools.', True, ()),
    ('allow_insecure_remote_no_auth', 'MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH', False, 'auth',
        'Explicitly permit a remote no-auth bind.', False, ()),
    ('client_tokens_enabled', 'MENHIR_CLIENT_TOKENS_ENABLED', False, 'auth', 'Enable per-client bearer tokens.',
        False, ()),
    ('startup_scope', 'MENHIR_STARTUP_SCOPE', 'full', 'runtime', 'Configure startup scope.', False, ()),
    ('runtime_mode', 'MENHIR_RUNTIME_MODE', 'production', 'runtime', 'Configure runtime mode.', False, ()),
    ('explorer_enabled', 'MENHIR_EXPLORER_ENABLED', True, 'runtime', 'Enable Explorer routes.', False, ()),
    ('privacy_redact', 'MENHIR_PRIVACY_REDACT', False, 'telemetry', 'Enable privacy redact.', False, ()),
    ('saga_reconcile_startup_mode', 'MENHIR_SAGA_RECONCILE_STARTUP_MODE', 'observe', 'saga',
        'Configure saga reconcile startup mode.', False, ()),
    ('oauth_enabled', 'MENHIR_OAUTH_ENABLED', False, 'auth', 'Enable oauth enabled.', False, ()),
    ('oauth_as_enabled', 'MENHIR_OAUTH_AS_ENABLED', False, 'auth', 'Enable oauth as enabled.', False, ()),
    ('oauth_as_refresh_tokens_enabled', 'MENHIR_OAUTH_AS_REFRESH_TOKENS_ENABLED', False, 'auth',
        'Enable oauth as refresh tokens enabled.', False, ()),
    ('oauth_as_refresh_without_offline_access_enabled', 'MENHIR_OAUTH_AS_REFRESH_WITHOUT_OFFLINE_ACCESS_ENABLED',
        False, 'auth', 'Permit refresh tokens without offline_access consent.', False, ()),
    ('trusted_proxy', 'MENHIR_TRUSTED_PROXY', False, 'auth', 'Enable trusted proxy.', False, ()),
    ('oauth_as_dir', 'MENHIR_OAUTH_AS_DIR', '', 'auth', 'Configure oauth as dir.', False, ()),
    ('oauth_as_code_ttl_s', 'MENHIR_OAUTH_AS_CODE_TTL_S', 120.0, 'auth', 'Configure oauth as code ttl s.', False, ()),
    ('oauth_as_stale_client_max_age_s', 'MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S', 86400, 'auth',
        'Configure oauth as stale client max age s.', False, ()),
    ('oauth_as_max_clients', 'MENHIR_OAUTH_AS_MAX_CLIENTS', 1000, 'auth', 'Configure oauth as max clients.', False,
        ()),
    ('oauth_as_consent_secret', 'MENHIR_OAUTH_AS_CONSENT_SECRET', '', 'auth', 'Configure oauth as consent secret.',
        False, ()),
    ('oauth_as_consent_ttl_s', 'MENHIR_OAUTH_AS_CONSENT_TTL_S', 300.0, 'auth', 'Configure oauth as consent ttl s.',
        False, ()),
    ('oauth_as_session_ttl_s', 'MENHIR_OAUTH_AS_SESSION_TTL_S', 600.0, 'auth', 'Configure oauth as session ttl s.',
        False, ()),
    ('operator_key', 'MENHIR_OPERATOR_KEY', '', 'auth', 'Configure operator key.', False, ()),
    ('oauth_public_base_url', 'MENHIR_PUBLIC_BASE_URL', '', 'auth', 'Configure oauth public base url.', False, ()),
    ('trusted_proxy_peers', 'MENHIR_TRUSTED_PROXY_PEERS', ('127.0.0.1', '::1'), 'auth',
        'Configure trusted proxy peers.', False, ()),
    ('oauth_as_refresh_ttl_s', 'MENHIR_OAUTH_AS_REFRESH_TTL_S', 2592000, 'auth',
        'Configure oauth as refresh ttl s.', False, ()),
    ('oauth_audiences', 'MENHIR_OAUTH_AUDIENCE', (), 'auth', 'Configure oauth audiences.', False,
        ('MENHIR_OAUTH_AUDIENCES',)),
    ('oauth_authorization_servers', 'MENHIR_AUTHORIZATION_SERVERS', (), 'auth',
        'Configure oauth authorization servers.', False, ()),
    ('oauth_resource', 'MENHIR_OAUTH_RESOURCE', '', 'auth', 'Configure oauth resource.', False,
        ('MENHIR_MCP_RESOURCE',)),
    ('oauth_issuer', 'MENHIR_OAUTH_ISSUER', '', 'auth', 'Configure oauth issuer.', False, ()),
    ('oauth_jwks_uri', 'MENHIR_OAUTH_JWKS_URI', '', 'auth', 'Configure oauth jwks uri.', False, ()),
    ('oauth_scopes_supported', 'MENHIR_OAUTH_SCOPES_SUPPORTED', ('menhir:read', 'menhir:write', 'menhir:admin'),
        'auth', 'Configure oauth scopes supported.', False, ()),
    ('oauth_read_scopes', 'MENHIR_OAUTH_READ_SCOPES', ('menhir:read',), 'auth', 'Configure oauth read scopes.',
        False, ()),
    ('oauth_write_scopes', 'MENHIR_OAUTH_WRITE_SCOPES', ('menhir:write',), 'auth', 'Configure oauth write scopes.',
        False, ()),
    ('oauth_admin_scopes', 'MENHIR_OAUTH_ADMIN_SCOPES', ('menhir:admin',), 'auth', 'Configure oauth admin scopes.',
        False, ()),
    ('oauth_jwks_cache_ttl_s', 'MENHIR_OAUTH_JWKS_CACHE_TTL_S', 300, 'auth', 'Configure oauth jwks cache ttl s.',
        False, ()),
    ('oauth_http_timeout_s', 'MENHIR_OAUTH_HTTP_TIMEOUT_S', 5.0, 'auth', 'Configure oauth http timeout s.', False,
        ()),
    ('oauth_clock_skew_s', 'MENHIR_OAUTH_CLOCK_SKEW_S', 60, 'auth', 'Configure oauth clock skew s.', False, ()),
    ('oauth_allowed_algorithms', 'MENHIR_OAUTH_ALLOWED_ALGORITHMS', ('RS256',), 'auth',
        'Configure oauth allowed algorithms.', False, ()),
    ('instance_id', 'MENHIR_INSTANCE_ID', '', 'runtime', 'Configure instance id.', False, ()),
)

_ENV_ONLY = (
    ('MENHIR_CONSOLE_SCHEME', None, 'runtime', 'Console color scheme; absent chooses by terminal/background.'),
    ('MENHIR_LOG_DIR', None, 'telemetry', 'Log directory override; absent uses runtime log location.'),
    ('MENHIR_SNAPSHOT_RECEIVE_MODE', None, 'runtime', 'Snapshot receive mode; absent resolves to off.'),
    ('MENHIR_INGEST_ALLOWED_ROOTS', '', 'runtime', 'Allowed ingest roots; empty refuses every non-operator ingest.'),
    ('MENHIR_ALLOW_SYSTEM_PYTHON', '', 'runtime', 'Explicit system-interpreter opt-in; empty is disabled.'),
    ('MENHIR_BENCH_RESULTS_ROOT', None, 'telemetry', 'Bench result root; absent disables the result catalog.'),
    ('MENHIR_BENCH_ACTIVE_RUN_ID', None, 'telemetry', 'Optional benchmark run correlation identifier.'),
    ('MENHIR_EMBEDDING_CACHE_MAX_SIZE', None, 'runtime', 'Embedding cache capacity; absent or invalid uses 512.'),
    ('MENHIR_GRAPHITI_DISABLE_REASONING', '', 'runtime',
        'Optional provider reasoning suppression; empty is disabled.'),
    ('MENHIR_LOG_LEVEL', None, 'telemetry', 'Log severity override; absent uses configured logger level.'),
    ('MENHIR_HOST_PID_NAMESPACE_VERIFIABLE', '', 'saga', 'Permit local PID liveness evidence; empty is disabled.'),
    ('MENHIR_SAGA_ALL_WRITERS_GATE_AWARE', '', 'saga',
        'Operator claim that all writers honor saga gates; empty is disabled.'),
    ('MENHIR_STATE_DIR', '', 'runtime', 'State path override; empty uses legacy workspace or ~/.menhir.'),
    ('MENHIR_MCP_TELEMETRY_DB', '', 'telemetry', 'Telemetry SQLite path override; empty uses the state directory.'),
    ('MENHIR_ALLOW_INSECURE_BACKEND_URL', '', 'auth',
        'Permit insecure non-loopback backend transport; empty is disabled.'),
    ('MENHIR_FRONTIER_TRACE', '', 'frontier', 'Optional retrieval diagnostics; empty is disabled.'),
    ('MENHIR_FRONTIER_ORACLE_SUBSET', '', 'frontier', 'Optional oracle subset; empty keeps the default oracle set.'),
    ('MENHIR_TELEMETRY_BUSY_TIMEOUT_S', '5', 'telemetry', 'SQLite busy timeout in seconds; raw absent default is 5.'),
    ('MENHIR_MCP_TIMEOUT', '120', 'runtime', 'MCP timeout in seconds; raw absent default is 120.'),
    ('MENHIR_RECALL_COMPACT', '', 'frontier', 'Compact recall output; empty keeps full output.'),
)

_REQUIRES = {
    "personal_memory_scalar_view_authority_enabled": ("personal_memory_scalar_state_enabled",),
    "personal_memory_event_history_authority_enabled": ("personal_memory_event_history_enabled",),
    "frontier_belief_gate": ("frontier_warden_gate",),
    "frontier_evidence_anchor": ("frontier_warden_gate",),
    "frontier_contradiction_interrupt": ("frontier_warden_gate",),
    "personal_memory_scalar_reconcile_attribute": ("personal_memory_scalar_state_enabled",),
    "personal_memory_scalar_reconcile_scope": ("personal_memory_scalar_state_enabled",),
    "personal_memory_scalar_reconcile_subject": ("personal_memory_scalar_state_enabled",),
}
# These describe scoped prerequisites/data producers; they do not impose runtime validation.
_REQUIREMENT_SCOPE = {
    "personal_memory_scalar_view_authority_enabled": "Scalar state produces the Views; retained Views can outlive the writer gate.",
    "personal_memory_event_history_authority_enabled": "Event history produces the assertions; retained assertions can outlive the writer gate.",
    "frontier_belief_gate": "Warden verdict enforcement needs the master gate; belief scoring is independent.",
    "frontier_evidence_anchor": "Ranked Warden admission only; not every recall/context section.",
    "frontier_contradiction_interrupt": "Ranked Warden admission only; independent advisories remain.",
    **{key: "Reconciliation acts inside scalar-state perception." for key in
       ("personal_memory_scalar_reconcile_attribute", "personal_memory_scalar_reconcile_scope",
        "personal_memory_scalar_reconcile_subject")},
}
_VERSIONED = frozenset({"personal_memory_scalar_reconcile_attribute",
    "personal_memory_scalar_reconcile_scope", "personal_memory_scalar_reconcile_subject"})
_OBSERVATION = frozenset({"shadow_context_composition", "frontier_shadow",
    "personal_memory_consolidation_audit_enabled", "personal_memory_recall_audit_enabled",
    "personal_memory_scalar_deterministic_shadow", "MENHIR_FRONTIER_TRACE",
    "MENHIR_BENCH_ACTIVE_RUN_ID", "MENHIR_BENCH_RESULTS_ROOT"})

FEATURES = {
    setting: FeatureFlag(setting, env, default, category, description, recall,
        env_var_aliases=aliases, requires=_REQUIRES.get(setting, ()),
        requirement_scope=_REQUIREMENT_SCOPE.get(setting, ""),
        emission_requires=("personal_memory_consolidation_audit_enabled",)
            if setting == "personal_memory_scalar_deterministic_shadow" else (),
        version_bump=setting in _VERSIONED, observation_only=setting in _OBSERVATION)
    for setting, env, default, category, description, recall, aliases in _SETTINGS
}
FEATURES.update({
    env: FeatureFlag("", env, default, category, description,
        recall_affecting=env == "MENHIR_FRONTIER_ORACLE_SUBSET",
        observation_only=env in _OBSERVATION)
    for env, default, category, description in _ENV_ONLY
})

# Undocumented inventory exceptions. Growth needs a deliberate reviewed change to the test ratchet.
EXEMPT: dict[str, str] = {}
RETIRED = {
    "frontier_brief_builder": "2026-10-01: replaced by the on-demand recall_timeline tool",
    "MENHIR_FRONTIER_BRIEF_BUILDER": "2026-10-01: replaced by the on-demand recall_timeline tool",
}
