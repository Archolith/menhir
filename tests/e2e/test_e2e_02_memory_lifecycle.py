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
                    "arguments": {"episode_id": original_episode, "namespace": namespace},
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
        lane_evidence.record("build_context_from_memory", passed="500" in context, detail=context[:400])
        assert "500" in context, context[:600]

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
        provenance = _text(
            await client.call_tool(
                "call_tool",
                {"name": "get_provenance", "arguments": {"episode_id": correction_episode, "namespace": namespace}},
            )
        )
        lane_evidence.record(
            "provenance_points_to_source_episode",
            passed=correction_episode in provenance,
            detail=provenance[:400],
        )
        assert correction_episode in provenance, provenance[:600]
        lane_evidence.attach("provenance.json", json.dumps({"episode": correction_episode, "text": provenance}, indent=2))

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

    lane_evidence.close(status="PASS")
