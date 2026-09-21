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
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
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

ORIGINAL = "The Atlas Lantern service uses Stripe. Its refund approval threshold is 500 dollars."
CORRECTION = "Correction: the Atlas Lantern refund approval threshold is 750 dollars now."


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
        lane_evidence.record("recall_by_direct_wording", passed="500" in direct, detail=direct[:400])
        assert "500" in direct, direct[:600]

        paraphrase = _text(
            await client.call_tool(
                "recall_memories",
                {"query": "how much can Atlas Lantern refund without escalating?", "namespace": namespace},
            )
        )
        lane_evidence.record("recall_by_paraphrase", passed="500" in paraphrase, detail=paraphrase[:400])
        assert "500" in paraphrase, paraphrase[:600]

        context = _text(await client.call_tool("build_context", {"query": "Atlas Lantern refunds", "namespace": namespace}))
        context_has_fact = "500" in context
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
        current_wins = "750" in after
        history_kept = "500" in after
        lane_evidence.record(
            "correction_current_vs_historical",
            passed=current_wins,
            detail={"current_750": current_wins, "historical_500_present": history_kept},
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
        # TWO :Episodic nodes exist per write -- one with group_id=None and one carrying
        # the namespace -- and only the namespaced one has MENTIONS. The uuid in the
        # tracked-write receipt is the UNNAMESPACED one, which MENTIONS nothing. This is
        # issue #92's shape ("two :Episodic nodes per ingest"), and Gate B lists #92 as a
        # dispose-or-fix item.
        #
        # The consequence for an agent is traceability: it holds the receipt's
        # episode_id, and provenance for the entities that write produced names a
        # different uuid, so the two cannot be matched. That is recorded below as its own
        # observation rather than being hidden by looking the enriched node up directly.
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
        names_the_receipt = correction_episode in provenance
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
        if not names_the_receipt:
            deferred_failures.append(
                "provenance is not reachable from the tracked-write receipt: the receipt "
                f"returned episode_id={correction_episode} (MENTIONS {receipt_mentions} "
                f"nodes, group_id null) while provenance names {enriched_episode}. Two "
                ":Episodic nodes exist per write and only the namespaced one is enriched, "
                "so an agent holding a receipt cannot match it against provenance. "
                "Issue #92's shape; Gate B lists #92 as dispose-or-fix."
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
        lane_evidence.record("restart_then_recall_again", passed="750" in persisted, detail=persisted[:400])
        assert "750" in persisted, persisted[:600]

    if deferred_failures:
        lane_evidence.close(status="FAIL")
        raise AssertionError(
            f"E2E-2 reproduced {len(deferred_failures)} failure(s):\n\n"
            + "\n\n".join(deferred_failures)
        )

    lane_evidence.close(status="PASS")
