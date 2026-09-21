"""E2E-4 - WorkArtifact lifecycle.

Uses the fixture artifact corpus in ``_harness/artifact_corpus.py``.

The stable-UUID criterion is the one with teeth: move an artifact file, run
audit/reconcile, and the identity must survive. Issue #104 and the snapshot plan both
record the same class of failure -- a corpus audit that scanned a tree it could not see
and confidently reported 190 of 202 sources missing. This lane must therefore also
assert that an unreadable corpus path is an ERROR, never a clean parity report.

REGISTRATION GOES THROUGH THE OPERATOR CLI, NOT MCP
----------------------------------------------------
``audit_artifact_corpus`` is the only artifact tool on the MCP surface that touches the
corpus, and it is read-only by design -- its own docstring sends the caller to
``menhir artifacts reconcile --apply`` to write anything. So the lane drives both: the
audit through stdio (which is what an agent sees) and the apply through the installed
CLI (which is what an operator runs). Asserting only the MCP half would leave the
registration path -- the half that actually writes -- untested.

``--plan-digest`` is a compare-and-set over the ledger: the digest names the exact plan
that was reviewed, and applying a stale one is refused. The lane passes the digest it
just read, which is also why audit and reconcile cannot be collapsed into one call here.

WHY THE MOVE USES ``git mv``
-----------------------------
``MatchBasis`` ranks ``GIT_RENAME`` above ``EXACT_LOCATOR`` and far above
``UNIQUE_CONTENT_SHA256``. A filesystem rename with no commit would push the matcher
onto its weakest basis, and the lane would be proving that content hashing happens to
work rather than that the rename path does.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.e2e._harness.artifact_corpus import (
    EXPECTED_ARTIFACTS,
    INDEX_PATH,
    MOVE_DESTINATION,
    MOVE_SOURCE,
    OUTSIDE_CORPUS_PATH,
    UNPARSEABLE_STATUS_PATH,
    move_document,
)
from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.stack import run_menhir_cli

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400)]

CRITERIA = [
    "validate_fixture_corpus",
    "index_and_outside_documents_are_not_artifacts",
    "read_list_artifact_via_mcp",
    "unparseable_status_registers_unresolved_not_coerced",
    "link_two_artifacts",
    "illegal_relation_rejected",
    "legal_state_transition",
    "illegal_transition_rejected",
    "supersede_direction_and_status",
    "moved_file_keeps_stable_uuid",
    "unreadable_corpus_path_is_error_not_clean_parity",
    "restart_preserves_relationships_status_source",
]


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    parts = [getattr(item, "text", "") for item in content]
    return "\n".join(part for part in parts if part)


def _audit_json(config: E2EConfig, corpus, *, feature_env: dict[str, str]) -> dict:
    result = run_menhir_cli(
        config,
        "artifacts",
        "audit",
        "--repository",
        corpus.repository,
        "--repo",
        str(corpus.path),
        "--json",
        feature_env=feature_env,
    )
    assert result.returncode == 0, (
        f"`menhir artifacts audit` exited {result.returncode}\n"
        f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
    )
    return json.loads(result.stdout)


def _reconcile(
    config: E2EConfig,
    corpus,
    digest: str,
    *,
    feature_env: dict[str, str],
    allow_new: bool = False,
) -> subprocess.CompletedProcess[str]:
    args = [
        "artifacts",
        "reconcile",
        "--repository",
        corpus.repository,
        "--repo",
        str(corpus.path),
        "--apply",
        "--plan-digest",
        digest,
        "--json",
    ]
    if allow_new:
        args.append("--allow-new-repository")
    return run_menhir_cli(config, *args, feature_env=feature_env)


def _uuid_by_path(audit: dict, relative: str) -> str | None:
    for action in audit.get("actions", []):
        if (action.get("path") or "").replace("\\", "/") == relative:
            return action.get("artifact_uuid")
    return None


async def test_e2e_04_workartifacts(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    e2e_artifact_corpus,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    lane_evidence.record_stack(features=feature_combo.label, **e2e_artifact_corpus.as_evidence())
    corpus = e2e_artifact_corpus

    # --- audit, then apply --------------------------------------------------------
    audit = _audit_json(e2e_config, corpus, feature_env=feature_env)
    lane_evidence.attach("audit-initial.json", json.dumps(audit, indent=2))

    paths_in_ledger = {
        (a.get("path") or "").replace("\\", "/") for a in audit.get("actions", [])
    }
    every_expected_seen = set(EXPECTED_ARTIFACTS) <= paths_in_ledger
    # An index is not work and an out-of-route document is not the corpus's business.
    # Either one appearing here is a false finding an operator would chase forever.
    index_excluded = INDEX_PATH not in paths_in_ledger
    outside_excluded = OUTSIDE_CORPUS_PATH not in paths_in_ledger
    lane_evidence.record(
        "validate_fixture_corpus",
        passed=every_expected_seen and bool(audit.get("plan_digest")),
        detail={
            "expected": sorted(EXPECTED_ARTIFACTS),
            "missing_from_ledger": sorted(set(EXPECTED_ARTIFACTS) - paths_in_ledger),
            "counts": audit.get("counts"),
        },
    )
    assert every_expected_seen, (
        f"the audit ledger did not mention {sorted(set(EXPECTED_ARTIFACTS) - paths_in_ledger)}"
    )
    lane_evidence.record(
        "index_and_outside_documents_are_not_artifacts",
        passed=index_excluded and outside_excluded,
        detail={"index_excluded": index_excluded, "outside_route_excluded": outside_excluded},
    )
    assert index_excluded, f"{INDEX_PATH} is an index and must not enter the corpus"
    assert outside_excluded, f"{OUTSIDE_CORPUS_PATH} is outside every route and must be ignored"

    applied = _reconcile(
        e2e_config,
        corpus,
        audit["plan_digest"],
        feature_env=feature_env,
        allow_new=True,
    )
    assert applied.returncode == 0, (
        f"`menhir artifacts reconcile --apply` exited {applied.returncode}\n"
        f"stdout:\n{applied.stdout[-2000:]}\nstderr:\n{applied.stderr[-2000:]}"
    )
    lane_evidence.attach("reconcile-initial.json", applied.stdout)

    # The uuids the graph now holds, read back from a fresh audit rather than parsed out
    # of the apply output: the question is what was persisted, not what was reported.
    after_apply = _audit_json(e2e_config, corpus, feature_env=feature_env)
    lane_evidence.attach("audit-after-apply.json", json.dumps(after_apply, indent=2))
    registered = {
        path: _uuid_by_path(after_apply, path) for path in EXPECTED_ARTIFACTS
    }
    assert all(registered.values()), (
        f"artifacts without a uuid after apply: "
        f"{[p for p, u in registered.items() if not u]}"
    )

    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=feature_env
    ) as client:
        # --- read and list -----------------------------------------------------------
        listing = _text(await client.call_tool("call_tool", {"name": "list_artifacts", "arguments": {"limit": 50}}))
        plan_path = ".agent/plans/shop-refund-threshold.md"
        plan_uuid = registered[plan_path]
        review_uuid = registered[".agent/reviews/shop-refund-threshold-review.md"]
        wrapup_uuid = registered[".agent/for-review/WRAPUP-shop-refund-threshold.md"]

        detail = _text(
            await client.call_tool(
                "call_tool", {"name": "get_artifact", "arguments": {"artifact_uuid": plan_uuid}}
            )
        )
        listed = f"uuid={plan_uuid}" in listing
        typed = "[PLAN]" in detail and "status=PROPOSED" in detail
        # The embodiment is what ties the graph record back to a file on disk. An
        # artifact with no locator cannot be reconciled against anything later.
        embodied = "embodiment" in detail and "shop-refund-threshold.md" in detail.replace("\\", "/")
        lane_evidence.record(
            "read_list_artifact_via_mcp",
            passed=listed and typed and embodied,
            detail={"listed": listed, "typed_and_status": typed, "embodiment_present": embodied},
        )
        lane_evidence.attach("artifact-detail.txt", detail)
        assert listed, listing[:800]
        assert typed, detail[:800]
        assert embodied, detail[:800]

        # --- an unreadable status must not be coerced ---------------------------------
        unclear_uuid = _uuid_by_path(after_apply, UNPARSEABLE_STATUS_PATH)
        assert unclear_uuid, f"{UNPARSEABLE_STATUS_PATH} was never registered"
        unclear = _text(
            await client.call_tool(
                "call_tool", {"name": "get_artifact", "arguments": {"artifact_uuid": unclear_uuid}}
            )
        )
        # It holds its type's initial status because nothing readable said otherwise --
        # and that fact must be stated, or PROPOSED reads as a declaration nobody made.
        holds_initial = "status=PROPOSED" in unclear
        says_unmapped = "status unmapped" in unclear
        lane_evidence.record(
            "unparseable_status_registers_unresolved_not_coerced",
            passed=holds_initial and says_unmapped,
            detail={"holds_initial_status": holds_initial, "declares_unresolved": says_unmapped},
        )
        assert holds_initial, unclear[:600]
        assert says_unmapped, (
            "a document whose Status header could not be read was registered as PROPOSED "
            f"with no indication that nobody declared it:\n{unclear[:600]}"
        )

        # --- relationships --------------------------------------------------------------
        linked = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "link_artifacts",
                    "arguments": {
                        "source_uuid": review_uuid,
                        "target_uuid": plan_uuid,
                        "relation": "reviews",
                    },
                },
            )
        )
        lane_evidence.record(
            "link_two_artifacts",
            passed="REVIEWS" in linked and "Declared" in linked,
            detail=linked[:400],
        )
        assert "Declared" in linked, linked[:400]

        # A plan cannot review anything: `relation_is_legal` checks the stored types, and
        # the refusal is the model enforcing the direction rather than the caller.
        illegal_link = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "link_artifacts",
                    "arguments": {
                        "source_uuid": plan_uuid,
                        "target_uuid": review_uuid,
                        "relation": "reviews",
                    },
                },
            )
        )
        lane_evidence.record(
            "illegal_relation_rejected",
            passed=illegal_link.startswith("Refused:"),
            detail=illegal_link[:400],
        )
        assert illegal_link.startswith("Refused:"), (
            f"a plan was allowed to review a review:\n{illegal_link[:400]}"
        )

        # --- lifecycle -------------------------------------------------------------------
        legal = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "transition_artifact",
                    "arguments": {"artifact_uuid": plan_uuid, "to_status": "REVIEWED"},
                },
            )
        )
        lane_evidence.record(
            "legal_state_transition",
            passed="PROPOSED -> REVIEWED" in legal,
            detail=legal[:400],
        )
        assert "PROPOSED -> REVIEWED" in legal, legal[:400]

        # REVIEWED -> IMPLEMENTED skips APPROVED and IMPLEMENTING. The whole point of the
        # typed lifecycle is that a step cannot be skipped, so this must be refused AND
        # must leave the artifact where it was.
        illegal = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "transition_artifact",
                    "arguments": {"artifact_uuid": plan_uuid, "to_status": "IMPLEMENTED"},
                },
            )
        )
        after_illegal = _text(
            await client.call_tool(
                "call_tool", {"name": "get_artifact", "arguments": {"artifact_uuid": plan_uuid}}
            )
        )
        refused = illegal.startswith("Refused:")
        unchanged = "status=REVIEWED" in after_illegal
        lane_evidence.record(
            "illegal_transition_rejected",
            passed=refused and unchanged,
            detail={"response": illegal[:300], "status_unchanged": unchanged},
        )
        assert refused, f"a plan skipped APPROVED and IMPLEMENTING:\n{illegal[:400]}"
        assert unchanged, (
            f"the illegal transition was refused but the status moved anyway:\n{after_illegal[:600]}"
        )

        # --- supersession -----------------------------------------------------------------
        batching_uuid = registered[MOVE_SOURCE]
        superseded = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "supersede_artifact",
                    "arguments": {"new_uuid": batching_uuid, "old_uuid": plan_uuid},
                },
            )
        )
        old_after = _text(
            await client.call_tool(
                "call_tool", {"name": "get_artifact", "arguments": {"artifact_uuid": plan_uuid}}
            )
        )
        relations = _text(
            await client.call_tool(
                "call_tool",
                {"name": "get_artifact_relationships", "arguments": {"artifact_uuid": batching_uuid}},
            )
        )
        # Both halves or neither. An edge pointing at an artifact still marked REVIEWED,
        # or a SUPERSEDED artifact with no record of what replaced it, are each a state
        # the graph is documented never to hold.
        moved_to_superseded = "status=SUPERSEDED" in old_after
        edge_recorded = plan_uuid in relations
        lane_evidence.record(
            "supersede_direction_and_status",
            passed=moved_to_superseded and edge_recorded,
            detail={
                "response": superseded[:300],
                "old_is_superseded": moved_to_superseded,
                "edge_recorded": edge_recorded,
            },
        )
        lane_evidence.attach("supersession-relationships.txt", relations)
        assert moved_to_superseded, old_after[:600]
        assert edge_recorded, (
            f"{plan_uuid} is SUPERSEDED but nothing records what replaced it:\n{relations[:800]}"
        )

    # --- the move ---------------------------------------------------------------------
    move_document(corpus, MOVE_SOURCE, MOVE_DESTINATION)
    moved_audit = _audit_json(e2e_config, corpus, feature_env=feature_env)
    lane_evidence.attach("audit-after-move.json", json.dumps(moved_audit, indent=2))

    moved_applied = _reconcile(
        e2e_config, corpus, moved_audit["plan_digest"], feature_env=feature_env
    )
    assert moved_applied.returncode == 0, (
        f"reconcile after the move exited {moved_applied.returncode}\n"
        f"stdout:\n{moved_applied.stdout[-2000:]}\nstderr:\n{moved_applied.stderr[-2000:]}"
    )

    final_audit = _audit_json(e2e_config, corpus, feature_env=feature_env)
    uuid_at_new_path = _uuid_by_path(final_audit, MOVE_DESTINATION)
    uuid_at_old_path = _uuid_by_path(final_audit, MOVE_SOURCE)
    stable = uuid_at_new_path == batching_uuid
    old_path_gone = uuid_at_old_path is None
    lane_evidence.record(
        "moved_file_keeps_stable_uuid",
        passed=stable and old_path_gone,
        detail={
            "uuid_before_move": batching_uuid,
            "uuid_at_new_path": uuid_at_new_path,
            "old_path_still_registered": not old_path_gone,
        },
    )
    assert stable, (
        f"moving {MOVE_SOURCE} to {MOVE_DESTINATION} changed the artifact's identity "
        f"({batching_uuid} -> {uuid_at_new_path}); every relationship declared against "
        "the old uuid now points at nothing"
    )
    assert old_path_gone, f"{MOVE_SOURCE} is still registered after being moved away"

    # --- an unreadable corpus is an error, never clean parity -----------------------------
    missing = Path(str(corpus.path) + "-does-not-exist")
    assert not missing.exists()
    unreadable = run_menhir_cli(
        e2e_config,
        "artifacts",
        "audit",
        "--repository",
        corpus.repository,
        "--repo",
        str(missing),
        "--json",
        feature_env=feature_env,
    )
    combined = (unreadable.stdout + unreadable.stderr).lower()
    # Deliberately narrow. An earlier draft accepted any output containing "not ", which
    # matches almost any English sentence and would have passed on a clean report saying
    # "nothing to do" -- the exact answer this criterion exists to reject.
    errored = unreadable.returncode != 0 or any(
        token in combined
        for token in ("error", "unavailable", "does not exist", "no such", "cannot")
    )
    # #104's shape: a tree the scanner could not read, reported as parity. If the command
    # succeeds it must NOT claim the corpus is fine -- a clean ledger over an absent
    # directory is the confident wrong answer that authorizes deletions.
    claimed_clean = False
    if unreadable.returncode == 0 and unreadable.stdout.strip():
        try:
            payload = json.loads(unreadable.stdout)
        except json.JSONDecodeError:
            payload = {}
        claimed_clean = payload.get("actions") == [] and bool(payload.get("plan_digest"))
    lane_evidence.record(
        "unreadable_corpus_path_is_error_not_clean_parity",
        passed=errored and not claimed_clean,
        detail={
            "returncode": unreadable.returncode,
            "claimed_clean_parity": claimed_clean,
            "output": (unreadable.stdout + unreadable.stderr)[:600],
        },
    )
    assert not claimed_clean, (
        "auditing an absent repository reported clean parity with an empty ledger -- the "
        "#104 failure: a completeness answer derived from a tree the server never saw"
    )
    assert errored, (
        f"auditing an absent repository neither failed nor said anything was wrong:\n"
        f"{(unreadable.stdout + unreadable.stderr)[:600]}"
    )

    # --- restart ---------------------------------------------------------------------------
    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=feature_env
    ) as client:
        plan_after = _text(
            await client.call_tool(
                "call_tool", {"name": "get_artifact", "arguments": {"artifact_uuid": plan_uuid}}
            )
        )
        relations_after = _text(
            await client.call_tool(
                "call_tool",
                {"name": "get_artifact_relationships", "arguments": {"artifact_uuid": batching_uuid}},
            )
        )
        moved_after = _text(
            await client.call_tool(
                "call_tool", {"name": "get_artifact", "arguments": {"artifact_uuid": batching_uuid}}
            )
        )
        status_survived = "status=SUPERSEDED" in plan_after
        relationships_survived = plan_uuid in relations_after
        source_survived = MOVE_DESTINATION.rsplit("/", 1)[-1] in moved_after.replace("\\", "/")
        lane_evidence.record(
            "restart_preserves_relationships_status_source",
            passed=status_survived and relationships_survived and source_survived,
            detail={
                "status": status_survived,
                "relationships": relationships_survived,
                "source_locator": source_survived,
            },
        )
        assert status_survived, plan_after[:600]
        assert relationships_survived, relations_after[:800]
        assert source_survived, (
            f"after restart the moved artifact does not carry its new locator "
            f"({MOVE_DESTINATION}):\n{moved_after[:600]}"
        )

    lane_evidence.close(status="PASS")
