"""E2E-7 - restart and interrupted work.

"No false READY" is the criterion this lane exists for. A process killed mid-enrichment
must not leave a record that claims completion, and the recovered state must be one of:
eventual READY, explicit FAILED, or a documented recoverable state. "Probably finished"
is not one of them.

WHAT "NO FALSE READY" MEANS HERE, CONCRETELY
--------------------------------------------
Reading the status string alone cannot decide this. READY is the correct answer if the
work genuinely completed before the kill, and the wrong answer if the record was marked
complete by a writer that never finished. The two are indistinguishable from the label.

So the lane asserts the label against the graph: if the episode says READY, then
``(:Episodic {uuid})-[:MENTIONS]->()`` must hold at least one edge. An episode claiming
completion while having extracted nothing is precisely the false READY -- a record a
later recall would treat as authoritative and empty.

The converse is also checked. An episode still marked PROCESSING after the owning
process is gone is only acceptable while something can still move it; the lane waits for
a terminal state and fails if the record sits in an in-flight state with no owner, since
that is a stall dressed up as progress.

WHY kill() AND NOT terminate()
------------------------------
``terminate()`` lets the process flush, drain and mark in-flight work FAILED -- which is
the behaviour under test. Using it would prove the recovery path works when the process
was allowed to prepare for it, which is not the scenario that produces a false READY.
``BackendProcess.kill()`` takes no shutdown path on either platform.
"""

from __future__ import annotations

import re
import time
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session, wait_for_project_indexed
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.stack import graph_query, start_backend

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400), pytest.mark.provider("deterministic")]

CRITERIA = [
    "kill_during_queued_enrichment",
    "no_false_ready_after_restart",
    "eventual_ready_or_explicit_failed_or_documented_recoverable",
    "restart_recovers_structure_artifacts_todos",
    "idempotent_rerun_of_same_fixture",
]

EPISODE_ID = re.compile(r"episode_id[=:]\s*([0-9a-f-]{36})")
STATUS = re.compile(r"^status:\s*(\S+)", re.MULTILINE)

#: States that are a legitimate resting place after a crash. Anything else -- in
#: particular an in-flight state that never moves -- is a stall being reported as work.
TERMINAL_STATES = {"READY", "FAILED"}

#: States the product documents as recoverable, i.e. something is still expected to move
#: them. They are acceptable transiently and unacceptable as a final answer.
#:
#: ENRICHING is taken from `domain/models.py:38`, not guessed. It was missing from an
#: earlier version of this set, so a perfectly ordinary in-flight episode was reported as
#: an unrecognized state -- the diagnostic was wrong even though the verdict was right.
IN_FLIGHT_STATES = {"PENDING", "QUEUED", "ENRICHING", "PROCESSING", "RETRY", "UNKNOWN"}

#: How long to let the restarted backend finish work the kill interrupted.
#:
#: Generous on purpose. The local Windows run settled inside 300s; the CI runner did not,
#: and "still ENRICHING at the deadline" cannot distinguish a stalled episode from a slow
#: one. Failing on the short window would have reported a stall that was not there.
SETTLE_TIMEOUT_S = 600.0

MEMORY = (
    "The Kestrel billing reconciler retries failed charges three times before parking "
    "them in the dead-letter queue for manual review."
)


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    parts = [getattr(item, "text", "") for item in content]
    return "\n".join(part for part in parts if part)


def _state(status_body: str) -> str:
    match = STATUS.search(status_body)
    return match.group(1).upper() if match else "UNPARSEABLE"


