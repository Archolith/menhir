"""E2E-9 - typed scalar state through the installed stack.

Each lane starts its own backend with the scalar flags it needs (layered over the feature
combo). It captures user turns through ``POST /api/turn-evidence`` as a host hook does,
admits a memory on each through the stdio bridge as an agent does, runs ``POST
/api/phase3/run``, and checks the result three ways: the phase3 response, the durable graph
(read-only), and the MCP tools an agent would use.

The model is the scripted fake: it answers the typed-scalar prompt with fixed
observations for the turns it is shown, and every other chat prompt falls through to the
deterministic Graphiti answers. A plain chat prompt the script does not recognise is
logged as unmatched and fails the lane.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
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
    authority_verdicts,
    close_pass,
    envelope_reply,
    lane_backend,
    record_turn,
    run_phase3,
    scalar_views,
    typed_assertions,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model
from tests.e2e._harness.stack import graph_query

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200)]

SCALAR_ON = {
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED": "true",
}

COINS = [
    Obs("I have 37 coins", {"attribute": "owned_coin_count", "value_kind": "count",
                            "operation": "absolute", "value": 37}),
    Obs("I bought 5 more coins", {"attribute": "owned_coin_count", "value_kind": "count",
                                  "operation": "delta", "value": 5}),
]
COIN_TURNS = [
    ("I have 37 coins in my jar.", "2026-07-01T10:00:00+00:00"),
    ("Yesterday I bought 5 more coins at the fair.", "2026-07-03T10:00:00+00:00"),
]


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(getattr(item, "text", "") or "" for item in content)


def _no_unmatched(script: Script, evidence: LaneEvidence) -> bool:
    evidence.attach("model-calls.json", script.evidence())
    return not script.unmatched


async def _capture(e2e_config: E2EConfig, client, namespace: str,
                   turns: list[tuple[str, str]]) -> list[str]:
    ids = []
    for text, at in turns:
        turn = record_turn(e2e_config, namespace, text, occurred_at=at)
        await admit_turn(client, namespace, text, turn)
        ids.append(turn)
    return ids


SCALAR_CRITERIA = [
    "phase3_runs_scalar_stage_with_k_samples",
    "absolute_then_delta_folds_to_current_view",
    "assertions_are_current_and_agent_tier",
    "recall_returns_the_current_value",
    "no_unscripted_model_prompt",
]


async def test_e2e_09_scalar_state(
    e2e_config: E2EConfig,
    e2e_installed,
    fresh_graph,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e9-{uuid4().hex[:12]}"
    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(COINS))])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **SCALAR_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                await _capture(e2e_config, client, namespace, COIN_TURNS)
                result = run_phase3(e2e_config, lane_evidence, namespace, k=3)

                calls = script.calls("scalar")
                lane_evidence.record(
                    "phase3_runs_scalar_stage_with_k_samples",
                    passed=bool(result.get("scalar_enabled")) and len(calls) == 3
                    and result.get("scalar_llm_calls") == 3,
                    detail={"scalar_enabled": result.get("scalar_enabled"),
                            "scalar_llm_calls": result.get("scalar_llm_calls"),
                            "model_calls": len(calls)},
                )

                views = scalar_views(e2e_config, namespace)
                coins = [v for v in views if v["attribute"] == "owned_coin_count"]
                lane_evidence.record(
                    "absolute_then_delta_folds_to_current_view",
                    passed=len(coins) == 1 and float(coins[0]["value"]) == 42.0
                    and result.get("scalar_states_written", 0) >= 1,
                    detail={"views": views,
                            "scalar_states_written": result.get("scalar_states_written")},
                )

                rows = typed_assertions(e2e_config, namespace)
                lane_evidence.attach("typed-assertions.json", json.dumps(rows, indent=2, default=str))
                ops = sorted((r["operation"], float(r["value"])) for r in rows if r["current"])
                lane_evidence.record(
                    "assertions_are_current_and_agent_tier",
                    passed=ops == [("absolute", 37.0), ("delta", 5.0)]
                    and all(r["claim_ordinal"] == 0 for r in rows)
                    and all(r["evidence_tier"] == "agent" for r in rows),
                    detail={"current_ops": ops, "rows": len(rows),
                            "tiers": sorted({str(r["evidence_tier"]) for r in rows})},
                )

                recalled = _text(await client.call_tool(
                    "recall_memories", {"query": "how many coins do I have", "namespace": namespace}))
                lane_evidence.attach("recall.json", recalled)
                verdicts = authority_verdicts(recalled)
                lead = [v for v in verdicts if v.get("attribute") == "owned_coin_count"]
                lane_evidence.record(
                    "recall_returns_the_current_value",
                    passed=len(lead) == 1 and lead[0].get("kind") == "current"
                    and str(lead[0].get("value")) == "42" and lead[0].get("status") == "leads"
                    and bool(coins) and lead[0].get("view_uuid") == coins[0]["uuid"],
                    detail=verdicts,
                )

    lane_evidence.record("no_unscripted_model_prompt", passed=_no_unmatched(script, lane_evidence),
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, SCALAR_CRITERIA)


ROUTER_CRITERIA = [
    "promoted_class_is_routed_without_the_model",
    "unpromoted_turn_still_goes_to_the_model",
    "routed_and_model_views_both_written",
    "shadow_compares_without_changing_the_result",
    "no_unscripted_model_prompt",
]

STAMPS = [Obs("I have 12 stamps", {"attribute": "stamps", "value_kind": "count",
                                   "operation": "absolute", "value": 12})]


def _shadow_events(db: Path) -> list[dict]:
    """The deterministic-shadow audit events the backend recorded (read-only)."""
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT status, details_json FROM lifecycle_events "
            "WHERE event = 'deterministic_shadow' ORDER BY id").fetchall()
    finally:
        conn.close()
    return [{"status": status, "details": json.loads(details) if details else None}
            for status, details in rows]


async def test_e2e_09_scalar_router_and_shadow(
    e2e_config: E2EConfig,
    e2e_installed,
    fresh_graph,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    # "I weigh 80 kg" is a c_measurement surface; with that class promoted the router writes
    # it with no model call. "I have 12 stamps" is c_count, eligible but not promoted, so it
    # must still reach the model.
    namespace = f"e2e9r-{uuid4().hex[:12]}"
    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(STAMPS))])
    # The shadow's only success signal is a consolidation-audit event, so audit is on and its
    # telemetry DB is this lane's own file.
    telemetry_db = lane_evidence.directory / "telemetry.db"
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **SCALAR_ON,
               "MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_ROUTER": "true",
               "MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_CLASSES": "c_measurement",
               "MENHIR_SCALAR_DETERMINISTIC_SHADOW": "true",
               "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED": "true",
               "MENHIR_MCP_TELEMETRY_DB": str(telemetry_db)}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                await _capture(e2e_config, client, namespace, [
                    ("I weigh 80 kg.", "2026-07-01T10:00:00+00:00"),
                    ("I have 12 stamps.", "2026-07-02T10:00:00+00:00"),
                ])
            result = run_phase3(e2e_config, lane_evidence, namespace, k=3)

            calls = script.calls("scalar")
            prompts = [call.prompt for call in calls]
            lane_evidence.record(
                "promoted_class_is_routed_without_the_model",
                passed=bool(calls) and not any("80 kg" in p for p in prompts),
                detail={"model_calls": len(calls)},
            )
            lane_evidence.record(
                "unpromoted_turn_still_goes_to_the_model",
                passed=len(calls) == 3 and all("12 stamps" in p for p in prompts),
                detail={"scalar_llm_calls": result.get("scalar_llm_calls")},
            )
            views = {v["attribute"]: v for v in scalar_views(e2e_config, namespace)}
            weight, stamps = views.get("weight"), views.get("stamps")
            lane_evidence.record(
                "routed_and_model_views_both_written",
                passed=bool(weight) and float(weight["value"]) == 80.0
                and bool(stamps) and float(stamps["value"]) == 12.0,
                detail={"views": list(views.values())},
            )
            # The shadow is telemetry only: with it on, both Views are still written and the
            # backend must not have failed the comparison.
            log = (lane_evidence.directory / "backend.log").read_text(encoding="utf-8",
                                                                      errors="replace")
            failed = "deterministic scalar shadow failed" in log
            shadow = _shadow_events(telemetry_db)
            lane_evidence.attach("shadow-events.json", json.dumps(shadow, indent=2, default=str))
            lane_evidence.record(
                "shadow_compares_without_changing_the_result",
                passed=not failed and len(views) == 2 and bool(shadow)
                and all(e["status"] == "ok" for e in shadow),
                detail={"shadow_failed_in_log": failed,
                        "shadow_events": [e["status"] for e in shadow]},
            )

    lane_evidence.record("no_unscripted_model_prompt", passed=_no_unmatched(script, lane_evidence),
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, ROUTER_CRITERIA)


REPAIR_CRITERIA = [
    "unbindable_subject_is_stored_advisory_without_a_view",
    "enrichment_creates_the_subject_entity",
    "repair_pass_binds_and_writes_the_view_without_model_calls",
    "no_unscripted_model_prompt",
]

#: "Stripe" is one of the entities the deterministic Graphiti answers always extract, so the
#: subject has no entity until a memory is enriched, and exactly one afterwards.
STRIPE = [Obs("Stripe has 3 offices", {"subject": "Stripe", "attribute": "office_count",
                                       "value_kind": "count", "operation": "absolute",
                                       "value": 3})]


async def test_e2e_09_scalar_binding_repair(
    e2e_config: E2EConfig,
    e2e_installed,
    fresh_graph,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e9b-{uuid4().hex[:12]}"
    text = "Stripe has 3 offices, as far as I know."
    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(STRIPE))])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **SCALAR_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            # Pass 1: the turn is captured but nothing has been enriched, so no entity named
            # Stripe exists in the namespace and the claim cannot be bound.
            turn = record_turn(e2e_config, namespace, text, occurred_at="2026-07-01T10:00:00+00:00")
            first = run_phase3(e2e_config, lane_evidence, namespace, label="phase3-unbound")
            rows = typed_assertions(e2e_config, namespace)
            views = [v for v in scalar_views(e2e_config, namespace)
                     if v["attribute"] == "office_count"]
            lane_evidence.record(
                "unbindable_subject_is_stored_advisory_without_a_view",
                passed=first.get("scalar_advisory", 0) >= 1 and len(rows) == 1
                and bool(rows[0]["binding_pending"]) and not views
                and first.get("scalar_repair_pending", 0) >= 1,
                detail={"rows": rows, "views": views,
                        **{k: first.get(k) for k in ("scalar_advisory", "scalar_repair_pending")}},
            )

            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                admitted = await admit_turn(client, namespace, text, turn)
                found = re.search(r"episode_id[=:]\s*([0-9a-f-]{36})", admitted)
                assert found, admitted[:400]
                episode = found.group(1)
                status = _text(await client.call_tool("call_tool", {
                    "name": "get_enrichment_status",
                    "arguments": {"episode_uuid": episode, "wait": True, "timeout_s": 180.0,
                                  "namespace": namespace}}))
            entities = graph_query(
                e2e_config,
                "MATCH (n:Entity) WHERE n.group_id = $ns AND toLower(n.name) = 'stripe' "
                "AND NOT coalesce(n.is_view, false) RETURN n.uuid AS uuid",
                ns=namespace,
            )
            lane_evidence.record(
                "enrichment_creates_the_subject_entity",
                passed="status: READY" in status and len(entities) == 1,
                detail={"status": status[:300], "entities": entities},
            )

            # Pass 2: no new turns, so no extraction; the repair pass alone must bind the row
            # to the new entity and write its View.
            calls_before = len(script.calls("scalar"))
            second = run_phase3(e2e_config, lane_evidence, namespace, label="phase3-repair")
            rows = typed_assertions(e2e_config, namespace)
            views = [v for v in scalar_views(e2e_config, namespace)
                     if v["attribute"] == "office_count"]
            lane_evidence.record(
                "repair_pass_binds_and_writes_the_view_without_model_calls",
                passed=len(views) == 1 and float(views[0]["value"]) == 3.0
                and bool(entities) and views[0]["subject_uuid"] == entities[0]["uuid"]
                and not any(r["binding_pending"] for r in rows if r["current"])
                and second.get("scalar_repair_pending") == 0
                and len(script.calls("scalar")) == calls_before,
                detail={"views": views, "rows": rows,
                        "scalar_repair_pending": second.get("scalar_repair_pending"),
                        "scalar_llm_calls": second.get("scalar_llm_calls")},
            )

    lane_evidence.record("no_unscripted_model_prompt", passed=_no_unmatched(script, lane_evidence),
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, REPAIR_CRITERIA)


HISTORY_CRITERIA = [
    "history_view_lists_every_assertion_in_order",
    "state_view_and_history_view_share_the_slot",
    "view_items_tool_lists_the_contributors",
    "no_unscripted_model_prompt",
]


async def test_e2e_09_scalar_history(
    e2e_config: E2EConfig,
    e2e_installed,
    fresh_graph,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e9h-{uuid4().hex[:12]}"
    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(COINS))])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **SCALAR_ON,
               "MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED": "true"}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                await _capture(e2e_config, client, namespace, COIN_TURNS)
                run_phase3(e2e_config, lane_evidence, namespace, k=3)

                history = scalar_views(e2e_config, namespace, kind="scalar_history")
                state = scalar_views(e2e_config, namespace)
                rows = graph_query(
                    e2e_config,
                    "MATCH (v:Entity {view_kind: 'scalar_history'}) WHERE v.group_id = $ns "
                    "AND coalesce(v.view_current, true) "
                    "RETURN v.sh_entry_count AS entries, v.view_payload AS payload",
                    ns=namespace,
                )
                lane_evidence.attach("history-views.json", json.dumps(rows, indent=2, default=str))
                payload = json.loads(rows[0]["payload"]) if rows and rows[0]["payload"] else {}
                entries = payload.get("entries") if isinstance(payload, dict) else payload
                values = [float(e.get("value")) for e in (entries or [])
                          if isinstance(e, dict) and e.get("value") is not None]
                lane_evidence.record(
                    "history_view_lists_every_assertion_in_order",
                    passed=len(rows) == 1 and rows[0]["entries"] == 2 and values == [37.0, 5.0],
                    detail={"entries": rows[0]["entries"] if rows else None, "values": values},
                )
                lane_evidence.record(
                    "state_view_and_history_view_share_the_slot",
                    passed=len(history) == 1 and len(state) == 1
                    and history[0]["attribute"] == state[0]["attribute"] == "owned_coin_count"
                    and float(state[0]["value"]) == 42.0,
                    detail={"history": history, "state": state},
                )

                items = _text(await client.call_tool("call_tool", {
                    "name": "get_scalar_view_items",
                    "arguments": {"view_uuid": state[0]["uuid"] if state else "",
                                  "namespace": namespace}}))
                lane_evidence.attach("view-items.txt", items)
                lane_evidence.record(
                    "view_items_tool_lists_the_contributors",
                    passed="I have 37 coins" in items and "I bought 5 more coins" in items,
                    detail=items[:800],
                )

    lane_evidence.record("no_unscripted_model_prompt", passed=_no_unmatched(script, lane_evidence),
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, HISTORY_CRITERIA)
