"""E2E-8 - isolation and adversarial cases.

Carries the permanent regression pin for #88: two namespaces sharing one session_id
with similar content must stay isolated. #88 closed on a live-LLM reproducer at
9f101e29 (both namespaces READY, own entities, same-group MENTIONS, zero cross-group
MENTIONS); this lane is where that assertion lives for the release.

Every criterion here is a negative test. Per Gate C, a negative test that fails is not a
lane failure to be retried -- it is a release-blocking issue with a reproduced failure.

WHY THIS MODULE IS THREE TESTS
------------------------------
A lane declares one provider, and these criteria need two opposite ones. Proving the
#88 isolation pin needs a provider that succeeds, because the failure mode is entities
extracted into the wrong silo and an extraction that never happens cannot land anywhere.
Proving "provider failure does not silently pass" needs one that reliably fails.
Combining them would mean one of the two criteria was asserted against a provider that
could not produce its failure mode. So they are separate tests in the same module.

The third test is the declared-pending remainder, and it exists rather than being
deleted because Gate C counts checklist items: a criterion that quietly stops being
listed reads as a criterion that was met.

THE #88 PIN USES THE DETERMINISTIC PROVIDER, NOT A LIVE ONE
------------------------------------------------------------
#88 closed on a live-LLM reproducer, and the scaffold assumed the pin needed one. It
does not, and the fake is the better instrument here: it returns the SAME entities for
both namespaces, so the two silos are maximally confusable by content. A live model
would give each namespace slightly different entity names, which makes cross-contamination
*easier* to avoid by accident and therefore a weaker test of the boundary.

What is asserted is the graph fact, not the recall text: zero
``(:Episodic)-[:MENTIONS]->()`` edges crossing a ``group_id`` boundary. Recall returning
clean results is consistent both with correct isolation and with a read-time filter
hiding a write that landed in the wrong silo -- and the second one is still a leak, one
that surfaces the moment any other read path forgets the filter.
"""

from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session, wait_for_project_indexed
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.artifact_corpus import (
    EXPECTED_ARTIFACTS,
    MALFORMED_PATH,
    build_artifact_corpus,
    write_malformed_document,
)
from tests.e2e._harness.fixture_repo import UNINDEXED_PATH, build_fixture_repo
from tests.e2e._harness.pending import declare_pending
from tests.e2e._harness.stack import graph_query, run_menhir_cli

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(3000)]

ISOLATION_CRITERIA = [
    "same_session_id_two_namespaces_stay_isolated",
    "same_filenames_two_projects_do_not_cross_link",
    "stale_or_unknown_coverage_never_reports_safe",
    "oversized_memory_or_diff_refused_as_documented",
    "capped_scan_does_not_authorize_destructive_prune",
]

PROVIDER_CRITERIA = ["provider_failure_does_not_silently_pass"]

CORPUS_CRITERIA = ["malformed_artifact_metadata_fails_without_corruption"]

DEFERRED_CRITERIA = ["invalid_beacon_input_does_not_clobber_manifest"]

EPISODE_ID = re.compile(r"episode_id[=:]\s*([0-9a-f-]{36})")

#: Near-identical content in both silos. Similar wording is the condition under which
#: #88 leaked; distinct content would not exercise the boundary being pinned.
TENANT_A = "The Northwind billing service charges customers monthly and retries failures twice."
TENANT_B = "The Northwind billing service charges customers monthly and retries failures twice."

#: MAX_DIFF_CHARS is 50_000 (services/enrichment_steps.py:183). Comfortably past it.
OVERSIZED_DIFF = "diff --git a/x b/x\n" + ("+padding line that is long enough to matter\n" * 4000)


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    parts = [getattr(item, "text", "") for item in content]
    return "\n".join(part for part in parts if part)


