"""Runtime capability snapshot reported by menhir runtime preflight checks."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    #: Models a hosted endpoint did not list at GET /models but is expected to serve anyway.
    #: Readiness is still True -- gateways routinely omit models they serve -- but the report
    #: says so, because a bare [ok] next to a startup log warning about the same model reads
    #: as two health signals disagreeing.
    unlisted_models: tuple[str, ...] = field(default_factory=tuple)

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
