"""Memory lifecycle operations for the in-process backend adapter.

Extracted from ``backend_runtime_data_ops``: flag/unflag/promote, the erasure saga, and
namespace deletion. The facade module re-exports nothing from here directly -- these methods
are reached through ``RuntimeProviderDataOpsMixin``, which composes this mixin.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.domain.namespace import DEFAULT_NAMESPACE, namespace_to_group_id

from . import backend_runtime_data_ops as _ops

# The facade module's logger, fetched by name: ``logging.getLogger`` returns the same object for
# the same name, so log records keep the logger name they had before the extraction.
logger = logging.getLogger("menhir.core.backend_runtime_data_ops")


class RuntimeMemoryDataOpsMixin:
    """Flag/promote and erasure operations for the in-process backend adapter."""

    async def _require_own_memory(self, node_uuid: str) -> None:
        """Refuse a pinned caller that named another silo's memory node.

        THE reason this lives at the backend boundary rather than in each caller: these methods
        are reached from at least four places -- MCP tools, named REST routes
        (`DELETE /api/memory/{uuid}`, `/flag`, `/unflag`), the generic dispatch at
        `/api/internal/backend/{operation}`, and the CLI. Guarding them per-caller has been
        tried four times in this cluster and each fix exempted the surface added next.

        The pin is read from the request context, not taken as a parameter. A parameter is
        something a caller can forget to pass, and a caller that forgets is exactly the caller
        this defends against.

        `fetch_memory_by_uuid` is the lookup because it matches `:Entity` OR `:Episodic` and
        already accepts a namespace -- the same call `delete_memory`'s MCP guard uses, so the
        two surfaces cannot disagree about what ownership means.
        """
        await _ops.require_own_object(
            uuid=node_uuid,
            lookup=lambda uuid, **kw: self.fetch_memory_by_uuid(uuid, **kw),
            label="memory",
        )

    async def flag_memory(
        self, node_uuid: str, bootstrap_scope: str | None = None
    ) -> bool:
        await self._require_own_memory(node_uuid)
        if bootstrap_scope is None:
            return bool(
                await self._off_loop(self.built.graph_adapter.flag_memory, node_uuid)
            )
        return bool(
            await self._off_loop(
                self.built.graph_adapter.flag_memory,
                node_uuid,
                bootstrap_scope=bootstrap_scope,
            )
        )

    async def unflag_memory(self, node_uuid: str) -> bool:
        await self._require_own_memory(node_uuid)
        return bool(
            await self._off_loop(self.built.graph_adapter.unflag_memory, node_uuid)
        )

    async def promote_memory(self, node_uuid: str) -> bool:
        await self._require_own_memory(node_uuid)
        return bool(
            await self._off_loop(self.built.graph_adapter.promote_memory, node_uuid)
        )

    def _erasure(self) -> Any:
        """The erasure saga, built once per provider and cached.

        Cached because constructing it re-runs the sidecar schema check; the coordinator itself
        holds no per-request state.
        """
        existing = getattr(self, "_erasure_coordinator", None)
        if existing is None:
            from menhir.services.erasure_coordinator import ErasureCoordinator

            existing = ErasureCoordinator(graph_adapter=self.built.graph_adapter)
            self._erasure_coordinator = existing
        return existing

    async def delete_memory(self, node_uuid: str) -> bool:
        """Erase a memory: the graph node AND every sidecar row addressable to it (CF-165).

        This used to be a graph-only DETACH DELETE, which left the node's verbatim prior content
        readable in the SQLite sidecar. It now runs the durable erasure saga instead.

        Two behaviour changes worth knowing. It returns True when sidecar content was erased even
        if the graph node was already absent -- an absorbed merge participant is removed from the
        graph while its recovery snapshot survives, so that case is a real erasure, not a
        "not found". And it is journaled, so a crash mid-erasure is resumed rather than leaving
        content behind.

        The return type stays ``bool`` deliberately: widening it would break this method's
        Protocol, its HTTP client, and every MCP caller. The saga distinguishes
        ``graph_already_absent`` from ``nothing_to_erase`` internally, but callers currently
        cannot see which -- recorded as a known gap rather than smuggled through this signature.
        """
        from menhir.services.erasure_coordinator import DELETION_SUCCEEDED_REASONS

        # No guard here: this delegates to erase_memory, which carries it. A second call would
        # double the lookups and, worse, invite the two to drift apart.
        outcome = await self.erase_memory(node_uuid)
        # Allowlist, not "anything except nothing_to_erase". That predicate answered True for
        # every reason it did not know about, so a failed PREPARE and a quarantined
        # residual-content outcome both reported the memory as deleted, and each reason added
        # later inherited the same default. Unknown reasons now read as failure.
        return outcome.get("reason") in DELETION_SUCCEEDED_REASONS

    async def erase_memory(self, node_uuid: str) -> dict[str, Any]:
        """Erase a memory and report which outcome occurred (CF-165).

        The richer sibling of delete_memory. ``graph_already_absent`` is the case a bool
        cannot express: the node was gone from the graph, but sidecar content WAS erased --
        which is what a merge leaves behind for its absorbed participant.
        """
        await self._require_own_memory(node_uuid)
        outcome = await self._off_loop(self._erasure().erase_memory, node_uuid)
        return dict(outcome or {})

    async def delete_namespace(
        self,
        namespace: str,
        *,
        max_nodes: int = 200,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete all memory in a namespace silo. Refuses the default/shared namespace.

        Safety gate: counts nodes first. If the count exceeds `max_nodes`, refuses
        (ValueError, reporting the actual count) unless `force=True`. `dry_run=True`
        only reports the count/would-delete decision -- no deletion, regardless of
        `force`. This is a blast-radius guard, not a backup -- deletion is still
        irreversible once it proceeds.
        """
        if not namespace or not namespace.strip() or namespace == DEFAULT_NAMESPACE:
            raise ValueError(f"refusing to delete the default/shared namespace: {namespace!r}")

        # A pinned caller may only tear down its OWN silo. This is the one operation where the
        # namespace is the target rather than a filter, so the pin cannot be "applied" to it in
        # the usual sense -- injecting the pin would silently retarget the deletion at the
        # caller's own namespace, destroying real data on what the caller believes is a
        # different request. Refused, loudly, instead.
        #
        # At the backend boundary because `DELETE /api/namespace/{namespace}` takes the target
        # from the URL path and never consulted the pin at all.
        pin = _ops.pinned_namespace()
        if pin and namespace.strip() != pin:
            raise PermissionError(
                f"Refused: this client is pinned to namespace {pin!r} and cannot delete "
                f"namespace {namespace.strip()!r}."
            )
        group_id = namespace_to_group_id(namespace)
        if group_id == "":
            raise ValueError("refusing to delete the default graphiti group")

        node_count = await self._off_loop(
            self.built.graph_adapter.count_namespace, group_id, namespace=namespace
        )
        would_delete = force or node_count <= max_nodes

        if dry_run:
            return {
                "namespace": namespace,
                "node_count": int(node_count),
                "max_nodes": max_nodes,
                "would_delete": bool(would_delete),
                "dry_run": True,
            }

        if not would_delete:
            raise ValueError(
                f"namespace {namespace!r} has {node_count} nodes, exceeding the safety "
                f"limit of {max_nodes}; pass force=true to delete anyway, or dry_run=true "
                f"to inspect the count first without deleting anything"
            )

        # Erasure, not a bare graph delete: the namespace's members are captured before the
        # partition is destroyed, then their sidecar content is purged too. The blast-radius gate
        # above is unchanged and still runs first.
        outcome = await self._off_loop(
            self._erasure().erase_namespace, group_id, namespace=namespace
        )
        # An abstain must not reduce to ``deleted: 0``. That is the same "failed read reads as
        # an empty set" conflation the coordinator now refuses, one layer up: the response shape
        # below cannot express "did not run", so an operator would see a namespace reported as
        # empty when in fact nothing was attempted. Raise instead -- the erasure touched nothing,
        # so retrying once the graph is reachable is safe.
        from menhir.services.erasure_coordinator import (
            ERASED,
            GRAPH_ALREADY_ABSENT,
            MEMBERSHIP_CAPTURE_FAILED,
            RESIDUAL_CONTENT,
        )

        reason = outcome.get("reason")
        if reason == MEMBERSHIP_CAPTURE_FAILED:
            raise RuntimeError(
                f"erasure of namespace {namespace!r} abstained: its membership could not be "
                f"enumerated, so nothing was deleted. Retry when the graph is reachable."
            )
        if reason == RESIDUAL_CONTENT:
            # The purge did not cover what it claimed and the saga is quarantined for review.
            # Returning a row count here would report a quarantined operation as a completed
            # deletion, which is the failure mode this whole finding is about.
            raise RuntimeError(
                f"erasure of namespace {namespace!r} left residual content after the purge and "
                f"has been quarantined for review (op_id={outcome.get('op_id')}); "
                f"residual={outcome.get('residual')}"
            )

        unaddressable = outcome.get("unaddressable") or []
        not_covered = outcome.get("not_covered") or []
        if unaddressable or not_covered:
            logger.warning(
                "erasure %s for namespace %s did not remove everything: unaddressable=%s "
                "not_covered=%s",
                outcome.get("op_id"),
                namespace,
                sorted(unaddressable),
                sorted(not_covered),
            )
        logger.info(
            "erasure %s for namespace %s: reason=%s purged=%s",
            outcome.get("op_id"),
            namespace,
            reason,
            outcome.get("purged"),
        )
        # The response now carries completeness. The previous shape was justified on the grounds
        # that a documented contract is not the place to smuggle new fields -- true while the
        # extra detail was merely diagnostic, and false now: whether the erasure actually removed
        # everything is not a detail about the answer, it IS the answer. Flattening it meant the
        # coordinator's "I could not cover this content class" was computed, logged, and then
        # discarded one layer below the operator who needed it.
        return {
            "namespace": namespace,
            "deleted": int(outcome.get("graph_deleted") or 0),
            "reason": reason,
            # False whenever a content class was left behind, whether because nothing can ever
            # address it or because THIS operation could not derive its subjects.
            "complete": bool(
                reason in (ERASED, GRAPH_ALREADY_ABSENT)
                and not unaddressable
                and not not_covered
            ),
            "unaddressable": sorted(unaddressable),
            "not_covered": sorted(not_covered),
        }
