"""Data model surface for the evidence publication intent protocol.

Status constants, failure types, frozen value objects, provider protocols, and row-mapping
helpers shared by :mod:`menhir.infrastructure.evidence_publication_intents`, which re-exports
every public name defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Protocol

PENDING = "PENDING"
CLAIMED = "CLAIMED"
FINALIZED = "FINALIZED"
QUARANTINED = "QUARANTINED"

_OPAQUE_DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class PublicationIntentError(RuntimeError):
    """Base failure for a publication intent transition."""


class PublicationDispatchSuppressed(PublicationIntentError):
    """A stable intent already exists, so another ambiguous dispatch is refused."""


class PublicationActivationBlocked(PublicationIntentError):
    """The optional protocol lacks a prerequisite needed to fail closed."""


@dataclass(frozen=True)
class PublicationActivationStatus:
    """Whether this local repository can safely dispatch publication intents."""

    enabled: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class TombstoneProbe:
    """Opaque HMAC lookup produced for one active tombstone key."""

    digest: str
    key_id: str


@dataclass(frozen=True)
class GraphitiArtifactManifest:
    """Created-only Graphiti rows that must transition together with the episode."""

    node_uuids: tuple[str, ...]
    edge_uuids: tuple[str, ...]
    complete: bool
    quarantine_safe: bool


class EvidenceTombstoneDigestService(Protocol):
    """Managed HMAC key-ring boundary; raw erased IDs never enter tombstone queries."""

    def active_key_ids(self) -> tuple[str, ...]: ...

    def probes_for_publication(
        self,
        *,
        namespace_key: str,
        queued_episode_uuid: str,
        remote_episode_uuid: str | None,
        operation_id: str,
    ) -> tuple[TombstoneProbe, ...]: ...


class GraphitiArtifactManifestService(Protocol):
    """Proves which Graphiti rows were created by one publication operation."""

    def created_artifacts(
        self,
        *,
        intent: "PublicationIntent",
        remote_episode_uuid: str | None,
    ) -> GraphitiArtifactManifest: ...


@dataclass(frozen=True)
class PublicationIntent:
    """Generation-fenced identity captured before one Graphiti dispatch."""

    intent_key: str
    operation_id: str
    episode_uuid: str
    namespace_key: str
    group_id: str
    expected_name: str
    source_description: str
    reference_time: str
    generation: int
    status: str
    resolved_episode_uuid: str | None = None
    dispatch_allowed: bool = False
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_generation: int | None = None


@dataclass(frozen=True)
class PublicationTransition:
    """Outcome of finalizing or quarantining one remote publication."""

    intent_key: str
    status: str
    resolved_episode_uuid: str | None
    candidate_count: int
    tombstone_count: int
    reason: str | None

    @property
    def finalized(self) -> bool:
        return self.status == FINALIZED


@dataclass(frozen=True)
class EpisodeArtifact:
    """Exact Graphiti artifact reconstructed for an intent reconciliation."""

    resolved_episode_uuid: str
    entity_uuids: tuple[str, ...]
    edge_uuids: tuple[str, ...]


def publication_intent_key(episode_uuid: str) -> str:
    """Return the stable publication-intent key for a queued episode UUID."""

    return f"evidence-publication:{episode_uuid}"


def publication_operation_id(episode_uuid: str) -> str:
    """Return the stable external-operation identity for a queued episode UUID."""

    return f"graphiti-add-episode:{episode_uuid}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _intent_from_row(row: dict[str, object], *, dispatch_allowed: bool = False) -> PublicationIntent:
    return PublicationIntent(
        intent_key=str(row.get("intent_key") or ""),
        operation_id=str(row.get("operation_id") or ""),
        episode_uuid=str(row.get("episode_uuid") or ""),
        namespace_key=str(row.get("namespace_key") or ""),
        group_id=str(row.get("group_id") or ""),
        expected_name=str(row.get("expected_name") or ""),
        source_description=str(row.get("source_description") or ""),
        reference_time=str(row.get("reference_time") or ""),
        generation=int(row.get("generation") or 0),
        status=str(row.get("status") or PENDING),
        resolved_episode_uuid=str(row.get("resolved_episode_uuid") or "") or None,
        dispatch_allowed=dispatch_allowed,
        lease_owner=str(row.get("lease_owner") or "") or None,
        lease_token=str(row.get("lease_token") or "") or None,
        lease_generation=(
            int(row["lease_generation"])
            if row.get("lease_generation") is not None
            else None
        ),
    )
