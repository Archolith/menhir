"""E2E-18 - opting a lane out and back in, across backend restarts (#170 item 10).

One namespace on one graph goes through four backends, each a restart with different flags:

1. **on**: scalar, event and counter lanes with both authority paths.
2. **lanes off, authority on**: the writers stop, and authority stops with them.
3. **all off**: recall falls back to ordinary memories.
4. **back on**: the lanes catch up on the turns captured while they were off.

Turns captured while a lane is off are not lost. The re-enabled lanes read them from their
durable cursors without re-reading turns they already finished.

Authority requires its lane (config/authority_gates.py). A lane that is off no longer updates
its Views or events, so serving them as current would be false-current. With the lane off,
recall serves ordinary memories even when the authority flag is still on.
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
    Obs,
    admit_turn,
    close_pass,
    envelope_reply,
    lane_backend,
    listed_turns,
    post_json,
    record_turn,
    scalar_views,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e.test_e2e_13_state_semantics import _EVENT_LINE, EVENT_PROMPT
from tests.e2e.test_e2e_16_counter import (
    COREF_PROMPT,
    CROSS_CHECK_PROMPT,
    EXTRACT_PROMPT,
    VERIFY_PROMPT,
    _extract_reply,
    counters,
)
from tests.e2e.test_e2e_17_recall_authority import (
    _ask,
    _event_reply,
    _events,
    _scalar,
    _sources_text,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400)]

ON = {
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED": "true",
    # The passes under test are explicit phase3 runs.
    "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S": "86400",
}
LANES_OFF = {**ON,
             "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "false",
             "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED": "false",
             "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED": "false"}
#: Scalar authority is always on (#168 freeze) and follows its lane, so only event authority is
#: switched off here; the scalar half of this stage equals stage 2.
ALL_OFF = {**LANES_OFF,
           "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED": "false"}

COINS = [
    Obs("I have 37 coins", {"attribute": "owned_coin_count", "value_kind": "count",
                            "operation": "absolute", "value": 37}),
    Obs("I bought 5 more coins", {"attribute": "owned_coin_count", "value_kind": "count",
                                  "operation": "delta", "value": 5}),
]

#: Captured while every lane is on.
BEFORE = [
    ("I have 37 coins in my jar.", "2026-07-01T10:00:00+00:00"),
    ("I bought a red kayak at the lake shop.", "2026-07-01T11:00:00+00:00"),
    ("I bought a blue kayak at the marina.", "2026-07-03T10:00:00+00:00"),
    ("I have 15 mugs in the kitchen.", "2026-07-01T12:00:00+00:00"),
]
#: Captured while the lanes are off.
WHILE_OFF = [
    ("Yesterday I bought 5 more coins at the fair.", "2026-07-05T10:00:00+00:00"),
    ("I bought a green kayak at the harbor.", "2026-07-06T10:00:00+00:00"),
    ("I bought a new mug at the market today.", "2026-07-07T10:00:00+00:00"),
]

EVENTS_EXTRA = {"I bought a green kayak": "green kayak"}

CRITERIA = [
    "lanes_build_state_while_on",
    "lane_off_does_no_model_work",
    "lane_off_keeps_stored_state",
    "authority_requires_its_lane",
    "full_opt_out_falls_back_to_ordinary_recall",
    "re_enable_reads_only_the_backlog",
    "re_enabled_lanes_catch_up",
    "phase3_reports_caught_up",
    "no_unscripted_model_prompt",
]

_LANE_RULES = ("scalar", "event", "extract", "cross_check", "coref", "verify")


def _phase3(config: E2EConfig, evidence: LaneEvidence, namespace: str, label: str) -> dict:
    result = post_json(config, "/api/phase3/run",
                       {"namespace": namespace, "k": 3, "counter_state": True})
    evidence.attach(f"{label}.json", json.dumps(result, indent=2, default=str))
    return result


def _calls(script: Script) -> dict[str, int]:
    return {name: len(script.calls(name)) for name in _LANE_RULES}


def _event_reply_with_green(call) -> str:
    envelopes = json.loads(_event_reply(call))
    lines = {int(i): t for i, t in _EVENT_LINE.findall(call.prompt)}
    for envelope in envelopes:
        text = lines.get(int(envelope["episode"]), "").lower()
        for phrase, obj in EVENTS_EXTRA.items():
            if phrase.lower() in text:
                envelope["events"].append({
                    "subject": "user", "predicate": "bought", "object": obj, "object_display": "",
                    "domain": "", "when": "", "stated_span": phrase})
    return json.dumps(envelopes)


def _coins(views: list[dict]) -> list[float]:
    return [float(v["value"]) for v in views if v["attribute"] == "owned_coin_count"]


def _prompts(script: Script, name: str, start: int) -> str:
    return "\n".join(call.prompt for call in script.calls(name)[start:])


def _turns_listed(script: Script, name: str, start: int) -> set[str]:
    """The turn texts the lane's prompts listed (not the instructions, which carry examples)."""
    seen: set[str] = set()
    for call in script.calls(name)[start:]:
        if name == "event":
            seen.update(text for _, text in _EVENT_LINE.findall(call.prompt))
        else:
            seen.update(text for _, text in listed_turns(call.prompt))
    return seen


