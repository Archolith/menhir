"""Typed runtime settings model for menhir."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .settings_model_core import MemorySettingsCore
from .settings_model_env import ARTIFACT_RECONCILE_MODES, memory_settings_from_env
from .settings_model_validation import validate_memory_settings

if TYPE_CHECKING:
    from menhir.domain.retrieval_tuning import RetrievalTuningConfig


@dataclass(frozen=True)
class MemorySettings(MemorySettingsCore):
    """Runtime settings used by menhir."""

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

    # Deterministic canonical-self binding rollout. "off" preserves pre-change behavior exactly;
    # "observe" evaluates and records the decision without rewriting the payload; "enforce" binds.
    # Default off until the plan's acceptance gates pass -- a proven self bypasses graphiti dedup
    # entirely, which is a durable-write-semantics change.
    canonical_self_binding_mode: str = "off"

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
    #
    # Raised 2048 -> 8192 on 2026-09-09. Every figure above was measured on NON-REASONING models,
    # where completion tokens are all content. On a reasoning model the reasoning tokens are billed
    # and counted inside the SAME max_tokens ceiling, so the effective content budget is whatever
    # reasoning leaves behind. Measured on date-smoke cc5ded98, k=3, identical input:
    #     gpt-4o-mini   output 403 / 372 / 349   reasoning 0 / 0 / 0        -> 0 truncated
    #     gpt-5.6-luna  output 1809 / 2048 / 1970 reasoning 1384 / 1969 / 1552
    # The middle Luna sample hit 2048 EXACTLY -- the cap -- leaving 79 tokens for the JSON array.
    # It truncated, parsed as `malformed_json`, and became a lost sample, which then scored `absent`
    # against every claim the other two samples found. Agreement could not exceed 2/3 and the whole
    # namespace committed nothing. Exactly the failure this comment block already predicted.
    #
    # 8192 is chosen against Luna's reasoning demand (observed up to 1969, and UNKNOWN above that
    # because the cap censored it) plus the content demand above, with the same generous margin the
    # 2048 figure used. Do not tune this against content demand alone while any configured model
    # emits reasoning tokens.
    personal_memory_consolidation_max_tokens: int = 8192
    # Ask the provider to skip reasoning for the consolidation extractor. OPT-IN, and it must stay
    # opt-in: this travels as `extra_body={"reasoning": {"enabled": false}}`, which OpenRouter
    # understands and api.openai.com rejects outright, so defaulting it on would 400 every native
    # OpenAI deployment. Typed-scalar perception is mechanical schema-filling -- gpt-4o-mini does it
    # in ~370 completion tokens with no reasoning at all, while gpt-5.6-luna spent 516-1552 reasoning
    # tokens per sample on the same input. Reasoning is also charged and counted inside
    # `personal_memory_consolidation_max_tokens`, so it is what pushed a k-sample into truncation.
    # Turning it off is therefore both a cost and a headroom lever -- but it is UNMEASURED against
    # perception recall, which is the thing k-sample agreement actually depends on. Measure before
    # adopting.
    personal_memory_consolidation_disable_reasoning: bool = False
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
    api_key: str = field(default="", repr=False)
    operator_key: str = field(default="", repr=False)  # MENHIR_OPERATOR_KEY — Claude, Codex, human operator
    agent_key: str = field(default="", repr=False)     # MENHIR_AGENT_KEY — Qwen, Gemini, Reasonix
    readonly_key: str = field(default="", repr=False)  # MENHIR_READONLY_KEY — dashboards, read integrations
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
    # Production selects the public route surface. Candidate-readonly is the
    # mutation-fenced pre-cutover proof target.
    runtime_mode: str = "production"
    client_policy_path: str = ""
    client_policy_digest: str = ""
    cors_origins: tuple[str, ...] = ()
    instance_id: str = ""
    release_id: str = ""
    source_fence_key_id: str = ""
    source_fence_token: str = field(default="", repr=False)
    # Source-only Ed25519 private key. This path is configured only on the old
    # authority and is deliberately absent from target Compose/backup state.
    source_fence_private_key_path: str = ""
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
    oauth_signing_key_path: str = ""
    oauth_refresh_retry_keyring_path: str = ""
    oauth_as_code_ttl_s: float = 120.0
    oauth_as_access_ttl_s: int = 3600
    oauth_as_consent_secret: str = field(default="", repr=False)
    oauth_as_consent_ttl_s: float = 300.0
    oauth_as_session_ttl_s: float = 600.0
    oauth_as_register_rate: int = 20
    oauth_as_register_window_s: int = 600
    oauth_as_approve_rate: int = 10
    oauth_as_approve_window_s: int = 300
    oauth_as_max_clients: int = 1000
    oauth_as_stale_client_max_age_s: int = 86400
    # Refresh-token grant for the embedded AS. OFF by default: enabling it adds
    # offline-access tokens to the token endpoint, so it is an explicit operator choice.
    # TTL bounds refresh-token lifetime in seconds (default 30 days); must be > 0.
    oauth_as_refresh_tokens_enabled: bool = False
    # Explicitly permit refresh issuance after owner consent even when a client
    # omits the conventional offline_access scope. OFF by default.
    oauth_as_refresh_without_offline_access_enabled: bool = False
    oauth_as_refresh_ttl_s: int = 2592000
    # Exact refresh retries inside this short window receive the already-issued response instead
    # of triggering family-wide replay revocation. This covers a response lost at the public
    # proxy boundary and concurrent calls by one public client. Zero preserves strict rotation.
    oauth_as_refresh_retry_grace_s: float = 0.0
    trusted_proxy: bool = False
    trusted_proxy_peers: tuple[str, ...] = ("127.0.0.1", "::1")

    # Backend-first MCP client mode
    backend_url: str = ""
    mcp_client_user_id: str = "claude-code"
    mcp_client_id: str = ""
    mcp_client_name: str = "claude-code"

    # Langfuse
    langfuse_host: str = ""
    langfuse_public_key: str = field(default="", repr=False)
    langfuse_secret_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        """Validate bounds on critical numeric settings."""
        validate_memory_settings(self)

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
        return memory_settings_from_env(cls)
