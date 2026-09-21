"""E2E-5 - TODO lifecycle.

The repeat-close criterion is the interesting one: closing an already-closed TODO
must be safe and say so, rather than silently succeeding or corrupting state.

Acceptance criteria are the ``CRITERIA`` list below, taken from the approved release
plan's Phase C section.

WHY THIS LANE INGESTS THE FIXTURE
---------------------------------
``location_and_project_metadata`` is not satisfied by a ``code_ref`` string coming back
out unchanged -- that proves storage, not resolution. ``add_todo`` documents a
``REFERENCES_FILE`` edge to the structural file entity matching ``code_ref``, and the
only way to observe that edge is to point it at a file the graph actually holds. So the
fixture is ingested first and the ``code_ref`` names a real file in it.

The unresolvable case is asserted in the same breath, and matters more: a ``code_ref``
naming a path the graph has never seen must come back marked unresolved, not silently
bare. A todo that quietly drops its anchor reads exactly like a todo that never had one.

NO PROVIDER
-----------
Nothing here enriches. ``add_todo`` writes a ``:Todo`` node directly -- todos "never
decay, never go through enrichment" -- and the structural half of ``ingest_project`` is
a graph write, not a model call. The lane therefore declares no provider and cannot
make a live call even if something downstream tried.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session, wait_for_project_indexed
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]

CRITERIA = [
    "add_repository_relative_todo",
    "list_and_read_todo",
    "location_and_project_metadata",
    "unresolvable_location_is_marked_not_dropped",
    "close_todo",
    "open_closed_filtering_semantics",
    "repeat_close_is_safe",
    "invalid_close_is_safe",
    "restart_persistence",
]

#: Deliberately longer than the 100 characters ``list_todos`` truncates at, so
#: ``list_and_read_todo`` can prove the two surfaces differ as documented rather than
#: asserting the same string twice.
TODO_TEXT = (
    "Replace the synchronous order writer in storage.py with a batched flush, keeping the "
    "documented guardrail that no background writer is introduced, and update the storage "
    "tests to cover the batch boundary."
)
ANCHORED_REF = "src/shop/storage.py:12"

#: Prose, not a path. A location is "resolved" when the author's free text NORMALIZES
#: into a project and path -- `todo_location.py` is a parser, and every
#: ``unresolved_reason`` it emits (`not_a_path`, `path_escapes_root`,
#: `absolute_path_outside_workspace`) is a parsing failure.
#:
#: An earlier version of this lane used a well-formed path to a file the fixture never
#: contains and expected it to come back unresolved. That was wrong about the contract:
#: the path parses fine, and a todo location records where the author pointed rather
#: than asserting the file exists. Whether the target is indexed is `query_structure`'s
#: question, and E2E-3 asks it there.
UNRESOLVABLE_REF = "think about the refund logic sometime"


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    parts = [getattr(item, "text", "") for item in content]
    return "\n".join(part for part in parts if part)


def _uuid_of(receipt: str) -> str:
    for token in receipt.split():
        if token.startswith("uuid="):
            return token[len("uuid=") :]
    raise AssertionError(f"no uuid= in add_todo receipt: {receipt!r}")


async def test_e2e_05_todos(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    e2e_fixture_repo,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    lane_evidence.record_stack(features=feature_combo.label, **e2e_fixture_repo.as_evidence())
    namespace = f"e2e5-{uuid4().hex}"
    project = f"shop-{uuid4().hex[:8]}"

    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=feature_env
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
                        # CF-257: a directory with no identity file and no candidate is
                        # NEEDS_DECISION, not an automatic scan -- a moved repo, a new
                        # machine and a fresh clone are indistinguishable at that point.
                        # A generated fixture is genuinely new, so the lane says so
                        # rather than leaving the guard to guess.
                        "identity_action": "new",
                    },
                },
            )
        )
        assert ingest.startswith("Scanned "), f"fixture ingest did not scan: {ingest[:400]}"

        # ingest_project can return before the graph write lands -- its own
        # formatter says "Graph write running in background" while still opening
        # "Scanned <project>:", so the receipt cannot distinguish the two. Wait for
        # the read surface an agent would query.
        await wait_for_project_indexed(client, project)

        # --- create -----------------------------------------------------------------
        receipt = _text(
            await client.call_tool(
                "add_todo",
                {
                    "text": TODO_TEXT,
                    "code_ref": ANCHORED_REF,
                    "priority": "high",
                    "structure_project": project,
                    "namespace": namespace,
                },
            )
        )
        lane_evidence.record(
            "add_repository_relative_todo",
            passed="Created TODO uuid=" in receipt and "[HIGH]" in receipt,
            detail=receipt[:400],
        )
        assert "Created TODO uuid=" in receipt, receipt[:400]
        todo_uuid = _uuid_of(receipt)

        # --- list and read ----------------------------------------------------------
        listing = _text(await client.call_tool("list_todos", {"status": "open", "namespace": namespace}))
        in_listing = f"uuid={todo_uuid}" in listing
        listing_truncated = "truncated at 100 chars" in listing

        full = _text(
            await client.call_tool(
                "call_tool", {"name": "get_todo", "arguments": {"uuid": todo_uuid, "namespace": namespace}}
            )
        )
        # The tail of TODO_TEXT is past the listing's cut, so its presence here is what
        # separates "get_todo returned the record" from "get_todo returned the summary".
        full_text_present = TODO_TEXT[-40:] in full
        lane_evidence.record(
            "list_and_read_todo",
            passed=in_listing and listing_truncated and full_text_present,
            detail={
                "uuid_in_listing": in_listing,
                "listing_announced_truncation": listing_truncated,
                "full_text_in_get_todo": full_text_present,
            },
        )
        assert in_listing, listing[:600]
        assert full_text_present, full[:600]

        # --- location and project metadata ------------------------------------------
        has_code_ref = f"code_ref: {ANCHORED_REF}" in full
        has_status = "status=open" in full
        has_namespace = f"namespace: {namespace}" in full
        # `get_todo` renders a resolved anchor as `location: <project>:<path>` from the
        # TodoLocation rows, and an unresolved one as `location: (unresolved: <reason>)`.
        # The separate `linked file:` line comes from the older linked_file_path field,
        # which this path does not populate -- asserting on it looked right and passed
        # vacuously for the unresolvable case below, because the string never appears at
        # all. The project name in the line is what proves the anchor resolved against
        # the project this lane ingested rather than some other one.
        resolved_location = f"location: {project}:" in full.replace("\\", "/")
        lane_evidence.record(
            "location_and_project_metadata",
            passed=has_code_ref and has_status and has_namespace and resolved_location,
            detail={
                "code_ref": has_code_ref,
                "status_open": has_status,
                "namespace": has_namespace,
                "location_resolved_to_project": resolved_location,
            },
        )
        assert has_code_ref, full[:600]
        assert has_namespace, full[:600]
        assert resolved_location, (
            f"code_ref names a file the fixture ingest wrote into project {project}, so "
            f"the todo's location should have resolved against it:\n{full[:600]}"
        )

        # A path the graph has never held must come back marked, never silently bare.
        orphan_receipt = _text(
            await client.call_tool(
                "add_todo",
                {
                    "text": "Investigate the never-indexed module.",
                    "code_ref": UNRESOLVABLE_REF,
                    "structure_project": project,
                    "namespace": namespace,
                },
            )
        )
        orphan_uuid = _uuid_of(orphan_receipt)
        orphan = _text(
            await client.call_tool(
                "call_tool", {"name": "get_todo", "arguments": {"uuid": orphan_uuid, "namespace": namespace}}
            )
        )
        ref_kept = f"code_ref: {UNRESOLVABLE_REF}" in orphan
        # The anchor must be present AND marked unresolved. Two distinct failures are
        # being excluded: silently dropping the location line, which leaves an
        # unanchorable todo indistinguishable from an unanchored one; and fabricating a
        # path out of prose, which `todo_location.py` states it never does.
        says_unresolved = "location: (unresolved:" in orphan
        not_falsely_resolved = f"location: {project}:" not in orphan.replace("\\", "/")
        lane_evidence.record(
            "unresolvable_location_is_marked_not_dropped",
            passed=ref_kept and says_unresolved and not_falsely_resolved,
            detail={
                "code_ref_retained": ref_kept,
                "marked_unresolved": says_unresolved,
                "not_falsely_resolved": not_falsely_resolved,
                "body": orphan[:400],
            },
        )
        assert ref_kept, orphan[:600]
        assert not_falsely_resolved, (
            "a code_ref pointing at an unindexed path resolved to a real project "
            f"location; the graph claimed a file it does not hold:\n{orphan[:600]}"
        )
        assert says_unresolved, (
            "a code_ref pointing at an unindexed path produced no unresolved marker, so "
            f"an unanchorable todo is indistinguishable from an unanchored one:\n{orphan[:600]}"
        )

        # --- close ------------------------------------------------------------------
        closed = _text(
            await client.call_tool(
                "call_tool", {"name": "close_todo", "arguments": {"uuid": todo_uuid, "namespace": namespace}}
            )
        )
        lane_evidence.record(
            "close_todo", passed=f"Closed TODO {todo_uuid}" in closed, detail=closed[:400]
        )
        assert f"Closed TODO {todo_uuid}" in closed, closed[:400]

        # --- open/closed filtering --------------------------------------------------
        open_after = _text(await client.call_tool("list_todos", {"status": "open", "namespace": namespace}))
        closed_after = _text(await client.call_tool("list_todos", {"status": "closed", "namespace": namespace}))
        gone_from_open = f"uuid={todo_uuid}" not in open_after
        present_in_closed = f"uuid={todo_uuid}" in closed_after
        # The orphan is still open, so an "open" listing that lost everything would be a
        # filter that broke rather than a filter that worked.
        others_still_open = f"uuid={orphan_uuid}" in open_after
        lane_evidence.record(
            "open_closed_filtering_semantics",
            passed=gone_from_open and present_in_closed and others_still_open,
            detail={
                "absent_from_open": gone_from_open,
                "present_in_closed": present_in_closed,
                "unrelated_todo_still_open": others_still_open,
            },
        )
        assert gone_from_open, open_after[:600]
        assert present_in_closed, closed_after[:600]
        assert others_still_open, open_after[:600]

        # --- repeat close is safe ----------------------------------------------------
        again = _text(
            await client.call_tool(
                "call_tool", {"name": "close_todo", "arguments": {"uuid": todo_uuid, "namespace": namespace}}
            )
        )
        # "Closed TODO <uuid>" a second time would mean the write path ran again on an
        # already-closed record. The documented answer is the not-found/already-closed
        # line, and it must arrive as a normal response rather than an error.
        said_already_closed = "not found or already closed" in again
        still_closed = _text(
            await client.call_tool(
                "call_tool", {"name": "get_todo", "arguments": {"uuid": todo_uuid, "namespace": namespace}}
            )
        )
        state_intact = "status=closed" in still_closed
        lane_evidence.record(
            "repeat_close_is_safe",
            passed=said_already_closed and state_intact,
            detail={"response": again[:200], "state_after": state_intact},
        )
        assert said_already_closed, again[:400]
        assert state_intact, still_closed[:600]

        # --- invalid close is safe ---------------------------------------------------
        unknown = str(uuid4())
        invalid = _text(
            await client.call_tool(
                "call_tool", {"name": "close_todo", "arguments": {"uuid": unknown, "namespace": namespace}}
            )
        )
        lane_evidence.record(
            "invalid_close_is_safe",
            passed="not found or already closed" in invalid,
            detail=invalid[:400],
        )
        assert "not found or already closed" in invalid, invalid[:400]

    # --- restart ---------------------------------------------------------------------
    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=feature_env
    ) as client:
        after_restart = _text(
            await client.call_tool(
                "call_tool", {"name": "get_todo", "arguments": {"uuid": todo_uuid, "namespace": namespace}}
            )
        )
        survived_closed = "status=closed" in after_restart
        ref_survived = f"code_ref: {ANCHORED_REF}" in after_restart
        open_listing = _text(await client.call_tool("list_todos", {"status": "open", "namespace": namespace}))
        orphan_survived = f"uuid={orphan_uuid}" in open_listing
        lane_evidence.record(
            "restart_persistence",
            passed=survived_closed and ref_survived and orphan_survived,
            detail={
                "closed_state_survived": survived_closed,
                "code_ref_survived": ref_survived,
                "open_todo_survived": orphan_survived,
            },
        )
        assert survived_closed, after_restart[:600]
        assert ref_survived, after_restart[:600]
        assert orphan_survived, open_listing[:600]

    lane_evidence.close(status="PASS")
