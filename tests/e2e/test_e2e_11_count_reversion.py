"""E2E-11 - a counting re-version keeps the old count until the new reading is stored.

First backend: counting on, extract v2 off. Two gym visits give a counted View of 2.
Second backend, same graph: extract v2 on, which changes the counting version, so every
page is read again. Every new counting sample is unreadable. The old rows must stay
current and the View must stay 2; before the re-version safety change the old rows were
retired before the model ran, so a failed reading left the count at 0.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.phase3 import (
    COUNTING_PROMPT,
    SCALAR_PROMPT,
    envelope_reply,
    lane_backend,
    run_phase3,
    scalar_views,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e.test_e2e_10_counting import (
    COUNTING_ON,
    GYM,
    GYM_TURNS,
    _capture,
    _finish,
    _occurrences,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200)]

CRITERIA = [
    "first_version_counts_the_happenings",
    "new_version_reads_every_page_again",
    "failed_new_reading_keeps_the_old_rows_current",
    "counted_view_keeps_its_total",
    "no_unscripted_model_prompt",
]


async def test_e2e_11_count_reversion_keeps_count(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e11r-{uuid4().hex[:12]}"
    readable = envelope_reply(GYM)
    state = {"readable": True}
    script = Script([
        Rule("scalar", SCALAR_PROMPT, envelope_reply([])),
        Rule("counting", COUNTING_PROMPT,
             lambda call: readable(call) if state["readable"] else "Sorry, I can't help."),
    ])

    def gym() -> list[dict]:
        return [v for v in scalar_views(e2e_config, namespace)
                if v["attribute"] == "gym_visit_count"]

    with scripted_model(script) as model:
        v1 = {**feature_env, **model.env, **COUNTING_ON}
        v2 = {**v1, "MENHIR_PERSONAL_MEMORY_COUNTING_EXTRACT_V2": "true"}

        with lane_backend(e2e_config, e2e_installed, lane_evidence, v1, name="backend-v1"):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=v1) as client:
                await _capture(e2e_config, client, namespace, GYM_TURNS)
            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-v1")
        first = _occurrences(e2e_config, namespace, lane_evidence)
        first_ids = sorted(r["id"] for r in first if r["current"])
        view_v1 = gym()
        lane_evidence.record(
            "first_version_counts_the_happenings",
            passed=len(first_ids) == 2 and len(view_v1) == 1 and float(view_v1[0]["value"]) == 2.0,
            detail={"rows": first, "view": view_v1},
        )

        state["readable"] = False
        before = len(script.calls("counting"))
        with lane_backend(e2e_config, e2e_installed, lane_evidence, v2, name="backend-v2"):
            result = run_phase3(e2e_config, lane_evidence, namespace, label="phase3-v2")
        new_calls = script.calls("counting")[before:]
        lane_evidence.record(
            "new_version_reads_every_page_again",
            passed=len(new_calls) >= 3
            and all("Wednesday evening I went to the gym" in c.prompt for c in new_calls),
            detail={"new_counting_calls": len(new_calls),
                    **{k: result.get(k) for k in result if k.startswith("counting_")}},
        )
        after = _occurrences(e2e_config, namespace, lane_evidence)
        after_ids = sorted(r["id"] for r in after if r["current"])
        lane_evidence.record(
            "failed_new_reading_keeps_the_old_rows_current",
            passed=after_ids == first_ids,
            detail={"before": first_ids, "after": after_ids, "rows": after},
        )
        view_v2 = gym()
        lane_evidence.record(
            "counted_view_keeps_its_total",
            passed=len(view_v2) == 1 and float(view_v2[0]["value"]) == 2.0,
            detail=view_v2,
        )
    _finish(script, lane_evidence, CRITERIA)
