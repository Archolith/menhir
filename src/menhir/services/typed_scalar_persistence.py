"""Typed-scalar assertion persistence and pending-binding repair."""

from __future__ import annotations

import logging
from typing import Any, Callable

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure import consolidation_audit as _audit
from menhir.services.typed_scalar_rules import (
    PERCEPTION_EVIDENCE_TIER,
    LookupNamespaceEntities,
    ResolveSelfSubject,
    TypedScalarDecision,
    _ADVISORY_UUID_PREFIX,
    _assertion_from_proposal,
    _readable_vote,
    _resolve_subject,
    _resolve_valid_time,
    _utc_now_iso,
    advisory_subject_uuid,
    classify_absolute_semantics,
)

logger = logging.getLogger(__name__)


def _rebuild_succeeded(result: Any) -> bool:
    """Treat only an explicit incomplete projection as unsafe to clear its durable marker."""
    if not isinstance(result, dict) or result.get("complete") is False:
        return False
    history = result.get("history")
    if history is None:
        return True
    if not isinstance(history, dict):
        return False
    if history.get("complete") is not None:
        return bool(history.get("complete"))
    return not bool(history.get("error"))


def bind_and_persist_typed_scalars(
    decisions: list[TypedScalarDecision], *,
    linked_entities_for_episode: Callable[[str], list[dict[str, Any]]],
    record_assertion: Callable[[TypedAssertion], dict[str, Any]],
    rebuild_scalar_state: Callable[[str], dict[str, Any]],
    episode_reference_time: Callable[[str], str | None] | None = None,
    namespace: str | None = None,
    perceiver_version: str = "v1",
    now: Callable[[], str] | None = None,
    mark_projection_complete: Callable[[list[str]], Any] | None = None,
    reconcile_scalar_projection: Callable[[TypedAssertion, str], Any] | None = None,
    resolve_self_subject: ResolveSelfSubject | None = None,
    lookup_namespace_entities: LookupNamespaceEntities | None = None,
    embed: "Callable[[str], list[float] | None] | None" = None,
    embed_version: str | None = None,
) -> dict[str, Any]:
    """Bind committed decisions to resolved entities and persist durable ``:TypedAssertion`` rows.

    Bound assertions normally rebuild the entity-wide scalar projection through
    ``rebuild_scalar_state`` for backwards compatibility. When ``reconcile_scalar_projection`` is
    supplied, the newly persisted assertion itself is handed to that seam instead, so the caller may
    dirty/materialize/certify only its exact lifecycle slot. The assertion-level ``projection_pending``
    marker is cleared only after that reconciliation returns successfully. A reconciliation exception
    is deliberately not converted into a legacy rebuild: both durable recovery signals must remain
    visible after a failed lifecycle cutover.

    Never records for an abstained decision; never projects a ``binding_pending`` advisory. Returns
    {persisted, bound, advisory, mismatched, rebuilt, results}. ``rebuilt`` remains the compatibility
    counter for successfully completed projections regardless of which projection seam completed them.

    ``lookup_namespace_entities`` is the OPTIONAL same-namespace repository fallback (see
    ``_resolve_subject``): it is consulted only after exact local episode matching fails, and only when
    a nonblank ``namespace`` is present. Absent/None keeps binding strictly local."""
    learned_at = (now or _utc_now_iso)()
    results: list[dict[str, Any]] = []
    bound = advisory = mismatched = rebuilt = 0
    for d in decisions:
        _audit.audit(
            "gate", d.veto or ("commit" if d.committed else "vetoed"),
            namespace=namespace,
            details={
                "source_key": d.source_key,
                "agreement": d.agreement,
                "k": d.k,
                "reason": d.reason,
                "distribution": {_readable_vote(k): v for k, v in (d.distribution or {}).items()},
                "no_proposal": bool(d.committed and d.proposal is None),
            },
        )
        if not d.committed or d.proposal is None:
            continue
        p = d.proposal
        ref = episode_reference_time(p.episode_uuid) if episode_reference_time else None
        valid_at, time_basis = _resolve_valid_time(p.when, ref, learned_at)

        entities = linked_entities_for_episode(p.episode_uuid) or []
        subject_uuid, subject_display = _resolve_subject(
            p.subject_text, entities, namespace, resolve_self_subject,
            lookup_namespace_entities=lookup_namespace_entities)
        is_bound = subject_uuid is not None
        if not is_bound:
            subject_uuid = advisory_subject_uuid(d.source_key)
            subject_display = p.subject_text

        name_embedding = None
        if embed is not None and embed_version and p.stated_span:
            try:
                name_embedding = embed(p.stated_span)
            except Exception:
                logger.warning("write-time observation embed failed; leaving for backfill", exc_info=True)
                name_embedding = None
        assertion = _assertion_from_proposal(
            p, subject_uuid=subject_uuid, subject_display=subject_display or p.subject_text,
            valid_at=valid_at, learned_at=learned_at, time_basis=time_basis,
            name_embedding=name_embedding,
            embed_version=(embed_version if name_embedding is not None else None),
            evidence_tier=PERCEPTION_EVIDENCE_TIER, namespace=namespace,
            perceiver_version=perceiver_version)
        rec = record_assertion(assertion)
        binding_pending = bool(rec.get("binding_pending"))
        binding_mismatch = bool(rec.get("binding_mismatch"))

        _audit.audit(
            "perceive",
            "mismatch" if binding_mismatch else "pending" if binding_pending
            else "bound" if is_bound else "advisory",
            namespace=namespace, subject_uuid=subject_uuid, episode_uuid=getattr(p, "episode_uuid", None),
            details={
                "source_key": d.source_key,
                "attribute": getattr(assertion, "attribute", None),
                "value": getattr(assertion, "value", None),
                "value_kind": getattr(assertion, "value_kind", None),
                "operation": getattr(assertion, "operation", None),
                "valid_at": valid_at, "time_basis": time_basis,
                "assertion_id": rec.get("assertion_id"),
            },
        )

        entry = {
            "source_key": d.source_key, "subject_uuid": subject_uuid,
            "bound": is_bound and not binding_pending and not binding_mismatch,
            "binding_pending": binding_pending, "binding_mismatch": binding_mismatch,
            "assertion_id": rec.get("assertion_id"),
        }
        if binding_mismatch:
            mismatched += 1
            logger.warning(
                "typed-scalar binding mismatch on source_key=%s: claim already bound to a different "
                "entity than %s; not rebinding", d.source_key, subject_uuid)
        elif is_bound and not binding_pending:
            bound += 1
            assertion_id = str(rec.get("assertion_id") or "")
            if reconcile_scalar_projection is not None:
                # The operation id is anchored to the durable assertion row, not an in-memory batch
                # ordinal, so lifecycle certification replay derives the same id after a retry.
                entry["reconciliation"] = reconcile_scalar_projection(assertion, assertion_id)
                projection_complete = True
            else:
                entry["rebuild"] = rebuild_scalar_state(subject_uuid)
                projection_complete = _rebuild_succeeded(entry["rebuild"])

            if projection_complete:
                if mark_projection_complete is not None and assertion_id:
                    mark_projection_complete([assertion_id])
                rebuilt += 1
            else:
                entry["projection_incomplete"] = True
        else:
            advisory += 1
        results.append(entry)

    return {
        "persisted": len(results), "bound": bound, "advisory": advisory,
        "mismatched": mismatched, "rebuilt": rebuilt, "results": results,
    }


