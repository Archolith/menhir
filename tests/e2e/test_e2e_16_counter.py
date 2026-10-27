"""E2E-16 - the counter lane (``counter_state: true``) through the installed stack (#170 item 5).

The scripted model answers the counter's extraction prompt with fixed events per turn, the
coreference judge with "same purchase", the verifier with "correct", and the holistic
cross-check with no total. Three passes over namespace A: the first folds stated totals with
later purchases and acquisitions, a retold purchase and two ambiguous amounts; the second adds a
user correction; the third re-folds everything. Namespace B holds the same measures with
different values.
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
    post_json,
)
from tests.e2e._harness.scripted_provider import Call, Rule, Script, scripted_model
from tests.e2e._harness.stack import graph_query
from tests.e2e.test_e2e_14_replay import capture

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]

#: Unique substrings of the counter prompts (services/perception.py).
EXTRACT_PROMPT = "COUNTABLE EVENTS for a deterministic aggregator"
CROSS_CHECK_PROMPT = "NAME of one quantity they track"
COREF_PROMPT = "SAME single real-world purchase"
VERIFY_PROMPT = "You audit an assembled fact"
K = 3

COUNTER_ON = {
    "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED": "true",
    # The pass under test is the explicit phase3 run; keep the background job from racing it.
    "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_INTERVAL_S": "86400",
    # Scalar on (with empty replies) so the response carries lane_outcomes.
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
}

_LINE = re.compile(r"^\[(\d+)\] \[(\d{4}-\d{2}-\d{2})\] (.*)$", re.MULTILINE)


def _events(sample: int) -> dict[str, dict]:
    """Extraction events keyed by a phrase of the turn they come from. ``sample`` is the
    position of this call among the pass's k samples; only the records amount depends on it."""
    return {
        "I have 15 mugs": {"kind": "assertion", "measure": "mugs", "value": 15, "what": "I have 15 mugs"},
        "I bought a new mug": {"kind": "acquire", "measure": "mugs", "identity": "blue mug",
                               "category": "kitchen", "what": "bought a new mug"},
        "I have spent $60 on groceries": {"kind": "assertion", "measure": "grocery_spend", "value": 60,
                                          "what": "spent $60 on groceries"},
        "I spent $15 at the farmers market": {"kind": "purchase", "measure": "grocery_spend",
                                              "value": 15, "category": "groceries",
                                              "what": "spent $15 at the farmers market"},
        "maybe 20 or 30 records": {"kind": "assertion", "measure": "records",
                                   "value": 30 if sample == 1 else 20, "what": "20 or 30 records"},
        "about 5 or 6 bikes": {"kind": "assertion", "measure": "bikes", "value": 5,
                               "what": "about 5 or 6 bikes"},
        "I have 12 mugs": {"kind": "assertion", "measure": "mugs", "value": 12, "what": "I have 12 mugs"},
        "I have spent $30 on groceries": {"kind": "assertion", "measure": "grocery_spend", "value": 30,
                                          "what": "spent $30 on groceries"},
    }


def _extract_reply(call: Call) -> str:
    table = _events(call.index % K)
    out = []
    for index, day, text in _LINE.findall(call.prompt):
        for phrase, event in table.items():
            if phrase.lower() in text.lower():
                out.append({"episode": int(index), "subject": "user", "when": day, **event})
    return json.dumps(out)


TURNS = {
    "a": [
        ("I have 15 mugs in the kitchen.", "2026-07-01T10:00:00+00:00"),
        ("I have spent $60 on groceries this month.", "2026-07-01T11:00:00+00:00"),
        ("I bought a new mug at the market today.", "2026-07-05T10:00:00+00:00"),
        ("I spent $15 at the farmers market this morning.", "2026-07-05T11:00:00+00:00"),
        ("Like I said, I spent $15 at the farmers market on Sunday.", "2026-07-06T09:00:00+00:00"),
        ("I own maybe 20 or 30 records.", "2026-07-02T10:00:00+00:00"),
        ("I own about 5 or 6 bikes.", "2026-07-02T11:00:00+00:00"),
    ],
    "b": [
        ("I have 12 mugs on the shelf.", "2026-07-01T12:00:00+00:00"),
        ("I have spent $30 on groceries so far.", "2026-07-01T13:00:00+00:00"),
    ],
}
CORRECTION = ("Actually it's 80, not 75.", "2026-07-08T10:00:00+00:00")


def counters(config: E2EConfig, namespace: str) -> dict[str, dict]:
    rows = graph_query(
        config,
        "MATCH (v:Entity {view_kind: 'counter'}) WHERE v.group_id = $ns "
        "AND coalesce(v.view_current, true) AND NOT coalesce(v.retired, false) "
        "RETURN v.qs_counter AS counter, v.view_value AS value, v.uuid AS uuid, "
        "       v.view_audit_gate AS gate, v.supporting_event_count AS n, "
        "       toString(v.valid_at) AS valid_at ORDER BY counter",
        ns=namespace,
    )
    return {str(r["counter"]): {**r, "value": float(r["value"])} for r in rows}


