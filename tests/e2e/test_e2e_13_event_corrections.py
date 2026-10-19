"""E2E-13 event corrections - "Actually, I bought the bike on ..." replaces the earlier entry (#170 item 5).

A later turn with a correction cue restating the same item replaces the one earlier timeline entry.
A later turn that names another occurrence ("a new lamp") is not a correction. The replacement is
read-side only, so erasing the correcting turn brings the original entry back.
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
    admit_turn,
    close_pass,
    lane_backend,
    record_turn,
    run_phase3,
)
from tests.e2e._harness.scripted_provider import Call, Rule, Script, scripted_model
from tests.e2e.test_e2e_13_state_semantics import (
    _EVENT_LINE,
    EVENT_ON,
    EVENT_PROMPT,
    _timelines,
)
from tests.e2e.test_e2e_14_deletion import delete_memory

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1500)]

#: phrase -> object. The scripted model proposes every one it sees.
EVENTS = {
    "I bought a bike": "bike",
    "I bought the bike": "bike",
    "I bought a lamp": "lamp",
    "I bought a new lamp": "lamp",
}

ORIGINAL = ("I bought a bike at the bike shop.", "2026-07-10T10:00:00+00:00")
CORRECTION = ("Actually, I bought the bike on 2026-07-03.", "2026-07-12T10:00:00+00:00")
PASSES = [
    [ORIGINAL, ("I bought a lamp at the market.", "2026-07-09T10:00:00+00:00")],
    [CORRECTION, ("Actually, I bought a new lamp today.", "2026-07-12T11:00:00+00:00")],
]

CRITERIA = [
    "correction_replaces_the_earlier_entry",
    "another_purchase_is_not_a_correction",
    "erasing_the_correction_restores_the_original",
    "no_unscripted_model_prompt",
]


def _event_reply(call: Call) -> str:
    envelopes = []
    for index, text in _EVENT_LINE.findall(call.prompt):
        events = [{"subject": "user", "predicate": "bought", "object": obj, "object_display": "",
                   "domain": "", "when": "", "stated_span": phrase}
                  for phrase, obj in EVENTS.items() if phrase.lower() in text.lower()]
        envelopes.append({"episode": int(index), "events": events})
    return json.dumps(envelopes)


def _whens(timeline: list[dict], obj: str) -> list[str]:
    if len(timeline) != 1:
        return []
    return sorted(str(e.get("when", ""))[:10] for e in timeline[0]["entries"]
                  if e.get("object_key") == obj)


async def test_e2e_13_event_corrections(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e13c-{uuid4().hex[:12]}"
    script = Script([Rule("event", EVENT_PROMPT, _event_reply)])
    snapshots: list[list[dict]] = []
    turn_ids: dict[str, str] = {}
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **EVENT_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for number, turns in enumerate(PASSES, start=1):
                    for text, at in turns:
                        turn_ids[text] = record_turn(e2e_config, namespace, text, occurred_at=at)
                        await admit_turn(client, namespace, text, turn_ids[text])
                    run_phase3(e2e_config, lane_evidence, namespace, label=f"phase3-pass{number}")
                    snapshots.append(_timelines(e2e_config, namespace))
            deleted = delete_memory(e2e_config, str(turn_ids[CORRECTION[0]]))
            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-after-erase")
            snapshots.append(_timelines(e2e_config, namespace))
            lane_evidence.attach("timelines-per-pass.json", json.dumps(
                {"snapshots": snapshots, "deleted": deleted}, indent=2, default=str))

    first, second, third = snapshots
    lane_evidence.record(
        "correction_replaces_the_earlier_entry",
        passed=_whens(first, "bike") == ["2026-07-10"] and _whens(second, "bike") == ["2026-07-03"],
        detail={"pass1": _whens(first, "bike"), "pass2": _whens(second, "bike")},
    )
    lane_evidence.record(
        "another_purchase_is_not_a_correction",
        passed=_whens(second, "lamp") == ["2026-07-09", "2026-07-12"],
        detail={"pass2": _whens(second, "lamp")},
    )
    lane_evidence.record(
        "erasing_the_correction_restores_the_original",
        passed=bool(deleted.get("deleted")) and _whens(third, "bike") == ["2026-07-10"]
        and _whens(third, "lamp") == ["2026-07-09", "2026-07-12"],
        detail={"deleted": deleted, "bike": _whens(third, "bike"), "lamp": _whens(third, "lamp")},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