async def test_e2e_07_restart_interruption(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    e2e_fixture_repo,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    provider_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    lane_evidence.record_stack(
        features=feature_combo.label, provider="deterministic", **e2e_fixture_repo.as_evidence()
    )
    namespace = f"e2e7-{uuid4().hex}"
    project = f"shop-{uuid4().hex[:8]}"
    child_env = {**feature_env, **provider_env}

    # --- establish state that must survive, then queue work and kill mid-flight -------
    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        ingest = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "ingest_project",
                    "arguments": {
                        "path": str(e2e_fixture_repo.path),
                        "name": project,
                        "namespace": namespace,
                        # CF-257: the fixture carries no identity file on its first scan.
                        # The re-ingest later in this lane deliberately omits this -- by
                        # then the identity file exists, and an explicit action there
                        # would mint a NEW id and prune the first scan's files, which is
                        # the opposite of the idempotency being asserted.
                        "identity_action": "new",
                    },
                },
            )
        )
        assert ingest.startswith("Scanned "), ingest[:400]

        # ingest_project can return before the graph write lands -- its own
        # formatter says "Graph write running in background" while still opening
        # "Scanned <project>:", so the receipt cannot distinguish the two. Wait for
        # the read surface an agent would query.
        await wait_for_project_indexed(client, project)

        todo_receipt = _text(
            await client.call_tool(
                "add_todo",
                {
                    "text": "Survive the crash and still be here afterwards.",
                    "code_ref": "src/shop/service.py:20",
                    "structure_project": project,
                    "namespace": namespace,
                },
            )
        )
        todo_uuid = next(t[len("uuid=") :] for t in todo_receipt.split() if t.startswith("uuid="))

        # `add_memory`, not `add_memory_and_track`: the tracked variant waits for the
        # episode to settle, which would close the very window this lane needs open.
        queued = _text(
            await client.call_tool(
                "add_memory", {"text": MEMORY, "namespace": namespace}
            )
        )
        match = EPISODE_ID.search(queued)
        assert match, f"no episode_id in add_memory receipt: {queued[:400]}"
        episode = match.group(1)
        queued_pending = "state=PENDING" in queued

    # The kill lands here, with the episode queued and the owning process mid-flight.
    log_before = running_stack.kill()
    died_ungracefully = not running_stack.is_alive()
    lane_evidence.record(
        "kill_during_queued_enrichment",
        passed=died_ungracefully and queued_pending,
        detail={
            "episode": episode,
            "queued_state_pending": queued_pending,
            "backend_alive_after_kill": running_stack.is_alive(),
            "exit_code": running_stack.process.returncode,
        },
    )
    lane_evidence.attach("backend-before-kill.log", log_before)
    assert died_ungracefully, "backend survived kill()"
    assert queued_pending, f"episode was not pending at kill time: {queued[:400]}"

    # --- restart -----------------------------------------------------------------------
    # The restarted backend must be able to enrich, same as the killed one. Accepting a
    # degraded replacement would let the lane conclude "the episode never recovered"
    # when the truth is that nothing was left running to recover it.
    restarted = start_backend(
        e2e_config,
        e2e_installed,
        log_path=lane_evidence.directory / "backend-after-restart.log",
        feature_env=child_env,
        require_enrichment=True,
    )
    try:
        async with stdio_session(
            e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
        ) as client:
            # Poll to a terminal state rather than reading once: an in-flight state is
            # acceptable transiently and unacceptable as the final answer, and only
            # waiting distinguishes the two.
            deadline = time.monotonic() + SETTLE_TIMEOUT_S
            state = "UNPARSEABLE"
            status_body = ""
            while time.monotonic() < deadline:
                status_body = _text(
                    await client.call_tool(
                        "call_tool",
                        {
                            "name": "get_enrichment_status",
                            "arguments": {
                                "episode_uuid": episode,
                                "wait": True,
                                "timeout_s": 60.0,
                                "namespace": namespace,
                            },
                        },
                    )
                )
                state = _state(status_body)
                if state in TERMINAL_STATES:
                    break
            lane_evidence.attach("episode-status-after-restart.txt", status_body)

            # no_false_ready: the label must be backed by the graph.
            #
            # Counted on the NAMESPACED episode, not the receipt's uuid. Every write
            # produces two :Episodic nodes -- one with group_id null and one carrying the
            # namespace -- and only the namespaced one is enriched; the receipt returns
            # the other. Counting the receipt's twin reports zero for a perfectly healthy
            # write and turns #92's duplication into a false "enrichment lied" verdict.
            mentions = graph_query(
                e2e_config,
                "MATCH (e:Episodic)-[:MENTIONS]->(n) WHERE e.group_id = $group "
                "RETURN count(n) AS mentioned",
                group=namespace,
            )
            mentioned = mentions[0]["mentioned"] if mentions else 0
            claims_ready = state == "READY"
            backed_by_graph = mentioned > 0
            no_false_ready = (not claims_ready) or backed_by_graph
            lane_evidence.record(
                "no_false_ready_after_restart",
                passed=no_false_ready,
                detail={"state": state, "mentions": mentioned},
            )
            assert no_false_ready, (
                f"episode {episode} reports READY after an ungraceful kill but MENTIONS "
                f"nothing -- a completion claim with no extracted content:\n{status_body[:800]}"
            )

            resolved = state in TERMINAL_STATES
            lane_evidence.record(
                "eventual_ready_or_explicit_failed_or_documented_recoverable",
                passed=resolved,
                detail={
                    "final_state": state,
                    "terminal": resolved,
                    "known_in_flight": state in IN_FLIGHT_STATES,
                },
            )
            assert resolved, (
                f"episode {episode} never reached READY or FAILED after restart; it is "
                f"stuck in {state!r}, which is progress being reported where there is "
                f"none:\n{status_body[:800]}"
            )

            # --- what must have survived ------------------------------------------------
            todo_after = _text(
                await client.call_tool(
                    "call_tool",
                    {"name": "get_todo", "arguments": {"uuid": todo_uuid, "namespace": namespace}},
                )
            )
            structure_after = _text(
                await client.call_tool(
                    "query_structure",
                    {"query_type": "files", "project": project},
                )
            )
            todo_survived = f"uuid={todo_uuid}" in todo_after and "status=open" in todo_after
            structure_survived = "src/shop/storage.py" in structure_after.replace("\\", "/")
            lane_evidence.record(
                "restart_recovers_structure_artifacts_todos",
                passed=todo_survived and structure_survived,
                detail={"todo": todo_survived, "structure": structure_survived},
            )
            assert todo_survived, todo_after[:600]
            assert structure_survived, structure_after[:800]

            # --- idempotent re-run --------------------------------------------------------
            files_before = structure_after.count("\n")
            reingest = _text(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "ingest_project",
                        "arguments": {
                            "path": str(e2e_fixture_repo.path),
                            "name": project,
                            "force": True,
                            "namespace": namespace,
                        },
                    },
                )
            )
            files_after_body = _text(
                await client.call_tool(
                    "query_structure", {"query_type": "files", "project": project}
                )
            )
            files_after = files_after_body.count("\n")
            # A re-scan of an unchanged tree that grows the file listing has duplicated
            # nodes rather than merged them, which inflates every later blast radius.
            stable = files_after == files_before
            lane_evidence.record(
                "idempotent_rerun_of_same_fixture",
                passed=stable,
                detail={"lines_before": files_before, "lines_after": files_after, "reingest": reingest[:400]},
            )
            assert stable, (
                f"forced re-ingest of an unchanged fixture changed the file listing "
                f"({files_before} -> {files_after} lines):\n{files_after_body[:800]}"
            )
    finally:
        restarted.terminate()

    lane_evidence.close(status="PASS")
