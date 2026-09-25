"""Recall and context operations for the in-process backend adapter.

Extracted from ``backend_runtime_data_ops``: recall, view-entropy diagnostics, and context
building. Reached through ``RuntimeProviderDataOpsMixin``.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.recall import parse_query_preset

from .backend_shared import _to_jsonable


class RuntimeRecallDataOpsMixin:
    """Recall, context building, and retrieval diagnostics for the in-process backend adapter."""

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
