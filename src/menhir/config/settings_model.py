"""Typed runtime settings model for menhir."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .settings_helpers import (
    _getenv,
    _parse_float,
    _parse_int,
    assert_bind_safe,
    is_loopback_host,
    parse_bool_env,
    parse_client_namespaces,
    parse_client_tools,
    parse_csv_env,
)

if TYPE_CHECKING:
    from menhir.domain.retrieval_tuning import RetrievalTuningConfig


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


@dataclass(frozen=True)
class MemorySettings:
    """Runtime settings used by menhir."""

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_database: str = "neo4j"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # Local LLM (llama.cpp / OpenAI-compatible)
    local_llm_base_url: str = "http://127.0.0.1:8081/v1"
    local_llm_api_key: str = "not-needed"
    local_llm_chat_model: str = "qwen3.5-35b-a3b"
    local_llm_embed_model: str = ""
    local_llm_embed_base_url: str = ""

    # OpenAI
    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_embed_model: str = "text-embedding-3-small"

    # Gemini
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_api_key: str = ""
    gemini_chat_model: str = "gemini-2.5-flash"

    # Provider selection — applies to chat backend and all Graphiti components
    # Valid values: local | openai | gemini
    chat_provider: str = "local"
    graphiti_provider: str = "local"
    graphiti_embed_provider: str = ""   # inherits graphiti_provider when blank
    graphiti_reranker_provider: str = ""  # inherits graphiti_provider when blank

    # Graphiti tuning
    graphiti_add_episode_timeout_seconds: float = 300.0
    graphiti_request_stall_timeout_seconds: float = 45.0
    graphiti_episode_max_estimated_tokens: int = 12000
    #: Guardrail on the ASSEMBLED extraction request, not the episode text.  The
    #: episode is a tiny fraction of what actually gets sent (context, candidate
    #: entities, schema), so graphiti_episode_max_estimated_tokens cannot catch a
    #: request that overruns the model's context window.  This one can.
    graphiti_request_max_estimated_tokens: int = 100000

    # LLM generation
    llm_max_tokens: int = 4096

    # M6 sidecar expansion
    record_detailed_revisions: bool = True
    revision_retention_days: int = 14
    # CF-171: retention for the telemetry sidecar, tiered by role. The high-volume observability
    # tables (lifecycle_events at ~30 rows/ingest, mcp_events, episode_task_events,
    # lifecycle_actions) answer "what happened just now"; the diagnostic tables are what someone
    # reads investigating a defect weeks later, so a single short window would delete the history
    # needed to correlate a recurring failure. 0 disables pruning for that tier entirely.
    # `merge_audit` is never time-pruned -- see TelemetryLifecycleStoreMixin._RETENTION_TIERS.
    telemetry_observability_retention_days: int = 30
    telemetry_diagnostic_retention_days: int = 90

    # M6 LLM budget caps
    max_llm_calls_per_session_window: int = 50
    llm_session_window_seconds: int = 900
    max_llm_calls_per_enrichment_job: int = 10

    # Enrichment concurrency: max episodes extracting at once, serialized per namespace.
    # 1 == single-flight (required for memory-sensitive LOCAL models); raise for cloud
    # providers (e.g. the LongMemEval OpenAI build) to parallelize the drain.
    ingest_concurrency: int = 1

    # Shadow-mode context composition (Stage 1 of the context-composition production-integration
    # plan, .agent/plans/menhir-context-composition-production-integration.md): observe-only,
    # never applied. When on, every ingested episode gets a candidate-fact-edge retrieval +
    # grounded shadow-label classification + eligibility trace logged alongside the real
    # extraction outcome, with zero effect on what gets extracted or written. Off by default —
    # this is research instrumentation, not a recall/extraction behavior change. The shadow
    # LLM work runs OUTSIDE the per-namespace ingest gate as a detached background task (see
    # shadow_context_composition.py), so shadow_composition_timeout_s bounds only that background
    # task, never real episode completion latency.
    shadow_context_composition: bool = False
    shadow_composition_timeout_s: float = 30.0

    # Conflict suppression
    conflict_cooldown_days: int = 0  # 0 = permanent suppression; >0 = re-check after N days

    # Structure watcher
    structure_watcher_interval_s: float = 1800.0
    structure_watcher_enabled: bool = True

    # Artifact corpus reconciliation at startup: off | audit | safe_apply.
    # Defaults to `audit` -- drift is reported, nothing is mutated. `safe_apply`
    # is an operator choice after the one-time repair and the fixture suite pass,
    # never a default, because it lets a process write to the graph on boot.
    artifact_reconcile_mode: str = "audit"
    artifact_reconcile_repo: str = ""
    artifact_reconcile_repository: str = ""

    # Experience-counter maintenance job (telemetry -> QuantState counters). Env-toggleable so the
    # job can be paused independently of the rest of the scheduler (it can be an expensive fold +
    # per-counter embedding pass). MENHIR_EXPERIENCE_COUNTER_ENABLED=false removes it from the loop.
    experience_counter_enabled: bool = True

    # Verifier sync job (graph-native verifiers -> re-derive config/status registers from their
    # source of truth and flag referencing beliefs on change). Default off — opt in per box.
    verifier_sync_enabled: bool = False
    verifier_sync_interval_s: float = 300.0

    # Personal-memory consolidation job (gated perception -> count/amount Views from user turns).
    # Short interval + dirty-namespace filter keeps it cheap; all bias guards pinned on. Default off.
    personal_memory_consolidation_enabled: bool = False
    personal_memory_consolidation_interval_s: float = 300.0
    personal_memory_consolidation_k: int = 3
    personal_memory_consolidation_call_budget: int = 300
    # Completion-token cap for the consolidation extractor. The extractor must emit EVERY claim it
    # found in ONE JSON array, so this bounds claims-per-call, not prose length -- and a cut-off array
    # never closes, so exceeding it does not lose the tail, it loses the WHOLE response (unparseable
    # -> zero rows). At threshold=1.0 that is worse than it sounds: the lost sample scores `absent`
    # against every claim the other samples found, vetoing all of them.
    #
    # Was 512 (the `make_sync_chat` default, never overridden). Measured on the LME corpus, 19
    # namespaces x k=3 at an 8000 budget: demand p50 ~330, max 761, and 3/19 namespaces (16%) exceed
    # 512. Raised to 2048 = 2.7x the observed max.
    #
    # Demand tracks ROWS EMITTED, not input size -- the worst namespace measured (761 tokens) has
    # BELOW-median input and is 4x smaller than the largest, which needs 597. Do not re-tune this
    # against episode counts or prose volume.
    #
    # A cap costs nothing when unused: billing is on actual completion tokens, so headroom is free.
    # Prefer generous. This does NOT make truncation impossible, only unlikely -- the caller must
    # still treat an unparseable response as a lost sample rather than an abstention.
    personal_memory_consolidation_max_tokens: int = 2048
    # Dedicated chat model for the consolidation extractor ONLY (empty = use the global chat provider
    # model). gpt-4o-mini @ k=3 recovers known-good measures where gpt-4.1-nano abstains (see
    # .agent/plans/phase3-extractor-matrix-results.md); set this rather than the global model so
    # Graphiti enrichment stays on the cheaper model.
    personal_memory_consolidation_chat_model: str = ""
    # Bounded retry for the consolidation VERIFIER (Lever C4) — re-runs the full k-sample verifier
    # vote up to 1+N times, committing on the first attempt that clears (same unanimity bar per
    # attempt, so precision per commit is unchanged; a flaky-but-correct SUM just gets more chances).
    # Default 0 = exactly one attempt = behaviour identical to before. See the phase3
    # consumer-quality-pack live characterization.
    personal_memory_consolidation_verify_retries: int = 0
    # Deterministic SUM arithmetic grounding (precision-preserving cross-check adjustment): when a
    # fold-SUM's amounts are each an explicit price literally in their source span, prove the
    # arithmetic deterministically and skip the noisy holistic cross-check (the sharper verifier still
    # audits membership). PROMOTED to default True (2026-07-08): live characterization showed the
    # cross-check-dominated variants jump 40%->90-100% commit with wrong_view_writes=0 across OFF + 2x
    # ON. Set MENHIR_PERSONAL_MEMORY_SUM_GROUNDING=0 to disable. See the cross-check-quality pack.
    personal_memory_consolidation_sum_grounding: bool = True
    # ScalarStateView typed-scalar shadow path (Piece C.4.3), gated inside the same consolidation job.
    # OFF by default: the counter path is byte-identical when off. When on, the job also extracts typed
    # scalars, binds them to resolved entities, and persists :TypedAssertion + rebuilds ScalarStateViews
    # (behind the fresh-only activation gate). Runs on its OWN :ScalarConsolidationWatermark cursor, so
    # enabling it backfills historical namespaces. The perceiver version stamps the cursor + assertions;
    # bumping it revisits history so a newer perceiver can correct prior claims.
    personal_memory_scalar_state_enabled: bool = False
    # v2: deterministic when-discipline (temporal resolver + hedged-value abstention). assertion_key
    # omits valid_at/time_basis and the idempotent rewrite never rewrites temporal fields, so corrected
    # grounding only lands as a NEW interpreted assertion — a version bump makes it supersede v1 by
    # strict rank rather than silently no-op on the stored row.
    personal_memory_scalar_state_perceiver_version: str = "v2"
    # Step 7 current-state-only authority canary. OFF by default: when off, a current scalar_state
    # View is context only and NEVER suppresses an overlapping graph fact in recall (today's
    # behavior). When on, a View may suppress an older overlapping fact ONLY if all six authority
    # gates pass (current-state query + resolved subject + exact slot + grounded evidence +
    # unambiguous current View + complete overlap proof); every other case stays advisory. The
    # decision is computed by menhir.domain.scalar_view_authority.decide_view_authority.
    personal_memory_scalar_view_authority_enabled: bool = False
    # Attribute reconciliation in the k-sample consistency gate. OFF by default. When on, the samples
    # vote WITHOUT the free-text attribute name and the name is chosen modally afterwards (ties to the
    # longest, then lexicographic -- a pure function of the candidate set, never of sample order).
    # Measured cause: replaying the frozen LME panel through the real parser and gate, the model emits
    # the asked value in 100/100 namespace-trials and all k samples emit it in 81/100, but only 23/100
    # commit -- samples agree the user has 25 postcards and disagree on whether the slot is called
    # `postcard_count`, `collection_size`, or `count`, and the disagreement vetoes the fact. Turning
    # this on takes that panel to 34/100 at the shipped threshold of 1.0, and to 72/100 at a 2/3
    # threshold -- see gate_typed_scalars for the full grid. This flag is the SECOND-largest lever;
    # the unanimity requirement itself is the largest. It is independently settings-exposed below.
    # It is RECALL-affecting, not behavior-neutral: more claims
    # commit, so more assertions and Views are written. The chosen name lands in `slot_key`, so a
    # namespace consolidated with this on and off can hold the same fact under two different slots --
    # bump `personal_memory_scalar_state_perceiver_version` when flipping it if that matters.
    personal_memory_scalar_reconcile_attribute: bool = False
    # Scope/subject/self identity reconciliation -- the same defect as the attribute above, relocated.
    # The model smears one fact's identity across subject/attribute/scope in arbitrary order, so
    # exact-matching each field independently turns one agreed fact into several single-vote claims.
    # These vote on the identity TUPLE (never field-by-field, which could synthesize a slot no sample
    # proposed) and pick the modal combination. All OFF by default and all RECALL-affecting when on,
    # for the same reason as reconcile_attribute: more claims commit, so more assertions and Views are
    # written, and the reconciled subject/scope lands in the durable slot -- bump
    # `personal_memory_scalar_state_perceiver_version` when flipping these on an existing namespace.
    # Measured on the frozen LME panel at threshold=2/3 with reconcile_attribute on: +scope 65 -> 70,
    # +scope+subject -> 72 correct current Views out of 100, stale-as-current 0 throughout.
    personal_memory_scalar_reconcile_scope: bool = False
    personal_memory_scalar_reconcile_subject: bool = False
    # Fold first-person subjects ('I', 'me', 'my') to the bound self display before the vote, so the
    # vote key stops contradicting the binder. Measured effect on the LME panel: ZERO cells in every
    # configuration -- the extraction prompt already emits 'user' almost without exception. Exposed
    # because the vote key genuinely disagreed with the binder, not because it moved the number.
    personal_memory_scalar_canonical_self: bool = False
    # Agreement required by the typed-scalar gate only. Default 1.0 preserves today's unanimous
    # behavior. The env value also accepts ratio syntax (`2/3`) so operators do not accidentally use
    # 0.67, which is GREATER than two thirds and therefore still requires all three votes at k=3.
    # Settings exposure is not activation: scalar-state and attribute reconciliation remain
    # independently default-off.
    personal_memory_scalar_threshold: float = 1.0
    # Consolidation audit trail. OFF by default and behavior-neutral: when on, the consolidation job
    # emits a structured, replayable lifecycle event at each decision point (perception, binding /
    # pending-repair, scalar fold outcome, View write/retire/supersede, reconcile, counters, merges,
    # lifecycle promote/demote/delete) to the telemetry lifecycle_events store under
    # component='consolidation_audit'. Emission is best-effort and can never raise into the caller, so
    # toggling it changes only what is recorded, never what consolidation does. Read it back with the
    # get_consolidation_audit ops tool or menhir.infrastructure.consolidation_audit helpers.
    personal_memory_consolidation_audit_enabled: bool = False
    # Recall audit trail. OFF by default and behavior-neutral: when on, the recall path emits a
    # structured, replayable event at each Step 7 View-authority suppression decision (query intent,
    # candidate set, suppressible-provenance rows, per-gate advisory reason, final suppressed set) to
    # the telemetry lifecycle_events store under component='recall_audit'. Emission is best-effort and
    # can never raise into recall, so toggling it changes only what is recorded, never what recall
    # returns. Read it back with menhir.infrastructure.audit_trail.RECALL or the inspect script.
    personal_memory_recall_audit_enabled: bool = False
    # ScalarHistory projection: advisory, slot-keyed, ordered assertion history Views. OFF by
    # default. When on, the projection coordinator also builds scalar_history Views alongside
    # scalar_state, and the recall path includes a dedicated advisory history lane. When off,
    # stored scalar_history Views are excluded from generic recall (real rollback, not leakage).
    # See `.agent/plans/menhir-scalar-history-projection-plan.md`.
    personal_memory_scalar_history_enabled: bool = False
    # Observe-only deterministic typed-scalar shadow. When enabled, consolidation runs the pure
    # deterministic extractor beside the existing LLM gate and emits bounded comparison audit
    # telemetry. It never changes LLM decisions, writes, authority, or recall behavior.
    personal_memory_scalar_deterministic_shadow: bool = False
    # Opt-in deterministic scalar router; default-off preserves the existing LLM path.
    personal_memory_scalar_deterministic_router: bool = False
    personal_memory_scalar_deterministic_classes: tuple[str, ...] = ()
    # Event-history projection settings. All OFF by default; the perceiver version
    # stamps the projection cursor so bumping it revisits history.
    personal_memory_event_history_enabled: bool = False
    personal_memory_event_history_perceiver_version: str = "v1"
    personal_memory_event_history_authority_enabled: bool = False

    # Benchmark mode — when true, disables the background scheduler,
    # consolidation/decay, and orphan recovery so the store is never mutated
    # mid-measurement (LongMemEval Mode B isolation). Ingest + recall still work.
    benchmark_mode: bool = False

    # Frontier retrieval portions, mapped into a RetrievalTuningConfig at the recall entry
    # point via retrieval_tuning(). ALL portions default OFF so the shipped recall path is
    # byte-for-byte today's ScoringService behavior (merge-to-main neutrality gate + the
    # 2026-07-04 read-side bench verdict: the oracle stack is neutral-to-negative on
    # LongMemEval, so it does not earn being on by default). Enable any portion per
    # deployment via MENHIR_FRONTIER_BM25 / _ORACLE_RANKING / _INTENT_LENS / _WARDEN_GATE /
    # _SHADOW / etc. (This also resolves audit DOC-05: code defaults now match .env.example.)
    frontier_bm25: bool = False            # attributed hybrid (vector+BM25) candidate gen
    frontier_content_vector: bool = False  # add menhir-owned content-embedding cosine lane
    frontier_content_vector_replace_name: bool = False
    frontier_content_vector_k: int = 100
    frontier_content_vector_weight: float = 0.5
    frontier_fusion_admission_policy: str = "attributed"
    frontier_oracle_ranking: bool = False  # reorder survivors by the oracle combiner
    frontier_intent_lens: bool = False     # derive temporal lens from query text
    frontier_warden_gate: bool = False     # drop REFUSED / label FLAGGED (opt-in; aggressive)
    frontier_diversity_gate: bool = False  # reorder by evidence family to prevent spiral (opt-in)
    frontier_contradiction_interrupt: bool = False  # append ContradictionWarden to default chain (opt-in)
    frontier_belief_gate: bool = False     # add CurrentnessWarden + belief scoring to the chain; REQUIRES frontier_warden_gate to apply its verdicts (opt-in; aggressive)
    frontier_evidence_anchor: bool = False  # Guard 5 EvidenceAnchorWarden (only applies under warden_gate); set TRUE for code corpora, FALSE for anecdotal/conversational
    frontier_fact_edges: bool = False      # inject RELATES_TO fact edges (EntityEdge.fact) into the candidate pool; set TRUE for episodic/"what happened" corpora where the answer is a dated edge fact, not an entity name
    frontier_fact_edge_mode: str = "standalone"  # how fact edges enter the pool: "standalone" (terse fact as its own candidate; net-negative at N=30) or "pointer" (hydrate endpoint nodes' rich context; preferred)
    frontier_similarity_scale: str = "rrf"  # similarity lane scale (plan 1a/1b): "rrf" (today; RRF ~[0,2]) or "normalized" (divide search scores by the pinned RRF max -> [0,1], restoring PENDING's top-pin; ranking change, A/B before default flip)
    frontier_shadow: bool = False          # observe-only oracle/warden pass (trace)
    frontier_brief_builder: bool = False   # build_context stage (NOT recall tuning): keep the relevance-ranked list as the brief, then APPEND a supplementary temporal Timeline below it. Measured safe/neutral on LME (append +0.03 vs replace -0.10); off by default pending a lift verdict at larger N

    # HTTP server
    api_host: str = "127.0.0.1"
    api_port: int = 8100
    api_key: str = ""
    operator_key: str = ""  # MENHIR_OPERATOR_KEY — Claude, Codex, human operator
    agent_key: str = ""     # MENHIR_AGENT_KEY — Qwen, Gemini, Reasonix
    readonly_key: str = ""  # MENHIR_READONLY_KEY — dashboards, read integrations
    allow_insecure_remote_no_auth: bool = False  # MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH
    client_tokens_enabled: bool = False  # MENHIR_CLIENT_TOKENS_ENABLED — enforced per-client token tier

    # MENHIR_CLIENT_NAMESPACES — pin a client to a namespace, server-side.
    # Format: "client-name=namespace,other-client=other-namespace".
    # A pinned client's reads AND writes are FORCED into its namespace: the value
    # overrides any namespace the caller passes, so an untrusted or unreliable
    # client (e.g. a game-chat bot driven by a small model, which cannot be
    # trusted to pass the right argument) can never touch the default graph.
    # Empty (the default) preserves existing behavior for every client.
    client_namespaces: dict[str, str] = field(default_factory=dict)

    # MENHIR_CLIENT_TOOLS — restrict a client to a fixed allowlist of tools,
    # server-side. Format: "client-name=tool1|tool2,other-client=toolA|toolB"
    # (clients comma-separated, a client's tools '|'-separated).
    # A listed client sees ONLY its allowed tools in tools/list and is refused
    # if it tries to invoke any other tool. This is the tool-surface analogue of
    # client_namespaces: a small-model client (e.g. a game-chat bot) is handed a
    # tiny, purpose-built toolset instead of the full catalog, which it cannot
    # navigate and wastes prompt budget on. Composes with (does not replace) the
    # per-tier catalog filter. Empty (the default) leaves every client
    # unrestricted, preserving existing behavior.
    client_tools: dict[str, frozenset[str]] = field(default_factory=dict)

    # MENHIR_KNOWN_CLIENTS -- comma-separated client names that are RECOGNIZED but carry no
    # restriction. Exists because "known" and "restricted" are different facts, and CF-32's
    # refusal needs the first without implying the second.
    #
    # Under static-key auth the caller supplies its own client name, so once ANY per-client
    # restriction is configured an unrecognized name must be refused rather than treated as
    # unrestricted -- otherwise a shared-key holder simply names itself something unconfigured
    # and the restriction becomes opt-in by the party it restricts. But the only registries that
    # existed were client_namespaces and client_tools, and adding a name to either RESTRICTS it.
    # Registering an ordinary client like `claude-code` would have forced a namespace pin on it
    # as a side effect of making it recognized.
    #
    # Empty (the default) is unchanged behavior, and this list is consulted only when some
    # restriction is configured somewhere -- a deployment with no restrictions has nothing to
    # evade and refuses nothing.
    known_clients: frozenset[str] = frozenset()

    # HTTP process snapshot
    startup_scope: str = "full"
    cors_origins: tuple[str, ...] = ()
    instance_id: str = ""
    explorer_enabled: bool = True
    privacy_redact: bool = False

    # Startup saga recovery. "observe" (default) classifies the PREPARED backlog and logs one
    # summary, mutating nothing; "off" skips the pass entirely; "live" (CF-20c) takes the
    # reconciliation gate, runs the preflight, and replays abandoned rows before any local writer
    # is admitted.
    #
    # "live" is deliberately NOT the default and never becomes one by upgrading. Whether replay is
    # safe is a property of the DEPLOYMENT, not of the code -- what is in this journal, and whether
    # this host can be trusted to prove a writer is dead -- so it stays an explicit per-deployment
    # act taken after a clean preflight. In live mode a failed preflight or a not-write-ready run
    # is FATAL to startup: an instance that cannot clear its backlog must admit no writers.
    saga_reconcile_startup_mode: str = "observe"

    # OAuth resource server + embedded authorization server
    oauth_enabled: bool = False
    oauth_public_base_url: str = ""
    oauth_resource: str = ""
    oauth_audiences: tuple[str, ...] = ()
    oauth_issuer: str = ""
    oauth_jwks_uri: str = ""
    oauth_authorization_servers: tuple[str, ...] = ()
    oauth_as_enabled: bool = False
    oauth_scopes_supported: tuple[str, ...] = ("menhir:read", "menhir:write", "menhir:admin")
    oauth_read_scopes: tuple[str, ...] = ("menhir:read",)
    oauth_write_scopes: tuple[str, ...] = ("menhir:write",)
    oauth_admin_scopes: tuple[str, ...] = ("menhir:admin",)
    oauth_jwks_cache_ttl_s: int = 300
    oauth_http_timeout_s: float = 5.0
    oauth_clock_skew_s: int = 60
    oauth_allowed_algorithms: tuple[str, ...] = ("RS256",)
    oauth_as_dir: str = ""
    oauth_as_code_ttl_s: float = 120.0
    oauth_as_access_ttl_s: int = 3600
    oauth_as_consent_secret: str = ""
    oauth_as_consent_ttl_s: float = 300.0
    oauth_as_session_ttl_s: float = 600.0
    oauth_as_register_rate: int = 20
    oauth_as_register_window_s: int = 600
    oauth_as_approve_rate: int = 10
    oauth_as_approve_window_s: int = 300
    oauth_as_max_clients: int = 1000
    oauth_as_stale_client_max_age_s: int = 86400
    trusted_proxy: bool = False
    trusted_proxy_peers: tuple[str, ...] = ("127.0.0.1", "::1")

    # Backend-first MCP client mode
    backend_url: str = ""
    mcp_client_user_id: str = "claude-code"
    mcp_client_id: str = ""
    mcp_client_name: str = "claude-code"

    # Langfuse
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    def __post_init__(self) -> None:
        """Validate bounds on critical numeric settings."""
        if self.graphiti_add_episode_timeout_seconds <= 0:
            raise ValueError(f"graphiti_add_episode_timeout_seconds must be > 0, got {self.graphiti_add_episode_timeout_seconds}")
        if self.api_port < 1 or self.api_port > 65535:
            raise ValueError(f"api_port must be 1-65535, got {self.api_port}")
        if self.max_llm_calls_per_session_window < 0:
            raise ValueError(f"max_llm_calls_per_session_window must be >= 0, got {self.max_llm_calls_per_session_window}")
        if self.max_llm_calls_per_enrichment_job < 0:
            raise ValueError(f"max_llm_calls_per_enrichment_job must be >= 0, got {self.max_llm_calls_per_enrichment_job}")
        if self.ingest_concurrency < 1:
            raise ValueError(f"ingest_concurrency must be >= 1, got {self.ingest_concurrency}")
        if self.llm_session_window_seconds < 1:
            raise ValueError(f"llm_session_window_seconds must be >= 1, got {self.llm_session_window_seconds}")
        if not 0 < self.personal_memory_scalar_threshold <= 1:
            raise ValueError(
                "personal_memory_scalar_threshold must be > 0 and <= 1, "
                f"got {self.personal_memory_scalar_threshold}"
            )
        if self.startup_scope not in {"full", "auth-only", "http-only", "no-backend"}:
            raise ValueError(f"startup_scope is invalid: {self.startup_scope!r}")
        positive_oauth_numbers = {
            "oauth_jwks_cache_ttl_s": self.oauth_jwks_cache_ttl_s,
            "oauth_http_timeout_s": self.oauth_http_timeout_s,
            "oauth_as_code_ttl_s": self.oauth_as_code_ttl_s,
            "oauth_as_access_ttl_s": self.oauth_as_access_ttl_s,
            "oauth_as_consent_ttl_s": self.oauth_as_consent_ttl_s,
            "oauth_as_session_ttl_s": self.oauth_as_session_ttl_s,
            "oauth_as_register_rate": self.oauth_as_register_rate,
            "oauth_as_register_window_s": self.oauth_as_register_window_s,
            "oauth_as_approve_rate": self.oauth_as_approve_rate,
            "oauth_as_approve_window_s": self.oauth_as_approve_window_s,
            "oauth_as_max_clients": self.oauth_as_max_clients,
        }
        for name, value in positive_oauth_numbers.items():
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        if self.oauth_clock_skew_s < 0:
            raise ValueError(f"oauth_clock_skew_s must be >= 0, got {self.oauth_clock_skew_s}")
        if self.oauth_as_stale_client_max_age_s < 0:
            raise ValueError(
                "oauth_as_stale_client_max_age_s must be >= 0, "
                f"got {self.oauth_as_stale_client_max_age_s}"
            )
        if not self.oauth_allowed_algorithms or any(
            algorithm.lower() == "none" for algorithm in self.oauth_allowed_algorithms
        ):
            raise ValueError("oauth_allowed_algorithms must be non-empty and cannot include 'none'")
        if self.trusted_proxy and not self.trusted_proxy_peers:
            raise ValueError("trusted_proxy requires at least one trusted_proxy_peers entry")
        if self.oauth_as_enabled and not self.oauth_public_base_url:
            raise ValueError(
                "oauth_public_base_url is required when the embedded authorization server is enabled"
            )
        if self.oauth_as_enabled:
            parsed = urlparse(self.oauth_public_base_url)
            host = (parsed.hostname or "").strip().lower()
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and is_loopback_host(host)
            ):
                raise ValueError(
                    "oauth_public_base_url must use HTTPS for the embedded authorization "
                    "server (loopback HTTP is allowed for local development)"
                )
        # Single source of truth: resolve the auth mode and enforce bind safety
        # through one path (assert_bind_safe -> resolve_auth_mode). Local import
        # avoids a circular import (menhir.api.oauth imports is_loopback_host).
        assert_bind_safe(self)

    def retrieval_tuning(self) -> "RetrievalTuningConfig":
        """Build the RetrievalTuningConfig for the active frontier portions.

        All portions default OFF, so with no MENHIR_FRONTIER_* env set this yields a config
        whose recall path is byte-for-byte today's ScoringService behavior. ``frontier_shadow``
        is NOT part of tuning — it drives the trace flag at the recall entry point.
        """
        from menhir.domain.retrieval_tuning import RetrievalTuningConfig

        return RetrievalTuningConfig(
            enable_bm25=self.frontier_bm25,
            enable_content_vector=self.frontier_content_vector,
            content_vector_replace_name=self.frontier_content_vector_replace_name,
            content_vector_k=self.frontier_content_vector_k,
            content_vector_weight=self.frontier_content_vector_weight,
            fusion_admission_policy=self.frontier_fusion_admission_policy,
            enable_oracle_ranking=self.frontier_oracle_ranking,
            enable_intent_lens=self.frontier_intent_lens,
            enable_warden_gate=self.frontier_warden_gate,
            enable_diversity_gate=self.frontier_diversity_gate,
            enable_contradiction_interrupt=self.frontier_contradiction_interrupt,
            enable_belief_gate=self.frontier_belief_gate,
            enable_evidence_anchor=self.frontier_evidence_anchor,
            enable_fact_edges=self.frontier_fact_edges,
            fact_edge_mode=self.frontier_fact_edge_mode,
            similarity_scale=self.frontier_similarity_scale,
            enable_assertion_shadow=self.frontier_shadow,
        )

    @classmethod
    def from_env(cls) -> "MemorySettings":
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
            # Gemini
            gemini_base_url=_getenv("GEMINI_BASE_URL", default=cls.gemini_base_url),
            gemini_api_key=_getenv("GEMINI_API_KEY", default=cls.gemini_api_key),
            gemini_chat_model=_getenv("GEMINI_CHAT_MODEL", default=cls.gemini_chat_model),
            # Provider selection
            chat_provider=_getenv("LLM_CHAT_PROVIDER", "MEMORY_CHAT_PROVIDER", default=cls.chat_provider),
            # GRAPHITI_PROVIDER is accepted as a final alias: it is the intuitive name
            # people reach for, and silently ignoring it (the canonical var is
            # GRAPHITI_LLM_PROVIDER) caused extraction to run on the wrong model.
            # graphiti_embed_provider / graphiti_reranker_provider inherit this when blank.
            graphiti_provider=_getenv("GRAPHITI_LLM_PROVIDER", "MEMORY_GRAPHITI_PROVIDER", "GRAPHITI_PROVIDER", default=cls.graphiti_provider),
            graphiti_embed_provider=_getenv("GRAPHITI_EMBED_PROVIDER", "MEMORY_GRAPHITI_EMBED_PROVIDER", default=cls.graphiti_embed_provider),
            graphiti_reranker_provider=_getenv("GRAPHITI_RERANKER_PROVIDER", "MEMORY_GRAPHITI_RERANKER_PROVIDER", default=cls.graphiti_reranker_provider),
            # Graphiti tuning
            graphiti_add_episode_timeout_seconds=_parse_float(
                _getenv("MEMORY_GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS", "GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS", default=str(cls.graphiti_add_episode_timeout_seconds)),
                env_var="MEMORY_GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS",
            ),
            graphiti_request_stall_timeout_seconds=_parse_float(
                _getenv("MEMORY_GRAPHITI_REQUEST_STALL_TIMEOUT_SECONDS", "GRAPHITI_REQUEST_STALL_TIMEOUT_SECONDS", default=str(cls.graphiti_request_stall_timeout_seconds)),
                env_var="MEMORY_GRAPHITI_REQUEST_STALL_TIMEOUT_SECONDS",
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
            ),
            llm_session_window_seconds=_parse_int(
                _getenv("MENHIR_LLM_SESSION_WINDOW_SECONDS", default=str(cls.llm_session_window_seconds)),
                env_var="MENHIR_LLM_SESSION_WINDOW_SECONDS",
            ),
            max_llm_calls_per_enrichment_job=_parse_int(
                _getenv("MENHIR_MAX_LLM_CALLS_PER_JOB", default=str(cls.max_llm_calls_per_enrichment_job)),
                env_var="MENHIR_MAX_LLM_CALLS_PER_JOB",
            ),
            ingest_concurrency=_parse_int(
                _getenv("MENHIR_INGEST_CONCURRENCY", default=str(cls.ingest_concurrency)),
                env_var="MENHIR_INGEST_CONCURRENCY",
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
            ),
            personal_memory_consolidation_enabled=parse_bool_env(_getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED", default=str(cls.personal_memory_consolidation_enabled))),
            personal_memory_consolidation_interval_s=_parse_float(
                _getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S", default=str(cls.personal_memory_consolidation_interval_s)),
                env_var="MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S",
            ),
            personal_memory_consolidation_k=int(_getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_K", default=str(cls.personal_memory_consolidation_k))),
            personal_memory_consolidation_call_budget=int(_getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_CALL_BUDGET", default=str(cls.personal_memory_consolidation_call_budget))),
            personal_memory_consolidation_max_tokens=int(_getenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_MAX_TOKENS", default=str(cls.personal_memory_consolidation_max_tokens))),
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
            cors_origins=parse_csv_env(_getenv("MENHIR_CORS_ORIGINS", default="")),
            instance_id=_getenv("MENHIR_INSTANCE_ID", default=cls.instance_id).strip(),
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
