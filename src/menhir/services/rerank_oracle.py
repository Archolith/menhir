"""CrossEncoderRerankOracle: the recall cross-encoder behind the batch oracle contract.

The model scores the first ``top_n`` candidates of a recall in one batch. Each scored candidate
gets a RELEVANCE result with ``probability = sigmoid(logit)`` and the raw logit in
``log_odds``; candidates past ``top_n``, or with a non-finite logit, get MISSING. Inference is
``cross_encoder_rerank.rerank_scores`` unchanged, so another relevance model can sit behind the
same contract without touching recall.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from menhir.domain.oracles import (
    CandidateMemory,
    OraclePolarity,
    OracleResult,
    OracleTarget,
    QueryContext,
)
from menhir.services.cross_encoder_rerank import rerank_scores, sigmoid

RERANK_ORACLE_NAME = "cross_encoder_rerank"
RERANK_SOURCE_FAMILY = "rerank"


class RerankFailed(RuntimeError):
    """The model could not score this batch; ``str(exc)`` is the recall note."""


@dataclass(frozen=True)
class _Row:
    uuid: str
    name: str | None
    content: str | None


def rerank_candidate(uuid: str, name: str | None, summary: str | None, content: str | None) -> CandidateMemory:
    """The oracle's view of one recall candidate: the fields the reranker reads."""
    return CandidateMemory(id=uuid, content=content or "", metadata={"name": name, "summary": summary})


def logits_of(results: dict[str, OracleResult]) -> dict[str, float]:
    """``{id: logit}`` for the candidates the oracle scored (finite logits only)."""
    return {cid: r.log_odds for cid, r in results.items() if r.log_odds is not None}


class CrossEncoderRerankOracle:
    """Batch relevance oracle over the local ONNX cross-encoder."""

    name = RERANK_ORACLE_NAME

    def __init__(self, *, model_dir: str, device: str, top_n: int, loader: Any = None) -> None:
        self.model_dir = model_dir
        self.device = device
        self.top_n = top_n
        self.loader = loader

    async def score_batch(
        self, query: QueryContext, candidates: Sequence[CandidateMemory]
    ) -> tuple[dict[str, OracleResult] | None, str | None]:
        """``(results, None)``, or ``(None, note)`` when the model failed for this recall."""
        rows = [
            _Row(c.id, c.metadata.get("name"), c.content or None)  # type: ignore[arg-type]
            for c in candidates
        ]
        summaries = {c.id: {"summary": c.metadata.get("summary")} for c in candidates}
        logits, note = await rerank_scores(
            query.text, rows, summaries, top_n=self.top_n,
            model_dir=self.model_dir, device=self.device, loader=self.loader,
        )
        if logits is None:
            return None, note
        results: dict[str, OracleResult] = {}
        for c in candidates:
            logit = logits.get(c.id)
            if logit is None:
                results[c.id] = OracleResult.neutral(self.name, note="past rerank_top_n")
            elif not math.isfinite(logit):
                results[c.id] = OracleResult.neutral(self.name, note="non-finite rerank logit")
            else:
                results[c.id] = OracleResult(
                    oracle=self.name, probability=sigmoid(logit), polarity=OraclePolarity.SUPPORT,
                    target=OracleTarget.RELEVANCE, source_family=RERANK_SOURCE_FAMILY,
                    log_odds=logit, note=f"rerank logit {logit:.3f}",
                )
        return results, None

    async def evaluate_batch(
        self, query: QueryContext, candidates: list[CandidateMemory]
    ) -> dict[str, OracleResult]:
        results, note = await self.score_batch(query, candidates)
        if results is None:
            raise RerankFailed(note or "rerank unavailable")
        return results


class PrecomputedBatchOracle:
    """Replays results a batch oracle already produced this recall, so the model runs once."""

    def __init__(self, name: str, results: dict[str, OracleResult]) -> None:
        self.name = name
        self._results = dict(results)

    async def evaluate_batch(
        self, query: QueryContext, candidates: list[CandidateMemory]
    ) -> dict[str, OracleResult]:
        return {c.id: self._results[c.id] for c in candidates if c.id in self._results}
