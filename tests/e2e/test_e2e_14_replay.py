"""E2E-14 - duplicate and replayed input through the installed stack (#170 item 5).

A host hook that fires twice for one prompt (same ``prompt_id``), a user who genuinely says the
same thing again (a new ``prompt_id``), a Phase 3 pass with nothing new to read, and a restart
with every perceiver version bumped so the scalar and counting lanes read every turn
again. None of these may duplicate a claim or move a View.
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
    COUNTING_PROMPT,
    SCALAR_PROMPT,
    Obs,
    admit_turn,
    close_pass,
    envelope_reply,
    lane_backend,
    post_json,
    run_phase3,
    scalar_views,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e._harness.stack import graph_query

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]

LANES_ON = {
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED": "true",
}
BUMPED = {
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_PERCEIVER_VERSION": "v3",
    "MENHIR_PERSONAL_MEMORY_COUNTING_PERCEIVER_VERSION": "v2",
}
LANES = ("scalar", "counting")

MUGS = [Obs("I have 12 mugs", {"attribute": "mug_count", "value_kind": "count",
                               "operation": "absolute", "value": 12})]
GYM = [Obs("I went to the gym", {"attribute": "gym_visit_count", "value_kind": "count",
                                 "operation": "occurrence", "value": 1, "importance": "tracked"})]
#: The View values every replay must leave in place: the mugs restated, two gym visits.
EXPECTED_VIEWS = {"gym_visit_count": 2.0, "mug_count": 12.0}
EXPECTED = sorted(EXPECTED_VIEWS.items())


def capture(config: E2EConfig, namespace: str, text: str, *, at: str, prompt_id: str) -> dict:
    """POST one user turn the way a host hook does, with its per-prompt id."""
    return post_json(config, "/api/turn-evidence", {
        "text": text, "role": "user", "declarant": "user", "namespace": namespace,
        "occurred_at": at, "source_kind": "e2e", "session_id": "e2e14", "prompt_id": prompt_id,
    })


def _assertions(config: E2EConfig, namespace: str) -> list[dict]:
    return graph_query(
        config,
        "MATCH (a:TypedAssertion) WHERE a.namespace = $ns "
        "OPTIONAL MATCH (h:TypedAssertionHead)-[:CURRENT]->(a) "
        "RETURN a.attribute AS attribute, a.value AS value, a.source_key AS source, "
        "       a.perceiver_version AS version, h IS NOT NULL AS current",
        ns=namespace,
    )


def _state(config: E2EConfig, namespace: str) -> dict:
    return {
        "views": sorted((v["uuid"], v["attribute"], str(v["value"]))
                        for v in scalar_views(config, namespace)),
    }


def _values(state: dict) -> list[tuple[str, float]]:
    """One (attribute, value) per current View; a duplicate View shows as a repeated attribute."""
    return sorted((attribute, float(value)) for _uuid, attribute, value in state["views"])


REPLAY_CRITERIA = [
    "double_fired_capture_is_one_turn",
    "double_fired_turn_yields_one_claim_per_lane",
    "genuine_restatement_keeps_one_view",
    "pass_without_new_input_is_a_noop",
    "perceiver_bump_replays_without_duplicates",
    "no_unscripted_model_prompt",
]


async def test_e2e_14_replay(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e14r-{uuid4().hex[:12]}"
    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(MUGS)),
                     Rule("counting", COUNTING_PROMPT, envelope_reply(GYM))])
    mugs = "I have 12 mugs in the cupboard."
    gym, gym_again = "Today I went to the gym after work.", "Saturday I went to the gym with Sam."
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **LANES_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env, name="backend-v2"):
            fired = {
                "mugs": [capture(e2e_config, namespace, mugs, at="2026-07-01T10:00:00+00:00",
                                 prompt_id="p-mugs") for _ in range(2)],
                "gym": [capture(e2e_config, namespace, gym, at="2026-07-04T18:00:00+00:00",
                                prompt_id="p-gym") for _ in range(2)],
            }
            # The user says the mugs line again two days later: a new prompt, a new turn.
            again = capture(e2e_config, namespace, mugs, at="2026-07-03T10:00:00+00:00",
                            prompt_id="p-mugs-again")
            second_gym = capture(e2e_config, namespace, gym_again, at="2026-07-05T18:00:00+00:00",
                                 prompt_id="p-gym-2")
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for text, turn in ((mugs, fired["mugs"][0]["turn_id"]),
                                   (gym, fired["gym"][0]["turn_id"]),
                                   (mugs, again["turn_id"]),
                                   (gym_again, second_gym["turn_id"])):
                    await admit_turn(client, namespace, text, str(turn))

            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-first")
            after_first = _state(e2e_config, namespace)
            rows_first = _assertions(e2e_config, namespace)
            calls_first = {name: len(script.calls(name)) for name in LANES}

            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-idle")
            after_idle = _state(e2e_config, namespace)
            calls_idle = {name: len(script.calls(name)) for name in LANES}

        # Restart against the same graph with every perceiver bumped: every turn is read again.
        bumped_env = {**env, **BUMPED}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, bumped_env, name="backend-bumped"):
            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-bumped")
            after_bump = _state(e2e_config, namespace)
            rows_bump = _assertions(e2e_config, namespace)
            calls_bump = {name: len(script.calls(name)) for name in LANES}

    lane_evidence.attach("captures.json", json.dumps(
        {**fired, "again": again, "second_gym": second_gym}, indent=2))
    lane_evidence.attach("assertions.json", json.dumps({"first": rows_first, "bumped": rows_bump},
                                                       indent=2, default=str))

    lane_evidence.record(
        "double_fired_capture_is_one_turn",
        passed=all(pair[0]["turn_id"] == pair[1]["turn_id"] and pair[0].get("created") is True
                   and pair[1].get("created") is False for pair in fired.values())
        and all(c.get("created") is True for c in (again, second_gym))
        and again["turn_id"] != fired["mugs"][0]["turn_id"],
        detail={k: [(c["turn_id"], c.get("created")) for c in v] for k, v in fired.items()},
    )
    sources = {attr: sorted(str(r["source"]) for r in rows_first if r["attribute"] == attr)
               for attr in EXPECTED_VIEWS}
    lane_evidence.record(
        "double_fired_turn_yields_one_claim_per_lane",
        passed=all(len(s) == 2 and len(set(s)) == 2 for s in sources.values())
        and ("gym_visit_count", 2.0) in _values(after_first),
        detail={"assertion_sources": sources, "views": _values(after_first)},
    )
    lane_evidence.record(
        "genuine_restatement_keeps_one_view",
        passed=_values(after_first) == EXPECTED,
        detail=after_first["views"],
    )
    lane_evidence.record(
        "pass_without_new_input_is_a_noop",
        passed=after_idle == after_first and calls_idle == calls_first,
        detail={"calls_first": calls_first, "calls_idle": calls_idle,
                "changed": after_idle != after_first},
    )
    current_bumped = {attr: [r for r in rows_bump if r["attribute"] == attr and r["current"]]
                      for attr in EXPECTED_VIEWS}
    # Counting stamps its effective version: extract v2 (+x2) and match (+m) are always on (#168).
    bumped_to = {"mug_count": "v3", "gym_visit_count": "v2+x2+m"}
    lane_evidence.record(
        "perceiver_bump_replays_without_duplicates",
        passed=all(calls_bump[name] > calls_idle[name] for name in LANES)
        and _values(after_bump) == EXPECTED
        and all(len(rows) == 2 and {r["version"] for r in rows} == {bumped_to[attr]}
                for attr, rows in current_bumped.items()),
        detail={"calls": calls_bump, "views": after_bump["views"],
                "current_rows": current_bumped},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, REPLAY_CRITERIA)
