"""Bounded, deterministic OracleExecutor (R4) — the menhir production interface.

Oracle separation is only useful if it stays fast AND deterministic. This executor fans
oracle evaluation out across a bounded async pool (so one slow IO/model oracle does not
dominate wall time) while guaranteeing the invariant that matters:

    parallel oracle execution must NOT make ranking nondeterministic.

Determinism is structural: results are grouped by candidate and reduced in a FIXED order
(candidate id, then the executor's stable oracle priority), AFTER the concurrent gather —
so execution order never affects the output. Oracles are read-only pure functions (the
CandidateMemory metadata is an immutable mapping), so concurrent vs sequential yields
identical results; ``max_concurrency`` only bounds resource use.

Beyond the bench prototype this adds: real bounded concurrency (semaphore), a per-oracle
timeout that degrades to a neutral result (missing != falsity), and a query-snapshot
prefetch seam (commit range / dependency cone / bulk metadata) so oracles read a prepared
snapshot instead of fetching the world.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from menhir.domain.oracles import (
    CandidateMemory,
    OracleResult,
    QueryContext,
    RetrievalOracle,
)

logger = logging.getLogger(__name__)

# A prefetch hook: given the query + candidates, prepare a shared snapshot (commit range,
# dependency cone, bulk metadata) the oracles will read. Returns enriched candidates.
PrefetchFn = Callable[[QueryContext, list[CandidateMemory]], Awaitable[list[CandidateMemory]]]


class OracleExecutor:
    """Evaluate every (candidate, oracle) pair under a concurrency bound, reduce deterministically."""

    def __init__(
        self,
        oracles: list[RetrievalOracle],
        *,
        max_concurrency: int = 16,
        per_oracle_timeout_s: float = 5.0,
        prefetch: PrefetchFn | None = None,
    ) -> None:
        if not oracles:
            raise ValueError("OracleExecutor needs at least one oracle")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.oracles = oracles
        self.max_concurrency = max_concurrency
        self.per_oracle_timeout_s = per_oracle_timeout_s
        self.prefetch = prefetch
        # Stable oracle priority order for deterministic reduction.
        self._order = {oracle.name: i for i, oracle in enumerate(oracles)}

    async def evaluate(
        self, query: QueryContext, candidates: list[CandidateMemory]
    ) -> dict[str, list[OracleResult]]:
        """Return {candidate_id: [OracleResult, ...]} in deterministic order.

        Concurrency is bounded by a semaphore; a per-oracle timeout degrades to a neutral
        result. Reduction order is fixed (candidate id, then oracle priority), independent
        of which task finished first."""
        if not candidates:
            return {}
        if self.prefetch is not None:
            candidates = await self.prefetch(query, candidates)

        sem = asyncio.Semaphore(self.max_concurrency)

        async def _run(oracle: RetrievalOracle, candidate: CandidateMemory) -> tuple[str, OracleResult]:
            async with sem:
                try:
                    # Oracles are sync pure functions; offload so a slow IO oracle does not
                    # block the loop. Determinism comes from the reduction sort, not order.
                    result = await asyncio.wait_for(
                        asyncio.to_thread(oracle.evaluate, query, candidate),
                        timeout=self.per_oracle_timeout_s,
                    )
                except asyncio.TimeoutError:
                    logger.warning("oracle %s timed out on candidate %s -> neutral", oracle.name, candidate.id)
                    result = OracleResult.neutral(oracle.name, note="timeout")
                except Exception as exc:  # an oracle must never break the pipeline
                    logger.warning("oracle %s failed on candidate %s: %s -> neutral", oracle.name, candidate.id, exc)
                    result = OracleResult.neutral(oracle.name, note=f"error: {exc.__class__.__name__}")
                return candidate.id, result

        tasks = [_run(oracle, cand) for cand in candidates for oracle in self.oracles]
        flat = await asyncio.gather(*tasks)

        grouped: dict[str, list[OracleResult]] = {c.id: [] for c in sorted(candidates, key=lambda c: c.id)}
        for cid, result in flat:
            grouped[cid].append(result)
        for cid in grouped:
            grouped[cid].sort(key=lambda r: self._order.get(r.oracle, len(self._order)))
        # Re-key in sorted candidate order for a deterministic dict iteration order.
        return {cid: grouped[cid] for cid in sorted(grouped)}
