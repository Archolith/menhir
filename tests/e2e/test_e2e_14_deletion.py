"""E2E-14 deletion - erasing a memory or a turn through the installed stack (#170 item 5).

The scalar and counting lanes fold five admitted turns. Then the user deletes one memory
(its Episodic) and two turns outright, and Phase 3 runs again. What was deleted must stay deleted:
no claim or View comes back from re-reading, no stored node keeps the erased words, and the
surviving evidence still folds.
"""

from __future__ import annotations

import json
import re
import urllib.request
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.phase3 import (
    COUNTING_PROMPT,
    SCALAR_PROMPT,
    admit_turn,
    close_pass,
    envelope_reply,
    lane_backend,
    run_phase3,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e._harness.stack import graph_query
from tests.e2e.test_e2e_14_replay import (
    EXPECTED,
    GYM,
    LANES,
    LANES_ON,
    MUGS,
    _assertions,
    _state,
    _values,
    capture,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]

_EPISODE_ID = re.compile(r"episode_id\W+([0-9a-f]{8}-[0-9a-f-]{27})")


def delete_memory(config: E2EConfig, uuid: str) -> dict:
    """``DELETE /api/memory/{uuid}``: an Episodic uuid or a TurnEvidence turn id."""
    request = urllib.request.Request(f"{config.backend_url}/api/memory/{uuid}", method="DELETE")
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def _snapshot(config: E2EConfig, namespace: str) -> dict:
    rows = _assertions(config, namespace)
    return {
        "views": _values(_state(config, namespace)),
        "assertions": sorted({r["attribute"] for r in rows}),
    }


def _carriers(config: E2EConfig, namespace: str, phrase: str, *, evidence_only: bool = False
              ) -> list[dict]:
    """Nodes of `namespace` with a string (or string-list) property containing `phrase`.
    `evidence_only`: captured turns and their projection episodes only."""
    scope = ("AND (n:TurnEvidence OR coalesce(n.is_evidence_projection, false)) "
             if evidence_only else "")
    return graph_query(
        config,
        f"MATCH (n) WHERE coalesce(n.namespace, n.group_id) = $ns {scope}"
        "AND any(k IN keys(n) WHERE (n[k] IS :: STRING AND n[k] CONTAINS $phrase) "
        "OR (n[k] IS :: LIST<STRING> AND any(x IN n[k] WHERE x CONTAINS $phrase))) "
        "RETURN labels(n) AS labels, coalesce(n.uuid, n.turn_id, n.assertion_id) AS id",
        ns=namespace, phrase=phrase,
    )


CRITERIA = [
    "deleted_memory_retracts_its_scalar_view",
    "deleted_memory_stays_deleted_after_next_pass",
    "deleted_memory_text_is_gone",
    "deleted_turn_text_is_gone",
    "deleted_turn_rebuilds_the_counted_view",
    "lanes_stay_healthy_after_deletion",
    "no_unscripted_model_prompt",
]


async def test_e2e_14_deletion(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e14d-{uuid4().hex[:12]}"
    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(MUGS)),
                     Rule("counting", COUNTING_PROMPT, envelope_reply(GYM))])
    turns = {
        "mugs": ("I have 12 mugs in the cupboard.", "2026-07-01T10:00:00+00:00"),
        "teapot": ("I bought a teapot at the market.", "2026-07-02T10:00:00+00:00"),
        "lantern": ("I bought a lantern for camping.", "2026-07-03T10:00:00+00:00"),
        "gym1": ("Today I went to the gym after work.", "2026-07-04T18:00:00+00:00"),
        "gym2": ("Saturday I went to the gym with Sam.", "2026-07-05T18:00:00+00:00"),
    }
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **LANES_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            ids: dict[str, dict] = {}
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for key, (text, at) in turns.items():
                    turn = capture(e2e_config, namespace, text, at=at, prompt_id=f"p-{key}")
                    body = await admit_turn(client, namespace, text, str(turn["turn_id"]))
                    match = _EPISODE_ID.search(body)
                    ids[key] = {"turn": str(turn["turn_id"]), "episode": match and match.group(1)}

            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-before")
            before = _snapshot(e2e_config, namespace)
            carried_before = _carriers(e2e_config, namespace, "12 mugs in the cupboard")
            turn_text_before = {key: _carriers(e2e_config, namespace, turns[key][0],
                                               evidence_only=True)
                                for key in ("gym2", "teapot")}

            # The user forgets the mugs memory (its admitted Episodic), and two turns outright.
            deleted = {
                "mugs_episode": delete_memory(e2e_config, str(ids["mugs"]["episode"])),
                "gym2_turn": delete_memory(e2e_config, ids["gym2"]["turn"]),
                "teapot_turn": delete_memory(e2e_config, ids["teapot"]["turn"]),
            }
            after_delete = _snapshot(e2e_config, namespace)
            result = run_phase3(e2e_config, lane_evidence, namespace, label="phase3-after")
            after_pass = _snapshot(e2e_config, namespace)
            memory_text_left = _carriers(e2e_config, namespace, "12 mugs in the cupboard")
            # A deleted turn takes its evidence copies with it; memories that cite it are kept.
            turn_text_left = {key: _carriers(e2e_config, namespace, turns[key][0],
                                             evidence_only=True)
                              for key in ("gym2", "teapot")}

    lane_evidence.attach("deletion.json", json.dumps(
        {"ids": ids, "deleted": deleted, "before": before, "after_delete": after_delete,
         "after_pass": after_pass, "carried_before": carried_before,
         "memory_text_left": memory_text_left, "turn_text_left": turn_text_left},
        indent=2, default=str))
    lanes = result.get("lane_outcomes") or {}

    def attrs(snap: dict) -> list[str]:
        return [attribute for attribute, _value in snap["views"]]

    lane_evidence.record(
        "deleted_memory_retracts_its_scalar_view",
        passed=before["views"] == EXPECTED and all(d.get("deleted") for d in deleted.values())
        and "mug_count" not in attrs(after_delete),
        detail={"before": before["views"], "after_delete": after_delete["views"],
                "deleted": deleted},
    )
    lane_evidence.record(
        "deleted_memory_stays_deleted_after_next_pass",
        passed="mug_count" not in attrs(after_pass) and "mug_count" not in after_pass["assertions"],
        detail={"views": after_pass["views"], "assertions": after_pass["assertions"]},
    )
    lane_evidence.record(
        "deleted_memory_text_is_gone",
        passed=len(carried_before) >= 2 and not memory_text_left,
        detail={"before": carried_before, "after": memory_text_left},
    )
    lane_evidence.record(
        "deleted_turn_text_is_gone",
        passed=all(turn_text_before.values()) and not any(turn_text_left.values()),
        detail={"before": turn_text_before, "after": turn_text_left},
    )
    lane_evidence.record(
        "deleted_turn_rebuilds_the_counted_view",
        passed=("gym_visit_count", 1.0) in after_delete["views"]
        and ("gym_visit_count", 1.0) in after_pass["views"],
        detail={"after_delete": after_delete["views"], "after_pass": after_pass["views"]},
    )
    lane_evidence.record(
        "lanes_stay_healthy_after_deletion",
        passed=all(not lanes.get(name, {}).get("stalled")
                   and not lanes.get(name, {}).get("namespaces_failed") for name in LANES),
        detail={"lanes": {name: lanes.get(name) for name in LANES},
                "view_repairs": result.get("view_repairs")},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
