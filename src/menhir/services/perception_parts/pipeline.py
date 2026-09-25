"""End-to-end orchestration: k extraction samples -> coref/verify wiring -> gate -> commit XOR
abstain, with committed groups folded to counter Views by the deterministic sink.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from menhir.domain.fold_algebra import Event
from menhir.infrastructure import consolidation_audit as _audit
from menhir.services.perception_parts.extract import extract_once
from menhir.services.perception_parts.families import _measure_noun_sig
from menhir.services.perception_parts.gate import gate
from menhir.services.perception_parts.keys import canonicalize_samples, count_spend_compound
from menhir.services.perception_parts.levers import (
    extract_stated_total,
    resolve_coreference,
    verify_candidate_detailed,
)
from menhir.services.perception_parts.model import Episode, GateDecision, _GraphAdapter
from menhir.services.seam_types import Embed, LlmComplete

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- (3) perceive -> fold


@dataclass
class PerceptionResult:
    committed: list[dict[str, Any]] = field(default_factory=list)
    abstained: list[GateDecision] = field(default_factory=list)
    decisions: list[GateDecision] = field(default_factory=list)
    #: raw extractor measure label -> canonical key, for the Phase 3 debug report's collapse table.
    raw_to_canonical: dict[str, str] = field(default_factory=dict)


def _emit_run_tally(
    run_tally: Any | None, graph_adapter: Any, *,
    subject: str, counter: str, value: float, namespace: str, source: str,
) -> None:
    """Record a run-level instrumentation tally.

    Prefers the :Metric saga (``run_tally.record_run_tally``) so the tally stays OUT of the
    semantic-recall layer. Falls back to the legacy :Entity ``record_counter`` only when no
    recorder is wired (non-scheduler callers / older tests). Never raises -- a failed diagnostic
    tally must not break perception.
    """
    try:
        if run_tally is not None:
            run_tally.record_run_tally(
                subject=subject, counter=counter, value=float(value), namespace=namespace
            )
        else:
            graph_adapter.record_counter(
                subject=subject, counter=counter, value=float(value),
                namespace=namespace, valid_at=None, source=source, name_embedding=None,
            )
    except Exception:
        logger.warning("failed to record run tally %s", counter, exc_info=True)


def perceive_and_fold(
    *,
    episodes: list[Episode],
    llm_complete: LlmComplete,
    graph_adapter: _GraphAdapter,
    k: int = 5,
    threshold: float = 1.0,
    namespace: str = "agent-experience",
    source: str = "perception",
    embed: Embed | None = None,
    dedup_threshold: float = 0.92,
    triangulation_tol: float = 0.0,
    record_abstentions: bool = False,
    run_tally: Any | None = None,
    cross_check: Callable[[str], float | None] | None = None,
    enable_cross_check: bool = False,
    coref_judge: LlmComplete | None = None,
    enable_coref: bool = False,
    coref_k: int = 3,
    verifier: Callable[[str, float, list[Event]], bool] | None = None,
    enable_verify: bool = False,
    verify_k: int = 3,
    verify_retries: int = 0,
    enable_stated_span_guard: bool = False,
    enable_sum_grounding: bool = False,
) -> PerceptionResult:
    """End-to-end perception boundary: extract k times -> gate -> commit XOR abstain.

    Committed groups fold to a counter View via `event_fold.fold_events_to_counter` (the deterministic
    sink). Abstained groups are a NO-OP by design — the raw episodes already carry the fallback, so
    absence of a View is the fallback (zero code). Optionally records one `perception_abstained`
    counter (the count of abstained measures this run) so the write-rate is itself a recallable fact
    (handoff sec 5). `k` samples require temp>0 in the injected `llm_complete` to be meaningful."""
    from menhir.services.event_fold import fold_events_to_counter

    # Lever B cross-check: an explicit `cross_check` wins; otherwise `enable_cross_check` builds the
    # default holistic derivation over the SAME episodes/LLM. `gate` calls it once per (subject,
    # measure), so k=1 for the cross-check falls out (per the plan's cost guard — holistic totals are
    # stable). Fully opt-in: neither set -> no veto-4, precision identical to before.
    if cross_check is None and enable_cross_check:
        cross_check = lambda measure: extract_stated_total(episodes, measure, llm_complete)  # noqa: E731

    samples = [extract_once(episodes, llm_complete) for _ in range(max(1, k))]

    # Lever C3 event coreference: collapse a purchase re-narrated across dates (which exact dedup
    # can't catch — different inferred dates). Applied per sample's sum/count groups BEFORE the gate;
    # a shared `memo` judges each (item, value) cluster once across all samples (cost guard). Opt-in:
    # explicit `coref_judge` wins, else `enable_coref` reuses `llm_complete`. Off -> behaviour unchanged.
    if coref_judge is None and enable_coref:
        coref_judge = llm_complete
    # The tri-state memo is the gate's window into coreference resolution: `None` when coref is off
    # (so the gate treats every ambiguous cluster as unresolved and vetoes — see veto 2b), a populated
    # dict when it ran (merge/separate = resolved, unsure = still vetoes).
    coref_memo: "dict[tuple[str, str], str] | None" = None
    if coref_judge is not None:
        coref_memo = {}
        for sample in samples:
            for g in sample:
                if g.reducer in ("sum", "count") and g.events:
                    g.events = resolve_coreference(g.events, coref_judge, k=coref_k, memo=coref_memo)

    # Lever C4 final verification: a focused audit of each candidate against its linked items, as the
    # last commit gate. Explicit `verifier` wins, else `enable_verify` builds the default over
    # `llm_complete`. Sharper than the blind cross-check (reviews the evidence); use one or both.
    # The default verifier supports the optional `anchor` kwarg for Law-3 candidates.
    if verifier is None and enable_verify:
        # detailed form returns (ok, votes, k) so a fail-closed SUM carries how-close it was into the
        # receipt (verifier receipt clarity); the gate accepts either a bool or this tuple.
        verifier = lambda measure, value, events, anchor=None: verify_candidate_detailed(  # noqa: E731
            measure, value, events, llm_complete, k=verify_k, anchor=anchor)

    # Measure-key canonicalization (pre-gate): collapse the same measure emitted under different
    # names across samples so the consistency gate votes on a stable canonical key, not raw
    # extractor labels. Identity map for measures that aren't in the alias table, so it is a no-op
    # for anything already stably keyed.
    samples, raw_to_canonical = canonicalize_samples(samples)

    decisions = gate(
        samples, threshold=threshold, embed=embed,
        dedup_threshold=dedup_threshold, triangulation_tol=triangulation_tol,
        cross_check=cross_check, verifier=verifier, coref_resolved=coref_memo,
        # episodes are needed by the stated-span guard AND the deterministic SUM-grounding path.
        episodes=episodes if (enable_stated_span_guard or enable_sum_grounding) else None,
        verify_retries=verify_retries,
        enable_sum_grounding=enable_sum_grounding,
    )

    result = PerceptionResult(decisions=decisions)
    result.raw_to_canonical = raw_to_canonical
    for d in decisions:
        if not d.committed:
            result.abstained.append(d)
            logger.info("perception abstained on (%s, %s): %s", d.subject, d.measure, d.reason)
            continue
        audit = {
            "view_audit_gate": "perception", "view_audit_agreement": round(d.agreement, 3),
            "view_audit_k": d.k, "view_audit_reason": d.reason,
            # corroboration verdict only — a deterministic bool. Lever-B veto-4 sets triangulated=True
            # when the holistic cross-check agrees, so the receipt records THAT it was corroborated
            # without persisting the model-stated total itself (invariant: model totals are gate inputs,
            # NEVER stored on a View). The raw cross_total stays in-memory on GateDecision for logs.
            "view_audit_triangulated": d.triangulated,  # None -> dropped by record()
            # deterministic-arithmetic corroboration used (SUM grounded from source spans, holistic
            # cross-check skipped). Only stamped when true, so existing Views are unchanged.
            **({"view_audit_sum_grounded": True} if d.sum_grounded else {}),
        }
        # a move-1 'stated' commit folds its single assertion event under SUM (sum of one = the
        # stated value); the deterministic sink only knows the scalar reducers.
        sink_reducer = "sum" if d.reducer == "stated" else d.reducer
        row = fold_events_to_counter(
            graph_adapter=graph_adapter, subject=d.subject, measure=d.measure,
            events=d.events, reducer=sink_reducer, namespace=namespace, source=source, embed=embed,
            audit=audit,
        )
        row.update({"agreement": d.agreement, "triangulated": d.triangulated})
        result.committed.append(row)

    # count-vs-spend partial co-extraction receipt (observability only — never changes what commits).
    # A 'bought N <noun> for $M' clause carries BOTH a COUNT and a SUM; the stochastic extractor
    # usually lands only the spend. When we detect the compound but did NOT commit both a count View
    # (==N) and a spend View (==M) for that noun, record a legible fail-closed receipt so the miss is
    # observable rather than silent (DECISION 1 = safety-only; co-extraction itself stays the
    # extractor's job and count-vs-spend stays a characterization case, not a gate).
    if record_abstentions:
        committed_decisions = [d for d in decisions if d.committed]

        def _committed(noun: str, target: float, reducers: tuple[str, ...]) -> bool:
            return any(
                d.reducer in reducers and d.value is not None
                and abs(float(d.value) - target) < 0.5 and noun in _measure_noun_sig(d.measure)
                for d in committed_decisions
            )

        partial = 0
        for ep in episodes:
            comp = count_spend_compound(ep.content)
            if comp is None:
                continue
            noun, cnt, spend = comp
            has_count = _committed(noun, float(cnt), ("count", "distinct_count", "stated"))
            has_spend = _committed(noun, float(spend), ("sum",))
            if not (has_count and has_spend):
                partial += 1
        if partial:
            _emit_run_tally(
                run_tally, graph_adapter, subject="perception",
                counter="count_vs_spend_partial", value=float(partial),
                namespace=namespace, source=source,
            )

    if record_abstentions and result.abstained:
        # bucket by firing veto, not one flat tally: the recall-recovery work needs to know WHICH
        # guard abstains most (and whether it is a recoverable class). Receipts stay out of semantic
        # recall (name_embedding=None), like perception_abstained always has.
        by_veto: dict[str, int] = defaultdict(int)
        for d in result.abstained:
            by_veto[d.veto] += 1
        for veto_label, n in {"perception_abstained": len(result.abstained), **{
                f"perception_abstained_{v}": c for v, c in by_veto.items()}}.items():
            _emit_run_tally(
                run_tally, graph_adapter, subject="perception", counter=veto_label,
                value=float(n), namespace=namespace, source=source,
            )
    _audit.audit(
        "counter", "fold", namespace=namespace,
        details={"episodes": len(episodes), "k": k,
                 "committed": len(getattr(result, "committed", []) or []),
                 "abstained": len(getattr(result, "abstained", []) or [])},
    )
    return result