@pytest.mark.provider("deterministic")
async def test_e2e_08_isolation(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    e2e_fixture_repo,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    provider_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    lane_evidence.record_stack(features=feature_combo.label, provider="deterministic")
    suffix = uuid4().hex[:10]
    ns_a, ns_b = f"e2e8a-{suffix}", f"e2e8b-{suffix}"
    proj_a, proj_b = f"alpha-{suffix}", f"beta-{suffix}"
    child_env = {**feature_env, **provider_env}

    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        # --- #88: ONE session, TWO namespaces ----------------------------------------
        # Deliberately one `stdio_session`. Two sessions would carry two session_ids and
        # would not reproduce the shape #88 leaked through.
        episodes: dict[str, str] = {}
        for namespace, text in ((ns_a, TENANT_A), (ns_b, TENANT_B)):
            receipt = _text(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "add_memory_and_track",
                        "arguments": {"text": text, "namespace": namespace, "timeout_s": 180.0},
                    },
                )
            )
            match = EPISODE_ID.search(receipt)
            assert match, f"no episode_id for {namespace}: {receipt[:400]}"
            episodes[namespace] = match.group(1)
            assert "status: READY" in receipt, (
                f"{namespace} did not reach READY, so its entities were never written and "
                f"the isolation assertion below would pass vacuously:\n{receipt[:600]}"
            )

        crossings = graph_query(
            e2e_config,
            "MATCH (e:Episodic)-[:MENTIONS]->(n) "
            "WHERE e.group_id IN $groups AND n.group_id <> e.group_id "
            "RETURN e.group_id AS episode_group, n.group_id AS entity_group, "
            "       e.uuid AS episode, n.uuid AS entity",
            groups=[ns_a, ns_b],
        )
        # The vacuity guard: zero crossings is only meaningful if same-group edges exist.
        same_group = graph_query(
            e2e_config,
            "MATCH (e:Episodic)-[:MENTIONS]->(n) "
            "WHERE e.group_id IN $groups AND n.group_id = e.group_id "
            "RETURN e.group_id AS group_id, count(n) AS mentioned",
            groups=[ns_a, ns_b],
        )
        by_group = {row["group_id"]: row["mentioned"] for row in same_group}
        both_extracted = by_group.get(ns_a, 0) > 0 and by_group.get(ns_b, 0) > 0
        isolated = not crossings
        lane_evidence.record(
            "same_session_id_two_namespaces_stay_isolated",
            passed=isolated and both_extracted,
            detail={
                "cross_group_mentions": crossings,
                "same_group_mentions": by_group,
                "episodes": episodes,
            },
        )
        lane_evidence.attach("isolation.json", json.dumps({"crossings": crossings, "same_group": by_group}, indent=2))
        assert both_extracted, (
            f"one or both namespaces extracted nothing ({by_group}); a zero cross-group "
            "count would prove nothing"
        )
        assert isolated, (
            f"#88 regression: {len(crossings)} MENTIONS edges cross a namespace boundary "
            f"from a single session: {crossings[:10]}"
        )

        # --- same filenames, two projects ---------------------------------------------
        # TWO DIRECTORIES, not one ingested twice. CF-257 writes the resolved identity to
        # a file inside the directory, so a second scan of the same path resolves to the
        # SAME project -- and forcing a new identity there prunes the first scan's files.
        # Either way one directory can only ever be one project, so re-scanning it would
        # not have tested cross-project linking at all.
        #
        # A second built copy gives two genuinely distinct projects whose every relative
        # path is identical, which is the condition under which a path-keyed graph
        # collapses them into one.
        twin = build_fixture_repo(e2e_config.fixtures_dir / f"shop-twin-{suffix}")
        lane_evidence.record_stack(twin_fixture=str(twin.path))
        for project, namespace, root in (
            (proj_a, ns_a, e2e_fixture_repo.path),
            (proj_b, ns_b, twin.path),
        ):
            ingested = _text(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "ingest_project",
                        "arguments": {
                            "path": str(root),
                            "name": project,
                            "namespace": namespace,
                            "identity_action": "new",
                        },
                    },
                )
            )
            assert ingested.startswith("Scanned "), ingested[:400]
            await wait_for_project_indexed(client, project)

        radius_a = _text(
            await client.call_tool(
                "query_structure",
                {"query_type": "blast_radius", "project": proj_a, "path": "src/shop/storage.py"},
            )
        )
        # A radius for project alpha that names project beta means the two ingests share
        # file nodes, and every impact answer for either project is then inflated by the
        # other's callers.
        no_cross_project = proj_b not in radius_a
        lane_evidence.record(
            "same_filenames_two_projects_do_not_cross_link",
            passed=no_cross_project,
            detail={"project_a": proj_a, "project_b": proj_b, "leaked": not no_cross_project},
        )
        lane_evidence.attach("cross-project-radius.txt", radius_a)
        assert no_cross_project, (
            f"blast radius for {proj_a} names {proj_b}; identical paths cross-linked two "
            f"projects:\n{radius_a[:1200]}"
        )

        # --- an unknown path never reads as safe ----------------------------------------
        unknown = _text(
            await client.call_tool(
                "query_structure",
                {"query_type": "blast_radius", "project": proj_a, "path": UNINDEXED_PATH},
            )
        )
        unknown_tests = _text(
            await client.call_tool(
                "query_structure",
                {"query_type": "affected_tests", "project": proj_a, "path": UNINDEXED_PATH},
            )
        )
        refused = "not indexed" in unknown
        no_zero_impact = "Total impact: 0 files" not in unknown
        tests_refused = "not in the structure graph" in unknown_tests
        # The specific dangerous sentence: an empty test set presented as fact.
        no_false_all_clear = "No specific tests found" not in unknown_tests
        lane_evidence.record(
            "stale_or_unknown_coverage_never_reports_safe",
            passed=refused and no_zero_impact and tests_refused and no_false_all_clear,
            detail={
                "blast_radius_refused": refused,
                "no_zero_impact_claim": no_zero_impact,
                "affected_tests_refused": tests_refused,
                "no_false_all_clear": no_false_all_clear,
            },
        )
        assert refused and no_zero_impact, unknown[:800]
        assert tests_refused and no_false_all_clear, unknown_tests[:800]

        # --- an oversized diff -------------------------------------------------------------
        oversized = _text(
            await client.call_tool(
                "add_memory",
                {
                    "text": "A change too large to attach in full.",
                    "diff": OVERSIZED_DIFF,
                    "namespace": ns_a,
                },
            )
        )
        # Two documented outcomes are acceptable and one is not. A refusal is fine, and a
        # truncation that SAYS it truncated is fine. Silently storing a partial diff while
        # reporting an ordinary success is the failure: every later reader treats the
        # fragment as the whole change.
        refused_outright = "Failed to store memory." in oversized or "too large" in oversized.lower()
        accepted = "Queued." in oversized
        marked = False
        stored: list[dict] = []
        if accepted:
            match = EPISODE_ID.search(oversized)
            assert match, oversized[:400]
            # Read the stored episode directly. The point is what was persisted, and a
            # formatted tool response would only show what a reader chose to render.
            stored = graph_query(
                e2e_config,
                "MATCH (e:Episodic {uuid: $uuid}) "
                "RETURN coalesce(e.content, '') AS content, "
                "       size(coalesce(e.content, '')) AS content_size",
                uuid=match.group(1),
            )
            marked = any(
                "[diff truncated]" in (row.get("content") or "")
                or (row.get("content_size") or 0) < len(OVERSIZED_DIFF)
                for row in stored
            )
            lane_evidence.attach(
                "oversized-diff-episode.json",
                json.dumps([{"content_size": r.get("content_size")} for r in stored], indent=2),
            )
        lane_evidence.record(
            "oversized_memory_or_diff_refused_as_documented",
            passed=refused_outright or (accepted and marked),
            detail={
                "refused": refused_outright,
                "accepted": accepted,
                "bounded_or_marked": marked,
                "submitted_diff_chars": len(OVERSIZED_DIFF),
                "stored_content_chars": [r.get("content_size") for r in stored],
                "response": oversized[:400],
            },
        )
        assert refused_outright or (accepted and marked), (
            "an oversized diff was accepted with no refusal and no truncation marker, so a "
            f"partial diff is stored as if complete:\n{oversized[:600]}"
        )

        # --- a capped scan must not authorize a destructive prune -------------------------
        before = graph_query(
            e2e_config,
            "MATCH (n) WHERE n.group_id = $group RETURN count(n) AS nodes",
            group=ns_a,
        )
        nodes_before = before[0]["nodes"] if before else 0
        assert nodes_before > 1, (
            f"namespace {ns_a} holds {nodes_before} nodes; the cap below would not be "
            "exceeded and the refusal would not be exercised"
        )

        # The refusal must be the tool's documented JSON error, carrying the force/dry_run
        # guidance -- not a 500. Until #132 this arrived as an opaque HTTPStatusError because
        # /api/internal/backend re-raised the backend's ValueError instead of mapping it; the
        # harness's backend-fault guard now stays ON here so a regression trips it.
        capped = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "delete_namespace",
                    "arguments": {"namespace": ns_a, "max_nodes": 1, "force": False},
                },
            )
        )
        after = graph_query(
            e2e_config,
            "MATCH (n) WHERE n.group_id = $group RETURN count(n) AS nodes",
            group=ns_a,
        )
        nodes_after = after[0]["nodes"] if after else 0
        # The bug this guards against is a gate that reports a refusal after the delete
        # has already run. Only the node count can tell those apart.
        documented_refusal = '"error"' in capped and "force=true" in capped
        nothing_deleted = nodes_after == nodes_before
        lane_evidence.record(
            "capped_scan_does_not_authorize_destructive_prune",
            passed=documented_refusal and nothing_deleted,
            detail={
                "response": capped[:400],
                "nodes_before": nodes_before,
                "nodes_after": nodes_after,
                "refusal_is_documented_json": documented_refusal,
            },
        )
        assert documented_refusal, (
            f"delete_namespace did not refuse past its cap with its documented JSON error "
            f"(#132): {capped[:600]}"
        )
        assert nothing_deleted, (
            f"delete_namespace reported a refusal but the graph lost "
            f"{nodes_before - nodes_after} nodes; the cap is checked after the delete"
        )

    lane_evidence.close(status="PASS")


