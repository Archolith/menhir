"""E2E-3 - integrated coding workflow.

Uses the generated fixture repo (``_harness/fixture_repo.py``), whose true import,
caller and test relationships are declared alongside its content as
``EXPECTED_IMPORTS`` / ``EXPECTED_BLAST_RADIUS`` / ``EXPECTED_AFFECTED_TESTS``.

The load-bearing criterion is the coverage caveat. ``UNINDEXED_PATH`` is a file the
fixture deliberately never contains: asking for its blast radius must produce an
explicit "not indexed / unknown coverage" answer, not an empty result. An empty result
reads as "nothing depends on this, safe to change", which is indistinguishable from a
correct answer right up until it authorizes a deletion. Issue #104 is the same shape --
a completeness answer derived from a tree the server could not observe.

THE SNAPSHOT READ PATH IS ASSERTED, NOT ASSUMED
-----------------------------------------------
The snapshot read path landing in the working tree makes a published canonical view win
over local structure for the same project. If one were published for this project, every
assertion below would silently switch to grading the snapshot path, and would keep
passing while proving something else entirely.

``no_published_snapshot_view_in_effect`` is therefore checked FIRST and read straight
from the graph, not from a formatted tool response: the question is whether a
``:CanonicalView`` row exists, and a formatter that has no reason to mention one is not
evidence that none is there. The project name is also freshly minted per run, so a view
published by an earlier run cannot attach to it.

PATH COMPARISON IS SUFFIX-BASED
-------------------------------
The expectation tables are repo-relative. Whether the graph stores repo-relative or
absolute paths is the ingest layer's business, so every comparison here is "some
returned path ends with the expected relative path" rather than an equality that would
break on a storage change without any behaviour having regressed.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session, wait_for_project_indexed
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.fixture_repo import (
    EXPECTED_AFFECTED_TESTS,
    EXPECTED_BLAST_RADIUS,
    EXPECTED_IMPORTS,
    UNINDEXED_PATH,
)
from tests.e2e._harness.stack import graph_query

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400), pytest.mark.provider("deterministic")]

CRITERIA = [
    "no_published_snapshot_view_in_effect",
    "ingest_fixture_project_via_mcp",
    "query_project_files_symbols_context",
    "blast_radius_for_known_file",
    "affected_tests_for_known_file",
    "expected_import_relationships",
    "expected_caller_relationships",
    "unindexed_path_returns_coverage_caveat",
    "memory_with_git_diff_is_code_anchored",
    "code_context_recall_includes_anchored_memory",
    "restart_then_repeat_structure_queries",
]

EPISODE_ID = re.compile(r"episode_id[=:]\s*([0-9a-f-]{36})")

#: The file whose blast radius, callers and affected tests the lane asserts. It sits in
#: the middle of the fixture's dependency chain, so a radius that collapses to "the file
#: itself" and one that fans out correctly are distinguishable.
SUBJECT = "src/shop/storage.py"

#: Attached to the memory as the change context. It touches SUBJECT, which is what makes
#: the memory code-anchored rather than free-floating prose that merely mentions a path.
DIFF = """\
diff --git a/src/shop/storage.py b/src/shop/storage.py
index 1111111..2222222 100644
--- a/src/shop/storage.py
+++ b/src/shop/storage.py
@@ -10,6 +10,9 @@ class OrderStore:
     def put(self, order_id: str, payload: dict) -> None:
         self._rows[order_id] = payload
