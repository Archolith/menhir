"""E2E-11 - the background pass sweeps undecided rows in a namespace with no new turns.

First pass (``POST /api/phase3/run``, namespace named): every decide sample is unreadable,
so the related happenings are stored ``uncertain``. The namespace then has no new turns,
so it is not counting-dirty. The decide script is switched to good answers and the lane
waits for the backend's own scheduler run, which names no namespace. Without the pending
listing that run never visits this namespace and the rows stay uncertain; with it, they
are re-decided: the retelling becomes a repeat and the family View settles at 2.
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
    run_phase3,
)
from tests.e2e._harness.scripted_provider import Call, Rule, Script, scripted_model
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
# Long enough that the explicit first pass finishes before the scheduler's second run is
# due (its first run is at startup, before any turn exists), short enough to keep the lane
# quick. A scheduler run that does start during the first pass only does the same work.
INTERVAL_S = 60
WAIT_S = 240

CRITERIA = [
    "unreadable_decide_leaves_related_rows_uncertain",
    "background_run_names_no_namespace_and_completes",
    "background_run_re_decides_the_quiet_namespace",
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
    good = decide_reply(RETELL, family="model kit")
    state = {"readable": False}

    def decide(call: Call) -> str:
        return good(call) if state["readable"] else "I cannot tell."

    script = Script([
        Rule("scalar", SCALAR_PROMPT, envelope_reply([])),
        Rule("counting", COUNTING_PROMPT, envelope_reply(KITS)),
        Rule("decide", DECIDE_PROMPT, decide),
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
            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-unreadable-decide")
            before = _occurrences(e2e_config, namespace, lane_evidence)
            lane_evidence.attach("occurrences-before.json", json.dumps(before, indent=2, default=str))
            uncertain = [r for r in before if r["status"] == "uncertain"]
            lane_evidence.record(
                "unreadable_decide_leaves_related_rows_uncertain",
                passed=len(before) == 3 and len(uncertain) >= 1
                and not any(r["status"] == "repeat" for r in before)
                and len(script.calls("decide")) >= 3,
                detail={"statuses": sorted(str(r["status"]) for r in before),
                        "decide_calls": len(script.calls("decide"))},
            )

            state["readable"] = True
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
            lane_evidence.record(
                "background_run_names_no_namespace_and_completes",
                passed=bool(started and completed) and started > switched_at
                and completed >= started and job.get("last_success") is True,
                detail={k: job.get(k) for k in ("running", "last_started_at",
                                                "last_completed_at", "last_success", "runs")},
            )

            after = _occurrences(e2e_config, namespace, lane_evidence)
            statuses = sorted(str(r["status"]) for r in after)
            lane_evidence.record(
                "background_run_re_decides_the_quiet_namespace",
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
