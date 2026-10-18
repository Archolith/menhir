"""E2E-11 - the count-match gate sets a bridge quote aside instead of vetoing agreed rows.

One turn tells two happenings. Two of the three counting samples quote each happening on
its own; the third quotes the whole sentence once. That wide quote overlaps both quotes of
another sample, so it cannot say which one it votes for. Without bridge resolution the
whole overlap component is ambiguous and every row in it is vetoed at 1/3, including the
two rows the other samples agree on. With it, the bridge is set aside and both happenings
are counted.
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
    DECIDE_PROMPT,
    SCALAR_PROMPT,
    decide_reply,
    envelope_reply,
    lane_backend,
    run_phase3,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e.test_e2e_10_counting import (
    MATCH_ON,
    _capture,
    _family_view,
    _finish,
    _occ,
    _occurrences,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200)]

TURN = ("Today I finished the Gundam model kit and the Zaku model kit.",
        "2026-07-01T18:00:00+00:00")
NARROW = [
    _occ("finished the Gundam model kit", "model_kit_finished_count",
         object="model kit", action="finished", item="Gundam"),
    _occ("the Zaku model kit", "model_kit_finished_count",
         object="model kit", action="finished", item="Zaku"),
]
WIDE = [
    _occ("I finished the Gundam model kit and the Zaku model kit", "model_kit_finished_count",
         object="model kit", action="finished", item="Gundam and Zaku"),
]

CRITERIA = [
    "three_counting_samples_one_with_a_bridge_quote",
    "agreed_happenings_are_stored_and_the_bridge_is_not",
    "family_view_counts_both_happenings",
    "no_unscripted_model_prompt",
]


async def test_e2e_11_count_match_bridges(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e11b-{uuid4().hex[:12]}"
    narrow = envelope_reply(NARROW)
    # Which sample gets the wide quote does not matter; the last reply repeats for later calls.
    script = Script([
        Rule("scalar", SCALAR_PROMPT, envelope_reply([])),
        Rule("counting", COUNTING_PROMPT, [narrow, narrow, envelope_reply(WIDE)]),
        Rule("decide", DECIDE_PROMPT, decide_reply([], family="model kit")),
    ])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **MATCH_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                await _capture(e2e_config, client, namespace, [TURN])
            result = run_phase3(e2e_config, lane_evidence, namespace)

            counting = script.calls("counting")
            lane_evidence.record(
                "three_counting_samples_one_with_a_bridge_quote",
                passed=len(counting) == 3 and result.get("counting_llm_calls", 0) >= 3,
                detail={k: result.get(k) for k in result if k.startswith("counting_")},
            )
            occ = _occurrences(e2e_config, namespace, lane_evidence)
            spans = sorted(str(r["span"]) for r in occ if r["current"])
            lane_evidence.record(
                "agreed_happenings_are_stored_and_the_bridge_is_not",
                passed=spans == ["finished the Gundam model kit", "the Zaku model kit"]
                and result.get("counting_occurrences_stored") == 2,
                detail={"current_spans": spans, "rows": occ,
                        "stored": result.get("counting_occurrences_stored"),
                        "gate_dropped": result.get("counting_gate_dropped")},
            )
            family = _family_view(e2e_config, namespace)
            lane_evidence.record(
                "family_view_counts_both_happenings",
                passed=len(family) == 1 and float(family[0]["value"]) == 2.0
                and family[0]["basis"] == "counted",
                detail=family,
            )
    _finish(script, lane_evidence, CRITERIA)
