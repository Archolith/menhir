"""Phase 3 debug report — makes the personal-memory perception pass observable.

Two things this report exists to expose, both found the hard way (see
`.agent/plans/phase3-canonicalization-guards.md`):

  1. **Measure-key scatter -> canonical collapse.** The extractor keys one running quantity under
     many labels across k samples; the report shows the raw labels, the canonical keys, and which
     raw labels collapsed, so a stalled fold reads as scatter rather than a mystery abstention.
  2. **Missing capture metadata.** Real Menhir episodes carry no role/speaker/source_kind, so Phase 3
     cannot tell a user turn from an assistant turn. The report states this outright instead of
     silently finding zero user-authored spans.

The extraction/canonicalization/gate stats are computed by re-running the SAME building blocks the
live pass uses (`extract_once` -> `canonicalize_samples` -> `gate`), so the report cannot drift from
the real pipeline. The gate is run with the cheap deterministic guards only (self-consistency,
count-floor, stated-span); the LLM cross-check/coref/verify levers are omitted to bound report cost.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from menhir.services.perception import (
    VETO_UNSUPPORTED_STATED,
    Embed,
    Episode,
    LlmComplete,
    canonicalize_samples,
    extract_once,
    gate,
)

#: episode properties that would let Phase 3 distinguish a user turn from an assistant/tool turn.
_ROLE_FIELDS = ("role", "speaker", "source_kind", "actor", "declarant")

_DERIVED_REDUCERS = ("sum", "count", "distinct_count")


def probe_capture_metadata(neo4j: Any, *, source_limit: int = 20) -> dict[str, Any]:
    """Count the legacy role/speaker/source_kind/actor/declarant fields on `Episodic` (all expected 0)
    and the `source` distribution, PLUS the `:Turn` capture stats (ADR 0001), so the report can state
    whether user-authored evidence is captured. Pure read; safe against the live graph."""
    from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

    total_rows = neo4j.execute("MATCH (e:Episodic) RETURN count(e) AS c")
    total = int(total_rows[0]["c"]) if total_rows else 0
    fields: dict[str, int] = {}
    for f in _ROLE_FIELDS:
        rows = neo4j.execute(f"MATCH (e:Episodic) WHERE e.{f} IS NOT NULL RETURN count(e) AS c")
        fields[f] = int(rows[0]["c"]) if rows else 0
    src_rows = neo4j.execute(
        "MATCH (e:Episodic) RETURN e.source AS source, count(*) AS c ORDER BY c DESC LIMIT $limit",
        params={"limit": int(source_limit)},
    )
    source_distribution = {str(r["source"]): int(r["c"]) for r in src_rows}
    return {
        "total_episodes": total,
        "fields": fields,
        "source_distribution": source_distribution,
        "turn_evidence": TurnEvidenceRepository(neo4j).evidence_stats(),
    }


def build_phase3_report(
    episodes: list[Episode],
    llm_complete: LlmComplete,
    *,
    k: int = 3,
    threshold: float = 1.0,
    embed: Embed | None = None,
    capture_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run extract(k) -> canonicalize -> gate over `episodes` and return a structured report of where
    candidates are lost. `capture_metadata` (from `probe_capture_metadata`) is folded in when given so
    the report can flag the absence of user-turn metadata."""
    samples = [extract_once(episodes, llm_complete) for _ in range(max(1, k))]
    raw_measures = Counter(g.measure for sample in samples for g in sample)

    canon_samples, raw_to_canon = canonicalize_samples(samples)
    canonical_measures = Counter(g.measure for sample in canon_samples for g in sample)
    collapsed = sorted({(raw, canon) for raw, canon in raw_to_canon.items() if raw != canon})

    # stated-span guard ON in the report (episodes supplied) so quarantines are visible.
    decisions = gate(canon_samples, threshold=threshold, embed=embed, episodes=episodes)

    accepted, rejected, quarantined, fold_derived = [], [], [], []
    by_veto: Counter = Counter()
    for d in decisions:
        if d.committed:
            accepted.append({"subject": d.subject, "measure": d.measure,
                             "value": d.value, "reducer": d.reducer})
            if d.reducer in _DERIVED_REDUCERS:
                fold_derived.append({"subject": d.subject, "measure": d.measure,
                                     "value": d.value, "reducer": d.reducer})
            continue
        by_veto[d.veto] += 1
        entry = {"subject": d.subject, "measure": d.measure, "veto": d.veto, "reason": d.reason}
        rejected.append(entry)
        if d.veto == VETO_UNSUPPORTED_STATED:
            quarantined.append(entry)

    fields = (capture_metadata or {}).get("fields", {})
    turn_evidence = (capture_metadata or {}).get("turn_evidence", {})
    user_evidence = int(turn_evidence.get("user_evidence", 0))
    # user-authored evidence is present if either legacy role metadata exists OR :TurnEvidence capture
    # has user records; the latter is the ADR 0001 path.
    user_turn_metadata_present = any(int(v) > 0 for v in fields.values()) or user_evidence > 0

    return {
        "input": {
            "total_episodes": (capture_metadata or {}).get("total_episodes", len(episodes)),
            "episodes_scanned": len(episodes),
            "capture_metadata_available": capture_metadata is not None,
            "role_field_counts": dict(fields),
            "user_turn_metadata_present": user_turn_metadata_present,
            "turn_evidence": turn_evidence,
            "source_distribution": (capture_metadata or {}).get("source_distribution", {}),
        },
        "extraction": {
            "k": k,
            "raw_candidates": sum(raw_measures.values()),
            "raw_measure_keys": sorted(raw_measures),
            "raw_measure_counts": dict(raw_measures),
        },
        "canonicalization": {
            "canonical_measure_keys": sorted(canonical_measures),
            "raw_to_canonical": raw_to_canon,
            "collapsed": collapsed,
        },
        "consistency": {
            "accepted": accepted,
            "rejected": rejected,
            "by_veto": dict(by_veto),
        },
        "guards": {"quarantined_stated": quarantined},
        "output": {
            "fold_derived_preserved": fold_derived,
            "committed_total": len(accepted),
        },
    }


