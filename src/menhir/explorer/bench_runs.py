"""Bounded filesystem catalog and read-only task projection for LongMemEval benchmark runs.

Contract: bench-inspection/v1

This module reads LME run artifacts (manifest, checkpoints, provenance) from a configured
allowlisted results root via MENHIR_BENCH_RESULTS_ROOT (required; fail closed if absent).
It never writes to Neo4j or imports archolith_bench runtime code.
The configured active run, plus provenance-verified recall runs that reuse its exact graph,
may combine artifact data with live graph projections.

Implementation is split across sibling modules (bench_runs_models, bench_runs_queries,
bench_runs_io, bench_runs_artifacts, bench_runs_audit, bench_runs_catalog, and
bench_runs_task_reader); every symbol remains importable from this module.
"""

from __future__ import annotations

import logging
import os

from .bench_runs_artifacts import (
    _discover_run_dirs,
    _find_checkpoint_files,
    _get_identity,
    _get_provenance_value,
    _IDENTITY_FIELDS,
    _parse_checkpoint_record,
    _read_checkpoint_scores,
    _read_provenance,
)
from .bench_runs_audit import (
    _audit_slot,
    _assertion_slot,
    _build_memory_inventory,
    _normalized_slot,
    _read_audit,
    annotate_assertion_fold_outcomes,
)
from .bench_runs_catalog import BenchRunCatalog
from .bench_runs_io import (
    _SAFE_COMPONENT,
    _is_bare_real_directory,
    _is_safe_component,
    _read_json,
    _safe_int,
    _safe_resolve,
    _safe_resolve_file,
)
from .bench_runs_models import (
    CONTRACT_VERSION,
    BenchRun,
    BenchScore,
    BenchTask,
    _primary_outcome,
)
from .bench_runs_queries import (
    _LIVE_ASSERTIONS_QUERY,
    _LIVE_EVIDENCE_QUERY,
    _LIVE_EVENT_ASSERTIONS_QUERY,
    _LIVE_EVENT_VIEWS_QUERY,
    _LIVE_FACTS_QUERY,
    _LIVE_HISTORY_VIEWS_QUERY,
    _LIVE_STATE_VIEWS_QUERY,
)
from .bench_runs_task_reader import (
    BenchRunTaskReader,
    _TEXT_KEYS,
    _redact_nested,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default provider factory — fail closed when env absent
# ---------------------------------------------------------------------------

_CACHED_CATALOG: BenchRunCatalog | None = None


def _filtered_task_ids(tasks: list[BenchTask], outcome: str | None) -> list[str]:
    """Return namespace list for tasks matching *outcome* category.

    ``outcome`` is one of ``"all"``, ``"passed"``, ``"failed"``, ``"unscored"``,
    or ``None`` (treated as ``"all"``).
    """
    if not outcome or outcome == "all":
        return [t.namespace for t in tasks]
    target = outcome.upper()
    target = "NOT SCORED" if target == "UNSCORED" else target
    return [t.namespace for t in tasks if (t.primary_outcome or "NOT SCORED") == target]


def _nav_neighbors(
    ordered: list[str],
    current: str,
) -> tuple[str | None, str | None]:
    """Return (prev_ns, next_ns) within *ordered* list around *current*."""
    try:
        idx = ordered.index(current)
    except ValueError:
        return None, None
    prev_ns = ordered[idx - 1] if idx > 0 else None
    next_ns = ordered[idx + 1] if idx < len(ordered) - 1 else None
    return prev_ns, next_ns


def _outcome_counts(tasks: list[BenchTask]) -> dict[str, int]:
    passed = sum(1 for t in tasks if t.primary_outcome == "PASSED")
    failed = sum(1 for t in tasks if t.primary_outcome == "FAILED")
    unscored = sum(1 for t in tasks if t.primary_outcome is None or t.primary_outcome == "NOT SCORED")
    return {"total": len(tasks), "passed": passed, "failed": failed, "unscored": unscored}


VALID_OUTCOMES = frozenset({"all", "failed", "passed", "unscored"})


_OUTCOME_PARAM_MAP: dict[str, str] = {
    "PASSED": "passed",
    "FAILED": "failed",
    "NOT SCORED": "unscored",
}


def _outcome_from_primary(primary: str | None) -> str:
    """Map a task's ``primary_outcome`` value to a filter param.

    ``"PASSED"`` → ``"passed"``, ``"FAILED"`` → ``"failed"``,
    ``"NOT SCORED"`` or ``None`` → ``"unscored"``.
    """
    if primary:
        mapped = _OUTCOME_PARAM_MAP.get(primary)
        if mapped:
            return mapped
    return "unscored"


def _normalize_outcome(raw: str | None) -> str:
    """Normalize *raw* to one of ``all``, ``failed``, ``passed``, ``unscored``.

    Invalid or missing values default to ``"all"``.
    """
    if raw and raw.strip() in VALID_OUTCOMES:
        return raw.strip()
    return "all"


def default_bench_run_provider() -> BenchRunCatalog:
    """Create a BenchRunCatalog from environment variables.

    Reads:
    - ``MENHIR_BENCH_RESULTS_ROOT`` — required. Path to results directory.
    - ``MENHIR_BENCH_ACTIVE_RUN_ID`` — optional active run ID.

    Returns a catalog that reports empty results when the env var is not set,
    so callers can display a clear configuration message.
    """
    global _CACHED_CATALOG
    if _CACHED_CATALOG is not None:
        return _CACHED_CATALOG

    results_root = os.environ.get("MENHIR_BENCH_RESULTS_ROOT")
    if not results_root or not results_root.strip():
        logger.warning(
            "MENHIR_BENCH_RESULTS_ROOT is not set. Benchmark catalog will be empty. "
            "Set this env var to the archolith-bench results directory."
        )
        _CACHED_CATALOG = BenchRunCatalog(results_root=None, active_run_id=None)
        return _CACHED_CATALOG

    active_run_id = os.environ.get("MENHIR_BENCH_ACTIVE_RUN_ID")
    if active_run_id and not active_run_id.strip():
        active_run_id = None

    _CACHED_CATALOG = BenchRunCatalog(results_root=results_root, active_run_id=active_run_id)
    return _CACHED_CATALOG
