"""Base field set for the ``MemorySettings`` dataclass.

Holds the Neo4j, LLM-provider, Graphiti-tuning and LLM-budget fields that
:class:`menhir.config.settings_model.MemorySettings` inherits. Keeping this a
frozen dataclass base preserves the combined field order exactly, so the
generated ``__init__`` signature, ``repr`` and ``eq`` of ``MemorySettings`` are
identical to the pre-split single-module definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MemorySettingsCore:
    """Backend/provider connectivity and LLM budget fields for ``MemorySettings``."""

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_database: str = "neo4j"
    neo4j_user: str = "neo4j"
    neo4j_password: str = field(default="", repr=False)

    # Local LLM (llama.cpp / OpenAI-compatible)
    local_llm_base_url: str = "http://127.0.0.1:8081/v1"
    local_llm_api_key: str = field(default="not-needed", repr=False)
    local_llm_chat_model: str = "qwen3.5-35b-a3b"
    local_llm_embed_model: str = ""
    local_llm_embed_base_url: str = ""

    # OpenAI
    openai_api_key: str = field(default="", repr=False)
    openai_chat_model: str = "gpt-4o-mini"
    openai_embed_model: str = "text-embedding-3-small"

    # Provider selection — applies to chat backend and all Graphiti components
    # Valid values: local | openai
    chat_provider: str = "local"
    graphiti_provider: str = "local"
    graphiti_embed_provider: str = ""   # inherits graphiti_provider when blank

    # CF-195: operator-settable component of the embedding-identity stamp.
    #
    # `view_embedder_version` stamps `<base_url>|<embed_model>`, which catches a model change and
    # an endpoint change but CANNOT catch a weight swap behind the same alias at the same URL --
    # the file changes, the embedding space changes, and nothing in the configuration moves. A
    # server-side digest would detect it automatically; llama-server exposing one is unverified,
    # so this is the operator's manual lever for the case they already know about.
    #
    # APPENDED to the stamp, never a replacement: an operator who set this to a constant would
    # otherwise collapse two genuinely different endpoints to one identity, which is a worse
    # failure than the one being fixed. Blank means "absent" and leaves the stamp byte-identical,
    # so merely defining the setting does not trigger a corpus-wide re-embed.
    #
    # Changing it re-embeds the whole observation corpus on the next backfill. That is the point,
    # and it is not free -- set it only when the weights really did change.
    embed_version_override: str = ""
    graphiti_reranker_provider: str = ""  # inherits graphiti_provider when blank

    # Graphiti tuning
    graphiti_add_episode_timeout_seconds: float = 300.0
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
    #
    # OWNER RULING 2026-08-23: 50 -> 5000, interim. The 50 dated to the initial public release
    # (7274cccb, 2026-08-10) and was never calibrated against anything -- unlike the per-job cap
    # below, which CF-234 measured. Three things were wrong with it:
    #
    #   1. It sat BELOW the size of a single job. Against CF-234's own per-job measurement
    #      (p50 14, p90 45, p95 59, p99 84, max 140), a p95 job could not finish inside a fresh
    #      window at all, and the per-job cap of 100 was unreachable -- the two caps contradicted
    #      each other.
    #   2. Embeddings share this counter. `view_embedder` announces kind="embedding" into the
    #      same window as chat, so cheap embedding calls evict expensive extraction calls from a
    #      guard that exists to bound runaway CHAT fan-out.
    #   3. It was refusing work while the comments claimed enforcement was off. Only
    #      `providers.py` passes report_only; the graphiti client wrapper, `sync_llm` and
    #      `view_embedder` do not, and the flag defaults to False -- so those paths enforced.
    #
    # 26 episodes were marked FAILED and left unrecallable by this between 2026-08-20 and
    # 2026-08-23, which is exactly the outcome `providers.py` predicted in writing: a refusal has
    # no handler and falls into `_process_episode`'s generic `except Exception`.
    #
    # 5000 is deliberately a ceiling that normal work cannot reach, not a calibrated value. It
    # keeps a runaway bound in place while removing a throttle that was deleting memories. The
    # real fix is still open and is NOT this number: budget refusals need a requeue handler
    # rather than terminal failure, embeddings need to stop counting against a chat guard, and
    # the window should then be set from measured traffic the way CF-234 set the per-job cap.
    max_llm_calls_per_session_window: int = 5000
    llm_session_window_seconds: int = 900
    # CF-234, CALIBRATED 2026-08-21 from 5,016 real chat calls across 244 enrichment jobs in
    # `llm_usage_events` (owner ruling: measure it, do not arbitrarily raise it).
    #
    #   per job:  p50 14   p90 45   p95 59   p99 84   max 140
    #
    # The previous default of 10 sat BELOW the median: it would have refused **62.3% of real
    # jobs** mid-enrichment. That is the failure the ruling named -- "a control that doesn't
    # match reality" -- and in report-only mode it made the warning fire so often that the signal
    # was worthless.
    #
    # 100 is chosen as a RUNAWAY guard, which is what CF-79 filed this for: it sits above p99
    # (84) so normal work never trips it, and refuses 2 of 244 observed jobs (0.8%) -- the 140-
    # and 108-call outliers. Raising further to 150 would refuse nothing at all and stop being a
    # control.
    #
    # Enforcement remains OFF (report_only). Turning it on still needs the landing zone this
    # entry describes: an `LlmBudgetExceeded` handler that requeues rather than failing the
    # episode. Recalibrating first is what makes the report-only signal worth reading.
    max_llm_calls_per_enrichment_job: int = 100

    # Enrichment concurrency: max episodes extracting at once, serialized per namespace.
    # 1 == single-flight (required for memory-sensitive LOCAL models); raise for cloud
    # providers (e.g. the LongMemEval OpenAI build) to parallelize the drain.
    ingest_concurrency: int = 1
