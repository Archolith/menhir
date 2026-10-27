"""E2E-10 - counting (single happenings) through the installed stack.

Same shape as E2E-9: a backend per lane with its own flags, turns captured over REST and
admitted through the stdio bridge, one ``POST /api/phase3/run`` per pass, then the phase3
response, the graph (read-only) and recall are checked.

The scripted model answers three prompts: typed-scalar extraction (which runs first on
the same turns), counting extraction, and, with match on, the count-decide prompt. Any
other plain chat prompt is logged as unmatched and fails the lane.
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
    DECIDE_PROMPT,
    SCALAR_PROMPT,
    Obs,
    Retelling,
    admit_turn,
    authority_verdicts,
    close_pass,
    decide_reply,
    envelope_reply,
    lane_backend,
    record_turn,
    run_phase3,
    scalar_views,
    typed_assertions,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1500)]

SCALAR_ON = {
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
}
COUNTING_ON = {**SCALAR_ON, "MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED": "true"}
# Related-count matching and extraction v2 are always on (#168 freeze): counting alone runs both.
MATCH_ON = COUNTING_ON

COIN = Obs("I have 37 coins", {"attribute": "owned_coin_count", "value_kind": "count",
                               "operation": "absolute", "value": 37})


def _occ(phrase: str, attribute: str, **extra: str) -> Obs:
    return Obs(phrase, {"attribute": attribute, "value_kind": "count", "operation": "occurrence",
                        "value": 1, "importance": "tracked", **extra})


GYM = [_occ("I went to the gym", "gym_visit_count")]
GYM_TURNS = [
    ("On Monday I went to the gym before work.", "2026-07-06T18:00:00+00:00"),
    ("Wednesday evening I went to the gym again.", "2026-07-08T18:00:00+00:00"),
]

KITS = [
    _occ("I finished the Gundam model kit", "model_kit_finished_count",
         object="model kit", action="finished", item="Gundam"),
    _occ("I finished the Zaku model kit", "model_kit_finished_count",
         object="model kit", action="finished", item="Zaku"),
    _occ("Like I said, I finished the Gundam model kit", "model_kit_finished_count",
         object="model kit", action="finished", item="Gundam"),
]
KIT_TURNS = [
    ("Today I finished the Gundam model kit.", "2026-07-01T18:00:00+00:00"),
    ("This weekend I finished the Zaku model kit.", "2026-07-05T18:00:00+00:00"),
    # The retelling: same happening as the first turn, told again a week later.
    ("Like I said, I finished the Gundam model kit last week.", "2026-07-08T18:00:00+00:00"),
]
RETELL = [Retelling(new="Like I said", target="Gundam")]


def _script(counting: list[Obs], *, scalar: list[Obs] = (), decide=None) -> Script:
    rules = [Rule("scalar", SCALAR_PROMPT, envelope_reply(list(scalar))),
             Rule("counting", COUNTING_PROMPT, envelope_reply(counting))]
    if decide is not None:
        rules.append(Rule("decide", DECIDE_PROMPT, decide))
    return Script(rules)


async def _capture(e2e_config: E2EConfig, client, namespace: str,
                   turns: list[tuple[str, str]]) -> None:
    for text, at in turns:
        await admit_turn(client, namespace, text,
                         record_turn(e2e_config, namespace, text, occurred_at=at))


def _user_message(payload: dict) -> str:
    return "\n".join(str(m.get("content") or "") for m in payload.get("messages") or []
                     if m.get("role") == "user")


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(getattr(item, "text", "") or "" for item in content)


def _finish(script: Script, evidence: LaneEvidence, criteria: list[str]) -> None:
    evidence.attach("model-calls.json", script.evidence())
    evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                    detail=script.unmatched[:5])
    close_pass(evidence, criteria)


def _occurrences(e2e_config: E2EConfig, namespace: str, evidence: LaneEvidence) -> list[dict]:
    rows = typed_assertions(e2e_config, namespace)
    evidence.attach("typed-assertions.json", json.dumps(rows, indent=2, default=str))
    return [r for r in rows if r["operation"] == "occurrence"]


COUNTING_CRITERIA = [
    "counting_stage_runs_after_scalar_with_k_samples",
    "each_happening_stored_once_as_a_current_occurrence",
    "counted_view_totals_the_happenings",
    "recall_leads_with_the_counted_total",
    "no_unscripted_model_prompt",
]


async def test_e2e_10_counting(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e10-{uuid4().hex[:12]}"
    script = _script(GYM)
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **COUNTING_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                await _capture(e2e_config, client, namespace, GYM_TURNS)
                result = run_phase3(e2e_config, lane_evidence, namespace)
                lane_evidence.record(
                    "counting_stage_runs_after_scalar_with_k_samples",
                    passed=bool(result.get("counting_enabled"))
                    and len(script.calls("scalar")) == 3 and len(script.calls("counting")) == 3
                    and result.get("counting_llm_calls") == 3,
                    detail={k: result.get(k) for k in (
                        "counting_enabled", "counting_llm_calls", "counting_occurrences_stored",
                        "counting_namespaces_failed")},
                )
                occ = _occurrences(e2e_config, namespace, lane_evidence)
                lane_evidence.record(
                    "each_happening_stored_once_as_a_current_occurrence",
                    passed=len(occ) == 2 and all(r["current"] and r["claim_ordinal"] == 1
                                                 for r in occ)
                    and result.get("counting_occurrences_stored") == 2,
                    detail=occ,
                )
                views = [v for v in scalar_views(e2e_config, namespace)
                         if v["attribute"] == "gym_visit_count"]
                lane_evidence.record(
                    "counted_view_totals_the_happenings",
                    passed=len(views) == 1 and float(views[0]["value"]) == 2.0
                    and views[0]["basis"] == "counted",
                    detail=views,
                )
                recalled = _text(await client.call_tool(
                    "recall_memories", {"query": "how many times did I go to the gym",
                                        "namespace": namespace}))
                lane_evidence.attach("recall.json", recalled)
                lead = [v for v in authority_verdicts(recalled)
                        if v.get("attribute") == "gym_visit_count"]
                lane_evidence.record(
                    "recall_leads_with_the_counted_total",
                    passed=len(lead) == 1 and str(lead[0].get("value")) == "2"
                    and lead[0].get("basis") == "counted",
                    detail=authority_verdicts(recalled),
                )
    _finish(script, lane_evidence, COUNTING_CRITERIA)


EXTRACT_V2_CRITERIA = [
    "captured_scalar_claims_are_fenced_in_the_counting_prompt",
    "occurrences_carry_the_v2_perceiver_version",
    "counted_view_totals_the_happenings",
    "no_unscripted_model_prompt",
]


async def test_e2e_10_counting_extract_v2(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e10x-{uuid4().hex[:12]}"
    script = _script(GYM, scalar=[COIN])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **COUNTING_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                await _capture(e2e_config, client, namespace, [
                    ("I have 37 coins in my jar.", "2026-07-05T10:00:00+00:00"), *GYM_TURNS])
            result = run_phase3(e2e_config, lane_evidence, namespace)

            # The system prompt describes the fence too, so look only at the user message.
            prompts = [_user_message(c.payload) for c in script.calls("counting")]
            fenced = all("<<<CAPTURED-" in p and "<<<END-CAPTURED-" in p for p in prompts)
            inside = all(
                "I have 37 coins" in p.split("<<<CAPTURED-", 1)[1].split("<<<END-CAPTURED-", 1)[0]
                for p in prompts) if fenced else False
            lane_evidence.record(
                "captured_scalar_claims_are_fenced_in_the_counting_prompt",
                passed=len(prompts) == 3 and fenced and inside,
                detail={"calls": len(prompts), "fenced": fenced, "claim_inside_fence": inside,
                        "prompt_head": prompts[0][:1200] if prompts else ""},
            )
            occ = _occurrences(e2e_config, namespace, lane_evidence)
            versions = sorted({str(r["perceiver_version"]) for r in occ})
            lane_evidence.record(
                "occurrences_carry_the_v2_perceiver_version",
                passed=len(occ) == 2 and versions == ["v1+x2+m"],
                detail={"versions": versions, "stored": result.get("counting_occurrences_stored")},
            )
            views = [v for v in scalar_views(e2e_config, namespace)
                     if v["attribute"] == "gym_visit_count"]
            lane_evidence.record(
                "counted_view_totals_the_happenings",
                passed=len(views) == 1 and float(views[0]["value"]) == 2.0,
                detail=views,
            )
    _finish(script, lane_evidence, EXTRACT_V2_CRITERIA)


MATCH_CRITERIA = [
    "decide_runs_k_samples_over_related_happenings",
    "retelling_is_stored_as_a_repeat_of_the_first_telling",
    "family_view_counts_distinct_happenings_only",
    "no_unscripted_model_prompt",
]


async def _match_pass(e2e_config, e2e_installed, lane_evidence, env, namespace, script):
    async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                             feature_env=env) as client:
        await _capture(e2e_config, client, namespace, KIT_TURNS)
    return run_phase3(e2e_config, lane_evidence, namespace)


def _family_view(e2e_config: E2EConfig, namespace: str) -> list[dict]:
    return [v for v in scalar_views(e2e_config, namespace) if v["attribute"] == "model_kit_count"]


async def test_e2e_10_count_match_decide(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e10m-{uuid4().hex[:12]}"
    script = _script(KITS, decide=decide_reply(RETELL, family="model kit"))
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **MATCH_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            result = await _match_pass(e2e_config, e2e_installed, lane_evidence, env,
                                       namespace, script)
            decide_calls = script.calls("decide")
            lane_evidence.record(
                "decide_runs_k_samples_over_related_happenings",
                passed=len(decide_calls) == 3
                and all("Like I said" in c.prompt for c in decide_calls),
                detail={"decide_calls": len(decide_calls),
                        **{k: result.get(k) for k in result if k.startswith("counting_decided")}},
            )
            occ = _occurrences(e2e_config, namespace, lane_evidence)
            by_span = {}
            for row in occ:
                by_span.setdefault(row["span"], []).append(row)
            gundam = by_span.get("I finished the Gundam model kit", [])
            first = min(gundam, key=lambda r: r["id"]) if gundam else None
            statuses = sorted(str(r["status"]) for r in occ)
            repeat = [r for r in occ if r["status"] == "repeat"]
            lane_evidence.record(
                "retelling_is_stored_as_a_repeat_of_the_first_telling",
                passed=len(occ) == 3 and statuses == ["counted", "counted", "repeat"]
                and len(repeat) == 1 and repeat[0]["target"] in {r["id"] for r in gundam}
                and repeat[0]["target"] != repeat[0]["id"]
                and (result.get("counting_decided_repeat") or 0) >= 1,
                detail={"statuses": statuses, "first": first, "repeat": repeat},
            )
            family = _family_view(e2e_config, namespace)
            lane_evidence.record(
                "family_view_counts_distinct_happenings_only",
                passed=len(family) == 1 and float(family[0]["value"]) == 2.0
                and family[0]["basis"] == "counted" and not family[0]["count_uncertain"],
                detail={"family": family, "all_views": scalar_views(e2e_config, namespace)},
            )
    _finish(script, lane_evidence, MATCH_CRITERIA)


FAMILY_RECALL_CRITERIA = [
    "recall_leads_with_the_family_view",
    "family_verdict_lists_the_distinct_items",
    "raw_slot_is_not_a_second_verdict",
    "no_unscripted_model_prompt",
]


async def test_e2e_10_family_view_recall(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e10f-{uuid4().hex[:12]}"
    script = _script(KITS, decide=decide_reply(RETELL, family="model kit"))
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **MATCH_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            await _match_pass(e2e_config, e2e_installed, lane_evidence, env, namespace, script)
            family = _family_view(e2e_config, namespace)
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                recalled = _text(await client.call_tool(
                    "recall_memories", {"query": "how many model kits have I finished",
                                        "namespace": namespace}))
            lane_evidence.attach("recall.json", recalled)
            verdicts = authority_verdicts(recalled)
            lead = [v for v in verdicts if v.get("attribute") == "model_kit_count"]
            lane_evidence.record(
                "recall_leads_with_the_family_view",
                passed=len(lead) == 1 and str(lead[0].get("value")) == "2"
                and lead[0].get("basis") == "counted" and bool(family)
                and lead[0].get("view_uuid") == family[0]["uuid"],
                detail=verdicts,
            )
            # count_items is a JSON-encoded list by contract (ScalarAuthorityVerdict.count_items:
            # str | None; context_builder json.loads it).
            raw_items = lead[0].get("count_items") if lead else None
            items = [str(i).lower() for i in (json.loads(raw_items) if raw_items else [])]
            lane_evidence.record(
                "family_verdict_lists_the_distinct_items",
                passed=sorted(items) == ["gundam", "zaku"],
                detail={"count_items": items},
            )
            lane_evidence.record(
                "raw_slot_is_not_a_second_verdict",
                passed=not any(v.get("attribute") == "model_kit_finished_count" for v in verdicts),
                detail=[v.get("attribute") for v in verdicts],
            )
    _finish(script, lane_evidence, FAMILY_RECALL_CRITERIA)


SWEEP_CRITERIA = [
    "counted_view_written_with_counting_on",
    "turning_counting_off_retires_the_counted_view",
    "scalar_views_survive_the_sweep",
    "turning_counting_back_on_restores_it_without_model_calls",
    "no_unscripted_model_prompt",
]


async def test_e2e_10_counting_flag_sweep(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    # The sweep is the LLM-free pass that rebuilds a namespace's Views when the counting
    # flag no longer matches the marker the last pass left. Three backends, one graph.
    namespace = f"e2e10s-{uuid4().hex[:12]}"
    script = _script(GYM, scalar=[COIN])

    def gym_and_coins() -> tuple[list[dict], list[dict]]:
        views = scalar_views(e2e_config, namespace)
        return ([v for v in views if v["attribute"] == "gym_visit_count"],
                [v for v in views if v["attribute"] == "owned_coin_count"])

    with scripted_model(script) as model:
        on = {**feature_env, **model.env, **COUNTING_ON}
        off = {**on, "MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED": "false"}

        with lane_backend(e2e_config, e2e_installed, lane_evidence, on, name="backend-on"):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=on) as client:
                await _capture(e2e_config, client, namespace, [
                    ("I have 37 coins in my jar.", "2026-07-05T10:00:00+00:00"), *GYM_TURNS])
            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-on")
        gym, _coins = gym_and_coins()
        lane_evidence.record("counted_view_written_with_counting_on",
                             passed=len(gym) == 1 and float(gym[0]["value"]) == 2.0,
                             detail=gym)

        with lane_backend(e2e_config, e2e_installed, lane_evidence, off, name="backend-off"):
            run_phase3(e2e_config, lane_evidence, namespace, label="phase3-off")
        gym_off, coins_off = gym_and_coins()
        lane_evidence.record("turning_counting_off_retires_the_counted_view",
                             passed=not gym_off, detail=gym_off)
        lane_evidence.record("scalar_views_survive_the_sweep",
                             passed=len(coins_off) == 1 and float(coins_off[0]["value"]) == 37.0,
                             detail=coins_off)

        before = len(script.calls("counting"))
        with lane_backend(e2e_config, e2e_installed, lane_evidence, on, name="backend-on-again"):
            again = run_phase3(e2e_config, lane_evidence, namespace, label="phase3-on-again")
        gym_again, _ = gym_and_coins()
        lane_evidence.record(
            "turning_counting_back_on_restores_it_without_model_calls",
            passed=len(gym_again) == 1 and float(gym_again[0]["value"]) == 2.0
            and len(script.calls("counting")) == before and again.get("counting_llm_calls") == 0,
            detail={"views": gym_again, "counting_llm_calls": again.get("counting_llm_calls")},
        )
    _finish(script, lane_evidence, SWEEP_CRITERIA)
