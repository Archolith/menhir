"""Ranking explain rows for traced recalls: one row per returned result, for every strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from menhir.domain.retrieval_trace_models import RankingExplainRow


def build_ranking_rows(
    results: Sequence[Any],
    strategy: str,
    rerank: Mapping[str, float],
    explain: Mapping[str, Any],
    weights: Mapping[str, float],
) -> list[RankingExplainRow]:
    """``explain`` is what ``_apply_frontier`` recorded (``packets``, ``decisions``), or empty."""
    packets = explain.get("packets") or {}
    decisions = explain.get("decisions") or {}
    rows: list[RankingExplainRow] = []
    for rank, item in enumerate(results, start=1):
        uuid = str(getattr(item, "uuid", ""))
        packet = packets.get(uuid)
        decision = decisions.get(uuid)
        rows.append(RankingExplainRow(
            candidate_id=uuid,
            rank=rank,
            strategy=strategy,
            rerank_logit=rerank.get(uuid),
            role_logits=dict(packet.role_logits) if packet is not None else {},
            weights=dict(weights) if packet is not None else {},
            contributions=dict(packet.contributions) if packet is not None else {},
            ranking_score=packet.ranking_score if packet is not None else None,
            combiner_rank=decision.rank if decision is not None else None,
            decision=decision.decision.value if decision is not None else None,
            reason=decision.reason if decision is not None else "",
            label=decision.label if decision is not None else None,
            rationale=list(packet.rationale) if packet is not None else [],
        ))
    return rows