@pytest.mark.provider("failing")
async def test_e2e_08_provider_failure_is_not_silent(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    provider_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    """A provider that refuses every completion must not produce a READY episode.

    This is the one criterion an outage would otherwise satisfy by accident: with no
    provider configured at all, enrichment might never be attempted and the episode
    would rest in a state that looks like caution. The failing provider answers 400 to
    every request, so the pipeline definitely tried and definitely failed -- and the
    record must say so.
    """

    lane_evidence.record_stack(features=feature_combo.label, provider="failing")
    namespace = f"e2e8f-{uuid4().hex}"
    child_env = {**feature_env, **provider_env}

    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        receipt = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "add_memory_and_track",
                    "arguments": {
                        "text": "The Kestrel ledger reconciles nightly against the bank feed.",
                        "namespace": namespace,
                        # Returns as soon as the episode is terminal. With a provider that
                        # answers 400, FAILED arrives in seconds; the budget is a ceiling,
                        # not the expected duration.
                        "timeout_s": 120.0,
                    },
                },
            )
        )
        match = EPISODE_ID.search(receipt)
        assert match, f"no episode_id in receipt: {receipt[:400]}"
        episode = match.group(1)

        status = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "get_enrichment_status",
                    "arguments": {
                        "episode_uuid": episode,
                        "wait": True,
                        "timeout_s": 120.0,
                        "namespace": namespace,
                    },
                },
            )
        )
        lane_evidence.attach("failing-provider-status.txt", status)

        claims_ready = "status: READY" in status
        attempts_match = re.search(r"^attempts:\s*(\d+)", status, re.M)
        attempts = int(attempts_match.group(1)) if attempts_match else 0
        error_match = re.search(r"^error:\s*(.+)$", status, re.M)
        error_text = (error_match.group(1) if error_match else "").strip()
        reported_failure = "status: FAILED" in status and error_text not in ("", "(none)")
        mentions = graph_query(
            e2e_config,
            "MATCH (e:Episodic {uuid: $uuid})-[:MENTIONS]->(n) RETURN count(n) AS mentioned",
            uuid=episode,
        )
        mentioned = mentions[0]["mentioned"] if mentions else 0

        # Three things, and the first two are what make the third mean anything: the
        # pipeline TRIED (attempts >= 1 -- a backend that never picked the write up
        # would also be "not READY"), it recorded WHY (a real error, not "(none)"), and
        # it did not claim READY without extracted content. An earlier version checked
        # only the last and passed for 360s on an episode that was never attempted.
        tried = attempts >= 1
        not_silent = tried and reported_failure and not (claims_ready and mentioned == 0)
        lane_evidence.record(
            "provider_failure_does_not_silently_pass",
            passed=not_silent,
            detail={
                "claims_ready": claims_ready,
                "attempts": attempts,
                "error": error_text[:200],
                "reported_failure": reported_failure,
                "mentions": mentioned,
                "status": status[:400],
            },
        )
        assert tried, (
            f"episode {episode} was never attempted (attempts=0) under the failing "
            f"provider -- the backend did not enrich at all, so nothing was proven:\n"
            f"{status[:800]}"
        )
        assert not_silent, (
            f"every provider call returned 400 yet episode {episode} did not end FAILED "
            f"with a recorded error -- enrichment failure is not being reported:\n"
            f"{status[:800]}"
        )

    lane_evidence.close(status="PASS")