# ------------------------------------------------- (4) explicit pending-binding repair pass (C.4.4)
#
# A committed claim that could not be UNIQUELY bound at perception time is persisted as an advisory
# (`binding_pending=true`, `unbound:<source_key>` sentinel subject, NO View). Merge repair does not
# touch these — `orphaned_subject_uuids` deliberately EXCLUDES binding_pending — so nothing resolves
# them unless the same episode is re-perceived by chance. This pass makes it explicit: for each
# current advisory, re-resolve its raw subject against the episode's CURRENT linked entities (which
# may have been created/merged since the advisory was written) and, on a UNIQUE match, re-record it
# with the resolved subject. assertion_key omits subject_uuid, so the re-record lands on the SAME node
# via the C.4.3 pending->bound adoption (adopts subject onto assertion + head, clears binding_pending);
# then the View is rebuilt. Zero/multiple/still-unbindable -> left advisory for a later pass.


def _assertion_from_pending_row(row: dict[str, Any], *, subject_uuid: str, subject_display: str,
                                ) -> TypedAssertion:
    """Reconstruct the durable `TypedAssertion` from a stored advisory row + a resolved subject. The
    interpretation-bearing fields (attribute/scope/value_kind/unit/operation/value) and the span/
    ordinal are carried through UNCHANGED so the rebuilt assertion_key is identical to the advisory's
    (a same-node adoption, never a fork). Evidence tier is FORCED to the perception tier — repair is
    still the probabilistic path and must not grant authority. valid_at/learned_at/time_basis are
    preserved (ON MATCH does not rewrite them) so world-time semantics are unchanged."""
    return TypedAssertion(
        subject_uuid=subject_uuid, subject_display=subject_display,
        attribute=str(row["attribute"]), scope=str(row.get("scope") or ""),
        value_kind=str(row["value_kind"]), unit=str(row.get("unit") or ""),
        operation=str(row["operation"]), value=row.get("value"),
        stated_span=str(row["stated_span"]), episode_uuid=str(row["episode_uuid"]),
        valid_at=str(row["valid_at"]), learned_at=str(row["learned_at"]),
        span_start=int(row.get("span_start", -1)), span_end=int(row.get("span_end", -1)),
        claim_ordinal=int(row.get("claim_ordinal", 0)),
        time_basis=str(row.get("time_basis") or "explicit"),
        evidence_tier=PERCEPTION_EVIDENCE_TIER,
        perceiver_version=str(row.get("perceiver_version") or "v1"),
        namespace=row.get("namespace"),
        absolute_semantics=classify_absolute_semantics(
            str(row["stated_span"]), str(row["operation"])),
    )


