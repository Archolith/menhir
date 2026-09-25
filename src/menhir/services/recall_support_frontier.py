"""Frontier shadow passes and active oracle/warden application for the recall mixin stack."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from menhir.domain.recall import ScoredMemory
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.domain.retrieval_trace_models import (
    AssertionShadowRow,
    AssertionShadowTrace,
    FacetShadowRow,
    FacetShadowTrace,
)
from menhir.domain.truth.kinds import DIVERSITY_FAMILY as _FRONTIER_DIVERSITY_FAMILY
from menhir.infrastructure.telemetry import record_mcp_event
from menhir.services.recall_policies import (
    _blend_oracle_order,
    _frontier_trace_enabled,
    _oracle_similarity,
)

logger = logging.getLogger(__name__)


class RecallSupportFrontierMixin:
    async def _run_assertion_shadow(
        self,
        query: str,
        namespace: str | None,
        candidate_inputs: list[dict[str, object]],
        metadata_by_uuid: dict[str, Any],
        query_project: str | None = None,
        tuning: RetrievalTuningConfig | None = None,
    ) -> AssertionShadowTrace:
        """Observe-only oracle/warden pass over the recall candidate set.

        SHADOW means: this records what the AssertionPipeline *would* have admitted /
        flagged / refused and how the oracle combiner *would* have ranked the
        candidates — it never changes the recall results, never writes the graph, and
        never raises into the recall path (the caller wraps it). Its only effect is the
        :class:`AssertionShadowTrace` attached to the retrieval trace, so the frontier
        oracle stack can be measured against the shipped ScoringService ranking on the
        live graph before any active wiring.

        The candidate ``metadata`` is the prefetched ``fetch_candidate_metadata`` snapshot
        plus ``evidence_kinds`` and ``project`` derived by ``_attach_frontier_metadata``
        (SUPPORTED_BY :Evidence, structural ANCHORED_TO project, episode source). ``created_at``
        + ``evidence_kinds`` give Temporal/Evidence real anchors; the candidate ``project`` +
        the query's ``file_context_project`` give the ScopeOracle a real project axis. Still
        absent by data-model limit: ``repo``/``branch`` are not modeled anywhere;
        ``valid_at``/``expired_at`` are graphiti EDGE bitemporal; and ``artifact_type``/
        ``anchors`` live on gated L4 ``Artifact`` nodes. So Scope runs on project+namespace
        only and Intent runs partial — recorded in ``note``.
        """
        from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
        from menhir.domain.oracles import CandidateMemory, QueryContext
        from menhir.services.assertion_pipeline import AssertionPipeline

        cmems = []
        for c in candidate_inputs:
            md = dict(metadata_by_uuid.get(str(c["uuid"]), {}))
            md["similarity"] = _oracle_similarity(
                float(c.get("similarity") or 0.0),
                c.get("retrieval_score_kind"),
            )
            cmems.append(CandidateMemory(
                id=str(c["uuid"]),
                content=str(c.get("content") or c.get("name") or ""),
                metadata=md,
            ))
        qctx = QueryContext(text=query, namespace=namespace, project=query_project)
        pipeline = AssertionPipeline(
            LogSpaceOracleCombiner(),
            auto_intent=tuning.enable_intent_lens if tuning else False,
            contradiction_interrupt=tuning.enable_contradiction_interrupt if tuning else False,
            belief_gate=tuning.enable_belief_gate if tuning else False,
            evidence_anchor=tuning.enable_evidence_anchor if tuning else True,
        )
        resolved_lens = pipeline._resolve_intent(qctx).intent
        outcome = await pipeline.run(qctx, cmems)
        rows = [
            AssertionShadowRow(
                candidate_id=r.candidate_id,
                rank=r.rank,
                decision=r.decision.value,
                score=r.score,
                label=r.label,
            )
            for r in outcome.ranked
        ]
        return AssertionShadowTrace(
            intent=resolved_lens,
            admitted=len(outcome.admitted),
            flagged=len(outcome.flagged),
            refused=len(outcome.refused),
            rows=rows,
            note=(
                "observe-only: results unchanged. created_at feeds Temporal; evidence_kinds "
                "(SUPPORTED_BY/ANCHORED_TO/episode-source) feeds Evidence; project "
                "(ANCHORED_TO structure_project vs query file_context_project) + namespace feed "
                "Scope. Still absent: repo/branch (not modeled), valid_at/expired_at (edge "
                "bitemporal), artifact_type/anchors (gated L4) -- so Scope is project+namespace "
                "only and Intent runs partial."
            ),
        )

    async def _run_facet_pass(
        self,
        query: str,
        namespace: str | None,
        eligible_uuids: list[str],
        *,
        query_project: str | None = None,
        active: bool = False,
    ) -> FacetShadowTrace:
        """FACET candidate-generation pass over the recall pool.

        By default this is the observe-only shadow. With ``active=True`` the caller
        fuses the returned order into candidate ranking. One bounded bulk graph query
        derives facets from ANCHORED_TO/DEFINES + metadata; scope/stale discipline is
        deferred to the warden chain.
        """
        from menhir.domain.facet_candidate_source import FacetCandidateSource
        from menhir.domain.facet_derivation import derive_facets
        from menhir.domain.facets import FacetedQuery

        q_facets = derive_facets(content=query, project=query_project, namespace=namespace)
        query_pairs = sorted(f"{f}={v}" for f, v in q_facets.discrete_pairs())
        neo4j = getattr(self.graph_adapter, "neo4j", None)
        if neo4j is None:  # no graph reader (e.g. stub adapter) -> empty, never crash
            return FacetShadowTrace(
                pool=len(eligible_uuids), candidates=0, query_facets=query_pairs, rows=[],
                note=(
                    "active facet unavailable: no Neo4j reader; base ranking retained."
                    if active
                    else "observe-only: no Neo4j reader on the graph adapter; "
                    "facet shadow skipped."
                ),
            )

        from menhir.infrastructure.facet_graph_reader import Neo4jFacetGraphReader

        source = FacetCandidateSource(Neo4jFacetGraphReader(neo4j))
        explanations = await asyncio.to_thread(
            source.contribute, FacetedQuery(facets=q_facets), list(eligible_uuids)
        )
        rows = [
            FacetShadowRow(
                candidate_id=e.memory_id, rank=e.rank, score=e.score,
                convergence=e.convergence, matched_required=e.matched_required,
            )
            for e in explanations
        ]
        return FacetShadowTrace(
            pool=len(eligible_uuids),
            candidates=len(explanations),
            query_facets=query_pairs,
            rows=rows,
            note=(
                "active: FACET overlap + meet-point convergence fused into candidate ranking; "
                "scope/stale discipline deferred to the warden chain."
                if active
                else "observe-only: FACET candidate generation over the recall pool by facet "
                "overlap + meet-point convergence; scope/stale discipline deferred to the "
                "warden chain. Results unchanged."
            ),
        )

    async def _apply_frontier(
        self,
        query: str,
        namespace: str | None,
        scored: list[ScoredMemory],
        metadata_by_uuid: dict[str, Any],
        tuning: RetrievalTuningConfig,
        query_project: str | None = None,
    ) -> tuple[list[ScoredMemory], str | None]:
        """Apply the ACTIVE frontier portions to the post-floor survivors.

        Replaces the ScoringService order with the oracle combiner order
        (``enable_oracle_ranking``) and/or applies the warden gate
        (``enable_warden_gate``: drop REFUSED, label FLAGGED). The source-aware floor has
        already run inside ScoringService, so junk is gone either way — this only reorders
        and gates the survivors. ``enable_intent_lens`` selects the temporal lens fed to the
        oracle/warden path (no effect on its own).

        Unlike the observe-only shadow, this CHANGES results, so it must fail safe: any
        error degrades to the input (ScoringService) order — never breaks recall. Returns
        ``(results, note)``.
        """
        from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
        from menhir.domain.oracles import CandidateMemory, QueryContext
        from menhir.domain.diversity import diversify
        from menhir.services.assertion_pipeline import AssertionPipeline

        def _family_for(uuid: str) -> str:
            kinds = metadata_by_uuid.get(uuid, {}).get("evidence_kinds") or ()
            for k in kinds:
                fam = _FRONTIER_DIVERSITY_FAMILY.get(str(k))
                if fam:
                    return fam
            return "semantic"

        try:
            cmems = []
            for s in scored:
                md = dict(metadata_by_uuid.get(s.uuid, {}))
                # Inject ScoringService's retrieval relevance on SemanticOracle's [0, 1]
                # contract. The source value is RRF, not cosine, for normal graph search.
                md["similarity"] = _oracle_similarity(
                    s.breakdown.semantic_similarity,
                    s.retrieval_score_kind,
                )
                cmems.append(CandidateMemory(id=s.uuid, content=str(s.content or s.name or ""), metadata=md))
            qctx = QueryContext(text=query, namespace=namespace, project=query_project)
            pipeline = AssertionPipeline(
                LogSpaceOracleCombiner(), auto_intent=tuning.enable_intent_lens,
                contradiction_interrupt=tuning.enable_contradiction_interrupt,
                belief_gate=tuning.enable_belief_gate,
                evidence_anchor=tuning.enable_evidence_anchor,
            )
            outcome = await pipeline.run(qctx, cmems)

            if _frontier_trace_enabled():
                import collections
                breakdown = collections.Counter(
                    f"{r.decision.value}:{r.reason}" for r in outcome.ranked
                )
                logger.warning(
                    "FRONTIER_TRACE ns=%s query=%r cands=%d admitted=%d flagged=%d refused=%d breakdown=%s",
                    namespace, query[:60], len(cmems), len(outcome.admitted),
                    len(outcome.flagged), len(outcome.refused), dict(breakdown),
                )

            result = scored
            if tuning.enable_oracle_ranking:
                rank_of = {r.candidate_id: r.rank for r in outcome.ranked}
                result = _blend_oracle_order(
                    result,
                    rank_of,
                    oracle_weight=tuning.oracle_rank_weight,
                )
            if tuning.enable_diversity_gate:
                result = diversify(result, family_of=lambda s: _family_for(s.uuid))
            if tuning.enable_warden_gate:
                refused = {r.candidate_id for r in outcome.refused}
                label_of = {r.candidate_id: r.label for r in outcome.flagged}
                result = [
                    replace(s, warden_label=label_of[s.uuid]) if s.uuid in label_of else s
                    for s in result
                    if s.uuid not in refused
                ]
            portions = [
                p for p, on in (
                    ("oracle_ranking", tuning.enable_oracle_ranking),
                    ("warden_gate", tuning.enable_warden_gate),
                    ("diversity_gate", tuning.enable_diversity_gate),
                    ("intent_lens", tuning.enable_intent_lens),
                    ("belief_gate", tuning.enable_belief_gate),
                ) if on
            ]
            note = ("frontier: " + ",".join(portions)) if portions else None
            # belief_gate only ADDS CurrentnessWarden to the chain; warden_gate is the master
            # switch that APPLIES the chain's verdicts (drop REFUSED / label FLAGGED). With
            # belief_gate on but warden_gate off, those verdicts are computed and discarded —
            # warn so the gate is not silently inert.
            if tuning.enable_belief_gate and not tuning.enable_warden_gate:
                warn = (
                    "belief_gate has no effect without warden_gate "
                    "(belief verdicts computed but not applied)"
                )
                logger.warning("recall frontier: %s query=%r", warn, query[:60])
                note = f"{note} | {warn}" if note else warn
            return result, note
        except Exception:  # active path: degrade to the old order, never break recall
            logger.exception(
                "Frontier apply failed query=%r -> degrading to ScoringService order", query[:60]
            )
            record_mcp_event(
                kind="background",
                operation="frontier_apply",
                payload={"query": query[:60]},
                result={"error": "frontier_apply_failed"},
                success=False,
            )
            return scored, None

    async def _attach_frontier_metadata(
        self,
        uuids: list[str],
        metadata_by_uuid: dict[str, Any],
    ) -> None:
        """Merge DERIVED ``evidence_kinds`` and ``project`` into candidate metadata.

        Entity nodes store neither field; both are derived from graph provenance:
          - ``evidence_kinds`` from SUPPORTED_BY :Evidence, a ``file`` anchor when the
            candidate is structurally ANCHORED_TO code, and MENTIONS-ing episode sources
            mapped via ``evidence_kind_for_source``.
          - ``project`` from the structural anchor's ``structure_project``, but ONLY when all
            anchors agree on a single project (a memory spanning projects is left unscoped so
            the ScopeOracle stays permissive rather than inventing a false conflict).
        Feeds both the warden gate and the shadow (they read these metadata keys). Best-effort:
        a fetch failure leaves both absent (warden treats as unanchored; scope stays unknown)
        and never breaks recall."""
        from menhir.domain.self_reinforcement import evidence_kind_for_source

        try:
            rows = await asyncio.to_thread(
                self.graph_adapter.fetch_candidate_provenance, uuids
            )
        except Exception:
            logger.exception("Provenance fetch failed; candidates treated as unanchored/unscoped")
            return
        for row in rows:
            try:
                uuid = str(row.get("uuid") or "").strip()
                if not uuid:
                    raise ValueError("provenance row has no uuid")
                kinds: set[str] = {
                    str(k) for k in (row.get("evidence_node_kinds") or []) if k
                }
                anchor_projects = {
                    str(p) for p in (row.get("anchor_projects") or []) if p
                }
                if anchor_projects:
                    kinds.add("file")  # anchored to real code == a file anchor
                for src in row.get("episode_sources") or []:
                    if src is not None:
                        kinds.add(evidence_kind_for_source(str(src)))
                meta = metadata_by_uuid.get(uuid)
                if meta is not None:
                    meta["evidence_kinds"] = tuple(sorted(kinds))
                    if len(anchor_projects) == 1:
                        meta["project"] = next(iter(anchor_projects))
                    anchor_paths = [
                        str(p) for p in (row.get("anchor_paths") or []) if p
                    ]
                    if anchor_paths:
                        meta["anchor_paths"] = tuple(anchor_paths)
                    if len(anchor_projects) == 1:
                        meta["anchor_project"] = next(iter(anchor_projects))
            except Exception as exc:
                logger.error(
                    "Recall skipped malformed provenance row uuid=%r keys=%s: %s: %s",
                    row.get("uuid"),
                    sorted(str(key) for key in row),
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )
