"""Temporal and candidate memory operations for the in-process backend adapter."""

from __future__ import annotations

from typing import Any

from .backend_shared import _to_jsonable


class RuntimeMemoryAdminOpsMixin:
    """Temporal and candidate memory operations for the in-process backend adapter."""

    async def create_temporal(
        self,
        *,
        content: str,
        target_date: str,
        source: str = "claude-code",
        name: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        from menhir.services.ingest_limits import validate_memory_payload

        validate_memory_payload(content)
        kwargs: dict[str, Any] = {
            "content": content,
            "target_date": target_date,
            "source": source,
            "name": name,
            "flagged": flagged,
            "namespace": namespace,
            "turn_evidence_uuid": turn_evidence_uuid,
        }
        if bootstrap_scope is not None:
            kwargs["bootstrap_scope"] = bootstrap_scope
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.create_temporal, **kwargs)
        )

    async def list_temporal_in_window(
        self, *, window_days: int = 30, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_temporal_in_window,
                window_days=window_days,
                namespace=namespace,
            )
        )

    async def complete_temporal(self, uuid: str) -> bool:
        return bool(
            await self._off_loop(self.built.graph_adapter.complete_temporal, uuid)
        )

    async def create_candidate(
        self,
        *,
        content: str,
        source: str,
        cluster_id: str,
        label: str,
        kind: str = "memory",
        candidate_type: str = "other",
        type: str = "SEMANTIC",
        evidence_strength: str = "REPEATED",
        distinct_sessions: int = 0,
        first_seen: str | None = None,
        last_seen: str | None = None,
        notes: list[str] | None = None,
        source_confidence: float = 0.5,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.create_candidate,
                content=content,
                source=source,
                cluster_id=cluster_id,
                label=label,
                kind=kind,
                candidate_type=candidate_type,
                type=type,
                evidence_strength=evidence_strength,
                distinct_sessions=distinct_sessions,
                first_seen=first_seen,
                last_seen=last_seen,
                notes=notes,
                source_confidence=source_confidence,
                namespace=namespace,
            )
        )

    async def list_candidates(
        self, *, source: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_candidates,
                source=source,
                limit=limit,
            )
        )

    async def fetch_candidate(self, uuid: str) -> dict[str, Any] | None:
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.fetch_candidate, uuid)
        )

    async def promote_candidate(self, uuid: str) -> bool:
        return bool(
            await self._off_loop(self.built.graph_adapter.promote_candidate, uuid)
        )

    async def reject_candidate(self, uuid: str) -> bool:
        return bool(
            await self._off_loop(self.built.graph_adapter.reject_candidate, uuid)
        )

    async def approve_candidate(self, uuid: str) -> dict[str, Any]:
        # Service-level approval: promote + synchronous contradiction check.
        return _to_jsonable(await self.built.candidate_service.approve(uuid))
