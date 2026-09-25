"""Operation methods for the in-process backend adapter.

The operation groups live in sibling modules (``backend_runtime_data_ops_<aspect>.py``); this
module composes them and keeps the project-scan/structure writers, whose identity guards are
pinned to this module by tests and by the structure-writer census.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.services.project_ingest import build_project_narrative

from .request_context import get_request_tier
from .backend_shared import (
    _project_scan_from_dict,
    _push_background_error,
    _to_jsonable,
)
from .backend_runtime_data_ops_queue import RuntimeQueueDataOpsMixin
from .backend_runtime_data_ops_memory import RuntimeMemoryDataOpsMixin
from .backend_runtime_data_ops_recall import RuntimeRecallDataOpsMixin
from .backend_runtime_data_ops_fetch import RuntimeFetchDataOpsMixin
from .backend_runtime_data_ops_document import RuntimeDocumentDataOpsMixin
from .backend_runtime_data_ops_structure import RuntimeStructureDataOpsMixin

logger = logging.getLogger(__name__)


class RuntimeProviderDataOpsMixin(
    RuntimeQueueDataOpsMixin,
    RuntimeMemoryDataOpsMixin,
    RuntimeRecallDataOpsMixin,
    RuntimeFetchDataOpsMixin,
    RuntimeDocumentDataOpsMixin,
    RuntimeStructureDataOpsMixin,
):
    """Read/ingest/structure operations for the in-process backend adapter."""

    async def scan_and_write_project(
        self,
        path: str,
        *,
        name: str | None,
        force: bool,
        session_id: str,
        user_id: str,
        force_identity: bool = False,
        identity_action: str | None = None,
        adopt_project_id: str | None = None,
    ) -> dict[str, Any]:
        import asyncio
        import logging as _logging
        from menhir.core.ingest_guard import ensure_ingest_path_allowed
        from menhir.infrastructure.project_scanner import ProjectScanner
        _log = _logging.getLogger(__name__)
        from menhir.domain.project_identity import ensure_scan_root_owns_identity
        from menhir.infrastructure.repo_topology import classify_root
        # SEC-02: confine non-operator callers to the allowed ingest roots before scanning.
        tier = get_request_tier()
        path = str(ensure_ingest_path_allowed(path, tier=tier))
        project_name = name or path.rstrip("/\\").split("/")[-1].split("\\")[-1]

        # CF-257 phase 0. Project identity is a directory basename, so a worktree, a submodule or
        # a fork sharing that basename writes into the canonical project's silo -- and because the
        # fingerprint is looked up by the same name it mismatches, so the scan runs in full and
        # the per-project stale prune deletes the rows that copy does not have.
        #
        # Ordered BEFORE the scan, not before the write: scanning a large tree we are going to
        # refuse costs minutes for nothing, and the refusal does not depend on anything the scan
        # produces.
        topology = await asyncio.to_thread(classify_root, path)
        recorded_root_path = await self._off_loop(
            self.built.graph_adapter.get_project_root_path, project_name
        )
        ensure_scan_root_owns_identity(
            topology=topology,
            project_name=project_name,
            recorded_root_path=recorded_root_path,
            tier=tier,
            force=force_identity,
        )

        # CF-257 phase 1. Settle WHICH identity this scan writes under, before scanning: an
        # undecidable identity is not worth a multi-minute walk, and the decision cannot depend on
        # anything the scan produces. `project_id` is stamped alongside the name -- the MERGE key
        # is still the name until phase 3, so this records the identity without yet relying on it.
        from menhir.domain.project_identity import OPERATOR_TIER, ProjectIdentityRefused
        from menhir.services.project_identity_service import settle_project_identity

        # `scan_and_write_project` is agent tier by design -- scanning is ordinary work. But
        # `adopt` and `new` TRANSFER an identity: adopt re-points an existing project's id at a
        # different directory, and new abandons the id a checkout currently holds. Left at agent
        # tier, any caller could submit an arbitrary adopt_project_id and rebind a project it has
        # no relationship to. Scanning stays agent; changing which project a directory IS does not.
        #
        # FAIL CLOSED. `tier and tier != OPERATOR_TIER` let an UNBOUND tier through, and
        # `get_request_tier()` returns "" whenever no auth is configured or the ContextVar was
        # never set for this call -- so the gate was open on exactly the deployments least able
        # to notice. Identity transfer and `force_identity` both cross an identity boundary, so
        # both require the tier to be present AND to be operator. Ordinary scans remain available
        # without either override.
        if identity_action and tier != OPERATOR_TIER:
            raise ProjectIdentityRefused(
                f"identity_action={identity_action!r} transfers a project identity and requires "
                f"{OPERATOR_TIER} tier; this request is {tier!r}. Scanning does not require it."
            )

        claim, resolution = await asyncio.to_thread(
            settle_project_identity,
            self.built.graph_adapter,
            root_path=path,
            display_name=project_name,
            identity_action=identity_action,
            adopt_project_id=adopt_project_id,
        )
        if claim is None:
            # A typed result, not an exception: the callers are one-shot MCP and HTTP requests
            # with no interactive channel, and the watcher is unattended. Returning the payload
            # lets each decide -- retry with an action, or skip and report.
            return resolution.as_dict()

        scanner = ProjectScanner()
        scan = await asyncio.to_thread(scanner.scan, path, project_name)
        # Carried on the scan so every writer under `write_project` stamps it without a second
        # parameter threaded through four batch helpers.
        scan.project_id = claim.project_id
        scan.identity_generation = claim.generation

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
                # CF-257 phase 0. The ownership decision above was made BEFORE the scan, and
                # scanning a large tree takes minutes; this task then runs later still. In that
                # window another root can claim the name, or this directory can become a
                # worktree -- after which the write below would land under an identity that is no
                # longer this root's, and the per-project stale prune would delete the other
                # copy's files. Re-checked here for the same reason `_background_symbol_rescan`
                # re-checks: a detached task cannot inherit a decision's freshness, only its
                # value. The root was scannable moments ago, so shape is observable and the full
                # refusal applies.
                from menhir.domain.project_identity import (
                    ProjectIdentityRefused, ensure_scan_root_owns_identity,
                )
                from menhir.infrastructure.repo_topology import classify_root
                try:
                    ensure_scan_root_owns_identity(
                        topology=await asyncio.to_thread(classify_root, path),
                        project_name=project_name,
                        recorded_root_path=await self._off_loop(
                            self.built.graph_adapter.get_project_root_path, project_name
                        ),
                        tier=tier,
                        force=force_identity,
                    )
                except ProjectIdentityRefused as exc:
                    _log.warning(
                        "scan_and_write_project write refused: project=%s error=%s",
                        project_name, exc,
                    )
                    _push_background_error(
                        session_id, f"ingest {project_name} refused: {exc}"
                    )
                    return
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
        """DEPRECATED (CF-257) -- removed after phase 3. Use `scan_and_write_project`.

        This writes a structure payload the CALLER produced and judges it with a `root_path` the
        same caller supplied, so the server classifies a path string rather than the directory
        that produced the payload. A stale or secondary checkout reporting the canonical path is
        accepted, and the write carries the per-project stale prune -- so it deletes rows. No
        metadata check closes that, because every input to the check is caller-controlled.

        Operator tier is a MIGRATION BRIDGE, not the design: it makes the failure loud for
        agent-tier callers while legitimate use is measured. Removal is gated on an observation
        window through phase 3 showing no admitted calls, plus a release note.
        """
        import asyncio as _asyncio

        from menhir.core.ingest_guard import ensure_ingest_path_allowed
        from menhir.domain.project_identity import (
            ProjectIdentityRefused,
            ensure_scan_root_owns_identity,
        )
        from menhir.infrastructure.repo_topology import classify_root

        scan_obj = _project_scan_from_dict(scan)

        # CF-257 phase 0, second entry point. `scan_and_write_project` scans and writes; THIS
        # writes a scan the caller already produced, at agent tier, so guarding only the former
        # left the refusals bypassable by choosing this endpoint instead -- an older or
        # client-side scanner can submit a worktree's or a fork's structure under a canonical
        # project's name and it lands unchallenged.
        #
        # This is the same miss SEC-02 records in the block below ("The guard was applied to that
        # sibling and skipped here"), in the same function, one guard later. A new refusal has to
        # be applied to every writer, not to the path where the refusal was conceived.
        #
        # `allow_unobservable_root`: the payload's root_path may legitimately name a directory
        # that exists only on the sender's machine, so shape cannot be observed here. That makes
        # the worktree/submodule refusal unavailable BY CONSTRUCTION on this path -- stated
        # rather than silently true -- while the recorded-root refusal, which is what catches a
        # fork, still applies with full force.
        identity_tier = get_request_tier()
        # CF-257. Identity here is VALIDATED against the graph, never SETTLED from the payload.
        #
        # Settling was the bypass. `settle_project_identity` mints and binds, so calling it with a
        # caller-supplied `root_path` let this endpoint CREATE an identity for any path string --
        # or, worse, resolve to an existing project's id and write a caller-authored payload into
        # its silo, complete with the per-project stale prune. A non-null `project_id` proved only
        # that the field was populated, which is not a fact about the sender.
        #
        # What can be established here is narrow but real: whether THIS host has an active binding
        # for the claimed directory. That is server-side state keyed on (host, normalized root),
        # not anything the caller sends. It holds only when the sender is on the same machine as
        # the server -- which is the in-process MCP case, the one legitimate remaining use -- and
        # refuses everything else, as the review requires. Refusals are still recorded, so the
        # observation window continues to measure the endpoint while it is closed to misuse.
        from menhir.infrastructure.project_identity_binding import binding_for_root

        supplied_id = getattr(scan_obj, "project_id", None)
        bound_id = None
        if scan_obj.root_path:
            bound_id = await _asyncio.to_thread(
                binding_for_root, self.built.graph_adapter.neo4j, scan_obj.root_path
            )
        if not bound_id:
            raise ProjectIdentityRefused(
                f"write_project_structure cannot establish an identity for "
                f"{scan_obj.root_path!r}: no active project binding exists for that directory on "
                f"this host. This endpoint judges a payload by a path the caller supplied, so an "
                f"identity it cannot verify server-side is refused rather than invented. Use "
                f"scan_and_write_project, which scans the directory it writes."
            )
        if supplied_id and supplied_id != bound_id:
            raise ProjectIdentityRefused(
                f"write_project_structure was given project_id={supplied_id!r} for "
                f"{scan_obj.root_path!r}, which is bound to {bound_id!r} on this host. A supplied "
                f"identity must match the authoritative binding for the directory it claims."
            )
        # Re-bind the id we just read. Idempotent for an already-correct binding (the MERGE sets
        # `claim_generation` only ON CREATE), and it stamps `root_key` on a binding written before
        # that property existed, which is what lets the write boundary match on it at all.
        #
        # #98. It must NOT supply the claim generation. This used to overwrite
        # `scan_obj.identity_generation` with the value read back here, microseconds before the
        # fence compared that field against the same row -- so the compare-and-set compared a
        # number with itself and could never fail. The race the generation exists to catch (the
        # caller settles, time passes, the identity transfers to another checkout, the caller
        # writes anyway) was wide open on this path, which is the one path where the caller is
        # furthest away in time from its own scan.
        #
        # The generation therefore comes from the CALLER and is passed through untouched. This
        # call may reject; it may not supply.
        from menhir.infrastructure.project_identity_binding import bind_project_identity

        if scan_obj.identity_generation is None:
            raise ProjectIdentityRefused(
                "write_project_structure requires the identity_generation the caller settled its "
                "scan under. Without it there is nothing to compare the current binding against, "
                "and a write admitted on a generation the server filled in for itself is not "
                "checked at all. Re-scan and resubmit, or use scan_and_write_project."
            )

        await _asyncio.to_thread(
            bind_project_identity,
            self.built.graph_adapter.neo4j,
            project_id=bound_id,
            root_path=scan_obj.root_path,
        )
        scan_obj.project_id = bound_id
        ensure_scan_root_owns_identity(
            topology=await _asyncio.to_thread(classify_root, scan_obj.root_path),
            project_name=scan_obj.name,
            recorded_root_path=await self._off_loop(
                self.built.graph_adapter.get_project_root_path, scan_obj.name
            ),
            tier=identity_tier,
            force=False,
            allow_unobservable_root=True,
        )

        # If 'symbols' key is absent, the sender is an older MCP process that
        # predates symbol extraction.  Write the structural data now (fast), then
        # fire off a background task to rescan and fill in symbol nodes.
        if "symbols" not in scan:
            # SEC-02: the rescan reads the caller-supplied root off disk, so it is subject to the
            # same containment policy as ingest_document. The guard was applied to that sibling
            # and skipped here, which let an agent-tier caller have ProjectScanner walk any
            # directory on the host and persist its structure (signatures include default
            # argument values). The tier is captured HERE, on the request, because it lives in a
            # ContextVar that a detached task is not guaranteed to inherit.
            tier = get_request_tier()
            root = str(ensure_ingest_path_allowed(scan_obj.root_path, tier=tier))
            name = scan_obj.name
            _asyncio.create_task(
                self._background_symbol_rescan(root, name, session_id, user_id, tier=tier),
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
        self, root: str, name: str, session_id: str, user_id: str, *, tier: str | None
    ) -> None:
        import asyncio as _asyncio
        import logging as _logging
        import os as _os
        from menhir.core.ingest_guard import IngestPathNotAllowedError, ensure_ingest_path_allowed
        from menhir.infrastructure.project_scanner import ProjectScanner as _PS

        _log = _logging.getLogger(__name__)
        if not root or not await _asyncio.to_thread(_os.path.isdir, root):
            return
        # Re-checked here rather than trusted from the scheduling site: this coroutine is a
        # detached task and the tier it must be judged against is the one passed in, never a
        # ContextVar read from whatever context happens to be current when it runs.
        try:
            root = str(ensure_ingest_path_allowed(root, tier=tier))
        except IngestPathNotAllowedError as exc:
            _log.warning("Background symbol rescan refused: project=%s error=%s", name, exc)
            return
        # Re-checked here for the reason stated directly above about the ingest path: this is a
        # detached task, so the decision made at the scheduling site is stale by the time it runs.
        # The directory can be replaced by a worktree, or the project can be claimed by another
        # root, in that window -- and unlike the caller-supplied write, this one reads the tree
        # off disk, so the shape IS observable and the full refusal applies.
        from menhir.domain.project_identity import (
            ProjectIdentityRefused, ensure_scan_root_owns_identity,
        )
        from menhir.infrastructure.repo_topology import classify_root
        try:
            ensure_scan_root_owns_identity(
                topology=await _asyncio.to_thread(classify_root, root),
                project_name=name,
                recorded_root_path=await _asyncio.to_thread(
                    self.built.graph_adapter.get_project_root_path, name
                ),
                tier=tier,
                force=False,
            )
        except ProjectIdentityRefused as exc:
            _log.warning("Background symbol rescan refused: project=%s error=%s", name, exc)
            return
        # CF-257. This writer produces its OWN scan, so it carries no identity from the request
        # that scheduled it -- and the choke point refuses an id-less scan, which would have made
        # every symbol rescan a silent failure. It settles with no action: this path may resolve
        # an identity that already exists, never transfer one, because it runs detached from any
        # caller that could hold operator authority.
        from menhir.services.project_identity_service import settle_project_identity
        try:
            rescan_claim, resolution = await _asyncio.to_thread(
                settle_project_identity,
                self.built.graph_adapter,
                root_path=root,
                display_name=name,
            )
        except Exception as exc:
            _log.warning("Background symbol rescan identity failed: project=%s error=%s", name, exc)
            return
        if rescan_claim is None:
            _log.warning(
                "Background symbol rescan skipped: project=%s needs an identity decision (%s)",
                name, resolution.reason,
            )
            return
        try:
            fresh_scan = await _asyncio.to_thread(_PS().scan, root, name)
            fresh_scan.project_id = rescan_claim.project_id
            fresh_scan.identity_generation = rescan_claim.generation
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
