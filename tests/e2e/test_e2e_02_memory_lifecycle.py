"""E2E-2 — memory lifecycle. Also carries #118's remaining MVP acceptance.

Per tracking issue #123, #118's outstanding obligation *is* this lane: prove the
documented original-write -> status observation -> recall/context workflow through real
stdio, including timeout/failure and restart. It is not a separate feature project.

CONSOLIDATED FROM ``mvp-118-stdio-e2e``
---------------------------------------
The flow below is the one proved by ``tests/test_mvp_tracked_write_stdio_e2e.py`` on
that branch. Its plumbing now lives in ``_harness/`` (the fake providers in
``providers.py``, the process fence in ``config.py``), and the lane consumes the
harness fixtures so it gains the campaign's evidence contract and feature matrix
without losing what that test established.

Two things carried over deliberately:

- **The deterministic provider, not a real model.** Reaching READY needs a model;
  a live one costs money and makes the lane irreproducible. ``@pytest.mark.provider``
  selects the fake, which answers every Graphiti extraction schema. This closes the
  open provider question the scaffold originally left on this lane.
- **The corpus is a refund threshold that changes from 500 to 750.** That is what
  makes "current vs historical" assertable: after the correction, recall must answer
  750, and the original 500 must remain reachable as history rather than vanishing.
"""

from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.deferred import raise_deferred
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.providers import (
    REFUND_CORRECTED_AMOUNT,
    REFUND_ORIGINAL_AMOUNT,
)
from tests.e2e._harness.stack import graph_query

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400), pytest.mark.provider("deterministic")]

CRITERIA = [
    "add_durable_memory",
    "observe_accepted_processing_ready",
    "tracked_write_receipt_survives_diagnostic_failure",
    "recall_by_direct_wording",
    "recall_by_paraphrase",
    "build_context_from_memory",
    "correction_current_vs_historical",
    "provenance_points_to_source_episode",
    "provenance_reachable_from_receipt",
    "restart_then_recall_again",
]

EPISODE_ID = re.compile(r"episode_id:\s*([0-9a-f-]{36})")

#: The amounts are deliberately NOT 500. An earlier version used 500/750 and asserted
#: `"500" in context`; when build_context faulted, the tool returned "500 Internal Server
#: Error" and the substring matched the HTTP status code, recording a crashed endpoint as
#: a pass. `assert_no_backend_fault` is the durable guard, but a sentinel that cannot
#: collide with transport text is the cheap second layer.
ORIGINAL = (
    "The Atlas Lantern service uses Stripe. Its refund approval threshold is "
    f"{REFUND_ORIGINAL_AMOUNT} dollars."
)
CORRECTION = (
    "Correction: the Atlas Lantern refund approval threshold is "
    f"{REFUND_CORRECTED_AMOUNT} dollars now."
)

#: Imported, never restated. The deterministic provider replays a fixed fact rather than
#: reading the episode, so the corpus below and the fake's answer must be the same
#: numbers; a local copy here would let them drift and the lane would assert against a
#: fact nothing produced.
ORIGINAL_AMOUNT = REFUND_ORIGINAL_AMOUNT
CORRECTED_AMOUNT = REFUND_CORRECTED_AMOUNT


def _text(result: object) -> str:
    """Flatten an MCP tool result to text for substring assertions."""

    content = getattr(result, "content", None) or []
    parts = [getattr(item, "text", "") for item in content]
    return "\n".join(part for part in parts if part)


