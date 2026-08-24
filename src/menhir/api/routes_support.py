"""Shared helpers, policies, and models for REST API routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from menhir.core.backend_impl import RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend
from menhir.core.runtime import RuntimeContext
from menhir.domain.session import MemorySession, new_session
from menhir.infrastructure.telemetry import record_destructive_op
from menhir.mcp.service_access import (
    get_pinned_namespace,
    get_request_session,
    get_request_tier,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# REST tier enforcement (auth Phase 0 / tracker Q4) — mirror of the MCP tool path
# (mcp/contracts.py). The middleware resolves the bearer to a tier; routes enforce a
# minimum. An empty tier means auth is disabled (no keys configured) — enforcement is
# skipped, matching the MCP semantics exactly.
# ---------------------------------------------------------------------------

_TIER_RANK = {"readonly": 0, "agent": 1, "operator": 2}


def _require_tier(required: str) -> None:
    tier = get_request_tier()
    if tier and _TIER_RANK.get(tier, -1) < _TIER_RANK.get(required, 0):
        raise HTTPException(
            status_code=403,
            detail=f"Token tier '{tier}' cannot invoke this endpoint (requires '{required}')",
        )


def _try_record_destructive_op_rest(name: str) -> None:
    """Record operator-tier REST endpoint invocation for audit log (best-effort, non-blocking)."""
    try:
        tier = get_request_tier()
        session = get_request_session()
        user_id = session.user_id if session else ""
        session_id = session.session_id if session else ""
        record_destructive_op(
            surface="rest",
            name=name,
            tier=tier,
            session_id=session_id,
            user_id=user_id,
        )
    except Exception:
        pass  # Telemetry failures must never disrupt the caller


def _get_runtime_context(request: Request) -> RuntimeContext | None:
    return getattr(request.app.state, "runtime_ctx", None)


def _require_runtime_context(request: Request) -> RuntimeContext:
    runtime_ctx = _get_runtime_context(request)
    if runtime_ctx is None:
        raise HTTPException(status_code=503, detail="menhir runtime is not ready")
    return runtime_ctx


def _capability_payload(runtime_ctx: RuntimeContext | None) -> dict[str, bool]:
    capabilities = runtime_ctx.capabilities if runtime_ctx is not None else None
    if capabilities is None:
        return {
            "neo4j_ready": False,
            "embedder_ready": False,
            "llm_ready": False,
            "scheduler_ready": False,
            "reads_ready": False,
            "queue_writes_ready": False,
            "enrichment_ready": False,
        }
    return {
        "neo4j_ready": capabilities.neo4j_ready,
        "embedder_ready": capabilities.embedder_ready,
        "llm_ready": capabilities.llm_ready,
        "scheduler_ready": capabilities.scheduler_ready,
        "reads_ready": capabilities.reads_ready,
        "queue_writes_ready": capabilities.queue_writes_ready,
        "enrichment_ready": capabilities.enrichment_ready,
    }


def _service_payload(runtime_ctx: RuntimeContext | None) -> dict[str, str]:
    if runtime_ctx is None:
        return {"runtime": "starting"}
    built = runtime_ctx.built
    capabilities = runtime_ctx.capabilities
    return {
        "neo4j": "ok" if capabilities is None or capabilities.neo4j_ready else "unavailable",
        "graphiti": "ok" if capabilities is None or capabilities.graphiti_ready else "degraded",
        "ingest": "ok" if built.ingest_service and built.ingest_service.enrichment_enabled() else "queue_only",
        "recall": "ok" if capabilities is None or capabilities.reads_ready else "degraded",
        "scheduler": "ok" if capabilities is None or capabilities.scheduler_ready else "unavailable",
    }


def _get_backend(request: Request) -> MemoryBackend:
    runtime_ctx = _require_runtime_context(request)
    return RuntimeProvider(
        runtime_ctx.built,
        process_session=runtime_ctx.session,
        caller_session=_resolve_caller_session(request),
    )


def _resolve_caller_session(request: Request, *, default_user_id: str = "remote-api") -> MemorySession:
    bound_session = get_request_session()
    if bound_session is not None:
        return bound_session

    user_id = _caller_header(request, "user-id") or default_user_id
    session_id = _caller_header(request, "session-id") or None
    return new_session(user_id, session_id=session_id)


def _caller_header(request: Request, suffix: str) -> str:
    """Read ``x-menhir-<suffix>``, falling back to the deprecated ``x-yawn-<suffix>``.

    The legacy spelling is consulted only when the canonical header is absent or
    empty, so a client sending both cannot have the old value take precedence.
    """
    value = (request.headers.get(f"x-menhir-{suffix}") or "").strip()
    if not value:
        value = (request.headers.get(f"x-yawn-{suffix}") or "").strip()
    return value


def _resolve_namespace(request: Request, body_namespace: str | None) -> str | None:
    """Namespace precedence: server-side pin, then request body, then x-menhir-namespace header.

    None preserves the legacy global behavior (no isolation); an explicit value scopes
    the operation to that silo. The deprecated x-yawn-namespace spelling is still accepted.

    THE PIN WINS, and it is checked first. `MENHIR_CLIENT_NAMESPACES` binds a client to a
    namespace server-side, and `BaseTool._apply_pinned_namespace` documents the guarantee as
    absolute: a pinned client "cannot escape it, whether by passing another namespace or by
    omitting the argument entirely". That was true only of MCP tools. REST never consulted the
    pin at all, so a credential restricted to one namespace through MCP reached every namespace
    through HTTP by putting one in the request body -- the same client, the same server-side
    policy, one transport enforcing it.

    Nothing new is needed to fix that: the auth middleware already binds the request session
    (carrying `client_name`) on this path, so `get_pinned_namespace()` resolves here exactly as
    it does under MCP. It was simply never called.

    A mismatch is logged rather than rejected, matching the tool path's behaviour precisely --
    the two surfaces must not disagree about what a pinned client's request means.
    """
    pinned = get_pinned_namespace()
    if pinned:
        requested = (body_namespace or _caller_header(request, "namespace") or "").strip()
        if requested and requested != pinned:
            logger.warning(
                "namespace pin: REST client requested namespace=%r; forcing %r",
                requested,
                pinned,
            )
        return pinned
    if body_namespace:
        return body_namespace
    return _caller_header(request, "namespace") or None


class RecallRequest(BaseModel):
    query: str
    preset: str = "knowledge"
    limit: int = Field(default=10, ge=1, le=50)
    namespace: str | None = None
    # Whether to include SESSION-scoped (not-yet-promoted-to-PERSISTENT) memories.
    # The MCP recall tools (recall_memories, recall_context_memories, resources)
    # all pass include_session=True; the HTTP API historically did not expose it and
    # defaulted to False, so freshly-ingested memories were unrecallable over HTTP
    # until the scheduler promoted them -- which never happens under MENHIR_BENCHMARK_MODE.
    include_session: bool = False
    # Whether to include superseded View versions (view_current=false) — stale state kept for
    # history/provenance. Default false so they never compete with current state; set true for
    # historical/provenance/debug recall, where results carry is_superseded_view=true.
    include_superseded: bool = False
    # Whether source-time evidence may include expired/superseded relationship beliefs.
    # This enriches returned memories with history; it does not add invalidated candidates.
    include_invalidated: bool = False
    trace: bool = False


class RecallTemporalFact(BaseModel):
    fact: str | None = None
    valid_at: str | None = None
    invalid_at: str | None = None
    created_at: str | None = None
    expired_at: str | None = None
    is_current_belief: bool = True
    temporal_role: str = "current_belief"


class RecallMemory(BaseModel):
    uuid: str
    name: str
    content: str | None
    scope: str
    memory_type: str
    final_score: float
    retrieval_score: float | None = None
    retrieval_score_kind: str = "graphiti_rrf"
    relevance_basis: str = "legacy_rrf_threshold_unvalidated"
    # True only for a superseded View surfaced via include_superseded (the "clearly labeled" flag).
    is_superseded_view: bool = False
    # True for the deterministically-injected current scalar_state View: the authoritative CURRENT
    # value for a surfaced slot (Phase 4a.4/4c). The consumer leads its answer with this.
    is_scalar_authority: bool = False
    # Source/world-time evidence attached to this result. ``created_at``/``expired_at`` remain
    # belief-time metadata and must never substitute for a missing ``valid_at``.
    temporal_facts: list[RecallTemporalFact] = Field(default_factory=list)


class ScalarAuthorityContributorResponse(BaseModel):
    assertion_id: str
    relation: str
    operation: str
    value: Any
    stated_span: str
    valid_at: str | None = None
    evidence_tier: str | None = None
    episode_uuid: str | None = None


class ScalarAuthorityVerdictResponse(BaseModel):
    kind: str
    status: str
    subject_uuid: str
    attribute: str
    scope: str
    value_kind: str
    unit: str
    value: Any
    valid_at: str | None = None
    view_uuid: str | None = None
    has_foundation: bool
    contributors: list[ScalarAuthorityContributorResponse] = Field(default_factory=list)
    contributors_total: int = 0
    contributors_truncated: bool = False
    next_offset: int | None = None


class EventAuthorityVerdictResponse(BaseModel):
    """Structured first-person event authority, separate from scalar authority.

    Mirrors the domain ``EventAuthorityVerdict``. ``status`` is ``leads`` or ``advisory``.
    ``gate`` is an explanatory string (``pass`` for a fully grounded non-generic unique
    lead; selection-failure gates mirror ``EventRecallGate``). Selected grounding fields
    are None when a gate failed (advisory).
    """

    predicate: str
    object_key: str | None = None
    object_display: str | None = None
    valid_at: str | None = None
    stated_span: str | None = None
    assertion_key: str | None = None
    episode_uuid: str | None = None
    turn_evidence_uuid: str | None = None
    domain: str | None = None
    time_basis: str | None = None
    status: str
    gate: str
    reason: str
    subject_uuid: str
    has_foundation: bool
    kind: str


class RecallResponse(BaseModel):
    query: str
    preset: str
    results: list[RecallMemory]
    candidates_evaluated: int
    trace: dict[str, object] | None = None
    authority_layer: list[ScalarAuthorityVerdictResponse] | None = None
    event_authority_layer: list[EventAuthorityVerdictResponse] | None = None


class BootstrapContextRequest(BaseModel):
    reader_id: str = "default"
    workspace: str | None = None
    namespace: str | None = None
    query: str = ""
    limit: int = Field(default=5, ge=1, le=50)
    recent_limit: int = Field(default=5, ge=1, le=50)


class ContextRequest(BaseModel):
    query: str
    max_tokens: int = Field(default=2000, ge=100, le=10000)
    preset: str = "knowledge"
    include_scores: bool = False
    namespace: str | None = None


class ContextResponse(BaseModel):
    query: str
    context: str
    token_estimate: int
    memory_count: int
    truncated: bool
    preset: str


class MemoryRequest(BaseModel):
    episode: str
    source: str = "remote-api"
    session_id: str | None = None
    user_id: str | None = None
    diff: str | None = None
    namespace: str | None = None
    occurred_at: str | None = None
    flagged: bool = False
    bootstrap_scope: str | None = None
    turn_evidence_uuid: str | None = None


class MemoryResponse(BaseModel):
    episode_id: str
    status: str
    session_id: str
    flagged: bool = False
    bootstrap_scope: str | None = None


class TurnEvidenceRequest(BaseModel):
    """A candidate user turn a host lifecycle producer's deterministic triage judged may hold durable
    evidence (ADR 0001). The producer captures the declarant and the triage reasons; the server never
    infers them and never runs an LLM here. Only `text` (+ role) is required."""
    text: str
    role: str = "user"
    declarant: str | None = None
    session_id: str | None = None
    # Optional world time for replay/import producers. Live hooks omit this; recorded_at remains the
    # server receive time and processing cursor.
    occurred_at: str | None = None
    namespace: str | None = None
    source_kind: str = "unknown"
    source_id: str | None = None
    # Additive provenance labels (which producer captured this, at what version). Optional so
    # existing clients that omit them keep working unchanged; the server stores nulls.
    source_client: str | None = None
    hook_version: str | None = None
    cwd: str | None = None
    transcript_path: str | None = None
    triage_reason: list[str] | None = None
    triage_version: str | None = None
    metadata: dict[str, Any] | None = None
    turn_key: str | None = None
    # Stable per-prompt id (Claude Code `prompt_id` UUID, CC v2.1.196+): unique per genuine turn, stable
    # across a double-fired retry. When present the server keys idempotency on it so two genuine
    # repetitions of the same text stay distinct evidence sources (G18). Optional -- older/other
    # producers omit it and the server falls back to text-keying.
    prompt_id: str | None = None


class EpisodeAdmissionRequest(BaseModel):
    """Join a memory to the captured turn it was written in response to, after the fact.

    Exists because a host's post-tool lifecycle event fires AFTER `add_memory` has already run, so it
    cannot supply `turn_evidence_uuid` on the original call -- it can only report the pairing
    afterwards.

    TRUST: no better than the caller. Both ids are caller-supplied and the server cannot check that
    this memory really came from that turn. This adds no forgery surface that does not already
    exist -- a client holding the same key can already POST a `:TurnEvidence` with
    `declarant='user'` (see `TurnEvidenceRequest`) and write memories -- but do not mistake the
    resulting edge for verification. See the CORRECTION in
    `.agent/plans/menhir-evidence-projection-episodes.md`.
    """
    episode_uuid: str
    turn_evidence_uuid: str


class EpisodeAdmissionResponse(BaseModel):
    linked: bool
    #: uuid of the evidence projection minted from the turn's text, when this call minted one.
    #: None when the turn was already projected, was not user-declared, or did not resolve.
    projection_uuid: str | None = None


class TurnEvidenceResponse(BaseModel):
    turn_id: str
    created: bool
    recorded_at: str
    occurred_at: str | None = None


class ToolEventRequest(BaseModel):
    """A normalized Hook Center tool/file event (v0: file changes). A hook observes a real file edit/
    write/delete/rename and posts this; the server marks the affected structure-file node dirty so
    stale file references are detectable. NO file content or transcript — only the path + optional
    provenance. Only `event_type` and `path` are required; everything else is optional so hooks with
    partial metadata still succeed (fail-open)."""
    event_type: str = "file_changed"
    source_client: str | None = None      # claude_code | codex | opencode | unknown
    source_kind: str = "hook"
    session_id: str | None = None
    namespace: str | None = None
    project: str | None = None            # structure_project to scope the match (else path-only)
    repository: str | None = None         # stable graph ArtifactSource repository identity
    project_root: str | None = None
    cwd: str | None = None
    path: str | None = None
    old_path: str | None = None           # set on rename/move
    operation: str = "edit"               # write | edit | delete | rename | create
    before_hash: str | None = None
    after_hash: str | None = None
    mtime: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("repository", mode="before")
    @classmethod
    def _normalize_repository(cls, value: object) -> str | None:
        """Treat blank or malformed optional identity as missing so structural marking can proceed."""
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None


class ToolEventResponse(BaseModel):
    accepted: bool
    event_type: str
    operation: str
    matched: int          # existing structure-file nodes matched
    marked_dirty: bool
    ignored_reason: str | None = None
    # Optional and additive. Artifact reconciliation runs AFTER structural dirty
    # marking and cannot fail it: a path outside the corpus, an ambiguous move,
    # or a repository error all leave this null-or-unapplied and change nothing
    # about the structural half of the response.
    artifact_reconciliation: dict[str, Any] | None = None


class StaleAnchorVerificationRequest(BaseModel):
    """A durable audit receipt recording that a stale file-anchored memory was
    inspected against the current file."""
    memory_uuid: str
    project: str | None = None
    path: str
    outcome: str                              # still_valid | outdated | needs_review | superseded
    verified_by: str | None = None
    basis: str | None = None                  # e.g., "inspected_current_file"
    current_file_hash: str | None = None
    notes: str | None = None
    verified_at: str | None = None            # server-assigned if omitted


class StaleAnchorVerificationResponse(BaseModel):
    accepted: bool
    verification: dict[str, Any]


class FlagResponse(BaseModel):
    uuid: str
    flagged: bool
    bootstrap_scope: str | None = None


class UnflagResponse(BaseModel):
    uuid: str
    unflagged: bool


class HealthResponse(BaseModel):
    status: str
    services: dict[str, str]
    startup_mode: str | None = None
    # Echoes MENHIR_INSTANCE_ID when set, so a client (e.g. a smoke launcher) can
    # confirm it is talking to the exact server it started and not a different
    # process that happens to hold the same port. Absent/None in normal use.
    instance_id: str | None = None


class ReadyResponse(BaseModel):
    status: str
    startup_mode: str
    capabilities: dict[str, bool]
    failures: list[str]


class StatsResponse(BaseModel):
    since_hours: int
    startup_mode: str | None = None
    capabilities: dict[str, bool]
    services: dict[str, str]
    queue_depth: int
    enrichment_enabled: bool
    scheduler: dict[str, Any] | None = None
    enrichment: dict[str, Any]
    operations: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Phase 3 (personal-memory View consolidation) — narrow black-box surface for the
# archolith-bench menhir-phase3 benchmark. These run against a THROWAWAY instance:
# the benchmark posts :TurnEvidence, triggers consolidation, and inspects Views /
# abstention receipts / supersession without importing menhir or issuing Cypher.
# ---------------------------------------------------------------------------


class Phase3RunRequest(BaseModel):
    namespace: str = Field(..., min_length=1)
    k: int = Field(default=3, ge=1, le=9)
    source: str = "perception"
    call_budget: int | None = None
    counter_state: bool = True


class Phase3RunResponse(BaseModel):
    namespace: str
    phase3_selected: bool
    dirty_after: bool
    namespaces_dirty: int
    namespaces_processed: int
    views_written: int
    abstained: int
    corrections_applied: int
    llm_calls: int
    scalar_enabled: bool = False
    scalar_namespaces_processed: int = 0
    scalar_states_written: int = 0
    scalar_advisory: int = 0
    scalar_repair_pending: int = 0
    event_history_enabled: bool = False
    event_namespaces_processed: int = 0
    event_namespaces_failed: int = 0
    event_assertions_recorded: int = 0
    event_assertions_created: int = 0
    event_views_rebuilt: int = 0
    event_llm_calls: int = 0


class Phase3StatusResponse(BaseModel):
    namespace: str
    dirty: bool
    turn_evidence: int


class Phase3ViewsResponse(BaseModel):
    namespace: str
    count: int
    views: list[dict[str, Any]]
    receipts: list[dict[str, Any]]


class Phase3ResetResponse(BaseModel):
    namespace: str
    nodes_deleted: int
    turn_evidence_deleted: int


def _require_phase3_adapter(request: Request):
    """Return the graph adapter for Phase 3 endpoints, or 503 if the runtime is not ready."""
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="phase 3 consolidation unavailable")
    return runtime_ctx, adapter

_BACKEND_METHODS = {
    "queue_episode",
    "flag_memory",
    "unflag_memory",
    "promote_memory",
    "delete_memory",
    "erase_memory",
    "delete_namespace",
    "enqueue_pending_episode",
    "recall",
    "build_context",
    "view_entropy",
    "fetch_memory_by_uuid",
    "fetch_node_receipts",
    "fetch_recent_memories",
    "fetch_flagged_memories",
    "fetch_flagged_memory_bootstrap_version",
    "fetch_memories_by_scope",
    "fetch_memories_by_type",
    "get_scan_fingerprint",
    "ingest_document",
    "scan_and_write_project",
    "write_project_structure",
    "query_structure",
    "list_conflict_groups",
    "resolve_conflict_group",
    "requeue_conflicts_for_llm_review",
    "scan_for_conflicts",
    "confirm_pending_conflicts",
    "fetch_episode_processing",
    "list_episode_processing",
    "get_queue_depth",
    "get_failed_enrichment_count",
    "force_reset_failed_episode",
    "force_release_episode_lease",
    "fetch_stale_enriching_episodes",
    "recover_stale_enrichment_leases",
    "get_max_enrichment_attempts",
    "recover_orphans",
    "fetch_session_entities",
    "scheduler_force_takeover",
    "scheduler_status_snapshot",
    "scheduler_pause",
    "scheduler_resume",
    "fetch_operation_stats",
    "fetch_failure_summary",
    "fetch_enrichment_rate",
    "fetch_lifecycle_summary",
    "fetch_episode_task_events",
    "fetch_recent_failures",
    "fetch_recent_lifecycle_events",
    "record_conflict_resolution",
    "fetch_memory_overview",
    "circuit_breaker_snapshots",
    "embedding_cache_stats",
    "get_provider_config",
    "create_todo",
    "list_todos",
    "get_todo",
    "get_artifact",
    "list_artifacts",
    "list_artifact_questions",
    "get_artifact_relationships",
    "link_artifacts",
    "supersede_artifact",
    "transition_artifact_status",
    # Named `fetch_` rather than `audit_` so the read-only remainder rule below
    # classifies it by convention instead of by exception. The MCP tool it backs
    # is still called `audit_artifact_corpus`: the agent-facing name describes
    # the task, the dispatch name has to obey the tier-naming policy.
    "fetch_artifact_corpus_audit",
    "relocate_artifact_source",
    "close_todo",
    "delete_todo",
    "close_stale_todos",
    "create_temporal",
    "list_temporal_in_window",
    "complete_temporal",
    "create_candidate",
    "list_candidates",
    "fetch_candidate",
    "promote_candidate",
    "reject_candidate",
    "approve_candidate",
}

# Per-operation minimum tier for the internal dispatch. THE POLICY IS TOTAL: every operation is
# either listed below or read-only by the explicit remainder rule — adding an op to
# _BACKEND_METHODS without deciding its tier is caught by the test suite (implicit policy is
# forbidden — memory-governance.md §4).
_OP_TIER_OPERATOR = {
    "delete_memory", "erase_memory", "delete_namespace",
    "resolve_conflict_group", "requeue_conflicts_for_llm_review", "scan_for_conflicts",
    "confirm_pending_conflicts", "record_conflict_resolution",
    "force_reset_failed_episode", "force_release_episode_lease",
    "recover_stale_enrichment_leases", "recover_orphans",
    "scheduler_force_takeover", "scheduler_pause", "scheduler_resume",
    "promote_candidate", "reject_candidate", "approve_candidate",
    "promote_memory",
    # CF-257 phase 0. Raised from agent tier. This op writes a structure payload the CALLER
    # produced, and judges it using a `root_path` the same caller supplied -- so the server
    # classifies a path string, never the directory that actually produced the payload. A remote
    # worktree that reports the server's canonical path is classified as the canonical clone and
    # accepted, and the write carries the per-project stale prune. No metadata check can close
    # that, because every input to it is attacker-controlled; the only sound options are a
    # trusted server-side scan (`scan_and_write_project`, which is the modern path) or operator
    # tier. It also always belonged here by this file's own rule: the sets below reserve operator
    # for what deletes, and the stale prune deletes.
    "write_project_structure",
}
_OP_TIER_AGENT = {
    "queue_episode", "flag_memory", "unflag_memory", "enqueue_pending_episode",
    "ingest_document", "scan_and_write_project",
    "create_todo", "close_todo", "delete_todo", "close_stale_todos",
    "create_temporal", "complete_temporal", "create_candidate",
    # Artifact writes. Declaring a relationship or moving a lifecycle state is
    # an assertion about engineering history, so it is not readonly. None are
    # operator-tier: they are all reversible and none deletes anything.
    "link_artifacts", "supersede_artifact", "transition_artifact_status",
    # Relocation changes a source locator, which is an assertion about where a
    # document is -- reversible, and never a lifecycle or relationship change,
    # so agent rather than operator. `fetch_artifact_corpus_audit` is absent
    # deliberately: it writes nothing and falls to the readonly remainder.
    "relocate_artifact_source",
}
assert _OP_TIER_OPERATOR <= _BACKEND_METHODS and _OP_TIER_AGENT <= _BACKEND_METHODS, (
    "op tier map references unknown backend operations"
)


def _required_tier_for_operation(operation: str) -> str:
    if operation in _OP_TIER_OPERATOR:
        return "operator"
    if operation in _OP_TIER_AGENT:
        return "agent"
    return "readonly"  # recall/context/fetch_*/list_*/get_*/snapshots — the explicit remainder

_VALID_CLIENT_TIERS = frozenset({"operator", "agent", "readonly"})


class MintClientRequest(BaseModel):
    client_name: str
    tier: str = "readonly"


class MintClientResponse(BaseModel):
    client_id: str
    client_name: str
    tier: str
    token: str  # shown once at mint time — never retrievable again


class RevokeClientResponse(BaseModel):
    client_id: str
    revoked: bool


class ClientSummary(BaseModel):
    client_id: str
    client_name: str
    tier: str
    created_at: float


class ListClientsResponse(BaseModel):
    clients: list[ClientSummary]


def _get_client_token_store(request: Request):
    store = getattr(request.app.state, "client_token_store", None)
    if store is None:
        raise HTTPException(status_code=404, detail="Per-client token tier is not enabled")
    return store
