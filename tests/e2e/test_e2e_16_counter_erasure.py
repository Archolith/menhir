"""E2E-16 erasure - deleting the evidence behind a counter View (#170 item 4/5).

The counter lane folds a stated count plus a later acquisition (15 + 1 = 16) and a stated spend.
The user then deletes the acquisition turn and the spend memory. The counter Views they supported
must go at once, the next pass must refold the rest from the remaining turns only (mugs back to 15,
no spend), and the erased words must never reach the model again.
"""

from __future__ import annotations

import json
import re
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
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e.test_e2e_14_deletion import delete_memory
from tests.e2e.test_e2e_14_replay import capture
from tests.e2e.test_e2e_16_counter import (
    COREF_PROMPT,
    COUNTER_ON,
    CROSS_CHECK_PROMPT,
    EXTRACT_PROMPT,
    VERIFY_PROMPT,
    _extract_reply,
    counters,
    phase3,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]

_EPISODE_ID = re.compile(r"episode_id\W+([0-9a-f]{8}-[0-9a-f-]{27})")

TURNS = {
    "mugs": ("I have 15 mugs in the kitchen.", "2026-07-01T10:00:00+00:00"),
    "new_mug": ("I bought a new mug at the market today.", "2026-07-05T10:00:00+00:00"),
    "groceries": ("I have spent $30 on groceries so far.", "2026-07-01T13:00:00+00:00"),
}

CRITERIA = [
    "counters_fold_before_deletion",
    "deletion_retires_the_supported_counters",
    "refold_uses_only_the_remaining_turns",
    "erased_turns_are_not_reperceived",
    "no_unscripted_model_prompt",
]


async def test_e2e_16_counter_erasure(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e16e-{uuid4().hex[:10]}"
    script = Script([
        Rule("scalar", SCALAR_PROMPT, envelope_reply([])),
        Rule("extract", EXTRACT_PROMPT, _extract_reply),
        Rule("cross_check", CROSS_CHECK_PROMPT, ['{"total": null}']),
        Rule("coref", COREF_PROMPT, ['{"same_purchase": true}']),
        Rule("verify", VERIFY_PROMPT, ['{"correct": true}']),
    ])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **COUNTER_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            ids: dict[str, dict] = {}
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for key, (text, at) in TURNS.items():
                    turn = capture(e2e_config, namespace, text, at=at, prompt_id=f"p-{key}")
                    body = await admit_turn(client, namespace, text, str(turn["turn_id"]))
                    match = _EPISODE_ID.search(body)
                    ids[key] = {"turn": str(turn["turn_id"]), "episode": match and match.group(1)}

            phase3(e2e_config, lane_evidence, namespace, "phase3-before")
            before = counters(e2e_config, namespace)
            extract_before = len(script.calls("extract"))

            deleted = {
                "new_mug_turn": delete_memory(e2e_config, ids["new_mug"]["turn"]),
                "groceries_memory": delete_memory(e2e_config, str(ids["groceries"]["episode"])),
            }
            after_delete = counters(e2e_config, namespace)
            after_pass_result = phase3(e2e_config, lane_evidence, namespace, "phase3-after")
            after_pass = counters(e2e_config, namespace)

    later_prompts = [call.prompt for call in script.calls("extract")[extract_before:]]
    leaked = sorted({key for key in ("new_mug", "groceries")
                     for prompt in later_prompts if TURNS[key][0] in prompt})
    lane_evidence.attach("counter-erasure.json", json.dumps(
        {"ids": ids, "deleted": deleted, "before": before, "after_delete": after_delete,
         "after_pass": after_pass, "leaked": leaked, "later_extract_calls": len(later_prompts),
         "phase3_after": after_pass_result}, indent=2, default=str))

    def value(snap: dict, counter: str) -> float | None:
        return snap[counter]["value"] if counter in snap else None

    lane_evidence.record(
        "counters_fold_before_deletion",
        passed=value(before, "mugs") == 16.0 and value(before, "grocery_spend") == 30.0,
        detail=before,
    )
    lane_evidence.record(
        "deletion_retires_the_supported_counters",
        passed=all(d.get("deleted") for d in deleted.values())
        and "mugs" not in after_delete and "grocery_spend" not in after_delete,
        detail={"deleted": deleted, "after_delete": after_delete},
    )
    lane_evidence.record(
        "refold_uses_only_the_remaining_turns",
        passed=value(after_pass, "mugs") == 15.0 and "grocery_spend" not in after_pass,
        detail=after_pass,
    )
    lane_evidence.record(
        "erased_turns_are_not_reperceived",
        passed=bool(later_prompts) and not leaked,
        detail={"leaked": leaked, "later_extract_calls": len(later_prompts)},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
