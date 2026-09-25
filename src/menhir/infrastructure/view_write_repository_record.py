"""View record path for the shared View writer: anchor resolution and the generic upsert."""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.view_models import (
    _checked_template,
    _normalize_episode_uuids,
    _now,
)


class ViewRecordMixin:
    """`record` plus evidence-anchor resolution -- the one point every contributor-declaring
    writer crosses before a version is written (see `_resolve_evidence_anchors`)."""

    # ------------------------------------------------------------------ generic write

    def _resolve_evidence_anchors(
        self, episode_uuids: list[str], *, namespace: str | None,
    ) -> list[str]:
        """Normalize declared contributors to the anchor kind the View gate can accept.

        ``TypedAssertion.episode_uuid`` is a POLYMORPHIC anchor: the writer binds either an
        :Episodic (the legacy/fixture path) or a :TurnEvidence (the production ADR-0001 path that
        carries the declarant foundation) -- see typed_assertion_models.py's G14 bridge comment. The
        fold propagates whichever was stored, so a scalar View could declare either.

        But the shared FACT writer requires ``evidence_finalized = true``, and that is only ever set
        on :TurnEvidence (turn_evidence_repository.py) or on publication-intent artifacts. An
        Episodic-anchored contributor therefore made the write UNSATISFIABLE -- a 500 out of
        /api/phase3/run, not a degraded result -- while a TurnEvidence-anchored one succeeded. One
        fold could produce both.

        Episodic anchors are resolved through their ``ADMITTED_ON`` edge to the grounding
        :TurnEvidence, which is the same evidence reached the other way, so this only rewrites HOW
        the receipt names its anchor -- never WHICH evidence it claims. Tenant-scoped, so an
        Episodic can never be resolved onto a foreign silo's turn.

        Raises when an anchor cannot be resolved. That branch is deliberately LOUD: an empty receipt
        is NOT a safe fallback here the way it was for the OPERATOR-audience admission audit --
        scalar_state stamps ``view_audience = RECALL``, and view_live_provenance_cypher requires a
        non-empty receipt for RECALL views, so silently dropping contributors would publish a View
        that can never be recalled.
        """
        from menhir.domain.namespace import (
            normalize_namespace, tenant_scope_cypher, tenant_scope_params,
        )

        eps = [u for u in (episode_uuids or []) if u]
        if not eps:
            return []
        namespace_key = normalize_namespace(namespace)
        rows = self.neo4j.execute(
            f"""
            UNWIND $eps AS eid
            OPTIONAL MATCH (te:TurnEvidence {{turn_id: eid}})
            WHERE {tenant_scope_cypher("te")}
            OPTIONAL MATCH (ep:Episodic {{uuid: eid}})
            WHERE {tenant_scope_cypher("ep")}
            OPTIONAL MATCH (ep)-[:ADMITTED_ON]->(a:TurnEvidence)
            WHERE {tenant_scope_cypher("a")}
            WITH eid, te.turn_id AS direct,
                 coalesce(ep.evidence_finalized, false) AS ep_finalized,
                 coalesce(ep.evidence_quarantined, false) AS ep_quarantined,
                 [t IN collect(DISTINCT a.turn_id) WHERE t IS NOT NULL] AS grounded
            RETURN eid, direct, ep_finalized, ep_quarantined, grounded
            """,
            {"eps": eps, **tenant_scope_params(namespace_key)},
        )
        by_eid = {str(r["eid"]): r for r in rows}
        resolved: list[str] = []
        unresolved: list[str] = []
        for eid in eps:
            row = by_eid.get(str(eid))
            if row is None:
                unresolved.append(f"{eid}: names no in-tenant evidence")
                continue
            if row.get("direct"):
                resolved.append(str(row["direct"]))
                continue
            # Already-valid evidence passes through UNCHANGED. The writer's gate accepts a finalized,
            # unquarantined :Episodic as readily as a :TurnEvidence -- publication intents can set
            # evidence_finalized on an episode (evidence_publication_intents.py's artifact_node MATCH
            # carries no label constraint, and the intent manifest includes resolved_episode_uuid).
            # That path is currently inert only because no GraphitiArtifactManifestService is wired,
            # which is a wiring gap and not a guarantee. Rewriting such an anchor would make this
            # resolver STRICTER than the gate it feeds and reject evidence the writer would accept.
            if row.get("ep_finalized") and not row.get("ep_quarantined"):
                resolved.append(str(eid))
                continue
            grounded = [str(t) for t in (row.get("grounded") or [])]
            if len(grounded) == 1:
                resolved.append(grounded[0])
            elif not grounded:
                unresolved.append(
                    f"{eid}: :Episodic anchor with no ADMITTED_ON grounding :TurnEvidence"
                )
            else:
                unresolved.append(
                    f"{eid}: :Episodic anchor grounded on {len(grounded)} turns (ambiguous)"
                )
        if unresolved:
            raise ValueError(
                "scalar View write refused before persistence: declared contributors could not be "
                "resolved to finalizable :TurnEvidence anchors -- " + "; ".join(unresolved)
            )
        # Order-stable dedup: two Episodic anchors can ground on one turn.
        seen: set[str] = set()
        return [u for u in resolved if not (u in seen or seen.add(u))]

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
        key = self._key(namespace, subject, kind.key_discriminator(payload), subject_uuid=subject_uuid)
        # Normalize provenance ONCE, before the surface is rendered, so the supporting-event count
        # quoted in the summary is the count actually stored on the node (plan D1: sorted, dedup).
        # Contributor anchors are normalized to the kind the gate below can accept BEFORE the
        # surface is rendered, because resolution can change the list (an Episodic maps to its
        # grounding :TurnEvidence, and two Episodic anchors can collapse onto one turn). Doing
        # it here rather than in each record_* wrapper is deliberate: this is the one point
        # every contributor-declaring writer crosses. record_scalar_state resolved its own
        # anchors while record_counter and record_scalar_history did not, which made those two
        # paths' writes unsatisfiable -- see _resolve_evidence_anchors.
        eps = _normalize_episode_uuids(
            self._resolve_evidence_anchors(
                list(kind.episode_uuids(payload) or []), namespace=namespace))
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
