"""E2E-13 - state semantics through the installed stack (#170 item 5).

Corrections, out-of-order dates, expiry and ambiguous input, driven the way a host drives
Menhir: turns captured over REST, admitted over the stdio bridge, folded by ``POST
/api/phase3/run``, and checked against the durable graph. Each lane runs several passes on one
namespace so later evidence meets state an earlier pass already folded, which is where these
rules matter.

The model is the scripted fake. Corrections and hedges are decided by deterministic rules on the
stated span (typed_scalar_rules.classify_absolute_semantics / is_ambiguous_exact), so a scripted
span exercises the real rule.
"""

from __future__ import annotations

import itertools
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
    SCALAR_PROMPT,
    Obs,
    admit_turn,
    close_pass,
    envelope_reply,
    lane_backend,
    record_turn,
    run_phase3,
    scalar_views,
    typed_assertions,
)
from tests.e2e._harness.scripted_provider import Call, Rule, Script, scripted_model
from tests.e2e._harness.stack import graph_query

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1500)]

SCALAR_ON = {
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED": "true",
}

BOOKS_AT = "2026-07-05T10:00:00+00:00"   # every books claim shares one valid_at


def _count(phrase: str, attribute: str, value: float, operation: str = "absolute") -> Obs:
    return Obs(phrase, {"attribute": attribute, "value_kind": "count", "operation": operation,
                        "value": value})


SCALAR_OBS = [
    _count("I have 20 books", "owned_book_count", 20),
    _count("Actually, I have 22 books", "owned_book_count", 22),
    _count("I have 21 books", "owned_book_count", 21),
    _count("I have 50 stamps", "stamp_count", 50),
    _count("I have 40 stamps", "stamp_count", 40),
    _count("I have 3 cats", "cat_count", 3),
    # A count is read from its own span, expiries included, so the span states the old count.
    _count("I no longer have my 3 cats", "cat_count", 3, operation="expire"),
    # Hedged: the span states two alternatives, so the exact value must not be admitted.
    _count("I own about 30 or 40 records", "record_count", 30),
    # Only one of the k samples proposes this (see _proposed_once), below the shipped 2/3 threshold.
    # Samples cannot split on the number (a count is read from the span) and the shipped gate
    # reconciles differing attribute names, so a lone proposal is the shipped-settings abstain case.
    _count("I keep 9 plants", "plant_count", 9),
]

#: (text, occurred_at) per pass. Pass 2 brings a correction at the books claim's own time, a stamps
#: claim dated BEFORE the folded one, and an expiry. Pass 3 brings an ordinary books claim, again
#: at the same time.
PASSES = [
    [("I have 20 books on my shelf.", BOOKS_AT),
     ("I have 50 stamps in my album.", "2026-07-10T10:00:00+00:00"),
     ("I have 3 cats at home.", "2026-07-01T10:00:00+00:00"),
     ("I own about 30 or 40 records.", "2026-07-02T10:00:00+00:00"),
     ("I keep 9 plants on the balcony.", "2026-07-03T10:00:00+00:00")],
    [("Actually, I have 22 books on my shelf.", BOOKS_AT),
     ("I have 40 stamps in my album.", "2026-07-01T09:00:00+00:00"),
     ("I no longer have my 3 cats.", "2026-07-12T10:00:00+00:00")],
    [("I have 21 books on my shelf.", BOOKS_AT)],
]


def _proposed_once(base: Callable[[Call], str], attribute: str) -> Callable[[Call], str]:
    """``base``, except only the first call that sees ``attribute`` proposes it."""
    seen = itertools.count()

    def reply(call: Call) -> str:
        envelopes = json.loads(base(call))
        proposes = any(row["attribute"] == attribute for e in envelopes for row in e["observations"])
        if proposes and next(seen) > 0:
            for envelope in envelopes:
                envelope["observations"] = [row for row in envelope["observations"]
                                            if row["attribute"] != attribute]
        return json.dumps(envelopes)

    return reply


def _values(views: list[dict], attribute: str) -> list[float]:
    return [float(v["value"]) for v in views if v["attribute"] == attribute]


