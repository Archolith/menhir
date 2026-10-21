"""E2E-15 - a backend killed mid-pass, and two namespaces side by side (#170 item 5).

Two namespaces hold the same kinds of claim with different values: mugs, gym visits and a
purchase. The Phase 3 pass for namespace A is held inside its event-lane model call, and the
backend is killed there (no shutdown path). A fresh backend on the same graph must finish that
pass exactly once and resume its cursors. Throughout, no model prompt may list turns from both
namespaces, and each namespace's Views and recall must carry only its own values.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
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
    authority_verdicts,
    close_pass,
    envelope_reply,
    lane_backend,
    post_json,
    run_phase3,
    scalar_views,
)
from tests.e2e._harness.scripted_provider import Call, Rule, Script, scripted_model
from tests.e2e._harness.stack import graph_query
from tests.e2e.test_e2e_14_replay import (
    _EVENT_LINE,
    EVENT_PROMPT,
    GYM,
    LANES,
    LANES_ON,
    _assertions,
    _timeline_entries,
    capture,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]

MUGS = [Obs("I have 12 mugs", {"attribute": "mug_count", "value_kind": "count",
                               "operation": "absolute", "value": 12}),
        Obs("I have 7 mugs", {"attribute": "mug_count", "value_kind": "count",
                              "operation": "absolute", "value": 7})]
EVENTS = {"I bought a teapot": "teapot", "I bought a kettle": "kettle"}

TURNS = {
    "a": [("I have 12 mugs in the cupboard.", "2026-07-01T10:00:00+00:00"),
          ("I bought a teapot at the market.", "2026-07-02T10:00:00+00:00"),
          ("Today I went to the gym after work.", "2026-07-04T18:00:00+00:00"),
          ("Saturday I went to the gym with Sam.", "2026-07-05T18:00:00+00:00")],
    "b": [("I have 7 mugs on the shelf.", "2026-07-01T11:00:00+00:00"),
          ("I bought a kettle at the store.", "2026-07-02T11:00:00+00:00"),
          ("On Friday I went to the gym at dawn.", "2026-07-03T07:00:00+00:00")],
}
EXPECTED = {
    "a": {"views": [("gym_visit_count", 2.0), ("mug_count", 12.0)], "timeline": ["teapot"],
          "claims": {"mug_count": 1, "gym_visit_count": 2}},
    "b": {"views": [("gym_visit_count", 1.0), ("mug_count", 7.0)], "timeline": ["kettle"],
          "claims": {"mug_count": 1, "gym_visit_count": 1}},
}


def _snapshot(config: E2EConfig, namespace: str) -> dict:
    views = scalar_views(config, namespace)
    rows = _assertions(config, namespace)
    return {
        "views": sorted((v["attribute"], float(v["value"])) for v in views),
        "view_uuids": {v["attribute"]: v["uuid"] for v in views},
        "timeline": sorted(str(e.get("object_key")) for e in _timeline_entries(config, namespace)),
        "events": [r["object"] for r in graph_query(
            config, "MATCH (a:TypedEventAssertion) WHERE a.namespace = $ns "
            "RETURN a.object_key AS object ORDER BY object", ns=namespace)],
        "claims": {attr: {"sources": len({r["source"] for r in rows if r["attribute"] == attr}),
                          "current": sum(1 for r in rows if r["attribute"] == attr and r["current"])}
                   for attr in ("mug_count", "gym_visit_count")},
    }


def _exactly_once(snap: dict, expected: dict) -> bool:
    return (snap["views"] == expected["views"] and snap["timeline"] == expected["timeline"]
            and snap["events"] == expected["timeline"]
            and all(snap["claims"][attr] == {"sources": n, "current": n}
                    for attr, n in expected["claims"].items()))


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(getattr(item, "text", "") or "" for item in content)


CRITERIA = [
    "pass_was_killed_inside_the_event_stage",
    "killed_pass_finishes_exactly_once_after_restart",
    "restarted_backend_does_not_reread_finished_work",
    "prompts_never_mix_namespaces",
    "same_attribute_stays_per_namespace",
    "recall_reads_only_its_namespace",
    "no_unscripted_model_prompt",
]


async def test_e2e_15_restart_and_isolation(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    suffix = uuid4().hex[:10]
    ns = {"a": f"e2e15a-{suffix}", "b": f"e2e15b-{suffix}"}
    entered, release = threading.Event(), threading.Event()
    held: list[bool] = []

    def event_reply(call: Call) -> str:
        # Hold namespace A's first event call open until the backend has been killed.
        if not held and "teapot" in call.prompt:
            held.append(True)
            entered.set()
            release.wait(300)
        return json.dumps([
            {"episode": int(index), "events": [
                {"subject": "user", "predicate": "bought", "object": obj, "object_display": "",
                 "domain": "", "when": "", "stated_span": phrase}
                for phrase, obj in EVENTS.items() if phrase.lower() in text.lower()]}
            for index, text in _EVENT_LINE.findall(call.prompt)])

    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(MUGS)),
                     Rule("counting", COUNTING_PROMPT, envelope_reply(GYM)),
                     Rule("event", EVENT_PROMPT, event_reply)])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **LANES_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env, name="backend-killed") as backend:
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for key, turns in TURNS.items():
                    for i, (text, at) in enumerate(turns):
                        turn = capture(e2e_config, ns[key], text, at=at, prompt_id=f"p-{key}{i}")
                        await admit_turn(client, ns[key], text, str(turn["turn_id"]))

            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(post_json, e2e_config, "/api/phase3/run",
                                      {"namespace": ns["a"], "k": 3, "counter_state": False})
                reached = entered.wait(600)
                backend.kill()
                release.set()
                try:
                    pending.result(timeout=300)
                    interrupted = ""
                except Exception as exc:  # noqa: BLE001 - the killed request's error is the evidence
                    interrupted = repr(exc)[:300]
            mid = _snapshot(e2e_config, ns["a"])
            calls_at_kill = {name: len(script.calls(name)) for name in LANES}

        with lane_backend(e2e_config, e2e_installed, lane_evidence, env, name="backend-restarted"):
            run_phase3(e2e_config, lane_evidence, ns["a"], label="phase3-a-resumed")
            resumed = _snapshot(e2e_config, ns["a"])
            calls_resumed = {name: len(script.calls(name)) for name in LANES}
            run_phase3(e2e_config, lane_evidence, ns["a"], label="phase3-a-idle")
            idle = _snapshot(e2e_config, ns["a"])
            calls_idle = {name: len(script.calls(name)) for name in LANES}
            run_phase3(e2e_config, lane_evidence, ns["b"], label="phase3-b")
            final = {key: _snapshot(e2e_config, ns[key]) for key in ns}
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                recalled = {key: _text(await client.call_tool(
                    "recall_memories", {"query": "how many mugs do I have", "namespace": ns[key]}))
                    for key in ns}

    lane_evidence.attach("snapshots.json", json.dumps(
        {"mid": mid, "resumed": resumed, "idle": idle, "final": final,
         "calls": {"at_kill": calls_at_kill, "resumed": calls_resumed, "idle": calls_idle}},
        indent=2, default=str))
    lane_evidence.attach("recall.json", json.dumps(recalled, indent=2))

    lane_evidence.record(
        "pass_was_killed_inside_the_event_stage",
        passed=reached and bool(interrupted) and mid["timeline"] == [] and mid["events"] == [],
        detail={"reached": reached, "interrupted": interrupted, "mid": mid,
                "calls_at_kill": calls_at_kill},
    )
    lane_evidence.record(
        "killed_pass_finishes_exactly_once_after_restart",
        passed=_exactly_once(resumed, EXPECTED["a"]),
        detail={"resumed": resumed, "calls_resumed": calls_resumed},
    )
    # A lane whose stage committed before the kill resumes from its cursor; the interrupted lane
    # (event) and the one not yet reached (counting) run on the restarted backend.
    lane_evidence.record(
        "restarted_backend_does_not_reread_finished_work",
        passed=mid["claims"]["mug_count"]["current"] == 1
        and calls_resumed["scalar"] == calls_at_kill["scalar"]
        and calls_idle == calls_resumed and idle == resumed,
        detail={"calls_at_kill": calls_at_kill, "calls_resumed": calls_resumed,
                "calls_idle": calls_idle},
    )
    texts = {key: [text for text, _at in turns] for key, turns in TURNS.items()}
    mixed = [call.prompt[:300] for name in LANES for call in script.calls(name)
             if any(t in call.prompt for t in texts["a"]) and any(t in call.prompt for t in texts["b"])]
    lane_evidence.record(
        "prompts_never_mix_namespaces",
        passed=not mixed and all(any(t in call.prompt for name in LANES
                                     for call in script.calls(name)) for t in texts["b"]),
        detail={"mixed": mixed[:3]},
    )
    lane_evidence.record(
        "same_attribute_stays_per_namespace",
        passed=all(_exactly_once(final[key], EXPECTED[key]) for key in ns)
        and not set(final["a"]["view_uuids"].values()) & set(final["b"]["view_uuids"].values()),
        detail=final,
    )
    leads = {key: [v for v in authority_verdicts(recalled[key]) if v.get("attribute") == "mug_count"]
             for key in ns}
    lane_evidence.record(
        "recall_reads_only_its_namespace",
        passed=all(len(leads[key]) == 1 and float(leads[key][0].get("value")) == want
                   and leads[key][0].get("view_uuid") == final[key]["view_uuids"].get("mug_count")
                   for key, want in (("a", 12.0), ("b", 7.0))),
        detail=leads,
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