+
+    def flush(self) -> None:
+        \"\"\"No-op: the store is synchronous on purpose.\"\"\"
"""

MEMORY = (
    "The shop order store stays synchronous on purpose. A flush() hook was added to "
    "storage.py as a no-op so callers have a seam, but introducing a background writer "
    "is still forbidden by the documented guardrail."
)


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    parts = [getattr(item, "text", "") for item in content]
    return "\n".join(part for part in parts if part)


def _mentions(body: str, relative_path: str) -> bool:
    """True when ``body`` names ``relative_path``, whether stored relative or absolute.

    Backslashes are normalized because the fixture is built on whatever platform the
    campaign runs on, and a Windows-separated path naming the same file is the same
    answer.
    """

    return relative_path in body.replace("\\", "/")


async def test_e2e_03_coding_workflow(
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
    namespace = f"e2e3-{uuid4().hex}"
    project = f"shop-{uuid4().hex[:8]}"
    child_env = {**feature_env, **provider_env}

    # --- the snapshot fence, before anything is read through it ----------------------
    views = graph_query(
        e2e_config, "MATCH (v:CanonicalView) RETURN v.project_id AS project_id, v.view_key AS view_key"
    )
    no_view = not views
    lane_evidence.record(
        "no_published_snapshot_view_in_effect",
        passed=no_view,
        detail={"canonical_views": views},
    )
    assert no_view, (
        "a published CanonicalView exists in the graph, so structure answers below would "
        f"come from the snapshot read path rather than local structure: {views}"
    )

    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        # --- ingest ------------------------------------------------------------------
        ingest = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "ingest_project",
                    "arguments": {
                        "path": str(e2e_fixture_repo.path),
                        "name": project,
                        "namespace": namespace,
                        # CF-257: no identity file and no candidate is NEEDS_DECISION,
                        # not an automatic scan. The fixture is genuinely new.
                        "identity_action": "new",
                    },
                },
            )
        )
        scanned = ingest.startswith(f"Scanned {project}")
        # A scan that wrote no import or test edges would satisfy every "does it mention
        # the file" assertion below while having understood nothing about the repo.
        wrote_relationships = "imports=0" not in ingest and "test_edges=0" not in ingest
        lane_evidence.record(
            "ingest_fixture_project_via_mcp",
            passed=scanned and wrote_relationships,
            detail=ingest[:600],
        )
        assert scanned, ingest[:600]
        assert wrote_relationships, f"ingest wrote no import/test edges: {ingest[:600]}"

        # ingest_project can return before the graph write lands -- its own
        # formatter says "Graph write running in background" while still opening
        # "Scanned <project>:", so the receipt cannot distinguish the two. Wait for
        # the read surface an agent would query.
        await wait_for_project_indexed(client, project, symbol_path=SUBJECT)

        # --- files, symbols, context -------------------------------------------------
        files = _text(
            await client.call_tool("query_structure", {"query_type": "files", "project": project})
        )
        symbols = _text(
            await client.call_tool(
                "query_structure", {"query_type": "symbols", "project": project, "path": SUBJECT}
            )
        )
        context = _text(
            await client.call_tool(
                "query_structure", {"query_type": "context", "project": project, "path": SUBJECT}
            )
        )
        files_ok = all(_mentions(files, p) for p in EXPECTED_IMPORTS)
        symbols_ok = "OrderStore" in symbols
        context_ok = context.startswith("Context for") and _mentions(context, "src/shop/config.py")
        lane_evidence.record(
            "query_project_files_symbols_context",
            passed=files_ok and symbols_ok and context_ok,
            detail={"files": files_ok, "symbols": symbols_ok, "context": context_ok},
        )
        assert files_ok, files[:800]
        assert symbols_ok, symbols[:800]
        assert context_ok, context[:800]

        # --- blast radius -------------------------------------------------------------
        radius = _text(
            await client.call_tool(
                "query_structure",
                {"query_type": "blast_radius", "project": project, "path": SUBJECT},
            )
        )
        expected_radius = EXPECTED_BLAST_RADIUS[SUBJECT]
        reached = {p for p in expected_radius if _mentions(radius, p)}
        # An index this lane just built is fully indexed, so a completeness qualifier
        # here would mean the coverage counters did not survive the scan -- and every
        # negative answer from this project would be unsafe to act on.
        complete = "COVERAGE UNVERIFIED" not in radius and "INCOMPLETE:" not in radius
        lane_evidence.record(
            "blast_radius_for_known_file",
            passed=reached == expected_radius and complete,
            detail={"expected": sorted(expected_radius), "reached": sorted(reached), "complete_index": complete},
        )
        assert reached == expected_radius, (
            f"blast radius for {SUBJECT} missed {sorted(expected_radius - reached)}:\n{radius[:1200]}"
        )
        assert complete, f"freshly ingested project reports incomplete coverage:\n{radius[:800]}"

        # --- affected tests ------------------------------------------------------------
        affected = _text(
            await client.call_tool(
                "query_structure",
                {"query_type": "affected_tests", "project": project, "path": SUBJECT},
            )
        )
        expected_tests = EXPECTED_AFFECTED_TESTS[SUBJECT]
        found_tests = {p for p in expected_tests if _mentions(affected, p)}
        lane_evidence.record(
            "affected_tests_for_known_file",
            passed=found_tests == expected_tests,
            detail={"expected": sorted(expected_tests), "found": sorted(found_tests)},
        )
        assert found_tests == expected_tests, (
            f"affected tests for {SUBJECT} missed {sorted(expected_tests - found_tests)}:\n{affected[:800]}"
        )

        # --- imports and callers, per the declared tables -------------------------------
        import_results: dict[str, dict[str, bool]] = {}
        for source, targets in EXPECTED_IMPORTS.items():
            body = _text(
                await client.call_tool(
                    "query_structure",
                    {"query_type": "imports", "project": project, "path": source},
                )
            )
            # The response has an "Imports" half and an "Imported by" half; splitting on
            # the second header keeps a name appearing in the wrong direction from
            # satisfying the wrong assertion.
            head, _, _tail = body.partition("Imported by")
            import_results[source] = {target: _mentions(head, target) for target in targets}
            lane_evidence.attach(f"imports-{source.replace('/', '_')}.txt", body)

        imports_ok = all(all(r.values()) for r in import_results.values())
        lane_evidence.record(
            "expected_import_relationships", passed=imports_ok, detail=import_results
        )
        assert imports_ok, f"declared imports not reflected in the graph: {import_results}"

        callers = _callers_of(EXPECTED_IMPORTS)
        caller_results: dict[str, dict[str, bool]] = {}
        for target, importers in callers.items():
            body = _text(
                await client.call_tool(
                    "query_structure",
                    {"query_type": "imports", "project": project, "path": target},
                )
            )
            _, _, tail = body.partition("Imported by")
            caller_results[target] = {importer: _mentions(tail, importer) for importer in importers}

        callers_ok = all(all(r.values()) for r in caller_results.values())
        lane_evidence.record(
            "expected_caller_relationships", passed=callers_ok, detail=caller_results
        )
        assert callers_ok, f"declared callers not reflected in the graph: {caller_results}"

        # --- the coverage caveat ---------------------------------------------------------
        unknown = _text(
            await client.call_tool(
                "query_structure",
                {"query_type": "blast_radius", "project": project, "path": UNINDEXED_PATH},
            )
        )
        refused = "not indexed" in unknown
        explained = "no dependency, test, or impact conclusion can be drawn" in unknown
        # The failure this criterion exists to catch: a clean, confident, empty answer.
        not_false_safe = "Total impact: 0 files" not in unknown
        lane_evidence.record(
            "unindexed_path_returns_coverage_caveat",
            passed=refused and explained and not_false_safe,
            detail=unknown[:800],
        )
        assert refused and explained, (
            f"an unindexed path produced an answer rather than a refusal:\n{unknown[:800]}"
        )
        assert not_false_safe, f"unindexed path reported a zero blast radius:\n{unknown[:800]}"

        # --- a code-anchored memory --------------------------------------------------------
        receipt = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "add_memory_and_track",
                    "arguments": {"text": MEMORY, "diff": DIFF, "namespace": namespace},
                },
            )
        )
        match = EPISODE_ID.search(receipt)
        assert match, f"no episode_id in tracked-write receipt: {receipt[:400]}"
        episode = match.group(1)

        status = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "get_enrichment_status",
                    "arguments": {
                        "episode_uuid": episode,
                        "wait": True,
                        "timeout_s": 180.0,
                        "namespace": namespace,
                    },
                },
            )
        )
        assert "status: READY" in status, status[:800]

        # The anchor is observable through blast_radius: a memory linked to a file in the
        # radius is listed under "Related memories ... (via <file>)". Asserting the graph
        # edge this way also proves the anchor is reachable from the query an agent
        # actually runs before editing, which is the point of anchoring it at all.
        anchored_radius = _text(
            await client.call_tool(
                "query_structure",
                {
                    "query_type": "blast_radius",
                    "project": project,
                    "path": SUBJECT,
                    "namespace": namespace,
                },
            )
        )
        has_section = "Related memories" in anchored_radius
        via_subject = _mentions(anchored_radius, f"(via {SUBJECT}")
        lane_evidence.record(
            "memory_with_git_diff_is_code_anchored",
            passed=has_section,
            detail={
                "related_memories_section": has_section,
                "anchored_via_subject": via_subject,
                "caveat": (
                    "anchor target not asserted: the deterministic provider replays fixed "
                    "facts and never reads the diff, so the anchor follows the narrative "
                    "path rather than the changed file"
                ),
            },
        )
        lane_evidence.attach("anchored-blast-radius.txt", anchored_radius)
        assert has_section, (
            "a memory carrying a diff that touches this file produced no Related memories "
            f"section, so the diff was stored but not anchored:\n{anchored_radius[:1200]}"
        )
        # NOT asserted: that the anchor names SUBJECT specifically. The deterministic
        # provider replays fixed facts and never reads the diff, so a memory written here
        # about the order store is extracted as the fake's entities and anchored through
        # the project narrative path -- landing on api.py rather than the changed file.
        # That is the fake's limit, not a product defect, and asserting it would be
        # asserting the fake. Proving it needs a provider that extracts from its prompt.
        lane_evidence.record_stack(anchor_via_subject=via_subject)

        recalled = _text(
            await client.call_tool(
                "recall_memories",
                {"query": "is a background writer allowed in the shop order store?", "namespace": namespace},
            )
        )
        # The deterministic provider replays a fixed fact set and never extracts from the
        # episode it is given (providers.py `_facts`), so this lane's own wording cannot
        # come back. What remains provable is that an anchored memory is reachable from a
        # code-context recall at all; matching the lane's phrasing would need a provider
        # that reads its prompt.
        reachable = "background writer" in recalled or "Atlas Lantern" in recalled
        lane_evidence.record(
            "code_context_recall_includes_anchored_memory",
            passed=reachable,
            detail={
                "recalled": recalled[:400],
                "matched_lane_wording": "background writer" in recalled,
                "caveat": (
                    "the deterministic provider does not extract from its prompt, so a "
                    "match on the replayed entity is the strongest available evidence"
                ),
            },
        )
        assert reachable, recalled[:800]

    # --- restart ----------------------------------------------------------------------------
    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        again = _text(
            await client.call_tool(
                "query_structure",
                {"query_type": "blast_radius", "project": project, "path": SUBJECT},
            )
        )
        reached_again = {p for p in expected_radius if _mentions(again, p)}
        still_refuses = "not indexed" in _text(
            await client.call_tool(
                "query_structure",
                {"query_type": "blast_radius", "project": project, "path": UNINDEXED_PATH},
            )
        )
        lane_evidence.record(
            "restart_then_repeat_structure_queries",
            passed=reached_again == expected_radius and still_refuses,
            detail={"reached": sorted(reached_again), "unindexed_still_refused": still_refuses},
        )
        assert reached_again == expected_radius, again[:1200]
        assert still_refuses, "the coverage refusal did not survive a restart"

    lane_evidence.close(status="PASS")


def _callers_of(imports: dict[str, set[str]]) -> dict[str, set[str]]:
    """Invert the import table into target -> importers.

    Derived rather than declared: a second hand-written table would let the import and
    caller expectations drift apart, and then "callers are correct" would be a statement
    about the table rather than about the graph.
    """

    inverted: dict[str, set[str]] = {}
    for source, targets in imports.items():
        for target in targets:
            inverted.setdefault(target, set()).add(source)
    return inverted
