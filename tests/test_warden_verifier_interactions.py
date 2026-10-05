"""Release interaction controls using real services and existing offline stores.

Run with ``pytest tests/test_warden_verifier_interactions.py -o junit_family=legacy
--junitxml=interactions.xml``. Each case embeds a JSON ``warden_verifier_control``
property. These fixtures do not qualify answer quality, costs or defaults.
"""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.services.context_builder import ContextBuilderService
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from menhir.services.verifier_sync import VerifierContext, sync_verifiers
from tests.test_verifier_currentness import _FreshnessRepo, _RegisterStore


BASE = "2026-10-05T00:00:00+00:00"
PROBE = "2026-10-05T00:01:00+00:00"
ARMS = (("neither", False, False), ("warden", True, False),
        ("verifier", False, True), ("both", True, True))
CASES = (("anchored_current", "git", False, False),
         ("conversation_current", "agent", False, False),
         ("conflict_current", "git", True, False),
         ("conflict_historical", "git", True, True))


@pytest.mark.asyncio
@pytest.mark.parametrize("arm,warden,verifier", ARMS)
@pytest.mark.parametrize("source", ["unchanged", "changed", "unavailable", "already_flagged"])
@pytest.mark.parametrize("case,evidence_kind,conflict,historical", CASES)
async def test_warden_verifier_effects_and_sync_disabled_restart(
    arm, warden, verifier, source, case, evidence_kind, conflict, historical,
    monkeypatch, stub_graphiti_client, stub_memory_graph_adapter, record_property,
):
    from menhir.services import verifier_sync

    monkeypatch.delenv("MENHIR_FRONTIER_ORACLE_SUBSET", raising=False)
    clock = [BASE]
    monkeypatch.setattr(verifier_sync, "_utc_now_iso", lambda: clock[0])
    content = "The job is disabled."
    rows = [dict(uuid="belief", name="configuration", content=content,
        scope="PERSISTENT", type="SEMANTIC", freshness="ACTIVE", namespace="conversation",
        conflict_group_id="group" if conflict else None,
        conflict_status="unresolved" if conflict else None)]
    store = _RegisterStore()
    repo = _FreshnessRepo(rows)
    # Identical known last-success baseline in every arm. The tested interval begins below.
    monkeypatch.setenv("MENHIR_TEST_DRIFT", "false")
    sync_verifiers(repo=repo, graph_adapter=store, context=VerifierContext())
    if source == "already_flagged":
        rows[0].update(needs_review=True, review_reason="prior persisted drift", review_flagged_at=BASE)
    baseline = deepcopy(rows[0])
    clock[0] = PROBE
    if source == "unavailable":
        monkeypatch.delenv("MENHIR_TEST_DRIFT")
    else:
        monkeypatch.setenv("MENHIR_TEST_DRIFT", "true" if source == "changed" else "false")
    probes = sync_verifiers(repo=repo, graph_adapter=store, context=VerifierContext()) if verifier else []
    flagged = source == "already_flagged" or (verifier and source == "changed")
    expected_value = 1.0 if verifier and source == "changed" else 0.0
    successful = verifier and source != "unavailable"
    expected_success = PROBE if successful else BASE
    expected_status = "source_unavailable" if verifier and source == "unavailable" else "refreshed"
    assert store.values == {"conversation": expected_value, "foreign": 0.0}
    assert bool(rows[0].get("needs_review")) is flagged
    stored = rows[0]["verifier_evidence"][0]
    assert stored["value"] == expected_value
    assert stored["last_verified_at"] == expected_success
    assert stored["last_probe_status"] == expected_status
    assert stored["last_probe_at"] == (PROBE if verifier else BASE)
    if not verifier:
        assert rows[0] == baseline
    adapter = stub_memory_graph_adapter
    adapter.stale_anchored_memories = lambda **kwargs: []
    adapter.candidate_provenance_rows = [dict(uuid="belief",
        evidence_node_kinds=["git"] if evidence_kind == "git" else [], anchor_projects=[],
        episode_sources=["claude-code"] if evidence_kind == "agent" else [])]
    stub_graphiti_client.search_scored_results = [("belief", "configuration", .85)]
    tuning = RetrievalTuningConfig(enable_assertion_shadow=False,
        enable_warden_gate=warden, enable_evidence_anchor=True,
        enable_contradiction_interrupt=warden, enable_intent_lens=True)
    query = "have we already tried this configuration" if historical else "configuration"
    report = dict(schema_version=1, qualified_for_defaults=False, arm=arm, case=case,
        source=source, tuning=asdict(tuning), probes=probes, stored_evidence=deepcopy(stored),
        limits=["Authored single-candidate controls, not answer-quality/cost qualification",
                "Sync is invoked or skipped directly; scheduler wiring is tested separately",
                "Restart recreates services over offline persisted-state stubs"], phases=[])
    expected_returned = not (warden and (evidence_kind == "agent" or (conflict and not historical)))
    persisted = deepcopy(rows)
    for phase in ("after_probe", "restart_sync_disabled"):
        if phase == "restart_sync_disabled":
            # New repository/service instances, same durable snapshot; no probe is run.
            _FreshnessRepo(rows)
            clock[0] = "2026-10-05T00:02:00+00:00"
        # Database reads return snapshots; the stub otherwise aliases durable rows.
        adapter.candidate_metadata = deepcopy(rows)
        service = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=adapter,
                                scoring_service=ScoringService(), read_only=True)
        common = dict(namespace="conversation", source_memory_limit=0, update_access=False)
        baseline_recall = await service.recall(query, **common, tuning=RetrievalTuningConfig(
            enable_assertion_shadow=False, enable_intent_lens=True))
        result = await service.recall(query, **common, tuning=tuning, include_warden_status=True)
        assert bool(result.results) is expected_returned
        assert result.warden_status.state == ("applied" if warden else "disabled")
        assert result.warden_status.refused == int(not expected_returned)
        assert not result.warden_status.metadata_gaps
        context = await ContextBuilderService(recall_service=service, retrieval_tuning=tuning,
            source_memory_limit=0).build_context(query, namespace="conversation", max_tokens=2000)
        assert (content in context.context) is expected_returned
        assert ("Review required:" in context.context) is (expected_returned and flagged)
        evidence = None
        if expected_returned:
            memory = result.results[0]
            assert memory.content == content
            assert memory.final_score == baseline_recall.results[0].final_score
            assert memory.needs_review is flagged
            assert memory.warden_label == ("conflict" if warden and conflict else None)
            evidence = memory.verifier_evidence[0]
            assert evidence.value == expected_value
            assert evidence.last_verified_at == expected_success
            assert evidence.last_probe_status == expected_status
            assert evidence.last_probe_at == (PROBE if verifier else BASE)
            assert "does not verify the memory's prose" in context.context
            if flagged:
                assert "Verify the source before asserting this memory as current truth." in context.context
        report["phases"].append(dict(phase=phase, sync_enabled=verifier if phase == "after_probe" else False,
            returned=bool(result.results), refused=result.warden_status.refused,
            review_flag_persisted=bool(rows[0].get("needs_review")),
            review_warning_visible="Review required:" in context.context,
            warden_label=result.results[0].warden_label if expected_returned else None,
            evidence=asdict(evidence) if evidence else None))
        assert rows == persisted  # Recall/restart do not write or erase verifier state.
    assert report["phases"][0]["evidence"] == report["phases"][1]["evidence"]
    record_property("warden_verifier_control", json.dumps(report, sort_keys=True, default=str))
