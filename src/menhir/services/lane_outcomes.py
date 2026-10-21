"""Per-lane outcome report for one personal-memory consolidation pass (#170 item 4).

The pass result is one flat dict shared by four lanes (counter, scalar, event, counting). A busy
lane's counts can make an idle one look fine, so this report lists every lane on its own: whether
it was enabled, what it read, what each stage produced, and two flags:

- ``no_output``: enabled, read evidence, and produced nothing (its primary output is 0).
- ``stalled``: enabled, had dirty namespaces, and processed none (activation refused, budget
  exhausted, or every claim busy).

Neither flag is an error by itself; a namespace with no countable facts legitimately produces
nothing. They make the inactivity visible instead of hidden in a total.
"""

from __future__ import annotations

from typing import Any

#: lane -> (dirty key, processed key, failed key or None, input-rows key, primary output key,
#:          {stage name: result key})
_LANES: dict[str, tuple[str, str, str | None, str, str, dict[str, str]]] = {
    "counter": (
        "namespaces_dirty", "namespaces_processed", None, "counter_evidence_rows", "views_written",
        {"views_written": "views_written", "abstained": "abstained",
         "corrections_applied": "corrections_applied"},
    ),
    "scalar": (
        "scalar_namespaces_dirty", "scalar_namespaces_processed", None, "scalar_evidence_rows",
        "scalar_assertions_persisted",
        {"decisions": "scalar_decisions", "committed": "scalar_assertions_committed",
         "persisted": "scalar_assertions_persisted", "bound": "scalar_states_written",
         "binding_mismatched": "scalar_binding_mismatched", "advisory": "scalar_advisory",
         "views_rebuilt": "scalar_views_rebuilt"},
    ),
    "event": (
        "event_namespaces_dirty", "event_namespaces_processed", "event_namespaces_failed",
        "event_evidence_rows", "event_assertions_recorded",
        {"proposals": "event_proposals", "admitted": "event_proposals_admitted",
         "recorded": "event_assertions_recorded", "views_rebuilt": "event_views_rebuilt"},
    ),
    "counting": (
        "counting_namespaces_dirty", "counting_namespaces_processed", "counting_namespaces_failed",
        "counting_evidence_rows", "counting_occurrences_stored",
        {"occurrences_stored": "counting_occurrences_stored", "advisory": "counting_advisory"},
    ),
}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def lane_outcomes(result: dict[str, Any], *, enabled: dict[str, bool],
                  counter_evidence_rows: int = 0) -> dict[str, dict[str, Any]]:
    """Build the per-lane report from a pass result. ``enabled`` maps lane name -> flag; a lane
    missing from it is reported disabled. Disabled lanes are listed too, so absence is explicit."""
    source = {**result, "counter_evidence_rows": counter_evidence_rows}
    out: dict[str, dict[str, Any]] = {}
    for lane, (dirty, processed, failed, rows, primary, stages) in _LANES.items():
        on = bool(enabled.get(lane, False))
        if not on:
            out[lane] = {"enabled": False}
            continue
        report: dict[str, Any] = {
            "enabled": True,
            "namespaces_dirty": _int(source.get(dirty)),
            "namespaces_processed": _int(source.get(processed)),
            "input_rows": _int(source.get(rows)),
            "stages": {name: _int(source.get(key)) for name, key in stages.items()},
            "output": _int(source.get(primary)),
        }
        if failed is not None:
            report["namespaces_failed"] = _int(source.get(failed))
        report["no_output"] = report["input_rows"] > 0 and report["output"] == 0
        report["stalled"] = report["namespaces_dirty"] > 0 and report["namespaces_processed"] == 0
        out[lane] = report
    return out


__all__ = ["lane_outcomes"]
