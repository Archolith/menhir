"""The saga reconcile dispatcher's run report: one pass, summarised as a single reportable unit.

Extracted from :mod:`menhir.services.saga_reconcile_dispatcher` for file size and re-exported
from there; every public name in this module remains importable from the dispatcher module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReconcileRun:
    """One reconciliation pass, summarised as a single reportable unit.

    The run_id is what makes a large quarantine event legible as ONE startup incident instead of
    hundreds of unrelated row updates.
    """

    run_id: str
    scanned: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    counts_by_kind: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)
    oldest_prepared_at: str | None = None
    oldest_prepared_age_seconds: float | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    write_ready: bool = True
    blocking_reasons: list[str] = field(default_factory=list)
    #: False for a live pass. Reported rather than hardcoded, because a summary that always claims
    #: dry_run=True makes a live recovery report indistinguishable from a forecast.
    dry_run: bool = True
    #: True when the pass stopped early on a systemic condition. An aborted run is NEVER
    #: write-ready: the rule is "stop recovery and keep the writer gate closed", never "stop
    #: recovery and start normally".
    aborted: bool = False
    abort_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "scanned": self.scanned,
            "counts": self.counts,
            "counts_by_kind": self.counts_by_kind,
            "examples": self.examples,
            "oldest_prepared_at": self.oldest_prepared_at,
            "oldest_prepared_age_seconds": self.oldest_prepared_age_seconds,
            "write_ready": self.write_ready,
            "blocking_reasons": self.blocking_reasons,
            "outcomes": self.rows,
        }