async def test_e2e_08_malformed_artifact_metadata(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    """A document with invalid declared metadata is refused, and refused in isolation.

    Two separate properties, and the second is the one that matters. Rejecting the bad
    document is table stakes. What an adversarial case is really asking is whether one
    malformed record can take the corpus down with it -- the reconciler's own parser
    documents that ``errors`` is populated rather than raised so that "one malformed
    record must not stop the rest", and this is where that claim is tested rather than
    read.

    So the corpus is reconciled WITH the bad document present, and every good document
    must still register. A run that refuses the corpus wholesale would satisfy a naive
    "was it rejected?" assertion while being exactly the outage this guards against.

    Its own corpus, not the session fixture: E2E-4 moves a file in that one, and a lane
    that depended on another lane's mutations would pass or fail on ordering.
    """

    lane_evidence.record_stack(features=feature_combo.label, provider="none")
    corpus = build_artifact_corpus(
        e2e_config.fixtures_dir / f"artifact-corpus-malformed-{uuid4().hex[:8]}",
        repository=f"malformed-fixture-{uuid4().hex[:8]}",
    )
    write_malformed_document(corpus)
    lane_evidence.record_stack(**corpus.as_evidence())

    def _audit() -> dict:
        result = run_menhir_cli(
            e2e_config,
            "artifacts", "audit",
            "--repository", corpus.repository,
            "--repo", str(corpus.path),
            "--json",
            feature_env=feature_env,
        )
        assert result.returncode == 0, (
            f"audit exited {result.returncode} | stdout: {result.stdout[-1500:]} "
            f"| stderr: {result.stderr[-1500:]}"
        )
        return json.loads(result.stdout)

    audit = _audit()
    lane_evidence.attach("malformed-audit.json", json.dumps(audit, indent=2))

    by_path = {
        (a.get("path") or "").replace("\\", "/"): a for a in audit.get("actions", [])
    }
    bad = by_path.get(MALFORMED_PATH)
    assert bad is not None, f"{MALFORMED_PATH} is absent from the ledger entirely"

    # CONFLICT is deliberately outside SAFE_ACTION_KINDS: a conflict is a report, never a
    # mutation. So the kind is the assertion -- anything else means apply would write it.
    is_conflict = bad.get("kind") == "CONFLICT"
    names_the_reason = bad.get("conflict_kind") == "INVALID_DECLARED_METADATA"
    detail = " ".join(bad.get("detail") or [])
    # All three authoring mistakes, reported separately. A parser that stopped at the
    # first would leave the author fixing one error per round trip.
    lists_every_error = all(
        token in detail
        for token in ("invalid_artifact_uuid", "unknown_artifact_type", "derived_key_declared")
    )
    lane_evidence.record(
        "malformed_artifact_metadata_fails_without_corruption",
        passed=is_conflict and names_the_reason and lists_every_error,
        detail={"action": bad, "all_errors_listed": lists_every_error},
    )
    assert is_conflict, f"malformed metadata produced a {bad.get('kind')} action, not a CONFLICT"
    assert names_the_reason, bad
    assert lists_every_error, f"not every authoring error was reported: {detail!r}"

    # --- and the rest of the corpus still reconciles ---------------------------------
    applied = run_menhir_cli(
        e2e_config,
        "artifacts", "reconcile",
        "--repository", corpus.repository,
        "--repo", str(corpus.path),
        "--apply",
        "--plan-digest", audit["plan_digest"],
        "--allow-new-repository",
        "--json",
        feature_env=feature_env,
    )
    assert applied.returncode == 0, (
        "a single malformed document made the whole reconcile fail "
        f"(exit {applied.returncode}): {(applied.stdout + applied.stderr)[-2000:]}"
    )

    after = _audit()
    registered = {
        path: a.get("artifact_uuid")
        for path, a in {
            (x.get("path") or "").replace("\\", "/"): x for x in after.get("actions", [])
        }.items()
    }
    good_registered = [p for p in EXPECTED_ARTIFACTS if registered.get(p)]
    uncorrupted = len(good_registered) == len(EXPECTED_ARTIFACTS)
    still_refused = not registered.get(MALFORMED_PATH)
    lane_evidence.record(
        "malformed_artifact_metadata_fails_without_corruption",
        passed=is_conflict and names_the_reason and lists_every_error and uncorrupted and still_refused,
        detail={
            "good_documents_registered": sorted(good_registered),
            "expected_good": sorted(EXPECTED_ARTIFACTS),
            "malformed_still_unregistered": still_refused,
        },
    )
    assert uncorrupted, (
        "the malformed document blocked registration of "
        f"{sorted(set(EXPECTED_ARTIFACTS) - set(good_registered))}"
    )
    assert still_refused, (
        f"{MALFORMED_PATH} was registered despite its metadata being rejected"
    )

    lane_evidence.close(status="PASS")


async def test_e2e_08_deferred_criteria(lane_evidence: LaneEvidence, feature_combo: FeatureCombo) -> None:
    """The E2E-8 criterion whose prerequisite belongs to another lane.

    Kept as a declared-pending test rather than deleted: Gate C counts checklist items,
    and a criterion that stops appearing in the evidence tree is indistinguishable from
    one that was met.
    """

    declare_pending(
        lane_evidence,
        DEFERRED_CRITERIA,
        note=(
            "invalid_beacon_input_does_not_clobber_manifest needs the Beacon interpreter "
            "E2E-6 is blocked on (MENHIR_E2E_BEACON_PYTHON). It is the adversarial "
            "variant of that lane's happy path and should be written with it, not before."
        ),
    )
