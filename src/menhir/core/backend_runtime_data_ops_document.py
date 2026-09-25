"""Document ingest for the in-process backend adapter.

Extracted from ``backend_runtime_data_ops``: single-document ingest with identity settlement.
Reached through ``RuntimeProviderDataOpsMixin``.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.text_io import read_text_utf8

from . import backend_runtime_data_ops as _ops


class RuntimeDocumentDataOpsMixin:
    """Document ingest for the in-process backend adapter."""

    async def ingest_document(
        self,
        path: str,
        *,
        project: str | None,
        session_id: str,
        user_id: str,
        document_type: str = "generic",
        identity_action: str | None = None,
        adopt_project_id: str | None = None,
    ) -> dict[str, Any]:
        import asyncio

        from menhir.core.ingest_guard import ensure_ingest_path_allowed
        from menhir.domain.project_identity import OPERATOR_TIER, ProjectIdentityRefused
        from menhir.services.project_identity_service import settle_project_identity

        # SEC-02: confine non-operator callers to the allowed ingest roots before reading.
        tier = _ops.get_request_tier()
        p = ensure_ingest_path_allowed(path, tier=tier)
        structure_project = project or p.parent.name or p.stem
        structure_path = str(p.resolve())
        recorded_root_path = await self._off_loop(
            self.built.graph_adapter.get_project_root_path, structure_project
        )
        identity_root = recorded_root_path or str(p.parent.resolve())

        if identity_action and tier != OPERATOR_TIER:
            raise ProjectIdentityRefused(
                f"identity_action={identity_action!r} transfers a project identity and requires "
                f"{OPERATOR_TIER} tier; this request is {tier!r}. Document ingest does not "
                "require it."
            )

        # A document is a structural writer just as a project scan is. Settle before reading the
        # content or constructing narrative eligibility, so a decision response cannot drift into
        # either the structural graph or the semantic queue through a later branch.
        claim, resolution = await asyncio.to_thread(
            settle_project_identity,
            self.built.graph_adapter,
            root_path=identity_root,
            display_name=structure_project,
            identity_action=identity_action,
            adopt_project_id=adopt_project_id,
        )
        if claim is None:
            return resolution.as_dict()

        content_raw = await asyncio.to_thread(read_text_utf8, p)
        content_excerpt = content_raw[:2000]
        narrative = content_raw[:4000]

        await asyncio.to_thread(
            self.built.graph_adapter.write_document,
            str(p),
            content_excerpt,
            project=structure_project,
            structure_path=structure_path,
            project_id=claim.project_id,
            identity_generation=claim.generation,
            identity_root=identity_root,
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
