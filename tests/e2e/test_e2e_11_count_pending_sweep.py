"""E2E-11 - the background pass sweeps undecided rows in a namespace with no new turns.

First pass (``POST /api/phase3/run``, namespace named) runs with a call budget that covers
the scalar and counting extraction calls but not one decide call, so the related happenings
are stored undecided. The namespace then has no new turns, so it is not counting-dirty. The
lane waits for the backend's own scheduler run, which names no namespace and has the full
budget. Without the pending listing that run never visits this namespace and the rows stay
undecided; with it, they are decided: the retelling becomes a repeat and the family View
settles at 2. (Undecided rows, not uncertain ones: a quiet namespace re-decides an uncertain
row only after REDECIDE_COOLDOWN_S, which the offline and live tests cover.)
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.phase3 import (
    COUNTING_PROMPT,
    DECIDE_PROMPT,
    SCALAR_PROMPT,
    decide_reply,
    envelope_reply,
    lane_backend,
    post_json,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e.test_e2e_10_counting import (
    KIT_TURNS,
    KITS,
    MATCH_ON,
    RETELL,
    _capture,
    _family_view,
    _finish,
    _occurrences,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200)]

JOB = "consolidate_personal_memory"
# Long enough that capture and the explicit first pass finish before the scheduler's second
# run is due (its first run is at startup, before any turn exists). A scheduler run inside
# that window would decide the rows itself and fail the first criterion, not pass silently.
INTERVAL_S = 90
WAIT_S = 300
# Scalar (k=3) plus counting extraction (k=3) for the one page; a decide needs DECIDE_K more.
FIRST_PASS_BUDGET = 6

CRITERIA = [
    "short_budget_leaves_related_rows_undecided",
    "background_run_names_no_namespace_and_completes",
    "background_run_decides_the_quiet_namespace",
    "family_view_settles_after_the_background_run",
    "no_unscripted_model_prompt",
]


def _at(value: object) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _scheduler_job(e2e_config: E2EConfig) -> dict:
    with urllib.request.urlopen(f"{e2e_config.backend_url}/api/stats", timeout=30) as response:
        stats = json.loads(response.read().decode("utf-8"))
    scheduler = stats.get("scheduler") or {}
    job = (scheduler.get("jobs") or {}).get(JOB) or {}
    return {"running": scheduler.get("running"), "lease": scheduler.get("lease"), **job}


async def test_e2e_11_count_pending_sweep(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e11p-{uuid4().hex[:12]}"
    script = Script([
        Rule("scalar", SCALAR_PROMPT, envelope_reply([])),
        Rule("counting", COUNTING_PROMPT, envelope_reply(KITS)),
        Rule("decide", DECIDE_PROMPT, decide_reply(RETELL, family="model kit")),
    ])
    with scripted_model(script) as model:
        # The harness runs backends in benchmark mode, which never starts the maintenance
        # scheduler. The fix under test lives on the scheduler's path, so this lane turns
        # benchmark mode off for its own backend; the graph is still this lane's alone.
        env = {**feature_env, **model.env, **MATCH_ON,
               "MENHIR_BENCHMARK_MODE": "0",
               "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S": str(INTERVAL_S)}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                await _capture(e2e_config, client, namespace, KIT_TURNS)
            first = post_json(e2e_config, "/api/phase3/run",
                              {"namespace": namespace, "k": 3, "counter_state": False,
                               "call_budget": FIRST_PASS_BUDGET})
            lane_evidence.attach("phase3-short-budget.json",
                                 json.dumps(first, indent=2, default=str))
            before = _occurrences(e2e_config, namespace, lane_evidence)
            lane_evidence.attach("occurrences-before.json", json.dumps(before, indent=2, default=str))
            undecided = [r for r in before if r["status"] is None]
            lane_evidence.record(
                "short_budget_leaves_related_rows_undecided",
                passed=len(before) == 3 and len(undecided) >= 1
                and not any(r["status"] in ("repeat", "uncertain") for r in before)
                and int(first.get("counting_decided_deferred") or 0) >= 1
                and first.get("counting_dirty_after") is False
                and not script.calls("decide")
                and len(script.calls("counting")) == 3,
                detail={"statuses": sorted(str(r["status"]) for r in before),
                        "decide_calls": len(script.calls("decide")),
                        **{k: first.get(k) for k in ("llm_calls", "counting_decided_deferred",
                                                     "counting_dirty_after")}},
            )

            switched_at = datetime.now(UTC)
            decide_before = len(script.calls("decide"))
            job: dict = {}
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline:
                job = _scheduler_job(e2e_config)
                started, completed = _at(job.get("last_started_at")), _at(job.get("last_completed_at"))
                if started and completed and started > switched_at and completed >= started:
                    break
                await asyncio.sleep(5)
            lane_evidence.attach("scheduler-job.json", json.dumps(job, indent=2, default=str))
            started, completed = _at(job.get("last_started_at")), _at(job.get("last_completed_at"))
            last = job.get("last_result") or {}
            lane_evidence.record(
                "background_run_names_no_namespace_and_completes",
                passed=bool(started and completed) and started > switched_at
                and completed >= started and job.get("last_success") is True
                # Visited without a page read: listed by the pending sweep, not as dirty work.
                and int(last.get("counting_namespaces_dirty") or 0) >= 1
                and last.get("counting_namespaces_processed") == 0,
                detail={k: job.get(k) for k in ("running", "last_started_at",
                                                "last_completed_at", "last_success", "runs")},
            )

            after = _occurrences(e2e_config, namespace, lane_evidence)
            statuses = sorted(str(r["status"]) for r in after)
            lane_evidence.record(
                "background_run_decides_the_quiet_namespace",
                passed=statuses == ["counted", "counted", "repeat"]
                and len(script.calls("decide")) > decide_before
                and len(script.calls("counting")) == 3,
                detail={"statuses": statuses,
                        "decide_calls_in_background": len(script.calls("decide")) - decide_before,
                        "counting_calls": len(script.calls("counting")),
                        "last_result": job.get("last_result")},
            )
            family = _family_view(e2e_config, namespace)
            lane_evidence.record(
                "family_view_settles_after_the_background_run",
                passed=len(family) == 1 and float(family[0]["value"]) == 2.0
                and not family[0]["count_uncertain"],
                detail=family,
            )
    _finish(script, lane_evidence, CRITERIA)
