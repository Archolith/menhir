"""E2E-13 numberless stop - "I don't have dogs anymore" sets the count to 0 (#170 item 5).

A stop that states no number used to be dropped (no source count), so the old count stayed current.
It now folds as a count of 0, whatever number the model guessed. A stop that only shrinks the count
("not as many") still says nothing exact and keeps the old value.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.phase3 import (
    SCALAR_PROMPT,
    admit_turn,
    close_pass,
    envelope_reply,
    lane_backend,
    record_turn,
    run_phase3,
    scalar_views,
    typed_assertions,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e.test_e2e_13_state_semantics import SCALAR_ON, _count, _values

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1500)]

OBS = [
    _count("I have 3 dogs", "dog_count", 3),
    # The model guesses 5, a number the source never states; it must not reach the graph.
    _count("I don't have dogs anymore", "dog_count", 5, operation="expire"),
    _count("I have 6 fish", "fish_count", 6),
    _count("I don't have as many fish anymore", "fish_count", 2, operation="expire"),
]

PASSES = [
    [("I have 3 dogs at home.", "2026-07-01T10:00:00+00:00"),
     ("I have 6 fish in the tank.", "2026-07-01T11:00:00+00:00")],
    [("I don't have dogs anymore.", "2026-07-12T10:00:00+00:00"),
     ("I don't have as many fish anymore.", "2026-07-12T11:00:00+00:00")],
]

CRITERIA = [
    "numberless_stop_sets_the_count_to_zero",
    "model_number_never_reaches_the_graph",
    "partial_stop_keeps_the_old_count",
    "no_unscripted_model_prompt",
]


async def test_e2e_13_numberless_stop(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e13n-{uuid4().hex[:12]}"
    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(OBS))])
    snapshots: list[list[dict]] = []
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **SCALAR_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for number, turns in enumerate(PASSES, start=1):
                    for text, at in turns:
                        await admit_turn(client, namespace, text,
                                         record_turn(e2e_config, namespace, text, occurred_at=at))
                    run_phase3(e2e_config, lane_evidence, namespace, label=f"phase3-pass{number}")
                    snapshots.append(scalar_views(e2e_config, namespace))
            lane_evidence.attach("views-per-pass.json", json.dumps(snapshots, indent=2, default=str))
            rows = typed_assertions(e2e_config, namespace)
            lane_evidence.attach("typed-assertions.json", json.dumps(rows, indent=2, default=str))

    first, second = snapshots
    dog_rows = [r for r in rows if r["attribute"] == "dog_count"]
    lane_evidence.record(
        "numberless_stop_sets_the_count_to_zero",
        passed=_values(first, "dog_count") == [3.0] and _values(second, "dog_count") == [0.0],
        detail={"pass1": _values(first, "dog_count"), "pass2": _values(second, "dog_count")},
    )
    lane_evidence.record(
        "model_number_never_reaches_the_graph",
        passed=bool(dog_rows) and all(float(r["value"]) in (3.0, 0.0) for r in dog_rows)
        and any(r["operation"] == "absolute" and float(r["value"]) == 0.0 for r in dog_rows),
        detail=dog_rows,
    )
    lane_evidence.record(
        "partial_stop_keeps_the_old_count",
        passed=_values(first, "fish_count") == [6.0] and _values(second, "fish_count") == [6.0],
        detail={"pass1": _values(first, "fish_count"), "pass2": _values(second, "fish_count")},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
