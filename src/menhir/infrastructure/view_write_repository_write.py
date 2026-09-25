"""Core version write for the shared View writer: create-and-supersede and refusal diagnosis."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from menhir.infrastructure.neo4j import SAGA_MUTATION_TIMEOUT_S
from menhir.infrastructure.view_models import (
    ViewClass,
    _SHARED_STAMPS,
    _is_older,
    _label_for,
    _normalize_episode_uuids,
)

try:  # neo4j is a hard runtime dep; guard the import so unit imports without the driver still load.
    from neo4j.exceptions import ConstraintError as _Neo4jConstraintError
except Exception:  # pragma: no cover - driver always present in the running service
    _Neo4jConstraintError = ()  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class ViewVersionWriteMixin:
    """`_write_version` -- the one place a View version is written -- plus the current-version
    read and the refusal diagnosis that names which gate refused."""

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
        namespace_key = normalize_namespace(namespace)
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
            WITH f, actual
            WHERE ($old IS NULL AND actual IS NULL) OR actual.uuid = $old
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
            """
        params = {"uuid": new_uuid, "name": name[:300], "summary": summary[:1000],
                  "ns": (namespace or ""), "ns_stamped": ns_stamped,
                  "namespace_key": namespace_key, **tenant_scope_params(namespace_key),
                  "operation_id": new_uuid, "key": key, "eps": eps,
                  "source": source, "sc": float(source_confidence),
                  "valid_at": valid_at, "now": now, "extra": extra, "emb": name_embedding,
                  "old": old_uuid}
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
            raise ValueError(self._diagnose_refusal(
                label=label, key=key, old_uuid=old_uuid, eps=eps,
                namespace_key=namespace_key, evidence_scope=evidence_scope,
            ))
        return {"uuid": new_uuid, "view_key": key, "kind": kind,
                "created": True, "superseded": current is not None,
                "episodes_present": len(eps), "episodes_missing": 0,
                "supporting_event_count": len(eps)}

    def _diagnose_refusal(
        self, *, label: str, key: str, old_uuid: str | None, eps: list[str],
        namespace_key: str, evidence_scope: str,
    ) -> str:
        """Explain WHICH gate refused the write in `create_and_supersede`.

        That statement drops its row for three independent reasons, and every one of them used to
        surface as the same "must resolve to live evidence" string:

          1. the compare-and-set on the current version (`actual.uuid = $old`) -- a CONCURRENT
             writer superseded the version we read. Nothing to do with evidence at all.
          2. contributor resolution -- an eid that is missing, out-of-tenant, unfinalized,
             quarantined, or ambiguous (>1 candidate).
          3. fence-generation equality -- the evidence is live but was stamped by a DIFFERENT
             namespace fence, or by this one before a purge bumped it.

        Misreporting (1) and (3) as (2) sent a real investigation down a race-condition path for a
        deterministic cross-tenant bug. This is best-effort and READ-ONLY: the write already failed,
        so a diagnosis that itself errors must never mask the refusal.
        """
        from menhir.domain.namespace import tenant_scope_params

        generic = ("FACT View write refused: every declared contributor UUID must resolve to live "
                   ":Episodic or :TurnEvidence evidence")
        try:
            rows = self.neo4j.execute(
                f"""
                OPTIONAL MATCH (actual:{label})
                WHERE (actual.view_key = $key OR actual.qs_key = $key)
                  AND coalesce(actual.view_current, actual.qs_current, true)
                WITH collect(actual.uuid) AS actual_uuids
                OPTIONAL MATCH (f:EvidenceNamespaceFence {{namespace_key: $namespace_key}})
                WITH actual_uuids, f.generation AS fence_generation
                CALL {{
                    UNWIND $eps AS eid
                    OPTIONAL MATCH (e)
                    WHERE (e:Episodic AND e.uuid = eid) OR (e:TurnEvidence AND e.turn_id = eid)
                    WITH eid, [c IN collect(CASE WHEN e IS NULL THEN null ELSE {{
                            in_tenant: ({evidence_scope}),
                            finalized: coalesce(e.evidence_finalized, false),
                            quarantined: coalesce(e.evidence_quarantined, false),
                            generation: coalesce(e.evidence_generation, e.publication_generation)
                        }} END) WHERE c IS NOT NULL] AS found
                    RETURN collect({{eid: eid, found: found}}) AS probes
                }}
                RETURN actual_uuids, fence_generation, probes
                """,
                {"key": key, "namespace_key": namespace_key, "eps": eps,
                 **tenant_scope_params(namespace_key)},
                timeout_s=SAGA_MUTATION_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - diagnosis is best-effort; never mask the refusal
            logger.debug("View refusal diagnosis failed for key=%s", key, exc_info=True)
            return generic
        if not rows:
            return generic

        row = rows[0]
        actual_uuids = [u for u in (row.get("actual_uuids") or []) if u is not None]
        fence_generation = row.get("fence_generation")
        probes = row.get("probes") or []
        observed = actual_uuids[0] if actual_uuids else None

        # (1) CAS: the current version is not the one we based this write on.
        if observed != old_uuid:
            return (
                f"View write refused: LOST UPDATE on view_key={key}. Expected the current version to "
                f"be {old_uuid!r} but found {observed!r} -- a concurrent writer superseded it between "
                f"our read and our write. This is NOT an evidence problem; retry from a fresh read."
            )

        # (2) contributor resolution, per declared uuid.
        problems: list[str] = []
        live: list[Any] = []
        for probe in probes:
            eid = probe.get("eid")
            found = probe.get("found") or []
            if not found:
                problems.append(f"{eid}: no :Episodic/:TurnEvidence node with that id exists")
                continue
            in_tenant = [c for c in found if c.get("in_tenant")]
            if not in_tenant:
                problems.append(
                    f"{eid}: exists but belongs to a DIFFERENT tenant than this View "
                    f"(namespace_key={namespace_key!r}); cross-tenant evidence never resolves"
                )
                continue
            usable = [c for c in in_tenant
                      if c.get("finalized") and not c.get("quarantined")]
            if not usable:
                states = ", ".join(
                    f"finalized={bool(c.get('finalized'))}/quarantined={bool(c.get('quarantined'))}"
                    for c in in_tenant
                )
                problems.append(f"{eid}: in-tenant but not usable evidence ({states})")
                continue
            if len(usable) > 1:
                problems.append(f"{eid}: AMBIGUOUS -- {len(usable)} live candidates share that id")
                continue
            live.append(usable[0])
        if problems:
            return ("FACT View write refused: declared contributor UUIDs did not resolve to live "
                    ":Episodic or :TurnEvidence evidence -- " + "; ".join(problems))

        # (3) fence generation: resolved, live, in-tenant, but stamped by another generation.
        stale = [c for c in live if c.get("generation") != fence_generation]
        if stale:
            gens = sorted({c.get("generation") for c in stale})
            return (
                f"View write refused: FENCE GENERATION mismatch on namespace_key={namespace_key!r}. "
                f"All contributors resolved live, but {len(stale)} carry evidence_generation {gens} "
                f"while that namespace's fence is at generation {fence_generation}. The evidence was "
                f"stamped by a different fence (cross-namespace contributor) or predates a purge."
            )
        return generic
