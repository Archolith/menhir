"""E2E-11 - under count match, scalar history is keyed by family and leaves out repeats.

Three turns: the Gundam kit, the Zaku kit, and a retelling of the Gundam kit that decide
marks as a repeat. The counted View is the family slot ``model_kit_count`` = 2. Its history
View must be on the same slot and list the two counted happenings only. Before the change,
history kept the raw extracted slot (``model_kit_finished_count``) and listed the retelling.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.phase3 import decide_reply, lane_backend, scalar_views
from tests.e2e._harness.scripted_provider import scripted_model
from tests.e2e._harness.stack import graph_query
from tests.e2e.test_e2e_10_counting import (
    KITS,
    MATCH_ON,
    RETELL,
    _family_view,
    _finish,
    _match_pass,
    _occurrences,
    _script,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200)]

CRITERIA = [
    "retelling_is_decided_a_repeat",
    "history_view_is_on_the_family_slot",
    "history_lists_the_counted_happenings_only",
    "no_raw_slot_history_view",
    "no_unscripted_model_prompt",
]


async def test_e2e_11_count_match_history(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e11h-{uuid4().hex[:12]}"
    script = _script(KITS, decide=decide_reply(RETELL, family="model kit"))
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **MATCH_ON,
               "MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED": "true"}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            await _match_pass(e2e_config, e2e_installed, lane_evidence, env, namespace, script)

            occ = _occurrences(e2e_config, namespace, lane_evidence)
            statuses = sorted(str(r["status"]) for r in occ)
            lane_evidence.record(
                "retelling_is_decided_a_repeat",
                passed=statuses == ["counted", "counted", "repeat"],
                detail=statuses,
            )

            family = _family_view(e2e_config, namespace)
            history = scalar_views(e2e_config, namespace, kind="scalar_history")
            lane_evidence.record(
                "history_view_is_on_the_family_slot",
                passed=len(family) == 1 and [h["attribute"] for h in history] == ["model_kit_count"],
                detail={"history": history, "state": scalar_views(e2e_config, namespace)},
            )

            rows = graph_query(
                e2e_config,
                "MATCH (v:Entity {view_kind: 'scalar_history'}) WHERE v.group_id = $ns "
                "AND coalesce(v.view_current, true) AND v.ss_attribute = 'model_kit_count' "
                "RETURN v.sh_entry_count AS entries, v.view_payload AS payload",
                ns=namespace,
            )
            lane_evidence.attach("history-views.json", json.dumps(rows, indent=2, default=str))
            payload = str(rows[0]["payload"] or "") if rows else ""
            lane_evidence.record(
                "history_lists_the_counted_happenings_only",
                passed=len(rows) == 1 and rows[0]["entries"] == 2
                and "Zaku" in payload and "Gundam" in payload and "Like I said" not in payload,
                detail={"entries": rows[0]["entries"] if rows else None,
                        "payload": payload[:1500]},
            )
            lane_evidence.record(
                "no_raw_slot_history_view",
                passed=not any(h["attribute"] == "model_kit_finished_count" for h in history),
                detail=[h["attribute"] for h in history],
            )
    _finish(script, lane_evidence, CRITERIA)
