"""E2E-12 - every consolidation lane measured on its own (#170 item 4).

One backend with all three lanes on (scalar, event, counting), four turns captured over
REST and admitted through the stdio bridge, one ``POST /api/phase3/run``. The phase3 response's
``lane_outcomes`` must report each lane separately: what it read, each stage's count, and whether
it produced anything. The scalar fold (an absolute then a delta) and the counted View are checked
against the graph, so "fold correct" is measured, not inferred from a write count.

The scripted model answers the scalar, event and counting prompts with fixed observations. The
removed counter lane (#168) must not appear in the report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.phase3 import (
    COUNTING_PROMPT,
    SCALAR_PROMPT,
    Obs,
    admit_turn,
    close_pass,
    envelope_reply,
    lane_backend,
    post_json,
    record_turn,
    scalar_views,
)
from tests.e2e._harness.scripted_provider import Call, Rule, Script, scripted_model

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1500)]

#: Unique substring of the event (event_history_perception.py) prompt.
EVENT_PROMPT = "extract COMPLETED ACQUISITION EVENTS only"

ALL_LANES_ON = {
    # The pass under test is the explicit phase3 run; keep the background job from racing it.
    "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S": "86400",
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED": "true",
}

TURNS = [
    ("I have 37 coins in my jar.", "2026-07-01T10:00:00+00:00"),
    ("Yesterday I bought 5 more coins at the fair.", "2026-07-03T10:00:00+00:00"),
    ("On Monday I went to the gym before work.", "2026-07-06T18:00:00+00:00"),
    ("Wednesday evening I went to the gym again.", "2026-07-08T18:00:00+00:00"),
]
SCALAR = [
    Obs("I have 37 coins", {"attribute": "owned_coin_count", "value_kind": "count",
                            "operation": "absolute", "value": 37}),
    Obs("I bought 5 more coins", {"attribute": "owned_coin_count", "value_kind": "count",
                                  "operation": "delta", "value": 5}),
]
GYM = [Obs("I went to the gym", {"attribute": "gym_visit_count", "value_kind": "count",
                                 "operation": "occurrence", "value": 1, "importance": "tracked"})]

_EVENT_LINE = re.compile(r"^\[(\d+)\] (.*)$", re.MULTILINE)


def _event_reply(phrase: str, obj: str) -> Callable[[Call], str]:
    """One envelope per listed episode; a 'bought' event wherever ``phrase`` occurs."""

    def reply(call: Call) -> str:
        envelopes = []
        for index, text in _EVENT_LINE.findall(call.prompt):
            events = []
            if phrase.lower() in text.lower():
                events.append({"subject": "user", "predicate": "bought", "object": obj,
                               "object_display": "", "domain": "", "when": "",
                               "stated_span": phrase})
            envelopes.append({"episode": int(index), "events": events})
        return json.dumps(envelopes)

    return reply


CRITERIA = [
    "every_lane_is_reported_separately",
    "scalar_stages_are_counted_separately",
    "scalar_fold_is_correct",
    "event_lane_reports_its_own_stages",
    "counting_lane_reports_its_own_output",
    "no_busy_lane_is_flagged_idle",
    "no_unscripted_model_prompt",
]


async def test_e2e_12_lane_outcomes(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e12-{uuid4().hex[:12]}"
    script = Script([
        Rule("scalar", SCALAR_PROMPT, envelope_reply(SCALAR)),
        Rule("counting", COUNTING_PROMPT, envelope_reply(GYM)),
        Rule("event", EVENT_PROMPT, _event_reply("I bought 5 more coins", "coins")),
    ])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **ALL_LANES_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for text, at in TURNS:
                    await admit_turn(client, namespace, text,
                                     record_turn(e2e_config, namespace, text, occurred_at=at))
            result = post_json(e2e_config, "/api/phase3/run",
                               {"namespace": namespace, "k": 3})
            lane_evidence.attach("phase3.json", json.dumps(result, indent=2, default=str))
            lanes = result.get("lane_outcomes") or {}
            scalar = lanes.get("scalar", {})
            event, counting = lanes.get("event", {}), lanes.get("counting", {})

            lane_evidence.record(
                "every_lane_is_reported_separately",
                passed=all(lanes.get(name, {}).get("enabled") is True
                           for name in ("scalar", "event", "counting"))
                and all(lanes[name].get("input_rows") == len(TURNS)
                        for name in ("scalar", "event", "counting"))
                and not any(lanes[name].get("stalled") for name in lanes),
                detail=lanes,
            )
            stages = scalar.get("stages") or {}
            lane_evidence.record(
                "scalar_stages_are_counted_separately",
                passed=stages.get("committed") == 2 and stages.get("persisted") == 2
                and stages.get("bound") == 2 and stages.get("binding_mismatched") == 0
                and stages.get("advisory") == 0 and (stages.get("views_rebuilt") or 0) >= 1
                and scalar.get("output") == 2,
                detail=stages,
            )
            coins = [v for v in scalar_views(e2e_config, namespace)
                     if v["attribute"] == "owned_coin_count"]
            lane_evidence.record(
                "scalar_fold_is_correct",
                passed=len(coins) == 1 and float(coins[0]["value"]) == 42.0,
                detail=coins,
            )
            ev = event.get("stages") or {}
            lane_evidence.record(
                "event_lane_reports_its_own_stages",
                passed=ev.get("proposals") == 1 and ev.get("admitted") == 1
                and ev.get("recorded") == 1 and (ev.get("views_rebuilt") or 0) >= 1
                and event.get("no_output") is False and event.get("namespaces_failed") == 0
                and len(script.calls("event")) >= 1,
                detail=event,
            )
            gym = [v for v in scalar_views(e2e_config, namespace)
                   if v["attribute"] == "gym_visit_count"]
            lane_evidence.record(
                "counting_lane_reports_its_own_output",
                passed=counting.get("output") == 2 and counting.get("no_output") is False
                and len(gym) == 1 and float(gym[0]["value"]) == 2.0 and gym[0]["basis"] == "counted",
                detail={"counting": counting, "views": gym},
            )
            lane_evidence.record(
                "no_busy_lane_is_flagged_idle",
                passed="counter" not in lanes
                and not any(lanes[name].get("no_output") for name in ("scalar", "event", "counting")),
                detail=lanes,
            )

    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