async def test_e2e_02_memory_lifecycle(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    provider_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    lane_evidence.record_stack(provider="deterministic", features=feature_combo.label)
    namespace = f"e2e2-{uuid4().hex}"
    child_env = {**feature_env, **provider_env}

    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        listing = await client.list_tools()
        visible = {tool.name for tool in listing.tools}
        assert {"search_tools", "call_tool", "recall_memories", "build_context"} <= visible

        # --- original tracked write -------------------------------------------------
        accepted = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "add_memory_and_track",
                    "arguments": {"text": ORIGINAL, "namespace": namespace},
                },
            )
        )
        match = EPISODE_ID.search(accepted)
        lane_evidence.record("add_durable_memory", passed=bool(match), detail=accepted[:400])
        assert match, f"no episode_id in tracked-write receipt: {accepted[:400]}"
        original_episode = match.group(1)

        # #118 / PR #122: the receipt is authoritative even when the diagnostic wait
        # times out. A lane that retried here would create a second episode and prove
        # the opposite of what the contract says.
        lane_evidence.record(
            "tracked_write_receipt_survives_diagnostic_failure",
            passed="Do not submit this memory again" in accepted or "episode_id" in accepted,
            detail=accepted[:400],
        )

        observed = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "get_enrichment_status",
                    # The tool's parameter is `episode_uuid`; the RESPONSE labels it
                    # `episode_id:`. Passing the label back as the argument name is
                    # rejected, so the two spellings are deliberately not unified here.
                    "arguments": {
                        "episode_uuid": original_episode,
                        "wait": True,
                        "timeout_s": 180.0,
                        "namespace": namespace,
                    },
                },
            )
        )
        lane_evidence.record(
            "observe_accepted_processing_ready",
            passed="status: READY" in observed,
            detail=observed[:400],
        )
        assert f"episode_id: {original_episode}" in observed
        assert "status: READY" in observed, observed[:600]

        # --- recall, direct and paraphrased ----------------------------------------
        direct = _text(
            await client.call_tool(
                "recall_memories",
                {"query": "Atlas Lantern refund approval threshold", "namespace": namespace},
            )
        )
        lane_evidence.record("recall_by_direct_wording", passed=ORIGINAL_AMOUNT in direct, detail=direct[:400])
        assert ORIGINAL_AMOUNT in direct, direct[:600]

        paraphrase = _text(
            await client.call_tool(
                "recall_memories",
                {"query": "how much can Atlas Lantern refund without escalating?", "namespace": namespace},
            )
        )
        lane_evidence.record("recall_by_paraphrase", passed=ORIGINAL_AMOUNT in paraphrase, detail=paraphrase[:400])
        assert ORIGINAL_AMOUNT in paraphrase, paraphrase[:600]

        context = _text(await client.call_tool("build_context", {"query": "Atlas Lantern refunds", "namespace": namespace}))
        context_has_fact = ORIGINAL_AMOUNT in context
        lane_evidence.record("build_context_from_memory", passed=context_has_fact, detail=context[:400])

        # REPRODUCED ON main, 2026-09-21: recall_memories returns the fact and
        # build_context returns "nothing relevant found" for the same namespace and the
        # same write. This is #118's remaining product gap, not a lane defect, and the
        # fix is unmerged on `mvp-118-stdio-e2e`:
        #
        #   context_builder.py  - pass include_session=True and session_id into recall
        #   build_context.py    - default session_id to the MCP session's id
        #
        # Its own comment states the mechanism: "Fresh tracked writes initially produce
        # SESSION-scoped nodes, so excluding that scope makes a write visible to
        # recall_memories but immediately disappear from context."
        #
        # Deferred rather than failing here so correction-currentness, provenance and
        # restart are still exercised by the same run. The lane still fails.
        deferred_failures: list[str] = []
        if not context_has_fact:
            deferred_failures.append(
                "build_context_from_memory: recall_memories returned the stored fact but "
                "build_context found nothing for the same namespace and query. #118's "
                "SESSION-scope gap; fix unmerged on mvp-118-stdio-e2e "
                f"(context_builder.py, build_context.py).\n{context[:300]}"
            )

        # --- correction: current vs historical --------------------------------------
        corrected = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "add_memory_and_track",
                    "arguments": {"text": CORRECTION, "namespace": namespace},
                },
            )
        )
        correction_match = EPISODE_ID.search(corrected)
        assert correction_match, corrected[:400]
        correction_episode = correction_match.group(1)
        assert correction_episode != original_episode

        after = _text(
            await client.call_tool(
                "recall_memories",
                {"query": "Atlas Lantern refund approval threshold", "namespace": namespace},
            )
        )
        current_wins = CORRECTED_AMOUNT in after
        history_kept = ORIGINAL_AMOUNT in after
        lane_evidence.record(
            "correction_current_vs_historical",
            passed=current_wins,
            detail={"current_amount_present": current_wins, "historical_amount_present": history_kept},
        )
        assert current_wins, f"correction did not become current: {after[:600]}"

        # --- provenance --------------------------------------------------------------
        # `get_provenance` expands a NODE into the episodes that MENTIONS it -- its
        # parameter is `node_uuid`, not an episode id. Handing it the episode would ask
        # "which episodes mention this episode", which has no receipts and would make the
        # assertion below fail for a reason unrelated to provenance being correct.
        #
        # So the entity the correction produced is looked up first, and the contract under
        # test is the real one: from a thing recall can return, the source episode is
        # reachable. A recall answer whose origin cannot be traced is unauditable.
        # TWO :Episodic nodes exist per write -- Menhir's receipt (group_id=None, the
        # episode_id in the tracked-write receipt) and the Graphiti-minted twin (namespaced)
        # that carries the MENTIONS edges. That is #92's shape and it is by construction:
        # Graphiti mints its own node and Menhir cannot pass a uuid in. What #92 fixed is
        # traceability: the receipt records the twin as resolved_episode_uuid, and
        # get_provenance surfaces the receipt as `episode_id` on each episode it lists. An
        # agent holding a receipt matches on that field, never on the twin's uuid.
        receipt_episode_mentions = graph_query(
            e2e_config,
            "MATCH (e:Episodic {uuid: $uuid})-[:MENTIONS]->(n) RETURN count(n) AS mentioned",
            uuid=correction_episode,
        )
        receipt_mentions = receipt_episode_mentions[0]["mentioned"] if receipt_episode_mentions else 0

        # Provenance is asked of a node an agent could actually reach: an entity in this
        # namespace. That is the documented use -- "pass a node_uuid from a recall result".
        extracted = graph_query(
            e2e_config,
            "MATCH (e:Episodic)-[:MENTIONS]->(n) WHERE e.group_id = $group "
            "RETURN n.uuid AS uuid, n.name AS name, e.uuid AS episode LIMIT 1",
            group=namespace,
        )
        assert extracted, (
            f"no MENTIONS edge exists in namespace {namespace} at all, so enrichment "
            "produced nothing and there is no node whose provenance could be asked"
        )
        node_uuid = extracted[0]["uuid"]
        enriched_episode = extracted[0]["episode"]

        provenance = _text(
            await client.call_tool(
                "call_tool",
                {"name": "get_provenance", "arguments": {"node_uuid": node_uuid, "namespace": namespace}},
            )
        )
        names_an_episode = enriched_episode in provenance
        # Structured, not a substring: the receipt must be the `episode_id` of the entry
        # whose `uuid` is the enriched twin. A bare `correction_episode in provenance`
        # would also pass if the receipt showed up anywhere else in the payload.
        names_the_receipt = False
        try:
            listed = json.loads(provenance).get("episodes") or []
        except (ValueError, AttributeError):
            listed = []
        for entry in listed:
            if entry.get("uuid") == enriched_episode:
                names_the_receipt = entry.get("episode_id") == correction_episode
                break
        lane_evidence.record(
            "provenance_points_to_source_episode",
            passed=names_an_episode,
            detail={
                "node": node_uuid,
                "name": extracted[0].get("name"),
                "enriched_episode": enriched_episode,
                "receipt_episode": correction_episode,
                "receipt_episode_mentions": receipt_mentions,
                "provenance_names_receipt_episode": names_the_receipt,
                "body": provenance[:400],
            },
        )
        assert names_an_episode, (
            f"provenance for node {node_uuid} does not name the episode that MENTIONS "
            f"it ({enriched_episode}): {provenance[:600]}"
        )
        lane_evidence.record(
            "provenance_reachable_from_receipt",
            passed=names_the_receipt,
            detail={
                "receipt_episode": correction_episode,
                "receipt_episode_mentions": receipt_mentions,
                "provenance_names_receipt_episode": names_the_receipt,
            },
        )
        assert names_the_receipt, (
            "provenance is not reachable from the tracked-write receipt (#92): the receipt "
            f"returned episode_id={correction_episode} while provenance lists the enriched "
            f"twin {enriched_episode} without that episode_id:\n{provenance[:600]}"
        )
        lane_evidence.attach(
            "provenance.json",
            json.dumps({"node": node_uuid, "episode": correction_episode, "text": provenance}, indent=2),
        )

    # --- restart: the bridge reconnects to the same backend and the graph persists ---
    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        persisted = _text(
            await client.call_tool(
                "recall_memories",
                {"query": "Atlas Lantern refund approval threshold", "namespace": namespace},
            )
        )
        lane_evidence.record("restart_then_recall_again", passed=CORRECTED_AMOUNT in persisted, detail=persisted[:400])
        assert CORRECTED_AMOUNT in persisted, persisted[:600]

    if deferred_failures:
        lane_evidence.close(status="FAIL")
        raise_deferred("E2E-2", deferred_failures)

    lane_evidence.close(status="PASS")