async def test_e2e_18_opt_out_and_back(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e18-{uuid4().hex[:12]}"
    script = Script([
        Rule("scalar", SCALAR_PROMPT, envelope_reply(COINS)),
        Rule("event", EVENT_PROMPT, _event_reply_with_green),
        Rule("extract", EXTRACT_PROMPT, _extract_reply),
        Rule("cross_check", CROSS_CHECK_PROMPT, ['{"total": null}']),
        Rule("coref", COREF_PROMPT, ['{"same_purchase": true}']),
        Rule("verify", VERIFY_PROMPT, ['{"correct": true}']),
    ])
    coins_q = "How many coins do I have now?"
    kayak_q = "Which kayak did I buy most recently?"
    with scripted_model(script) as model:
        def env(flags: dict[str, str]) -> dict[str, str]:
            return {**feature_env, **model.env, **flags}

        async def capture(client, turns) -> None:
            for text, at in turns:
                await admit_turn(client, namespace, text,
                                 record_turn(e2e_config, namespace, text, occurred_at=at))

        # 1. on
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env(ON), name="backend-on"):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env(ON)) as client:
                await capture(client, BEFORE)
                _phase3(e2e_config, lane_evidence, namespace, "phase3-on")
                on_views, on_counters = scalar_views(e2e_config, namespace), counters(e2e_config, namespace)
                on_coins, _ = await _ask(client, namespace, coins_q, lane_evidence, "coins-on")
                on_kayak, _ = await _ask(client, namespace, kayak_q, lane_evidence, "kayak-on")

        # 2. lanes off, authority on
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env(LANES_OFF),
                          name="backend-lanes-off"):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env(LANES_OFF)) as client:
                await capture(client, WHILE_OFF)
                calls_before_off = _calls(script)
                off_result = _phase3(e2e_config, lane_evidence, namespace, "phase3-lanes-off")
                calls_after_off = _calls(script)
                off_views, off_counters = scalar_views(e2e_config, namespace), counters(e2e_config, namespace)
                off_coins, off_coins_ctx = await _ask(client, namespace, coins_q, lane_evidence,
                                                      "coins-lanes-off")
                off_kayak, off_kayak_ctx = await _ask(client, namespace, kayak_q, lane_evidence,
                                                      "kayak-lanes-off")

        # 3. all off
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env(ALL_OFF), name="backend-all-off"):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env(ALL_OFF)) as client:
                none_coins, none_ctx = await _ask(client, namespace, coins_q, lane_evidence, "coins-all-off")
                none_kayak, _ = await _ask(client, namespace, kayak_q, lane_evidence, "kayak-all-off")

        # 4. back on
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env(ON), name="backend-back-on"):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env(ON)) as client:
                start = _calls(script)
                back_result = _phase3(e2e_config, lane_evidence, namespace, "phase3-back-on")
                back_views, back_counters = scalar_views(e2e_config, namespace), counters(e2e_config, namespace)
                back_coins, _ = await _ask(client, namespace, coins_q, lane_evidence, "coins-back-on")
                back_kayak, _ = await _ask(client, namespace, kayak_q, lane_evidence, "kayak-back-on")

    def lead(recall: dict) -> tuple[str, str]:
        verdict = _scalar(recall, "owned_coin_count", "current")
        return (str(verdict[0].get("value")), str(verdict[0].get("status"))) if len(verdict) == 1 else ("", "")

    def kayak(recall: dict) -> tuple[str, str]:
        verdict = _events(recall)
        return ((str(verdict[0].get("object_key")), str(verdict[0].get("status")))
                if len(verdict) == 1 else ("", ""))

    mugs = {"on": on_counters, "off": off_counters, "back": back_counters}
    mug_values = {k: {c: v["value"] for c, v in snap.items()} for k, snap in mugs.items()}
    lane_evidence.attach("counters.json", json.dumps(mug_values, indent=2, default=str))
    lane_evidence.record(
        "lanes_build_state_while_on",
        passed=_coins(on_views) == [37.0] and lead(on_coins) == ("37", "leads")
        and kayak(on_kayak) == ("blue kayak", "leads") and 15.0 in mug_values["on"].values(),
        detail={"coins": _coins(on_views), "lead": lead(on_coins), "kayak": kayak(on_kayak),
                "counters": mug_values["on"]},
    )
    lane_evidence.record(
        "lane_off_does_no_model_work",
        passed=calls_after_off == calls_before_off and not off_result.get("scalar_enabled"),
        detail={"before": calls_before_off, "after": calls_after_off, "result": off_result},
    )
    lane_evidence.record(
        "lane_off_keeps_stored_state",
        passed=_coins(off_views) == [37.0] and mug_values["off"] == mug_values["on"],
        detail={"coins": _coins(off_views), "counters": mug_values["off"]},
    )
    # The newer turns say 42 coins and a green kayak; the frozen 37 and blue kayak must not lead.
    lane_evidence.record(
        "authority_requires_its_lane",
        passed="authority_layer" not in off_coins and "event_authority_layer" not in off_kayak
        and "i have 37 coins in my jar." in _sources_text(off_coins)
        and "i bought a blue kayak at the marina." in _sources_text(off_kayak)
        and "authority:" not in off_coins_ctx and "authority:" not in off_kayak_ctx,
        detail={"coins_keys": sorted(off_coins), "kayak_keys": sorted(off_kayak)},
    )
    lane_evidence.record(
        "full_opt_out_falls_back_to_ordinary_recall",
        passed="authority_layer" not in none_coins and "event_authority_layer" not in none_kayak
        and "i have 37 coins in my jar." in _sources_text(none_coins)
        and "i bought a blue kayak at the marina." in _sources_text(none_kayak)
        and "authority:" not in none_ctx,
        detail={"coins_keys": sorted(none_coins), "kayak_keys": sorted(none_kayak)},
    )
    scalar_seen = _prompts(script, "scalar", start["scalar"])
    event_seen = _prompts(script, "event", start["event"])
    extract_seen = _prompts(script, "extract", start["extract"])
    lane_evidence.attach("back-on-prompts.txt",
                         f"{scalar_seen}\n\n---\n\n{event_seen}\n\n---\n\n{extract_seen}")
    # Scalar and event read past their durable cursors. The counter lane re-reads every user turn
    # of a dirty namespace on each pass by design (load_user_episodes); it is recorded, not asserted.
    backlog = {text for text, _ in WHILE_OFF}
    scalar_turns = _turns_listed(script, "scalar", start["scalar"])
    event_turns = _turns_listed(script, "event", start["event"])
    lane_evidence.record(
        "re_enable_reads_only_the_backlog",
        passed=scalar_turns == backlog and event_turns == backlog,
        detail={"scalar_turns": sorted(scalar_turns), "event_turns": sorted(event_turns),
                "counter_turns": sorted(_turns_listed(script, "extract", start["extract"]))},
    )
    lane_evidence.record(
        "re_enabled_lanes_catch_up",
        passed=_coins(back_views) == [42.0] and lead(back_coins) == ("42", "leads")
        and kayak(back_kayak) == ("green kayak", "leads") and 16.0 in mug_values["back"].values(),
        detail={"coins": _coins(back_views), "lead": lead(back_coins), "kayak": kayak(back_kayak),
                "counters": mug_values["back"]},
    )
    lane_evidence.record(
        "phase3_reports_caught_up",
        passed=back_result.get("dirty_after") is False
        and back_result.get("event_dirty_after") is False,
        detail={k: back_result.get(k) for k in
                ("dirty_after", "event_dirty_after", "counting_dirty_after", "scalar_repair_pending")},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