def _history_values(config: E2EConfig, namespace: str, attribute: str) -> list[float]:
    rows = graph_query(
        config,
        "MATCH (v:Entity {view_kind: 'scalar_history'}) WHERE v.group_id = $ns "
        "AND v.ss_attribute = $attr AND coalesce(v.view_current, true) "
        "RETURN v.view_payload AS payload",
        ns=namespace, attr=attribute,
    )
    if len(rows) != 1 or not rows[0]["payload"]:
        return []
    payload = json.loads(rows[0]["payload"])
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    return [float(e["value"]) for e in entries or [] if isinstance(e, dict) and e.get("value") is not None]


SCALAR_CRITERIA = [
    "correction_beats_ordinary_claim_at_equal_time",
    "ordinary_rewrite_never_downgrades_a_correction",
    "backdated_claim_does_not_overwrite_newer_value",
    "history_orders_backdated_entry_by_date",
    "expire_retires_the_current_view",
    "hedged_value_abstains",
    "lone_sample_proposal_abstains",
    "no_unscripted_model_prompt",
]


async def test_e2e_13_scalar_semantics(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e13s-{uuid4().hex[:12]}"
    reply = _proposed_once(envelope_reply(SCALAR_OBS), "plant_count")
    script = Script([Rule("scalar", SCALAR_PROMPT, reply)])
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
            stamp_history = _history_values(e2e_config, namespace, "stamp_count")

    first, second, third = snapshots
    lane_evidence.record(
        "correction_beats_ordinary_claim_at_equal_time",
        passed=_values(first, "owned_book_count") == [20.0]
        and _values(second, "owned_book_count") == [22.0],
        detail={"pass1": _values(first, "owned_book_count"),
                "pass2": _values(second, "owned_book_count")},
    )
    lane_evidence.record(
        "ordinary_rewrite_never_downgrades_a_correction",
        passed=_values(third, "owned_book_count") == [22.0],
        detail={"pass3": _values(third, "owned_book_count")},
    )
    lane_evidence.record(
        "backdated_claim_does_not_overwrite_newer_value",
        passed=_values(first, "stamp_count") == [50.0] and _values(second, "stamp_count") == [50.0]
        and any(r["attribute"] == "stamp_count" and float(r["value"]) == 40.0 for r in rows),
        detail={"pass1": _values(first, "stamp_count"), "pass2": _values(second, "stamp_count")},
    )
    lane_evidence.record(
        "history_orders_backdated_entry_by_date",
        passed=stamp_history == [40.0, 50.0],
        detail={"history_values": stamp_history},
    )
    lane_evidence.record(
        "expire_retires_the_current_view",
        passed=_values(first, "cat_count") == [3.0] and _values(second, "cat_count") == []
        and any(r["attribute"] == "cat_count" and r["operation"] == "expire" for r in rows),
        detail={"pass1": _values(first, "cat_count"), "pass2": _values(second, "cat_count")},
    )
    lane_evidence.record(
        "hedged_value_abstains",
        passed=not _values(third, "record_count")
        and not any(r["attribute"] == "record_count" for r in rows),
        detail=[r for r in rows if r["attribute"] == "record_count"],
    )
    plant_prompts = [c for c in script.calls("scalar") if "9 plants" in c.prompt]
    lane_evidence.record(
        "lone_sample_proposal_abstains",
        passed=len(plant_prompts) == 3 and not _values(third, "plant_count")
        and not any(r["attribute"] == "plant_count" for r in rows),
        detail={"plant_prompts": len(plant_prompts),
                "rows": [r for r in rows if r["attribute"] == "plant_count"]},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, SCALAR_CRITERIA)


# ---- event history ------------------------------------------------------------------------------

#: Unique substring of event_history_perception.EVENT_SYSTEM_PROMPT.
EVENT_PROMPT = "extract COMPLETED ACQUISITION EVENTS only"
EVENT_ON = {"MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED": "true"}
_EVENT_LINE = re.compile(r"^\[(\d+)\] (.*)$", re.MULTILINE)

#: phrase -> object. The scripted model proposes every one it sees; the lane's own gates decide.
EVENTS = {
    "I bought a kayak": "kayak",
    "I bought a lantern": "lantern",
    "I bought a tent": "tent",
    "I never bought a drone": "drone",        # negated: must be dropped
    "I want to buy a bike": "bike",           # intent: must be dropped
}

#: Pass 1: the kayak at its turn's time, the lantern at the date its own sentence states (before
#: the turn), and two claims that are not completed acquisitions. Pass 2: a tent bought BEFORE the
#: kayak, arriving after the kayak was folded.
EVENT_PASSES = [
    [("I bought a kayak at the lake shop.", "2026-07-10T10:00:00+00:00"),
     ("On 2026-06-15 I bought a lantern for camping.", "2026-07-12T10:00:00+00:00"),
     ("I never bought a drone, they are too loud.", "2026-07-11T10:00:00+00:00"),
     ("I want to buy a bike someday.", "2026-07-11T11:00:00+00:00")],
    [("I bought a tent at the outdoor store.", "2026-07-01T10:00:00+00:00")],
]


def _event_reply(call: Call) -> str:
    envelopes = []
    for index, text in _EVENT_LINE.findall(call.prompt):
        events = [{"subject": "user", "predicate": "bought", "object": obj, "object_display": "",
                   "domain": "", "when": "", "stated_span": phrase}
                  for phrase, obj in EVENTS.items() if phrase.lower() in text.lower()]
        envelopes.append({"episode": int(index), "events": events})
    return json.dumps(envelopes)


def _timelines(config: E2EConfig, namespace: str) -> list[dict]:
    rows = graph_query(
        config,
        "MATCH (v:Entity {view_kind: 'timeline'}) WHERE v.group_id = $ns "
        "AND coalesce(v.view_current, true) AND NOT coalesce(v.retired, false) "
        "RETURN v.uuid AS uuid, v.view_predicate AS predicate, v.view_value AS count, "
        "       v.view_payload AS payload",
        ns=namespace,
    )
    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else []
        row["entries"] = payload.get("entries", []) if isinstance(payload, dict) else payload
    return rows


def _objects(timeline: list[dict]) -> list[str]:
    return [str(e.get("object_key") or "") for e in timeline[0]["entries"]] if len(timeline) == 1 else []


EVENT_CRITERIA = [
    "one_current_timeline_view_per_lane",
    "backdated_event_is_placed_by_date",
    "source_date_in_the_sentence_sets_the_event_time",
    "negated_and_intended_acquisitions_are_dropped",
    "no_unscripted_model_prompt",
]


async def test_e2e_13_event_semantics(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e13e-{uuid4().hex[:12]}"
    script = Script([Rule("event", EVENT_PROMPT, _event_reply)])
    snapshots: list[list[dict]] = []
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **EVENT_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for number, turns in enumerate(EVENT_PASSES, start=1):
                    for text, at in turns:
                        await admit_turn(client, namespace, text,
                                         record_turn(e2e_config, namespace, text, occurred_at=at))
                    run_phase3(e2e_config, lane_evidence, namespace, label=f"phase3-pass{number}")
                    snapshots.append(_timelines(e2e_config, namespace))
            lane_evidence.attach("timelines-per-pass.json", json.dumps(snapshots, indent=2, default=str))

    first, second = snapshots
    lane_evidence.record(
        "one_current_timeline_view_per_lane",
        passed=len(first) == 1 and len(second) == 1
        and second[0]["predicate"] == "acquired" and int(second[0]["count"] or 0) == 3,
        detail={"pass1": [(t["uuid"], t["count"]) for t in first],
                "pass2": [(t["uuid"], t["count"]) for t in second]},
    )
    lane_evidence.record(
        "backdated_event_is_placed_by_date",
        passed=_objects(first) == ["lantern", "kayak"]
        and _objects(second) == ["lantern", "tent", "kayak"],
        detail={"pass1": _objects(first), "pass2": _objects(second)},
    )
    lantern = [e for e in (second[0]["entries"] if len(second) == 1 else [])
               if e.get("object_key") == "lantern"]
    lane_evidence.record(
        "source_date_in_the_sentence_sets_the_event_time",
        passed=len(lantern) == 1 and str(lantern[0].get("when", "")).startswith("2026-06-15")
        and lantern[0].get("time_basis") == "explicit",
        detail=lantern,
    )
    lane_evidence.record(
        "negated_and_intended_acquisitions_are_dropped",
        passed=bool(second) and not {"drone", "bike"} & set(_objects(second))
        and len(script.calls("event")) >= 2,
        detail={"objects": _objects(second), "event_calls": len(script.calls("event"))},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, EVENT_CRITERIA)
