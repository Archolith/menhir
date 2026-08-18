"""Operation methods for the in-process backend adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from menhir.domain.namespace import DEFAULT_NAMESPACE, namespace_to_group_id
from menhir.domain.recall import parse_query_preset
from menhir.domain.session import new_session
from menhir.infrastructure.providers import ProviderConfig
from menhir.infrastructure.text_io import read_text_utf8
from menhir.services.project_ingest import build_project_narrative

from .request_context import get_request_tier
from .backend_shared import (
    _project_scan_from_dict,
    _push_background_error,
    _to_jsonable,
)

logger = logging.getLogger(__name__)


class RuntimeProviderDataOpsMixin:
    """Read/ingest/structure operations for the in-process backend adapter."""

    async def queue_episode(
        self,
        text: str,
        *,
        user_id: str,
        session_id: str,
        source: str = "mcp",
        diff: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        occurred_at: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        session = new_session(user_id, session_id=session_id)
        queue_kwargs: dict[str, Any] = {
            "diff": diff,
            "flagged": flagged,
            "namespace": namespace,
            "occurred_at": occurred_at,
            "turn_evidence_uuid": turn_evidence_uuid,
        }
        if bootstrap_scope is not None:
            queue_kwargs["bootstrap_scope"] = bootstrap_scope
        result = await self.built.ingest_service.queue_episode_for_enrichment(
            text, session, source, **queue_kwargs
        )
        return _to_jsonable(result)

    async def flag_memory(
        self, node_uuid: str, bootstrap_scope: str | None = None
    ) -> bool:
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
        return bool(
            await self._off_loop(self.built.graph_adapter.unflag_memory, node_uuid)
        )

    async def promote_memory(self, node_uuid: str) -> bool:
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

    async def enqueue_pending_episode(self, episode_uuid: str) -> bool:
        return bool(
            await self.built.ingest_service.enqueue_pending_episode(episode_uuid)
        )

    async def recall(
        self,
        query: str,
        *,
        preset: str = "knowledge",
        limit: int = 10,
        include_session: bool = False,
        include_superseded: bool = False,
        wait_for_pending: bool = False,
        file_context: str | None = None,
        file_context_project: str | None = None,
        namespace: str | None = None,
        include_invalidated: bool = False,
        trace: bool = False,
    ) -> dict[str, Any]:
        # Map the env-driven frontier portions into the recall call. With no
        # MENHIR_FRONTIER_* set this is all-off -> today's ScoringService path; trace is
        # driven by the shadow flag so the observe-only pass becomes reachable in prod.
        # Defensive: a settings object without the frontier surface (older stubs) falls
        # back to default tuning (all portions off) so recall behaves exactly as before.
        settings = getattr(self.built, "settings", None)
        tuning = settings.retrieval_tuning() if hasattr(settings, "retrieval_tuning") else None
        trace = trace or bool(getattr(settings, "frontier_shadow", False))
        # Only thread the frontier params when there is config to pass, so the call shape
        # is unchanged (and older recall stubs keep working) when no portion is enabled.
        frontier_kwargs: dict[str, Any] = {}
        if tuning is not None:
            frontier_kwargs["tuning"] = tuning
        if trace:
            frontier_kwargs["trace"] = trace
        result = await self.built.recall_service.recall(
            query,
            preset=parse_query_preset(preset),
            limit=limit,
            include_session=include_session,
            include_superseded=include_superseded,
            wait_for_pending=wait_for_pending,
            file_context=file_context,
            file_context_project=file_context_project,
            namespace=namespace,
            include_invalidated=include_invalidated,
            **frontier_kwargs,
        )
        payload = _to_jsonable(result)
        # 7.J flag-off wire compatibility: RecallResult carries optional structured layers
        # internally, but disabled/no-verdict responses retain the historical JSON shape.
        if payload.get("authority_layer") is None:
            payload.pop("authority_layer", None)
        if payload.get("event_authority_layer") is None:
            payload.pop("event_authority_layer", None)
        return payload

    async def view_entropy(
        self,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        top_k: int = 20,
        max_views: int = 50,
    ) -> dict[str, Any]:
        from menhir.services.view_entropy import probe_view_reachability

        result = await probe_view_reachability(
            recall_service=self.built.recall_service,
            graph_adapter=self.built.graph_adapter,
            namespace=namespace,
            kind=kind,
            top_k=top_k,
            max_views=max_views,
        )
        return _to_jsonable(result)

    async def build_context(
        self,
        query: str,
        *,
        max_tokens: int = 4000,
        preset: str = "knowledge",
        session_id: str | None = None,
        include_scores: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        result = await self.built.context_builder.build_context(
            query,
            max_tokens=max_tokens,
            preset=parse_query_preset(preset),
            session_id=session_id or self._effective_session_id(),
            include_scores=include_scores,
            namespace=namespace,
        )
        return _to_jsonable(result)

    async def fetch_memory_by_uuid(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {}
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_memory_by_uuid, node_uuid, **kwargs
            )
        )

    async def fetch_node_receipts(self, node_uuid: str) -> dict[str, Any] | None:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_node_receipts, node_uuid
            )
        )

    async def fetch_recent_memories(
        self, limit: int = 20, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"limit": limit}
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.fetch_recent_memories, **kwargs)
        )

    async def fetch_flagged_memories(
        self, limit: int = 50, workspace: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"limit": limit}
        if workspace is not None:
            kwargs["workspace"] = workspace
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.fetch_flagged_memories, **kwargs)
        )

    async def fetch_flagged_memory_bootstrap_version(
        self, workspace: str | None = None
    ) -> str:
        if workspace is None:
            return str(
                await self._off_loop(
                    self.built.graph_adapter.fetch_flagged_memory_bootstrap_version
                )
            )
        return str(
            await self._off_loop(
                self.built.graph_adapter.fetch_flagged_memory_bootstrap_version,
                workspace=workspace,
            )
        )

    async def fetch_memories_by_scope(
        self, scope: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"limit": limit}
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_memories_by_scope, scope, **kwargs
            )
        )

    async def fetch_memories_by_type(
        self, memory_type: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"limit": limit}
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_memories_by_type,
                memory_type,
                **kwargs,
            )
        )

    async def get_scan_fingerprint(self, project_name: str) -> str | None:
        return await self._off_loop(
            self.built.graph_adapter.get_scan_fingerprint, project_name
        )

    async def ingest_document(
        self,
        path: str,
        *,
        project: str | None,
        session_id: str,
        user_id: str,
        document_type: str = "generic",
    ) -> dict[str, Any]:
        import asyncio

        from menhir.core.ingest_guard import ensure_ingest_path_allowed
        # SEC-02: confine non-operator callers to the allowed ingest roots before reading.
        p = ensure_ingest_path_allowed(path, tier=get_request_tier())
        content_raw = await asyncio.to_thread(read_text_utf8, p)
        structure_project = project or p.parent.name or p.stem
        structure_path = str(p.resolve())
        content_excerpt = content_raw[:2000]
        narrative = content_raw[:4000]

        await asyncio.to_thread(
            self.built.graph_adapter.write_document,
            str(p),
            content_excerpt,
            project=structure_project,
            structure_path=structure_path,
            session_id=session_id,
            user_id=user_id,
            document_type=document_type,
        )
        return {
            "entity_written": True,
            "structure_project": structure_project,
            "structure_path": structure_path,
            "content_length": len(content_raw),
            "document_type": document_type,
            "narrative": f'Document "{p.name}" ({structure_project}):\n\n{narrative}',
        }

    async def scan_and_write_project(
        self,
        path: str,
        *,
        name: str | None,
        force: bool,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        import asyncio
        import logging as _logging
        from menhir.core.ingest_guard import ensure_ingest_path_allowed
        from menhir.infrastructure.project_scanner import ProjectScanner
        _log = _logging.getLogger(__name__)
        # SEC-02: confine non-operator callers to the allowed ingest roots before scanning.
        path = str(ensure_ingest_path_allowed(path, tier=get_request_tier()))
        project_name = name or path.rstrip("/\\").split("/")[-1].split("\\")[-1]
        scanner = ProjectScanner()
        scan = await asyncio.to_thread(scanner.scan, path, project_name)

        if not force:
            stored_fp = await self._off_loop(
                self.built.graph_adapter.get_scan_fingerprint, project_name
            )
            if stored_fp and stored_fp == scan.scan_fingerprint:
                return {"counts": {}, "narrative": "", "skipped": True}
            _log.debug(
                "Fingerprint mismatch: project=%s stored=%s computed=%s",
                project_name,
                stored_fp,
                scan.scan_fingerprint,
            )

        narrative = build_project_narrative(scan)
        meta = {
            "dirs": len(scan.directories),
            "files": len(scan.files),
            "deps": len(scan.dependencies),
            "endpoints": len(scan.endpoints),
            "imports": len(scan.imports),
            "test_edges": len(scan.test_edges),
            "cross_refs": len(scan.cross_project_refs),
            "symbols": len(scan.symbols),
            "call_edges": len(scan.call_edges),
        }

        # Write in background — Neo4j MERGE for thousands of symbols/edges can exceed
        # the HTTP client timeout.  Scan meta is available immediately; write completes
        # asynchronously and is logged on completion.
        async def _do_write() -> None:
            try:
                counts = await asyncio.to_thread(
                    self.built.graph_adapter.write_project_structure,
                    scan,
                    session_id,
                    user_id,
                )
                _log.info(
                    "scan_and_write_project complete: project=%s entities=%s edges=%s symbols=%d call_edges=%d",
                    project_name,
                    counts.get("entities"),
                    counts.get("edges"),
                    len(scan.symbols),
                    len(scan.call_edges),
                )
            except Exception as exc:
                _log.warning(
                    "scan_and_write_project write failed: project=%s error=%s",
                    project_name,
                    exc,
                )
                _push_background_error(
                    session_id, f"ingest {project_name} failed: {exc}"
                )

        asyncio.create_task(_do_write(), name=f"menhir-ingest-{project_name}")
        return {
            "counts": {},
            "narrative": narrative,
            "skipped": False,
            "meta": meta,
            "background": True,
        }

    async def write_project_structure(
        self, scan: dict[str, Any], *, session_id: str, user_id: str
    ) -> dict[str, int]:
        import asyncio as _asyncio

        scan_obj = _project_scan_from_dict(scan)
        # If 'symbols' key is absent, the sender is an older MCP process that
        # predates symbol extraction.  Write the structural data now (fast), then
        # fire off a background task to rescan and fill in symbol nodes.
        if "symbols" not in scan:
            root = scan_obj.root_path
            name = scan_obj.name
            _asyncio.create_task(
                self._background_symbol_rescan(root, name, session_id, user_id),
                name=f"menhir-symbol-rescan-{name}",
            )
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.write_project_structure,
                scan_obj,
                session_id,
                user_id,
            )
        )

    async def _background_symbol_rescan(
        self, root: str, name: str, session_id: str, user_id: str
    ) -> None:
        import asyncio as _asyncio
        import logging as _logging
        import os as _os
        from menhir.infrastructure.project_scanner import ProjectScanner as _PS

        _log = _logging.getLogger(__name__)
        if not root or not _os.path.isdir(root):
            return
        try:
            fresh_scan = await _asyncio.to_thread(_PS().scan, root, name)
            await _asyncio.to_thread(
                self.built.graph_adapter.write_project_structure,
                fresh_scan,
                session_id,
                user_id,
            )
            _log.info(
                "Background symbol rescan complete: project=%s symbols=%d",
                name,
                len(fresh_scan.symbols),
            )
        except Exception as exc:
            _log.warning(
                "Background symbol rescan failed: project=%s error=%s", name, exc
            )
            _push_background_error(session_id, f"symbol-rescan {name} failed: {exc}")

    async def query_structure(
        self, project: str, query_type: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        if query_type == "projects":
            return _to_jsonable(
                await self._off_loop(self.built.graph_adapter.list_structure_projects)
            )
        if query_type == "orphan_structure_projects":
            return _to_jsonable(
                await self._off_loop(self.built.graph_adapter.list_orphan_structure_projects)
            )
        if query_type == "documents":
            # params: optional path_filter (via "path"), document_type (via "doc_type")
            path_filter = params.get("path", "") if params else ""
            doc_type = params.get("doc_type") if params else None
            return _to_jsonable(
                await self._off_loop(
                    self.built.graph_adapter.query_documents,
                    project,
                    path_filter=path_filter,
                    document_type=doc_type,
                )
            )
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.query_structure,
                project,
                query_type,
                **(params or {}),
            )
        )
