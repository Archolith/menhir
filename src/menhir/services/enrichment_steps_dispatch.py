"""Dual-path helper: timeout-bounded Graphiti add_episode.

Extracted verbatim from ``enrichment_steps.py``; the facade module re-exports everything
here so existing ``menhir.services.enrichment_steps`` import sites keep working unchanged.
``record_lifecycle_event`` is resolved through the facade module at call time so
monkeypatching ``menhir.services.enrichment_steps.<name>`` keeps affecting this wrapper,
exactly as when everything lived in one module.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable

from menhir.domain.self_identity import (
    SelfEvidenceKind,
    SelfIdentityContext,
    SelfSubjectEndpointEnvelope,
)
from menhir.infrastructure.self_binding import (
    InvalidSelfSubjectDeclarationError,
    SelfBindMode,
)
from menhir.infrastructure.graphiti_patches import begin_extraction_receipt, clear_extraction_receipt

from menhir.infrastructure import GraphitiClient

#: Same logger object/name as the facade module: every record this wrapper emits must keep
#: the ``menhir.services.enrichment_steps`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.enrichment_steps")


# ---------------------------------------------------------------------------
# Dual-path helper — timeout-bounded Graphiti add_episode
# ---------------------------------------------------------------------------

async def add_episode_with_timeout(
    graphiti_client: GraphitiClient,
    *,
    name: str,
    episode_body: str,
    source_description: str,
    reference_time: datetime,
    episode_uuid: str | None = None,
    attempt: int = 1,
    timeout_s: float = 300.0,
    group_id: str = "",
    relationless_repair_context_loader: Callable[[], tuple[str, ...]] | None = None,
    self_identity: SelfIdentityContext | None = None,
    self_subject_endpoint: SelfSubjectEndpointEnvelope | None = None,
    self_bind_mode: SelfBindMode = SelfBindMode.OFF,
) -> Any:
    """Bound one Graphiti add_episode call so stuck requests fail back into retry flow.

    Takes explicit params (not ctx) because the legacy ``ingest_episode()``
    path also calls this function.

    ``self_identity`` carries the LOGICAL namespace and the trusted author evidence, separately
    from ``group_id``, which is the physical Graphiti partition. Logical ``default`` maps to
    physical ``""``, so identity must never be inferred from ``group_id``. Callers that cannot
    prove an author omit it and no self binding occurs.
    """

    from menhir.services import enrichment_steps as _facade

    receipt_episode_key = str(episode_uuid or "").strip()
    if self_subject_endpoint is not None:
        if self_bind_mode is not SelfBindMode.ENFORCE:
            raise InvalidSelfSubjectDeclarationError(
                "a self-subject endpoint may be dispatched only in enforce mode"
            )
        if not receipt_episode_key or self_subject_endpoint.episode_uuid != receipt_episode_key:
            raise InvalidSelfSubjectDeclarationError(
                "self-subject endpoint does not belong to the active pending episode"
            )
        if self_identity is None or self_subject_endpoint.namespace != self_identity.namespace:
            raise InvalidSelfSubjectDeclarationError(
                "self-subject endpoint namespace does not match its identity context"
            )
        if self_subject_endpoint.turn_evidence_uuid != str(
            self_identity.turn_evidence_uuid or ""
        ).strip():
            raise InvalidSelfSubjectDeclarationError(
                "self-subject endpoint turn does not match its identity context"
            )
    if (
        self_identity is not None
        and self_identity.evidence_kind is SelfEvidenceKind.EXPLICIT_SELF_SUBJECT
    ):
        # ``name`` is a display/reconciliation anchor, not an episode identifier. Allowing it to
        # stand in here lets a declaration scoped to one pending episode authorize an unrelated
        # Graphiti request that happens to reuse the same name. A structured declaration therefore
        # requires the external pending-episode UUID that owns this exact extraction invocation.
        if not receipt_episode_key:
            raise InvalidSelfSubjectDeclarationError(
                "an exact self-subject declaration requires the pending episode UUID; "
                "the episode name is not identity scope"
            )
        if str(self_identity.episode_uuid or "").strip() != receipt_episode_key:
            raise InvalidSelfSubjectDeclarationError(
                f"declared self subject belongs to episode {self_identity.episode_uuid!r}, not "
                f"pending episode {episode_uuid!r}; refusing Graphiti dispatch"
            )

    _facade.record_lifecycle_event(
        component="ingest_worker",
        event="entered_add_episode_timeout_wrapper",
        state="started",
        episode_uuid=episode_uuid,
        details={
            "name": name,
            "timeout_s": timeout_s,
        },
    )
    # Activate the combined-extraction receipt in THIS (parent) task, BEFORE the
    # asyncio.wait_for below. wait_for schedules graphiti_client.add_episode as a
    # separate Task with its own COPIED context, so a receipt created inside that call
    # (or the nested Graphiti add_episode task) would never be visible to the parent
    # task that later runs stamp_and_finalize. Setting the mutable receipt here means
    # both the wait_for child and Graphiti's own child task inherit the same object and
    # mutate it in place, which the parent then reads. (episode_body is the current-
    # episode text used to gate endpoint synthesis.)
    begin_extraction_receipt(
        receipt_episode_key or name,
        episode_body,
        source_description=source_description,
        relationless_repair_context_loader=relationless_repair_context_loader,
        self_identity=self_identity,
        self_subject_endpoint=self_subject_endpoint,
        self_bind_mode=self_bind_mode,
    )
    try:
        _facade.record_lifecycle_event(
            component="ingest_worker",
            event="add_episode_timeout_wrapper",
            state="started",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "timeout_s": timeout_s,
            },
        )
        result = await asyncio.wait_for(
            graphiti_client.add_episode(
                name=name,
                episode_body=episode_body,
                source_description=source_description,
                reference_time=reference_time,
                episode_uuid=episode_uuid,
                attempt=attempt,
                group_id=group_id,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        # stamp_and_finalize will not run for this episode; drop the receipt so a reused
        # worker task cannot carry stale extraction state into the next episode.
        clear_extraction_receipt()
        _facade.record_lifecycle_event(
            component="ingest_worker",
            event="add_episode_timeout_wrapper",
            state="timeout",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "timeout_s": timeout_s,
            },
        )
        raise TimeoutError(
            "graphiti add_episode timed out after "
            f"{timeout_s:.1f}s; remote completion status unknown"
        ) from exc
    except Exception:  # re-raised; record telemetry for any graphiti failure
        clear_extraction_receipt()
        _facade.record_lifecycle_event(
            component="ingest_worker",
            event="add_episode_timeout_wrapper",
            state="failed",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "timeout_s": timeout_s,
            },
        )
        raise
    else:
        _facade.record_lifecycle_event(
            component="ingest_worker",
            event="add_episode_timeout_wrapper",
            state="completed",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "timeout_s": timeout_s,
            },
        )
        return result