def repair_pending_bindings(
    pending_rows: list[dict[str, Any]], *,
    linked_entities_for_episode: Callable[[str], list[dict[str, Any]]],
    record_assertion: Callable[[TypedAssertion], dict[str, Any]],
    rebuild_scalar_state: Callable[[str, str | None], dict[str, Any]],
    mark_attempted: Callable[[list[str]], Any] | None = None,
    mark_projection_complete: Callable[[list[str]], Any] | None = None,
    allowed_namespaces: set[str] | None = None,
    resolve_self_subject: ResolveSelfSubject | None = None,
    lookup_namespace_entities: LookupNamespaceEntities | None = None,
) -> dict[str, Any]:
    """Repair current rows that need binding/projection work, then rebuild + mark projection complete.
    Deterministic given the injected seams (fully offline-testable). Two categories per row:

      * ADVISORY (`binding_pending`): re-resolve `subject_display` against the episode's CURRENT linked
        entities. UNIQUE match -> re-record with the resolved subject (adoption lands it on the same
        node + head, clears binding_pending, sets projection_pending). Zero/multiple/blank -> left
        advisory (still_pending); store still pending (episode/entity absent) -> still_pending;
        mismatch (bound elsewhere meanwhile) -> surfaced, left to the owner.
      * PROJECTION-ONLY (`projection_pending`, already bound): a prior bind's rebuild did not complete
        (crash) -> just rebuild for the stored `subject_uuid`.

    CRASH RECOVERY: the View rebuild is followed by `mark_projection_complete` for that row ONLY on
    success, so a crash/exception between the binding write and the rebuild leaves the row selectable
    (projection_pending stays set) on the next run. NAMESPACE FIDELITY: the View is rebuilt in the
    ROW's own durable namespace (never a fixed outer default), and an `allowed_namespaces` allowlist is
    enforced fail-closed (a row outside it is skipped, not misfiled). PER-ROW ISOLATION: a row-level
    exception is caught and counted (`errored`) so one bad row cannot abort the batch or block the
    fairness stamp. EVERY scanned assertion_id is passed to `mark_attempted` regardless of outcome, so
    a bounded scan is eventually complete. `lookup_namespace_entities` is the OPTIONAL same-namespace
    repository fallback (see `_resolve_subject`): consulted only after exact local episode matching
    fails and only when the ROW's own namespace is nonblank. Returns
    {scanned, repaired, still_pending, mismatched, errored, rebuilt, results}."""
    results: list[dict[str, Any]] = []
    scanned_ids: list[str] = []
    repaired = still_pending = mismatched = errored = rebuilt = 0
    for row in pending_rows:
        aid = str(row.get("assertion_id") or "")
        if aid:
            scanned_ids.append(aid)
        row_ns = row.get("namespace")
        if allowed_namespaces is not None and row_ns not in allowed_namespaces:
            results.append({"source_key": row.get("source_key"), "repaired": False,
                            "reason": "namespace not allowed"})
            continue
        try:
            is_advisory = bool(row.get("binding_pending"))
            if is_advisory:
                subject_text = str(row.get("subject_display") or "")
                entities = linked_entities_for_episode(str(row.get("episode_uuid") or "")) or []
                subject_uuid, subject_display = _resolve_subject(
                    subject_text, entities, row_ns, resolve_self_subject,
                    lookup_namespace_entities=lookup_namespace_entities)
                if subject_uuid is None:
                    still_pending += 1
                    results.append({"source_key": row.get("source_key"), "repaired": False,
                                    "reason": "not uniquely bindable"})
                    continue
                assertion = _assertion_from_pending_row(
                    row, subject_uuid=subject_uuid, subject_display=subject_display or subject_text)
                rec = record_assertion(assertion)
                if rec.get("binding_mismatch"):
                    mismatched += 1
                    results.append({"source_key": row.get("source_key"), "repaired": False,
                                    "subject_uuid": subject_uuid, "reason": "binding_mismatch"})
                    logger.warning(
                        "pending-binding repair: source_key=%s now bound to a different entity than "
                        "%s; leaving to owner", row.get("source_key"), subject_uuid)
                    continue
                if rec.get("binding_pending"):
                    still_pending += 1
                    results.append({"source_key": row.get("source_key"), "repaired": False,
                                    "subject_uuid": subject_uuid, "reason": "still binding_pending"})
                    continue
            else:
                subject_uuid = str(row.get("subject_uuid") or "")
                if not subject_uuid or subject_uuid.startswith(_ADVISORY_UUID_PREFIX):
                    still_pending += 1
                    results.append({"source_key": row.get("source_key"), "repaired": False,
                                    "reason": "projection row without a real subject"})
                    continue

            _audit.audit(
                "bind_repair", "adopted" if is_advisory else "projection",
                namespace=row_ns, subject_uuid=subject_uuid,
                episode_uuid=str(row.get("episode_uuid") or "") or None,
                details={"source_key": row.get("source_key"), "assertion_id": aid},
            )
            rebuild = rebuild_scalar_state(subject_uuid, row_ns)
            if not _rebuild_succeeded(rebuild):
                errored += 1
                results.append({"source_key": row.get("source_key"), "repaired": False,
                                "subject_uuid": subject_uuid, "namespace": row_ns,
                                "reason": "projection incomplete; pending marker preserved",
                                "rebuild": rebuild})
                continue
            if mark_projection_complete is not None and aid:
                mark_projection_complete([aid])
            repaired += 1
            rebuilt += 1
            results.append({"source_key": row.get("source_key"), "repaired": True,
                            "subject_uuid": subject_uuid, "namespace": row_ns, "rebuild": rebuild})
        except Exception:  # noqa: BLE001 - one bad row must not abort the batch or block fairness
            errored += 1
            logger.exception(
                "pending-binding repair errored on source_key=%s; leaving for retry",
                row.get("source_key"))

    if mark_attempted is not None and scanned_ids:
        mark_attempted(scanned_ids)

    return {
        "scanned": len(pending_rows), "repaired": repaired, "still_pending": still_pending,
        "mismatched": mismatched, "errored": errored, "rebuilt": rebuilt, "results": results,
    }
