"""Generic View registration, version writing, and provenance persistence."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from menhir.domain.self_authority import (
    UnconfirmedSelfAssertionError,
    is_canonical_self_subject,
)
from menhir.domain.self_identity import is_self_alias
from menhir.infrastructure.neo4j import SAGA_MUTATION_TIMEOUT_S
from menhir.infrastructure.self_binding import (
    SelfBindMode,
    resolve_bind_mode,
    structural_self_cypher,
)

try:  # neo4j is a hard runtime dep; guard the import so unit imports without the driver still load.
    from neo4j.exceptions import ConstraintError as _Neo4jConstraintError
except Exception:  # pragma: no cover - driver always present in the running service
    _Neo4jConstraintError = ()  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Placeholder for the supporting-event count inside a kind's `summary_template`. The unchanged-value
#: provenance refresh (plan D2) computes the count INSIDE Cypher (the union must be atomic), so the
#: summary cannot be pre-rendered in Python — it is rendered as a template here and the count is
#: substituted server-side in the same statement that writes the union.
from menhir.infrastructure.view_models import (
    AdmissionAuditKind,
    CounterKind,
    ScalarHistoryKind,
    ScalarStateKind,
    TimelineKind,
    ViewAudience,
    ViewClass,
    ViewKind,
    _COUNT_TOKEN,
    _SHARED_STAMPS,
    _checked_template,
    _counter_retrieval_text,
    _counter_summary,
    _day,
    _fmt,
    _is_older,
    _label_for,
    _log_missing_episodes,
    _normalize_entries,
    _normalize_episode_uuids,
    _now,
    _parse_dt,
    _render_timeline,
    _scalar_norm,
    _timeline_sig,
    _timeline_surface,
)

class ViewWriteRepositoryMixin:
    """Single writer for View :Entity nodes (all kinds). Direct Neo4j CRUD.

    The `_write_version` core stamps + supersedes + links provenance identically for every kind;
    the registered `ViewKind` supplies the kind name, value slot, surface, signature, and read
    projection. That split IS the "one View shape, many folds" claim, with each kind as its SSOT."""

    #: registry — add a memory type by adding its ViewKind here, nothing else.
    KINDS: dict[str, ViewKind] = {k.name: k for k in (
        CounterKind(), TimelineKind(), AdmissionAuditKind(), ScalarStateKind(),
        ScalarHistoryKind())}

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j
        self._canonical_self_binding_mode = SelfBindMode.OFF

    def configure_canonical_self_binding_mode(self, value: Any) -> None:
        self._canonical_self_binding_mode = resolve_bind_mode(value, strict=True)

    def _subject_is_structural_self(self, subject_uuid: str) -> bool:
        """Resolve the durable subject marker instead of trusting the caller's namespace."""

        rows = self.neo4j.execute(
            """
            MATCH (subject:Entity {uuid: $subject_uuid})
            RETURN __STRUCTURAL_SELF__ AS is_self
            LIMIT 1
            """.replace("__STRUCTURAL_SELF__", structural_self_cypher("subject")),
            {"subject_uuid": subject_uuid},
        )
        return bool(rows and rows[0].get("is_self", False))

    # ------------------------------------------------------------------ keys / surfaces (compat)

    @staticmethod
    def _key(namespace: str | None, subject: str, discriminator: str,
             *, subject_uuid: str | None = None) -> str:
        """Build the view_key. Identity is the text subject by default (every existing kind); when
        `subject_uuid` is supplied (scalar_state), the resolved entity UUID is the identity segment
        instead, so the key is entity-anchored, not text-anchored. A present-but-blank UUID is a bug
        (never silently fall back to text keying — that would recreate the rejected lexical sidecar)."""
        ns = (namespace or "").strip()
        if subject_uuid is not None:
            ident = subject_uuid.strip()
            if not ident:
                raise ValueError("subject_uuid must be non-blank when provided (scalar-state identity)")
            subj_seg = ident.lower()
        else:
            subj_seg = subject.strip().lower()
        return f"{ns}::{subj_seg}::{discriminator.strip().lower()}"

    @staticmethod
    def retrieval_text(subject: str, counter: str, value: float) -> str:
        """Counter BM25/embedding surface (kept as a static for callers that pre-embed it)."""
        return _counter_retrieval_text(subject, counter, value)

    @staticmethod
    def _timeline_surface(subject: str, entries: list[dict[str, Any]]) -> str:
        return _timeline_surface(subject, _normalize_entries(entries))

    # ------------------------------------------------------------------ generic write

    def record(self, kind_name: str, *, subject: str, subject_uuid: str | None = None,
               namespace: str | None = None,
               source: str = "consolidation", source_confidence: float = 0.6,
               name_embedding: list[float] | None = None,
               audit_props: dict[str, Any] | None = None,
               refresh_props: dict[str, Any] | None = None,
               authoritative: bool = False, **payload: Any) -> dict[str, Any]:
        """Kind-agnostic upsert. Resolves the ViewKind, builds key/surface/signature/value from it,
        and writes one shared-shape version. The public record_* methods are ergonomic wrappers.

        `subject_uuid` (optional): when given, the view_key is anchored on this resolved entity UUID
        instead of the text subject, while `view_subject` keeps the human-readable display. Existing
        kinds pass nothing and are byte-identical to before; only entity-anchored kinds (scalar_state)
        supply it.

        `audit_props` are provenance-only node properties (e.g. the perception gate's agreement/k/
        reason) — stamped onto the node but kept OUT of the signature (never trigger supersession)
        and OUT of the embedding/surface (never rank). A receipt, not a confidence signal."""
        kind = self.KINDS[kind_name]
        if self._canonical_self_binding_mode is SelfBindMode.ENFORCE:
            if subject_uuid is not None and (
                is_canonical_self_subject(subject_uuid, namespace)
                or self._subject_is_structural_self(subject_uuid)
            ):
                # Views are derived authority, not an alternate promotion channel. The structural
                # database check prevents a caller from pairing canonical UUID A with namespace B
                # to evade the deterministic UUID formula check.
                raise UnconfirmedSelfAssertionError(
                    "View cannot attach to canonical self without exact owner confirmation"
                )
            if (
                subject_uuid is None
                and kind.view_audience(payload) is ViewAudience.RECALL
                and is_self_alias(subject)
            ):
                # A recallable text-keyed View has no durable entity identity to prove that
                # ``user`` means an ordinary third party.  Refuse the ambiguous writer; callers
                # may supply a resolved non-self UUID where the API supports one.
                raise UnconfirmedSelfAssertionError(
                    "recallable View for a self alias requires a resolved non-self subject UUID"
                )
        key = self._key(namespace, subject, kind.key_discriminator(payload), subject_uuid=subject_uuid)
        # Normalize provenance ONCE, before the surface is rendered, so the supporting-event count
        # quoted in the summary is the count actually stored on the node (plan D1: sorted, dedup).
        eps = _normalize_episode_uuids(kind.episode_uuids(payload))
        payload["episode_uuids"] = eps
        name, summary = kind.surface(subject, payload)
        props = kind.write_props(subject, key, payload)
        # Authoritative rebuild replaces the projection with the deterministic result of the full
        # current log, so (a) it BYPASSES the LWW guard — a correction that moves the current value
        # backward in valid_at must install, not be rejected as a "late arrival"; and (b) on the
        # unchanged-signature path it fully re-renders every derived field, so a same-signature write
        # can never leave a stale surface. (valid_at is part of the scalar signature, so it is
        # identical on that path and needs no refresh.)
        effective_refresh = refresh_props
        if authoritative:
            effective_refresh = {
                "view_subject": subject.strip(),
                "name": name[:300], "summary": summary[:1000], "content": summary[:1000],
                **props,
                **(refresh_props or {}),
            }
        res = self._write_version(
            kind=kind.name, key=key, subject=subject, subject_uuid=subject_uuid,
            name=name, summary=summary,
            sig=kind.signature(payload), extra_props=props, audit_props=audit_props,
            namespace=namespace, valid_at=kind.valid_at(payload) or _now(),
            source=source, source_confidence=source_confidence,
            episode_uuids=eps, name_embedding=name_embedding,
            require_newer=kind.lww_register and not authoritative, refresh_props=effective_refresh,
            replace_provenance=authoritative,
            summary_template=_checked_template(kind, subject, payload, summary, len(eps)),
            lifecycle_props=kind.view_stamps(payload),
        )
        res["view_value"] = props.get("view_value")
        return res

    def _current_by_key(
        self, key: str, *, view_class: ViewClass = ViewClass.FACT
    ) -> dict[str, Any] | None:
        """The current version for a view_key, or None. Label-scoped to view_class so a FACT
        and a METRIC sharing a key are independent (never supersede each other). Falls back to
        qs_key/qs_current so pre-View counter nodes still supersede (no migration needed)."""
        label = _label_for(view_class)
        rows = self.neo4j.execute(
            f"MATCH (n:{label}) WHERE (n.view_key = $k OR n.qs_key = $k) "
            "AND coalesce(n.view_current, n.qs_current, true) "
            "RETURN n.uuid AS uuid, coalesce(n.view_sig, toString(n.qs_value)) AS sig, "
            "toString(n.valid_at) AS valid_at LIMIT 1",
            {"k": key},
        )
        return dict(rows[0]) if rows else None

    def _write_version(
        self, *, kind: str, key: str, subject: str, name: str, summary: str, sig: str,
        extra_props: dict[str, Any], namespace: str | None, valid_at: str, source: str,
        source_confidence: float, episode_uuids: list[str], name_embedding: list[float] | None,
        require_newer: bool = False, audit_props: dict[str, Any] | None = None,
        view_class: ViewClass = ViewClass.FACT, node_uuid: str | None = None,
        refresh_props: dict[str, Any] | None = None, summary_template: str | None = None,
        subject_uuid: str | None = None, replace_provenance: bool = False,
        lifecycle_props: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """The one place a View version is written. Idempotent on `sig`: unchanged -> refresh
        provenance/access only; changed -> create a fresh current version and supersede the prior
        one (kept, view_current=false, linked by SUPERSEDES). Recall stamps applied here so every
        kind is stamped identically. `extra_props` is the kind's value slot (+ compat).

        `view_class` selects the node label (FACT->:Entity, METRIC->:Metric). Supersession and
        current-lookup are label-scoped, so a FACT and a METRIC with the same key are independent
        and SUPERSEDES never crosses classes. METRIC writes reject semantic features (name
        embedding, episode provenance) here in the core -- defense in depth (plan A3/A4).

        `refresh_props` are stamped on the UNCHANGED-value path (where no new version is created).
        A same-value write still has fresh provenance -- e.g. a Metric's new receipt op id -- and
        the node must point at it (plan A4).

        `require_newer` (LWW registers, e.g. counter): a value CHANGE only supersedes when the
        incoming world-time `valid_at` is >= the current version's. A temporally-older event is
        stale and must NOT overwrite current (fold-algebra Law 1); it is skipped, current stays
        authoritative. Without this the reconcile is arrival-ordered and installs stale totals."""
        from menhir.domain.namespace import (
            normalize_namespace,
            stamped_namespace,
            tenant_scope_cypher,
            tenant_scope_params,
        )

        label = _label_for(view_class)
        eps = _normalize_episode_uuids(episode_uuids)
        lifecycle = lifecycle_props or {
            "view_class": view_class.value,
            "view_subtype": kind,
            "view_audience": "OPERATOR",
        }
        refresh_props = {**lifecycle, **dict(refresh_props or {})}
        if view_class is ViewClass.METRIC and (name_embedding is not None or eps):
            raise ValueError(
                "METRIC views are instrumentation, not memories: they must not carry a "
                "name_embedding or episode provenance (plan A3)."
            )

        now = datetime.now(timezone.utc).isoformat()
        ns_stamped = stamped_namespace(namespace)
        current = self._current_by_key(key, view_class=view_class)

        if current is not None and str(current.get("sig")) == str(sig):
            # An unchanged write refreshes access AND the provenance pointer -- it does not create a
            # new version (plan A4). `refresh_props` carries metric_last_receipt_op_id for a Metric:
            # a same-value fold still produces a NEW receipt, and the node must point at it. Without
            # this the node keeps the FIRST op's receipt id, so the saga's after-state fingerprint
            # (which expects THIS op's id) mismatches -> false drift -> NEEDS_REVIEW -> the write
            # fence then blocks every future write to that key. Same-value rewrites are the common
            # case for a telemetry fold, so that would brick the metric pipeline per key.
            if view_class is ViewClass.FACT and replace_provenance:
                # Authoritative rebuild REPLACES provenance with exactly the fold's contributor
                # episodes (union would keep a now-unsupported ep-old forever). Runs even with empty
                # eps so a shrunk contributor set prunes stale MENTIONS.
                prov = self._replace_fact_provenance(
                    current["uuid"], eps, now, summary_template=summary_template,
                    refresh_props=refresh_props, namespace=namespace,
                )
            elif view_class is ViewClass.FACT and eps:
                prov = self._refresh_fact_provenance(
                    current["uuid"], eps, now, summary_template=summary_template,
                    refresh_props=refresh_props, namespace=namespace,
                )
            elif view_class is ViewClass.FACT:
                namespace_key = normalize_namespace(namespace)
                touched = self.neo4j.execute(
                    """
                    MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
                    ON CREATE SET f.generation = 0, f.created_at = datetime($now)
                    SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
                    WITH f
                    MATCH (n:Entity {uuid: $u})
                    WHERE coalesce(n.view_current, n.qs_current, true)
                      AND NOT coalesce(n.retired, false)
                    SET n.last_accessed = $now,
                        n.view_fence_generation = f.generation
                    SET n += $refresh
                    RETURN n.uuid AS uuid
                    """,
                    {
                        "namespace_key": namespace_key,
                        "operation_id": f"touch:{current['uuid']}:{now}",
                        "u": current["uuid"],
                        "now": now,
                        "refresh": dict(refresh_props or {}),
                    },
                    timeout_s=SAGA_MUTATION_TIMEOUT_S,
                )
                if not touched:
                    raise ValueError("FACT View refresh refused after concurrent lifecycle change")
                prov = {}
            else:
                self.neo4j.execute(
                    f"MATCH (n:{label} {{uuid:$u}}) SET n.last_accessed=$now, n += $refresh "
                    "RETURN n.uuid",
                    {"u": current["uuid"], "now": now, "refresh": dict(refresh_props or {})},
                    timeout_s=SAGA_MUTATION_TIMEOUT_S,  # CF-211
                )
                prov = {}
            return {"uuid": current["uuid"], "view_key": key, "kind": kind,
                    "created": False, "superseded": False, **prov}

        # LWW guard (fold-algebra Law 1): for a value register, never let a temporally-OLDER event
        # overwrite the current version. Keep current authoritative; do not create a stale current.
        if require_newer and current is not None and _is_older(valid_at, current.get("valid_at")):
            return {"uuid": current["uuid"], "view_key": key, "kind": kind,
                    "created": False, "superseded": False, "stale_skipped": True}

        # `node_uuid` lets the saga coordinator FREEZE the new version's uuid at PREPARE, so a
        # crash-replay recreates the same node instead of forking a competing one (plan A6/E3).
        new_uuid = node_uuid or str(uuid4())
        namespace_key = normalize_namespace(namespace)
        evidence_scope = tenant_scope_cypher("e")
        # Shared view identity + the kind's value slot, merged onto the fixed recall stamps.
        extra: dict[str, Any] = {
            "is_view": True, "view_kind": kind, "view_key": key,
            "view_subject": subject.strip(), "view_current": True, "view_sig": str(sig),
            **lifecycle,
            # entity-anchored identity (scalar_state): the resolved UUID drives the key while
            # view_subject keeps the display. Only stamped when supplied, so existing kinds unchanged.
            **({"view_subject_uuid": subject_uuid.strip()} if subject_uuid else {}),
            **extra_props,
            # audit receipt: provenance-only, excluded from sig above so it never supersedes, and
            # never embedded/surfaced so it never ranks. Drop None values (Neo4j has no null props).
            **{k: v for k, v in (audit_props or {}).items() if v is not None},
        }
        if view_class is ViewClass.FACT:
            # Durable, version-local provenance (plan D1). The UUID list is the audit receipt and
            # MENTIONS is the live retention edge. A current FACT may only be written when every UUID
            # resolves to live evidence; explicit evidence erasure retires the dependent View.
            extra["episode_uuids"] = eps
            extra["supporting_event_count"] = len(eps)
        # DB-level "one current per view_key" boundary for scalar_state ONLY (C.4.4.4). The property is
        # unique-constrained and carried ONLY by a CURRENT scalar_state node, so two independent workers
        # that both read "no current" and race to CREATE cannot both commit a view_current=true node for
        # the slot — the loser's tx fails the constraint. NULL on every other kind, so metric/FACT are
        # byte-identical. Removed from the superseded node below and on retire, so a legitimate new
        # version / re-materialization is free to claim the key.
        if kind == "scalar_state":
            extra["ss_view_key_current"] = key
        # ATOMIC create-and-supersede (plan E, Phase 2). Creating the new current version and
        # marking the prior one noncurrent MUST be one statement: split into two execute() calls,
        # a crash between them left BOTH versions current, and fetch_metric_state's ORDER BY ...
        # LIMIT 1 then read the new one as a clean after-state -> the saga marked a two-current
        # graph COMMITTED. One statement closes that window.
        #
        # Two FOREACH-over-CASE guards instead of `WITH n WHERE ...`: a WHERE filters the row out
        # of the pipeline, which for a null embedding (every Metric) would drop `n` before the
        # supersede clause could run. FOREACH conditionally applies writes WITHOUT gating the row.
        old_uuid = current["uuid"] if current is not None else None
        # scalar_state ONLY: hand the current-key marker from the superseded node to the new one. The
        # OLD node's marker MUST be cleared BEFORE the new node is created carrying it: Neo4j enforces
        # property-uniqueness EAGERLY (at the write, not deferred to commit), so creating the new node
        # with ss_view_key_current while the old node still holds it trips the constraint mid-statement
        # -- deterministically breaking EVERY scalar supersession (proven on the live throwaway; see
        # plan menhir-scalar-view-supersession-dedup-race.md). Clearing old first means the key is never
        # held by two nodes at once. NULL/no-op for every other kind, so the shared write stays
        # byte-identical off the scalar path. Still ONE statement, so the crash-atomicity that made
        # create-and-supersede a single execute() is preserved (a mid-statement crash rolls back both).
        clear_current_key = "REMOVE o.ss_view_key_current" if kind == "scalar_state" else ""
        create_and_supersede = f"""
            MERGE (f:EvidenceNamespaceFence {{namespace_key: $namespace_key}})
            ON CREATE SET f.generation = 0, f.created_at = datetime($now)
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            OPTIONAL MATCH (actual:{label})
            WHERE (actual.view_key = $key OR actual.qs_key = $key)
              AND coalesce(actual.view_current, actual.qs_current, true)
            OPTIONAL MATCH (subject_entity:Entity {{uuid: $subject_uuid}})
            WITH f, actual, subject_entity
            WHERE (($old IS NULL AND actual IS NULL) OR actual.uuid = $old)
              AND ($allow_canonical_self OR subject_entity IS NULL OR NOT __STRUCTURAL_SELF__)
            CALL {{
                UNWIND CASE WHEN size($eps) = 0 THEN [null] ELSE $eps END AS eid
                OPTIONAL MATCH (e)
                WHERE ((e:Episodic AND e.uuid = eid) OR
                       (e:TurnEvidence AND e.turn_id = eid))
                  AND {evidence_scope}
                  AND e.evidence_finalized = true
                  AND NOT coalesce(e.evidence_quarantined, false)
                WITH eid, [candidate IN collect(DISTINCT e)
                           WHERE candidate IS NOT NULL] AS candidates
                WITH collect({{eid: eid, candidates: candidates}}) AS resolved
                RETURN [row IN resolved WHERE size(row.candidates) = 1 |
                        head(row.candidates)] AS evidence,
                       size([row IN resolved WHERE size(row.candidates) = 1]) AS resolved_count
            }}
            WITH f, actual, evidence, resolved_count
            WHERE resolved_count = size($eps)
              AND all(e IN evidence WHERE
                  coalesce(e.evidence_generation, e.publication_generation) = f.generation)
            OPTIONAL MATCH (old:{label} {{uuid: $old}})
            FOREACH (o IN CASE WHEN old IS NULL THEN [] ELSE [old] END |
                SET o.view_current = false, o.qs_current = false, o.superseded_by = $uuid,
                    o.expired_at = datetime($now), o.last_accessed = $now
                {clear_current_key})
            WITH f, old, evidence
            CREATE (n:{label} {{
                uuid: $uuid, name: $name, summary: $summary, content: $summary,
                group_id: $ns, namespace: $ns_stamped, {_SHARED_STAMPS},
                source: $source, source_confidence: $sc,
                valid_at: datetime($valid_at), created_at: datetime($now), last_accessed: datetime($now)
            }})
            SET n += $extra
            SET n.view_fence_generation = f.generation
            FOREACH (_ IN CASE WHEN $emb IS NULL THEN [] ELSE [1] END |
                SET n.name_embedding = $emb)
            FOREACH (o IN CASE WHEN old IS NULL THEN [] ELSE [old] END |
                SET n.supersedes = $old
                MERGE (n)-[:SUPERSEDES]->(o))
            FOREACH (e IN evidence | MERGE (e)-[:MENTIONS]->(n))
            RETURN n.uuid AS uuid
            """.replace("__STRUCTURAL_SELF__", structural_self_cypher("subject_entity"))
        params = {"uuid": new_uuid, "name": name[:300], "summary": summary[:1000],
                  "ns": (namespace or ""), "ns_stamped": ns_stamped,
                  "namespace_key": namespace_key, **tenant_scope_params(namespace_key),
                  "operation_id": new_uuid, "key": key, "eps": eps,
                  "source": source, "sc": float(source_confidence),
                  "valid_at": valid_at, "now": now, "extra": extra, "emb": name_embedding,
                  "old": old_uuid, "subject_uuid": subject_uuid,
                  "allow_canonical_self": (
                      self._canonical_self_binding_mode is not SelfBindMode.ENFORCE
                  )}
        if kind == "scalar_state":
            try:
                write_rows = self.neo4j.execute(
                    create_and_supersede, params, timeout_s=SAGA_MUTATION_TIMEOUT_S
                )
            except _Neo4jConstraintError:
                # A genuinely CONCURRENT writer won the current-key for this slot between our read and
                # CREATE. Our node rolled back; converge on the committed winner instead of forking a
                # duplicate current View. Now that old's marker is cleared BEFORE the new node takes it
                # (see above), single-writer supersession never reaches here, so this is the real
                # concurrent case: the winner was written by a peer folding the SAME committed log, so
                # it carries the value we intended -> a safe deduped no-op.
                winner = self._current_by_key(key, view_class=view_class)
                if winner is not None:
                    winner_sig = winner.get("sig")
                    # GUARDRAIL (plan): a dedup that lands on a value OTHER than the one we intended is a
                    # LOST supersession, not a benign no-op. It must be loud, never silent. (Expected to
                    # be unreachable after the ordering fix; kept as defense in depth.)
                    if str(winner_sig) != str(sig):
                        logger.warning(
                            "scalar_state View dedup converged on a STALE winner for key=%s: "
                            "intended sig=%s but winner sig=%s (lost supersession)", key, sig, winner_sig)
                    else:
                        logger.info(
                            "scalar_state View write deduped on concurrent create for key=%s; "
                            "converging on winner=%s", key, winner.get("uuid"))
                    return {"uuid": winner["uuid"], "view_key": key, "kind": kind,
                            "created": False, "superseded": False, "deduped": True,
                            "winner_sig": winner_sig}
                raise
        else:
            write_rows = self.neo4j.execute(
                create_and_supersede, params, timeout_s=SAGA_MUTATION_TIMEOUT_S
            )
        if not write_rows:
            if (
                self._canonical_self_binding_mode is SelfBindMode.ENFORCE
                and subject_uuid is not None
                and self._subject_is_structural_self(subject_uuid)
            ):
                raise UnconfirmedSelfAssertionError(
                    "View cannot attach to canonical self without exact owner confirmation"
                )
            raise ValueError(
                "FACT View write refused: every declared contributor UUID must resolve to live "
                ":Episodic or :TurnEvidence evidence"
            )
        return {"uuid": new_uuid, "view_key": key, "kind": kind,
                "created": True, "superseded": current is not None,
                "episodes_present": len(eps), "episodes_missing": 0,
                "supporting_event_count": len(eps)}

    def _link_episodes(
        self, node_uuid: str, episode_uuids: list[str], now: str,
        *, view_class: ViewClass = ViewClass.FACT,
    ) -> dict[str, Any]:
        """Provenance: (evidence)-[:MENTIONS]->(view) for each contributing source.

        Reports which episodes were actually PRESENT and which were MISSING instead of silently
        no-opping (plan D3). The old query inner-MATCHed the episodes, so a fact whose episodes had
        already been reaped got zero MENTIONS and zero signal about it -- the exact way prod facts
        ended up claiming supporting events they could not point at. Both legacy ``:Episodic`` and
        production ``:TurnEvidence`` inputs are evidence anchors.

        Only FACT views carry MENTIONS; METRIC nodes never do (the _write_version guard forbids
        passing episodes for METRIC), so this is a no-op there.
        """
        if not episode_uuids:
            return {"episodes_present": 0, "episodes_missing": 0}
        label = _label_for(view_class)
        rows = self.neo4j.execute(
            f"""
            MATCH (n:{label} {{uuid:$u}})
            UNWIND $eps AS eid
            OPTIONAL MATCH (ep:Episodic {{uuid: eid}})
            OPTIONAL MATCH (te:TurnEvidence {{turn_id: eid}})
            WITH n, coalesce(ep, te) AS e
            FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
                MERGE (e)-[:MENTIONS]->(n))
            RETURN collect(coalesce(e.uuid, e.turn_id)) AS present
            """,
            {"u": node_uuid, "eps": episode_uuids},
        )
        present = {str(u) for u in (dict(rows[0]).get("present") or [])} if rows else set()
        missing = [u for u in episode_uuids if u not in present]
        _log_missing_episodes(node_uuid, episode_uuids, missing)
        return {"episodes_present": len(present), "episodes_missing": len(missing)}

    def _refresh_fact_provenance(
        self, node_uuid: str, episode_uuids: list[str], now: str, *,
        summary_template: str | None, refresh_props: dict[str, Any] | None,
        namespace: str | None,
    ) -> dict[str, Any]:
        """Unchanged-value provenance refresh for a FACT (plan D2), in ONE Neo4j operation.

        Unions the stored `episode_uuids` with the incoming ones, sorts and deduplicates the union,
        rewrites `supporting_event_count` and the count-bearing summary/content, MERGEs MENTIONS for
        the episodes that exist, and returns the present/missing split -- while leaving `view_value`,
        `view_sig`, `valid_at`, the node UUID, and currentness untouched.

        The union happens INSIDE Cypher, never as a Python read-modify-write: two concurrent
        refreshes of the same fact would otherwise each read the same base list and the second would
        clobber the first's UUIDs. `collect(DISTINCT ...)` after an `ORDER BY` gives the sorted dedup
        server-side; the count is substituted into the summary template in the same statement, so the
        stored count and the quoted count cannot disagree even transiently.

        CONCURRENCY: being one statement is NOT sufficient. Neo4j is read-committed and does not
        protect against lost updates -- the write lock is taken at the `SET`, but the property read
        happens upstream in the `UNWIND`, so two concurrent refreshes could both read the same base
        list, serialize at the SET, and have the second commit a union missing the first's UUIDs.
        The leading `SET n.last_accessed = $now` exists to take the node's exclusive write lock
        BEFORE `episode_uuids` is read (Neo4j's documented explicit-locking pattern). A concurrent
        refresh then blocks there, and once it proceeds its read sees the other's committed list. It
        writes `last_accessed`, which this statement sets anyway, so the lock costs no extra property.
        """
        from menhir.domain.namespace import normalize_namespace, tenant_scope_cypher, tenant_scope_params

        namespace_key = normalize_namespace(namespace)
        rows = self.neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime($now)
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            MATCH (n:Entity {uuid:$u})
            WHERE coalesce(n.view_current, n.qs_current, true)
              AND NOT coalesce(n.retired, false)
            OPTIONAL MATCH (old_evidence)-[:MENTIONS]->(n)
            WHERE old_evidence:Episodic OR old_evidence:TurnEvidence
            WITH f, n, collect(DISTINCT coalesce(old_evidence.uuid,
                                                 old_evidence.turn_id)) AS old_mentions
            WHERE size(old_mentions) = size(coalesce(n.episode_uuids, []))
              AND all(eid IN old_mentions WHERE eid IN coalesce(n.episode_uuids, []))
            CALL {
                WITH n
                WITH n, (coalesce(n.episode_uuids, []) + $eps) AS all_eids
                UNWIND CASE WHEN size(all_eids) = 0 THEN [null] ELSE all_eids END AS eid
                WITH DISTINCT n, eid
                OPTIONAL MATCH (e)
                WHERE ((e:Episodic AND e.uuid = eid) OR
                       (e:TurnEvidence AND e.turn_id = eid))
                  AND """ + tenant_scope_cypher("e") + """
                  AND e.evidence_finalized = true
                  AND NOT coalesce(e.evidence_quarantined, false)
                WITH eid, [candidate IN collect(DISTINCT e)
                           WHERE candidate IS NOT NULL] AS candidates
                WITH collect({eid: eid, candidates: candidates}) AS resolved
                RETURN [row IN resolved WHERE size(row.candidates) = 1 |
                        head(row.candidates)] AS evidence,
                       [row IN resolved | row.eid] AS requested,
                       size([row IN resolved WHERE size(row.candidates) = 1]) AS resolved_count
            }
            WITH f, n, evidence, requested, resolved_count
            WHERE resolved_count = size(requested)
              AND all(e IN evidence WHERE
                  coalesce(e.evidence_generation, e.publication_generation) = f.generation)
            SET n.last_accessed = $now, n.view_fence_generation = f.generation
            WITH n, (coalesce(n.episode_uuids, []) + $eps) AS all_eids
            UNWIND CASE WHEN size(all_eids) = 0 THEN [null] ELSE all_eids END AS eid
            WITH n, eid ORDER BY eid
            WITH n, collect(DISTINCT eid) AS uuids
            SET n.episode_uuids = uuids,
                n.supporting_event_count = size(uuids),
                n.summary = CASE WHEN $tpl IS NULL THEN n.summary
                                 ELSE replace($tpl, $token, toString(size(uuids))) END,
                n.content = CASE WHEN $tpl IS NULL THEN n.content
                                 ELSE replace($tpl, $token, toString(size(uuids))) END
            SET n += $refresh
            WITH n, uuids
            CALL {
                WITH n, uuids
                UNWIND CASE WHEN size(uuids) = 0 THEN [null] ELSE uuids END AS eid
                OPTIONAL MATCH (ep:Episodic {uuid: eid})
                OPTIONAL MATCH (te:TurnEvidence {turn_id: eid})
                WITH n, coalesce(ep, te) AS e
                FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
                    MERGE (e)-[:MENTIONS]->(n))
                RETURN [uuid IN collect(coalesce(e.uuid, e.turn_id))
                        WHERE uuid IS NOT NULL] AS present
            }
            RETURN uuids AS stored, present
            """,
            {"u": node_uuid, "eps": episode_uuids, "now": now, "token": _COUNT_TOKEN,
             "namespace_key": namespace_key,
             "operation_id": f"refresh:{node_uuid}:{now}",
             "tpl": summary_template, "refresh": dict(refresh_props or {}),
             **tenant_scope_params(namespace_key)},
        )
        if not rows:
            raise ValueError(
                "FACT View provenance refresh refused: every stored contributor UUID must resolve "
                "to live :Episodic or :TurnEvidence evidence"
            )
        row = dict(rows[0])
        stored = [str(u) for u in (row.get("stored") or [])]
        present = {str(u) for u in (row.get("present") or [])}
        missing = [u for u in stored if u not in present]
        _log_missing_episodes(node_uuid, stored, missing)
        return {"episodes_present": len(present), "episodes_missing": len(missing),
                "supporting_event_count": len(stored)}

    def _replace_fact_provenance(
        self, node_uuid: str, episode_uuids: list[str], now: str, *,
        summary_template: str | None, refresh_props: dict[str, Any] | None,
        namespace: str | None,
    ) -> dict[str, Any]:
        """Unchanged-signature provenance REPLACEMENT for an authoritative rebuild, in ONE Neo4j
        operation. Unlike `_refresh_fact_provenance` (which UNIONS), this sets `episode_uuids` to
        EXACTLY the incoming contributor set, `supporting_event_count` to that length, prunes
        MENTIONS from episodes no longer in the set, and merges MENTIONS for the new set — because a
        full rebuild replaces the projection, a contributor episode that dropped out must not linger.
        `refresh_props` carries the full re-rendered projection (surface + ss_* + audit). Existing
        fact/counter consumers are untouched: they never pass replace_provenance, so they keep union.

        The leading `SET n.last_accessed` takes the node's write lock before provenance is rewritten
        (same explicit-locking pattern as the union path). Handles an EMPTY set: prune all MENTIONS,
        store []."""
        from menhir.domain.namespace import normalize_namespace, tenant_scope_cypher, tenant_scope_params

        namespace_key = normalize_namespace(namespace)
        rows = self.neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime($now)
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            MATCH (n:Entity {uuid:$u})
            WHERE coalesce(n.view_current, n.qs_current, true)
              AND NOT coalesce(n.retired, false)
            OPTIONAL MATCH (old_evidence)-[:MENTIONS]->(n)
            WHERE old_evidence:Episodic OR old_evidence:TurnEvidence
            WITH f, n, collect(DISTINCT coalesce(old_evidence.uuid,
                                                 old_evidence.turn_id)) AS old_mentions
            WHERE size(old_mentions) = size(coalesce(n.episode_uuids, []))
              AND all(eid IN old_mentions WHERE eid IN coalesce(n.episode_uuids, []))
            CALL {
                WITH n
                UNWIND CASE WHEN size($eps) = 0 THEN [null] ELSE $eps END AS eid
                OPTIONAL MATCH (e)
                WHERE ((e:Episodic AND e.uuid = eid) OR
                       (e:TurnEvidence AND e.turn_id = eid))
                  AND """ + tenant_scope_cypher("e") + """
                  AND e.evidence_finalized = true
                  AND NOT coalesce(e.evidence_quarantined, false)
                WITH eid, [candidate IN collect(DISTINCT e)
                           WHERE candidate IS NOT NULL] AS candidates
                WITH collect({eid: eid, candidates: candidates}) AS resolved
                RETURN [row IN resolved WHERE size(row.candidates) = 1 |
                        head(row.candidates)] AS evidence,
                       size([row IN resolved WHERE size(row.candidates) = 1]) AS resolved_count
            }
            WITH f, n, evidence, resolved_count
            WHERE resolved_count = size($eps)
              AND all(e IN evidence WHERE
                  coalesce(e.evidence_generation, e.publication_generation) = f.generation)
            SET n.last_accessed = $now,
                n.view_fence_generation = f.generation,
                n.episode_uuids = $eps,
                n.supporting_event_count = size($eps),
                n.summary = CASE WHEN $tpl IS NULL THEN n.summary
                                 ELSE replace($tpl, $token, toString(size($eps))) END,
                n.content = CASE WHEN $tpl IS NULL THEN n.content
                                 ELSE replace($tpl, $token, toString(size($eps))) END
            SET n += $refresh
            WITH n
            CALL {
                WITH n
                OPTIONAL MATCH (old)-[m:MENTIONS]->(n)
                WHERE NOT coalesce(old.uuid, old.turn_id) IN $eps
                  AND (old:Episodic OR old:TurnEvidence)
                DELETE m
                RETURN count(*) AS pruned
            }
            WITH n
            CALL {
                WITH n
                UNWIND CASE WHEN size($eps) = 0 THEN [null] ELSE $eps END AS eid
                OPTIONAL MATCH (ep:Episodic {uuid: eid})
                OPTIONAL MATCH (te:TurnEvidence {turn_id: eid})
                WITH n, coalesce(ep, te) AS e
                FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
                    MERGE (e)-[:MENTIONS]->(n))
                RETURN [uuid IN collect(coalesce(e.uuid, e.turn_id))
                        WHERE uuid IS NOT NULL] AS present
            }
            RETURN present
            """,
            {"u": node_uuid, "eps": episode_uuids, "now": now, "token": _COUNT_TOKEN,
             "namespace_key": namespace_key,
             "operation_id": f"replace:{node_uuid}:{now}",
             "tpl": summary_template, "refresh": dict(refresh_props or {}),
             **tenant_scope_params(namespace_key)},
        )
        if not rows:
            raise ValueError(
                "FACT View authoritative rebuild refused: every declared contributor UUID must "
                "resolve to live :Episodic or :TurnEvidence evidence"
            )
        present = {str(u) for u in (dict(rows[0]).get("present") or [])} if rows else set()
        missing = [u for u in episode_uuids if u not in present]
        _log_missing_episodes(node_uuid, episode_uuids, missing)
        return {"episodes_present": len(present), "episodes_missing": len(missing),
                "supporting_event_count": len(episode_uuids)}
