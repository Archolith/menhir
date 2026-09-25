"""Pydantic request/response models shared by the REST API routes (see routes_support)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from menhir.services.ingest_limits import MAX_DIFF_CHARS, MAX_EPISODE_CHARS


class RecallRequest(BaseModel):
    query: str
    preset: str = "knowledge"
    limit: int = Field(default=10, ge=1, le=50)
    namespace: str | None = None
    # Whether to include SESSION-scoped (not-yet-promoted-to-PERSISTENT) memories.
    # True, matching every MCP recall tool. It was False here for historical reasons, which
    # made the most basic first-run check -- write one memory, read it back over REST --
    # return nothing until the scheduler promoted it (never, under MENHIR_BENCHMARK_MODE).
    # A cold-start evaluator only found the fix by reading this comment. Pass false to see
    # promoted knowledge only.
    include_session: bool = True
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
    episode: str = Field(..., min_length=1, max_length=MAX_EPISODE_CHARS)
    source: str = "remote-api"
    session_id: str | None = None
    user_id: str | None = None
    # Same bound the enrichment compose path truncates at — reject at the API instead of
    # accepting a payload that would be silently clipped downstream.
    diff: str | None = Field(default=None, max_length=MAX_DIFF_CHARS)
    namespace: str | None = None
    occurred_at: str | None = None
    flagged: bool = False
    bootstrap_scope: str | None = None
    turn_evidence_uuid: str | None = None


class MemoryResponse(BaseModel):
    episode_id: str
    #: Without ``?wait=true``: the queue result (``queued``). With it: the terminal processing
    #: state after waiting -- ``ready``, ``failed``, or the still-pending state on timeout
    #: (``pending`` / ``enriching``). A cold-start evaluator got ``200 queued`` for a write whose
    #: enrichment had already failed for good, and the only trace was in the server log.
    status: str
    session_id: str
    flagged: bool = False
    bootstrap_scope: str | None = None
    #: Set when ``status`` is ``failed``: the error text and whether Menhir will retry it on its
    #: own (``retryable``), park it for an operator (``manual_review``), or never (``terminal``).
    error: str | None = None
    retry: str | None = None
    #: Set when ``wait`` elapsed before a terminal state.
    timed_out: bool = False
    #: With ``wait`` and ``status: ready``: how many entities the episode is linked to. ``0`` is a
    #: successful enrichment that extracted nothing recallable -- small models decline text that
    #: reads as meta ("SMOKE TEST: ...") or has no named things in it. Without the count a caller
    #: sees ``ready`` and an empty recall and cannot tell the two apart.
    entities_linked: int | None = None


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
    # Set when the LLM provider has rejected our credentials since the last successful
    # call: a server can be "ok" and "full" and still unable to enrich anything.
    provider_auth_failure: str | None = None
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
    provider_auth_failure: str | None = None


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
