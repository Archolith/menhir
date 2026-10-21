"""Lane orchestration for the Hook Center stale-anchor lane smoke.

Extracted verbatim from ``hook_center_stale_lane_smoke.py``: :func:`run_smoke`
drives the full stale-file-anchor lane end-to-end, plus its timestamp/list
helpers.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from hook_center_stale_lane_smoke_constants import (
    ANCHORED_AT, CHECK_KEYS, CONTROL_UUID, EVENT_HASH, MEMORY_SENTINEL,
    RESULT_FAIL, RESULT_PASS, RESULT_PASS_WITH_SKIPS, WRONG_PATH,
)
from hook_center_stale_lane_smoke_http import HTTPClient, SmokeHTTPError


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _shift_iso(base_iso: str, *, minutes: int = 0, days: int = 0) -> str:
    """Return ``base_iso`` shifted by the given delta, formatted as UTC ISO-8601 with a
    trailing Z. Derived from the DB's actual ``dirty_at`` (not the smoke process clock),
    so verified_at ordering holds regardless of clock skew between smoke and Neo4j."""
    try:
        dt = datetime.fromisoformat(base_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=minutes, days=days)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _find(items: list[dict], uuid: str) -> dict | None:
    for it in items:
        if it["uuid"] == uuid:
            return it
    return None


def run_smoke(config: argparse.Namespace, http: HTTPClient, backend, out) -> dict:
    """Drive the full lane. Returns the JSON summary dict. `out` is a diagnostic sink
    (callable taking a str) that writes to stderr / stays out of JSON stdout."""
    project = config.project
    path = config.path
    memory_uuid = config.memory_uuid
    uuids = [memory_uuid, CONTROL_UUID]
    hits = [(memory_uuid, "smoke stale-anchored memory", 0.85),
            (CONTROL_UUID, "smoke control memory", 0.80)]

    checks: dict[str, bool] = {}
    skipped: dict[str, str] = {}
    limitations: list[str] = []

    def run_recall_sync(fn):
        import asyncio
        return asyncio.run(fn)

    # --- clean slate ---
    if config.require_clean_start and backend.any_smoke_data(project, uuids):
        raise SmokeHTTPError(
            f"--require-clean-start: pre-existing smoke data for project {project!r}")
    backend.clean(project, uuids)
    backend.create_fixture(project, path, memory_uuid, CONTROL_UUID, ANCHORED_AT)
    out(f"fixture created: project={project} path={path} memory_uuid={memory_uuid}")

    # --- 1. tool event accepted (marks file dirty) ---
    resp = http.post_tool_event(project, path)
    checks["tool_event_accepted"] = bool(resp.get("accepted")) and bool(resp.get("marked_dirty"))
    out(f"[1] tool_event_accepted: accepted={resp.get('accepted')} marked_dirty={resp.get('marked_dirty')}")

    # --- 2. dirty file visible + dirty metadata written (path, dirty_at, operation, hash) ---
    dirty = http.get_dirty(project)
    dirty_files = dirty.get("dirty_files", [])
    dfile = next((d for d in dirty_files if d.get("path") == path), None)
    meta = backend.file_event_metadata(project, path)
    checks["dirty_file_visible"] = (
        bool(dfile) and bool(dfile.get("dirty_at"))
        and dfile.get("operation") == "edit"            # operation surfaced by the endpoint
        and meta.get("operation") == "edit"             # operation persisted on the node
        and meta.get("after_hash") == EVENT_HASH        # provenance hash persisted (not exposed by /dirty)
    )
    out(f"[2] dirty_file_visible: {bool(dfile)} dirty_at={dfile.get('dirty_at') if dfile else None} "
        f"op={dfile.get('operation') if dfile else None} hash={meta.get('after_hash')}")

    # --- 3. stale anchor visible (endpoint) ---
    stale = http.get_stale(project)
    anchors = stale.get("stale_anchors", [])
    sa = next((a for a in anchors if a.get("memory_uuid") == memory_uuid), None)
    checks["stale_anchor_visible"] = bool(sa) and sa.get("path") == path and bool(sa.get("dirty_at")) \
        and bool(sa.get("anchored_at"))
    # Derive verification timestamps from the DB's real dirty_at (not the smoke clock),
    # so pre/post-dirty ordering holds regardless of clock skew.
    dirty_at_iso = (sa or {}).get("dirty_at") or (dfile or {}).get("dirty_at") \
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out(f"[3] stale_anchor_visible: {bool(sa)} path={sa.get('path') if sa else None}")

    # --- 4. recall gets stale label (real RecallService.recall) ---
    if config.skip_recall:
        skipped["recall_stale_label"] = "--skip-recall passed"
        skipped["formatter_stale_advisory"] = "--skip-recall passed"
    else:
        items = run_recall_sync(backend.recall_items(project, hits))
        stale_item = _find(items, memory_uuid)
        ctrl_item = _find(items, CONTROL_UUID)
        info = (stale_item or {}).get("stale_anchor_info") or {}
        ctrl_info = (ctrl_item or {}).get("stale_anchor_info") or {}
        checks["recall_stale_label"] = (
            bool(stale_item) and info.get("stale_anchor") is True
            and info.get("stale_reason") == "file_changed_after_anchor"
            and info.get("path") == path
            and bool(info.get("dirty_at")) and bool(info.get("anchored_at"))
            and bool(ctrl_item) and ctrl_info.get("stale_anchor") is False
        )
        out(f"[4] recall_stale_label: stale={info.get('stale_anchor')} "
            f"control_stale={ctrl_info.get('stale_anchor')}")

        # --- 5. formatter advisory (real menhir.mcp.formatters) ---
        fmt = (stale_item or {}).get("formatted") or {}
        ctrl_fmt = (ctrl_item or {}).get("formatted") or {}
        checks["formatter_stale_advisory"] = (
            fmt.get("stale_anchor") is True
            and fmt.get("stale_action") == "verify_current_file_before_relying"
            and "Inspect the current file" in str(fmt.get("stale_advisory") or "")
            and "stale_action" not in ctrl_fmt and "stale_advisory" not in ctrl_fmt
        )
        out(f"[5] formatter_stale_advisory: action={fmt.get('stale_action')}")

    # --- 6. context builder warning is atomic ---
    if config.skip_context_builder:
        skipped["context_warning_atomic"] = "--skip-context-builder passed"
    elif config.skip_recall:
        skipped["context_warning_atomic"] = "context depends on recall (--skip-recall passed)"
    else:
        big = run_recall_sync(backend.build_context(project, hits, 5000))
        tiny = run_recall_sync(backend.build_context(project, hits, 1))
        # Large budget: the stale memory AND its warning both appear.
        both_present = ("Stale file anchor" in big and path in big
                        and "inspect the current file" in big.lower()
                        and MEMORY_SENTINEL in big)
        # Atomicity: at a budget too tight for memory + warning, NEITHER appears — not the
        # memory without its warning (that would be the "wrong current-state view" failure).
        neither_present = ("Stale file anchor" not in tiny and MEMORY_SENTINEL not in tiny)
        checks["context_warning_atomic"] = both_present and neither_present
        out(f"[6] context_warning_atomic: both_present={both_present} neither_present={neither_present}")

    # --- 9 + 10 (before any valid post-dirty receipt): wrong-path + pre-dirty ignored ---
    st, wp_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": WRONG_PATH,
        "outcome": "still_valid", "verified_at": _shift_iso(dirty_at_iso, minutes=5),
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    if st != 200:
        raise SmokeHTTPError(f"wrong-path receipt POST -> {st}: {wp_body}")
    st, pd_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": path,
        "outcome": "still_valid", "verified_at": _shift_iso(dirty_at_iso, days=-1),
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    if st != 200:
        raise SmokeHTTPError(f"pre-dirty receipt POST -> {st}: {pd_body}")

    if config.skip_recall:
        skipped["wrong_path_receipt_ignored"] = "--skip-recall passed"
        skipped["pre_dirty_receipt_ignored"] = "--skip-recall passed"
    else:
        items = run_recall_sync(backend.recall_items(project, hits))
        info = (_find(items, memory_uuid) or {}).get("stale_anchor_info") or {}
        no_enrich = info.get("stale_anchor") is True and info.get("stale_verification") is None
        # At this point only wrong-path + pre-dirty receipts exist; neither may enrich.
        checks["wrong_path_receipt_ignored"] = no_enrich
        checks["pre_dirty_receipt_ignored"] = no_enrich
        out(f"[9/10] wrong_path + pre_dirty ignored: no_enrich={no_enrich} "
            f"verification={info.get('stale_verification')}")

    # --- 7 + 8: valid post-dirty still_valid receipt records + enriches ---
    st, v_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": path,
        "outcome": "still_valid", "verified_at": _shift_iso(dirty_at_iso, minutes=5),
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    recorded = st == 200 and bool(v_body.get("accepted"))
    listed = http.get_verifications(memory_uuid)
    has_listed = any(v.get("path") == path and v.get("outcome") == "still_valid"
                     for v in listed.get("verifications", []))
    checks["verification_receipt_recorded"] = recorded and has_listed
    out(f"[7] verification_receipt_recorded: recorded={recorded} listed={has_listed}")

    if config.skip_recall:
        skipped["post_dirty_receipt_enriches"] = "--skip-recall passed"
    else:
        items = run_recall_sync(backend.recall_items(project, hits))
        info = (_find(items, memory_uuid) or {}).get("stale_anchor_info") or {}
        ver = info.get("stale_verification") or {}
        checks["post_dirty_receipt_enriches"] = (
            info.get("stale_anchor") is True            # still stale, never marked fresh
            and ver.get("outcome") == "still_valid"
            and ver.get("verified_by") == "smoke-agent"
            and ver.get("basis") == "inspected_current_file"
        )
        out(f"[8] post_dirty_receipt_enriches: stale={info.get('stale_anchor')} "
            f"outcome={ver.get('outcome')}")

    # --- 11. malformed timestamp is rejected (never stored / never reassures) ---
    st, mal_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": path,
        "outcome": "still_valid", "verified_at": "not-a-timestamp",
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    checks["malformed_timestamp_conservative"] = st == 400
    out(f"[11] malformed_timestamp_conservative: status={st}")

    # --- 12. outdated receipt recommends update/supersede, mutates no lifecycle ---
    # Strictly later than the still_valid receipt so it is the latest post-dirty one.
    st, o_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": path,
        "outcome": "outdated", "verified_at": _shift_iso(dirty_at_iso, minutes=10),
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    outdated_recorded = st == 200 and bool(o_body.get("accepted"))
    if config.skip_recall:
        # Still assert the no-lifecycle-mutation invariants (they do not need recall).
        no_mutation = (backend.memory_exists(memory_uuid)
                       and backend.dirty_flag_set(project, path)
                       and backend.stale_count(project) >= 1)
        checks["outdated_receipt_recommends_no_lifecycle_mutation"] = outdated_recorded and no_mutation
        out(f"[12] outdated (recall skipped): recorded={outdated_recorded} no_mutation={no_mutation}")
    else:
        items = run_recall_sync(backend.recall_items(project, hits))
        st_item = _find(items, memory_uuid) or {}
        info = st_item.get("stale_anchor_info") or {}
        fmt = st_item.get("formatted") or {}
        no_mutation = (backend.memory_exists(memory_uuid)
                       and backend.dirty_flag_set(project, path)
                       and backend.stale_count(project) >= 1)
        checks["outdated_receipt_recommends_no_lifecycle_mutation"] = (
            outdated_recorded
            and info.get("stale_anchor") is True                        # still stale
            and (info.get("stale_verification") or {}).get("outcome") == "outdated"
            and fmt.get("stale_action") == "do_not_rely_update_or_supersede"
            and no_mutation                                             # no delete/expire/clear
        )
        out(f"[12] outdated: action={fmt.get('stale_action')} no_mutation={no_mutation}")

    # --- compute result ---
    for key in CHECK_KEYS:
        if key not in checks and key not in skipped:
            skipped[key] = "not evaluated"

    failed = [k for k in CHECK_KEYS if checks.get(k) is False]
    if failed:
        result = RESULT_FAIL
    elif skipped:
        result = RESULT_PASS_WITH_SKIPS
    else:
        result = RESULT_PASS

    summary = {
        "result": result,
        "project": project,
        "path": path,
        "memory_uuid": memory_uuid,
        "checks": {k: checks[k] for k in CHECK_KEYS if k in checks},
        "safety": {
            "throwaway_project_used": True,
            "no_file_content_uploaded": True,
            "no_transcript_captured": True,
            "no_phase_3_changes": True,
            "no_turn_evidence_changes": True,
            "no_dirty_clearing": True,
            "no_auto_refresh": True,
        },
        "limitations": limitations,
    }
    if skipped:
        summary["skipped"] = skipped
    if failed:
        summary["failed"] = failed
    return summary
