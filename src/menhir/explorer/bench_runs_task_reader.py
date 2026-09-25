"""Read-only task projection and privacy redaction for the bench-run explorer.

Extracted from ``menhir.explorer.bench_runs``, which re-exports every symbol.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from .bench_runs_audit import (
    _build_memory_inventory,
    _read_audit,
    annotate_assertion_fold_outcomes,
)
from .bench_runs_catalog import BenchRunCatalog
from .bench_runs_io import _is_safe_component
from .bench_runs_models import CONTRACT_VERSION, BenchTask
from .bench_runs_queries import (
    _LIVE_ASSERTIONS_QUERY,
    _LIVE_EVIDENCE_QUERY,
    _LIVE_EVENT_ASSERTIONS_QUERY,
    _LIVE_EVENT_VIEWS_QUERY,
    _LIVE_FACTS_QUERY,
    _LIVE_HISTORY_VIEWS_QUERY,
    _LIVE_STATE_VIEWS_QUERY,
)
from menhir.explorer.recall_packet_prototype import build_typed_recall_packet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Recursive privacy redaction
# ---------------------------------------------------------------------------

_TEXT_KEYS = frozenset({
    "question", "answer",
    "response_text", "recalled", "gold",
    "text", "stated_span", "source_quote",
    "subject", "attribute", "value", "display", "summary", "content",
    "name", "fact", "object", "object_display", "quote", "what", "relation",
    "reason", "value_json",
})


def _redact_nested(obj: Any) -> Any:
    from menhir.privacy import redact_text
    if isinstance(obj, dict):
        return {
            k: redact_text(v, reveal=False) if k in _TEXT_KEYS and isinstance(v, str)
            else _redact_nested(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_nested(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Task projection reader
# ---------------------------------------------------------------------------


class BenchRunTaskReader:
    """Read-only task projection for a single run's task."""

    def __init__(self, catalog: BenchRunCatalog, repo_provider: Callable[[], Any] | None = None):
        self._catalog = catalog
        self._repo_provider = repo_provider

    def get_task_detail(
        self,
        run_id: str,
        namespace: str,
        *,
        reveal: bool = False,
    ) -> dict[str, Any] | None:
        if not _is_safe_component(namespace):
            return None

        run = self._catalog.get_run(run_id)
        if run is None:
            return None

        task: BenchTask | None = None
        for t in run.tasks:
            if t.namespace == namespace:
                task = t
                break
        if task is None:
            return None

        result: dict[str, Any] = {
            "contract": CONTRACT_VERSION,
            "run_id": run_id,
            "namespace": task.namespace,
            "question_id": task.question_id,
            "question": task.question,
            "answer": task.answer,
            "question_type": task.question_type,
            "turns": task.turns,
            "typed_assertions": task.typed_assertions,
            "scalar_views": task.scalar_views,
            "turn_evidence": task.turn_evidence,
            "scalar_llm_calls": task.scalar_llm_calls,
            "scalar_consolidated": task.scalar_consolidated,
            "ready": task.ready,
            "failed_remaining": task.failed_remaining,
            "graph_available": task.graph_available,
            "source_warning": task.source_warning,
            "is_active": run.is_active,
            "graph_source_run_id": run.live_graph_source_run_id,
            "historical_score_with_live_graph": bool(
                run.live_graph_source_run_id
                and run.live_graph_source_run_id != run.run_id
            ),
            "run_label": run.label,
            "run_variant": run.variant,
            "run_arm": run.arm,
            "run_menhir_commit": run.menhir_commit,
            "run_bench_commit": run.bench_commit,
            "run_started_at": run.started_at,
            "attempts": run.attempts,
            "noncanonical": run.noncanonical,
            "resumed": run.resumed,
            "phases": run.phases,
            "harness_exit": run.harness_exit,
            "primary_outcome": task.primary_outcome,
            "scores": [
                {
                    "arm": s.arm,
                    "correct": s.correct,
                    "score": s.score,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "response_text": s.response_text,
                    "recalled": s.recalled,
                    "gold": s.gold,
                    "question": s.question,
                }
                for s in task.scores
            ],
        }

        # The active run, or a provenance-verified recall run that reused its
        # exact graph, may read the shared live graph. Historical scores remain
        # artifact data and are labeled separately in the response.
        graph_source_run_id = run.live_graph_source_run_id
        if graph_source_run_id and self._repo_provider is not None:
            run_dir = self._catalog.get_run_dir(graph_source_run_id)
            live_result = self._try_live_query(
                namespace,
                run_dir,
                graph_source_run_id=graph_source_run_id,
                historical_run_id=run_id if graph_source_run_id != run_id else None,
            )
            if live_result is not None:
                result.update(live_result)

        if not reveal:
            result = _redact_nested(result)

        return result

    def _try_live_query(
        self,
        namespace: str,
        run_dir: Path | None = None,
        *,
        graph_source_run_id: str | None = None,
        historical_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Attempt a live graph query. Returns a partial update dict or None."""
        configured_label = (
            f"REUSED ACTIVE GRAPH ({graph_source_run_id})"
            if historical_run_id
            else "ACTIVE CONFIGURED"
        )
        try:
            repo = self._repo_provider()
            if repo is None:
                return {
                    "graph_available": False,
                    "source_warning": (
                        f"{configured_label} — Neo4j repository is not available. "
                        "Check that the server started with a working Neo4j connection."
                    ),
                }
            live = self._query_live(repo, namespace, run_dir=run_dir)
            evidence_count = len(live.get("evidence", []))
            assertion_count = len(live.get("assertions", []))
            view_count = len(live.get("views", []))
            history_count = len(live.get("history_views", []))
            event_assertion_count = len(live.get("event_assertions", []))
            event_view_count = len(live.get("event_views", []))
            fact_count = len(live.get("facts", []))
            has_content = (
                evidence_count > 0 or assertion_count > 0 or view_count > 0
                or history_count > 0 or event_assertion_count > 0
                or event_view_count > 0 or fact_count > 0
            )
            if has_content:
                source_warning = "LIVE BENCHMARK GRAPH"
                if historical_run_id:
                    source_warning = (
                        "HISTORICAL SCORE + LIVE SOURCE GRAPH: "
                        f"{historical_run_id} scores over graph {graph_source_run_id}."
                    )
                return {
                    "graph_available": True,
                    "source_warning": source_warning,
                    "graph_source_run_id": graph_source_run_id,
                    "historical_score_with_live_graph": bool(historical_run_id),
                    "live_graph": live,
                }
            else:
                return {
                    "graph_available": False,
                    "source_warning": (
                        f"{configured_label} — live graph query returned no content "
                        "for this namespace. The graph may be empty or the namespace "
                        "may not exist in the current graph."
                    ),
                }
        except Exception as exc:
            logger.warning("Live graph query failed for namespace %s: %s", namespace, exc)
            return {
                "graph_available": False,
                "source_warning": (
                    f"{configured_label} — live graph query failed: "
                    f"{type(exc).__name__}. Check Neo4j connectivity."
                ),
            }

    def _query_live(self, repo: Any, namespace: str, run_dir: Path | None = None) -> dict[str, Any]:
        evidence = repo.execute(_LIVE_EVIDENCE_QUERY, params={"namespace": namespace})
        assertions = repo.execute(_LIVE_ASSERTIONS_QUERY, params={"namespace": namespace})
        views = repo.execute(_LIVE_STATE_VIEWS_QUERY, params={"namespace": namespace})
        history_views = repo.execute(_LIVE_HISTORY_VIEWS_QUERY, params={"namespace": namespace})
        event_assertions = repo.execute(
            _LIVE_EVENT_ASSERTIONS_QUERY, params={"namespace": namespace}
        )
        event_views = repo.execute(_LIVE_EVENT_VIEWS_QUERY, params={"namespace": namespace})
        facts = repo.execute(_LIVE_FACTS_QUERY, params={"namespace": namespace})

        views_list = [dict(v) for v in views] if views else []
        history_list = [dict(v) for v in history_views] if history_views else []
        event_assertion_list = [dict(a) for a in event_assertions] if event_assertions else []
        event_view_list = [dict(v) for v in event_views] if event_views else []
        assertions_list = [dict(a) for a in assertions] if assertions else []
        facts_list = [dict(f) for f in facts] if facts else []
        evidence_list = [dict(e) for e in evidence] if evidence else []

        for hv in history_list:
            raw_payload = hv.get("payload")
            if isinstance(raw_payload, str):
                try:
                    hv["entries"] = json.loads(raw_payload)
                except (json.JSONDecodeError, TypeError):
                    hv["entries"] = []
            elif isinstance(raw_payload, list):
                hv["entries"] = raw_payload
            else:
                hv["entries"] = []
            if "op_counts" in hv and isinstance(hv["op_counts"], str):
                try:
                    hv["op_counts"] = json.loads(hv["op_counts"])
                except (json.JSONDecodeError, TypeError):
                    hv["op_counts"] = {}
            hv.pop("payload", None)

        for event_view in event_view_list:
            raw_payload = event_view.get("payload")
            if isinstance(raw_payload, str):
                try:
                    event_view["entries"] = json.loads(raw_payload)
                except (json.JSONDecodeError, TypeError):
                    event_view["entries"] = []
            elif isinstance(raw_payload, list):
                event_view["entries"] = raw_payload
            else:
                event_view["entries"] = []
            event_view.pop("payload", None)

        # Build evidence-by-id map for source quote resolution
        evidence_by_id = {str(e.get("id", "")): e for e in evidence_list}

        # Audit + fold outcomes
        telemetry_db = self._resolve_telemetry_db(run_dir) if run_dir else None
        audit_pass_id, audit, audit_warning = _read_audit(
            telemetry_db, namespace, assertions_list
        ) if telemetry_db else (None, [], None)

        assertions_annotated = annotate_assertion_fold_outcomes(
            assertions_list, views_list, history_list, audit
        )

        # Annotate assertions with source quote from evidence
        for assertion in assertions_annotated:
            evidence_id = str(assertion.get("evidence_id", ""))
            source = evidence_by_id.get(evidence_id)
            if source is not None:
                assertion["source_quote"] = source.get("text", "")
            else:
                # Fall back: search evidence whose founds include this assertion
                for e in evidence_list:
                    if assertion.get("id") in (e.get("founds") or []):
                        assertion["source_quote"] = e.get("text", "")
                        break

        inventory = _build_memory_inventory(
            assertions_annotated, views_list, history_list, facts_list, event_view_list
        )
        derivation_by_view_id = {
            item["id"]: item["derivation"]
            for item in inventory
            if item.get("memory_type") == "view"
        }
        for view in views_list + history_list + event_view_list:
            view["derivation"] = derivation_by_view_id.get(
                str(view.get("id") or ""), "unknown"
            )

        recall_packet = build_typed_recall_packet({
            "assertions": assertions_annotated,
            "views": views_list,
            "history_views": history_list,
            "event_assertions": event_assertion_list,
            "event_views": event_view_list,
            "memory_inventory": inventory,
        })

        scalar_roles = [
            {
                "id": "scalar-state",
                "name": "Current Scalar",
                "system_name": "Scalar State",
                "role": "Authoritative current value",
                "answers": "What is true now?",
                "count": len(views_list),
                "status": "available" if views_list else "empty",
            },
            {
                "id": "scalar-history",
                "name": "Change Scalar",
                "system_name": "Scalar History",
                "role": "Advisory numeric change record",
                "answers": "How did the value change over time?",
                "count": len(history_list),
                "status": "available" if history_list else "empty",
            },
            {
                "id": "event-history",
                "name": "Event Scalar",
                "system_name": "Event History",
                "role": "Ordered occurrence history",
                "answers": "What happened latest or immediately before it?",
                "count": len(event_view_list),
                "assertion_count": len(event_assertion_list),
                "status": "available" if event_view_list else "empty",
            },
        ]

        return {
            "evidence": evidence_list,
            "assertions": assertions_annotated,
            "views": views_list,
            "history_views": history_list,
            "event_assertions": event_assertion_list,
            "event_views": event_view_list,
            "scalar_roles": scalar_roles,
            "facts": facts_list,
            "memory_inventory": inventory,
            "recall_packet": recall_packet,
            "audit_pass_id": audit_pass_id,
            "audit_warning": audit_warning,
            "assertion_count": len(assertions_annotated),
            "evidence_count": len(evidence_list),
        }

    def _resolve_telemetry_db(self, run_dir: Path) -> Path | None:
        """Resolve telemetry DB path from the run's result directory.

        The telemetry DB must be inside the resolved run directory, must not be
        a symlink, and must be under the configured results root.
        """
        if self._catalog.results_root is None:
            return None
        candidate = run_dir / "mcp_telemetry.db"
        if not candidate.exists() or candidate.is_symlink():
            return None
        try:
            candidate.resolve().relative_to(self._catalog.results_root.resolve())
        except ValueError:
            return None
        return candidate