def format_phase3_report(report: dict[str, Any]) -> str:
    """Render a `build_phase3_report` dict as plain text for a CLI/ops surface."""
    inp = report["input"]
    ext = report["extraction"]
    can = report["canonicalization"]
    con = report["consistency"]
    out = report["output"]
    lines: list[str] = ["Phase 3 Debug Report", ""]

    lines.append("Input:")
    lines.append(f"  total episodes: {inp['total_episodes']} (scanned {inp['episodes_scanned']})")
    counts = inp["role_field_counts"]
    counts_str = ", ".join(f"{f}={counts.get(f, 0)}" for f in _ROLE_FIELDS) or "not probed"
    lines.append(f"  user_turn_metadata_present = {str(inp['user_turn_metadata_present']).lower()}")
    lines.append(f"  role/speaker/source_kind/actor/declarant counts = {counts_str}")
    if inp["source_distribution"]:
        top = ", ".join(f"{s}:{c}" for s, c in list(inp["source_distribution"].items())[:8])
        lines.append(f"  source distribution: {top}")
    lines.append("")

    tc = inp.get("turn_evidence", {})
    user_ev = int(tc.get("user_evidence", 0))
    lines.append("TurnEvidence Capture:")
    lines.append(f"  turn_evidence_table_exists: {str(bool(tc.get('turn_evidence_table_exists'))).lower()}")
    lines.append(f"  total_turn_evidence: {tc.get('total_turn_evidence', 0)}")
    lines.append(f"  claude_code_hook_evidence: {tc.get('claude_code_hook_evidence', 0)}")
    if tc.get("triage_version_counts"):
        lines.append(f"  triage_version counts: {tc['triage_version_counts']}")
    if tc.get("triage_reason_counts"):
        lines.append(f"  triage_reason counts: {tc['triage_reason_counts']}")
    if tc.get("latest_recorded_at"):
        lines.append(f"  latest_recorded_at: {tc['latest_recorded_at']}")
    if user_ev > 0:
        lines.append(f"  phase3_records_selected: {user_ev}")
        lines.append("  Phase 3 is processing selectively captured user-authored evidence.")
    else:
        lines.append("  No TurnEvidence records found. Phase 3 has no user-authored evidence from "
                     "Claude hooks yet.")
        lines.append("  A TurnEvidence producer is required for that (see ADR 0001).")
    lines.append("")

    lines.append("Extraction:")
    lines.append(f"  k samples: {ext['k']}")
    lines.append(f"  raw candidates: {ext['raw_candidates']}")
    lines.append(f"  raw measure keys ({len(ext['raw_measure_keys'])}): {', '.join(ext['raw_measure_keys']) or '-'}")
    lines.append("")

    lines.append("Canonicalization:")
    lines.append(f"  canonical measure keys ({len(can['canonical_measure_keys'])}): "
                 f"{', '.join(can['canonical_measure_keys']) or '-'}")
    if can["collapsed"]:
        lines.append("  raw -> canonical collapse:")
        for raw, canon in can["collapsed"]:
            lines.append(f"    {raw} -> {canon}")
    else:
        lines.append("  raw -> canonical collapse: (none)")
    lines.append("")

    lines.append("Consistency:")
    lines.append(f"  accepted: {len(con['accepted'])}")
    for a in con["accepted"]:
        lines.append(f"    + {a['measure']} = {a['value']} ({a['reducer']})")
    lines.append(f"  rejected: {len(con['rejected'])}  by veto: {con['by_veto'] or '{}'}")
    for r in con["rejected"]:
        lines.append(f"    - {r['measure']} [{r['veto']}] {r['reason']}")
    lines.append("")

    lines.append("Guards:")
    q = report["guards"]["quarantined_stated"]
    lines.append(f"  quarantined stated measures: {len(q)}")
    for r in q:
        lines.append(f"    ! {r['measure']}: {r['reason']}")
    lines.append("")

    lines.append("Output:")
    lines.append(f"  fold-derived views preserved: {len(out['fold_derived_preserved'])}")
    for f in out["fold_derived_preserved"]:
        lines.append(f"    = {f['measure']} = {f['value']} ({f['reducer']})")
    lines.append(f"  committed total: {out['committed_total']}")
    return "\n".join(lines)