def _value(snap: dict[str, dict], counter: str) -> float | None:
    return snap[counter]["value"] if counter in snap else None


def phase3(config: E2EConfig, evidence: LaneEvidence, namespace: str, label: str) -> dict:
    result = post_json(config, "/api/phase3/run",
                       {"namespace": namespace, "k": K, "counter_state": True})
    evidence.attach(f"{label}.json", json.dumps(result, indent=2, default=str))
    return result


CRITERIA = [
    "counter_lane_reports_its_own_outcome",
    "stated_spend_plus_later_purchase",
    "stated_count_plus_later_acquisition",
    "retold_purchase_counted_once",
    "correction_replaces_the_total",
    "correction_survives_a_later_refold",
    "disagreeing_samples_abstain",
    "hedged_stated_count_abstains",
    "namespaces_stay_separate",
    "no_unscripted_model_prompt",
]


async def test_e2e_16_counter(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    suffix = uuid4().hex[:10]
    ns = {"a": f"e2e16a-{suffix}", "b": f"e2e16b-{suffix}"}
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
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:

                async def say(key: str, text: str, at: str, n: int) -> None:
                    turn = capture(e2e_config, ns[key], text, at=at, prompt_id=f"p-{key}{n}")
                    await admit_turn(client, ns[key], text, str(turn["turn_id"]))

                for key, turns in TURNS.items():
                    for n, (text, at) in enumerate(turns):
                        await say(key, text, at, n)

                first = phase3(e2e_config, lane_evidence, ns["a"], "phase3-a-1")
                after_first = counters(e2e_config, ns["a"])
                coref_calls = len(script.calls("coref"))
                await say("a", *CORRECTION, 99)

            second = phase3(e2e_config, lane_evidence, ns["a"], "phase3-a-2")
            after_correction = counters(e2e_config, ns["a"])
            third = phase3(e2e_config, lane_evidence, ns["a"], "phase3-a-3")
            after_refold = counters(e2e_config, ns["a"])
            phase3(e2e_config, lane_evidence, ns["b"], "phase3-b")
            final = {key: counters(e2e_config, ns[key]) for key in ns}

    lane_evidence.attach("counters.json", json.dumps(
        {"after_first": after_first, "after_correction": after_correction,
         "after_refold": after_refold, "final": final}, indent=2, default=str))
    counter = (first.get("lane_outcomes") or {}).get("counter", {})

    lane_evidence.record(
        "counter_lane_reports_its_own_outcome",
        passed=first.get("counter_enabled") is True and counter.get("enabled") is True
        and (counter.get("output") or 0) >= 1 and not counter.get("stalled")
        and counter.get("input_rows") == len(TURNS["a"]),
        detail={"counter_enabled": first.get("counter_enabled"), "counter": counter},
    )
    lane_evidence.record(
        "stated_spend_plus_later_purchase",
        passed=_value(after_first, "grocery_spend") == 75.0,
        detail=after_first.get("grocery_spend"),
    )
    lane_evidence.record(
        "stated_count_plus_later_acquisition",
        passed=_value(after_first, "mugs") == 16.0,
        detail=after_first.get("mugs"),
    )
    lane_evidence.record(
        "retold_purchase_counted_once",
        passed=coref_calls >= 1 and _value(after_first, "grocery_spend") == 75.0,
        detail={"coref_calls": coref_calls, "grocery_spend": after_first.get("grocery_spend")},
    )
    lane_evidence.record(
        "correction_replaces_the_total",
        passed=_value(after_correction, "grocery_spend") == 80.0
        and after_correction["grocery_spend"]["gate"] == "correction"
        and second.get("corrections_applied") == 1,
        detail={"grocery_spend": after_correction.get("grocery_spend"),
                "corrections_applied": second.get("corrections_applied")},
    )
    lane_evidence.record(
        "correction_survives_a_later_refold",
        passed=_value(after_refold, "grocery_spend") == 80.0
        and third.get("corrections_applied") == 0,
        detail={"grocery_spend": after_refold.get("grocery_spend"),
                "corrections_applied": third.get("corrections_applied")},
    )
    lane_evidence.record(
        "disagreeing_samples_abstain",
        passed="records" not in after_refold,
        detail=after_refold.get("records"),
    )
    lane_evidence.record(
        "hedged_stated_count_abstains",
        passed="bikes" not in after_refold,
        detail=after_refold.get("bikes"),
    )
    texts = {key: [text for text, _at in turns] for key, turns in TURNS.items()}
    mixed = [call.prompt[:300] for call in script.calls("extract")
             if any(t in call.prompt for t in texts["a"]) and any(t in call.prompt for t in texts["b"])]
    lane_evidence.record(
        "namespaces_stay_separate",
        passed=not mixed and _value(final["b"], "mugs") == 12.0
        and _value(final["b"], "grocery_spend") == 30.0
        and not {r["uuid"] for r in final["a"].values()} & {r["uuid"] for r in final["b"].values()},
        detail={"mixed": mixed[:3], "b": final["b"]},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, CRITERIA)
