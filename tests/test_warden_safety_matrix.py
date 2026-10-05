"""Offline development controls, not corpus accuracy or default qualification.

Run with ``pytest tests/test_warden_safety_matrix.py -o junit_family=legacy
--junitxml=warden.xml``.
The JSON report is embedded in the JUnit property ``warden_safety_report``;
pytest scratch files are removed by the repository cleanup fixture. Real
recall/guard logic runs against the existing stub adapters.
"""
from dataclasses import asdict, dataclass
import json

import pytest

from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


@dataclass(frozen=True)
class Control:
    name: str
    gold: str
    evidence: str = "git"
    project: str = "menhir"
    expired: bool = False
    historical: bool = False
    conflict: bool = False
    resolved: bool = False


# Gold describes the authored fixture, independently of what the guards decide.
# Missing evidence is unknown, not proof that a statement is false.
CONTROLS = (
    Control("anchored_valid", "useful"),
    Control("conversation_valid", "useful", evidence="agent"),
    Control("unknown_provenance", "unknown", evidence="none", project=""),
    Control("wrong_scope", "harmful", project="other-project"),
    Control("stale_current", "harmful", expired=True),
    Control("stale_historical", "useful", expired=True, historical=True),
    Control("recorded_conflict", "harmful", conflict=True),
    Control("resolved_conflict", "useful", conflict=True, resolved=True),
    Control("historical_conflict", "useful", conflict=True, historical=True),
)
PROFILES = {"off": (False, False), "strict": (True, True), "conversational": (True, False)}
OPTIONAL_GUARDS = ((False, False), (True, False), (False, True), (True, True))


def summarize(rows: list[dict]) -> dict[str, int]:
    """Keep useful refusals, harmful admissions and unknowns separate."""
    return {
        "useful_refused": sum(r["gold"] == "useful" and not r["returned"] for r in rows),
        "useful_returned": sum(r["gold"] == "useful" and r["returned"] for r in rows),
        "harmful_refused": sum(r["gold"] == "harmful" and not r["returned"] for r in rows),
        "harmful_returned_warned": sum(
            r["gold"] == "harmful" and r["returned"] and bool(r["warnings"]) for r in rows),
        "harmful_returned_unwarned": sum(
            r["gold"] == "harmful" and r["returned"] and not r["warnings"] for r in rows),
        "unknown_returned": sum(r["gold"] == "unknown" and r["returned"] for r in rows),
        "unknown_refused": sum(r["gold"] == "unknown" and not r["returned"] for r in rows),
    }


def test_report_negative_controls_detect_both_errors():
    rows = [dict(gold="useful", returned=False, warnings=[]),
            dict(gold="harmful", returned=True, warnings=[]),
            dict(gold="harmful", returned=True, warnings=["conflict"]),
            dict(gold="unknown", returned=True, warnings=[])]
    assert summarize(rows) == dict(useful_refused=1, useful_returned=0,
        harmful_refused=0, harmful_returned_warned=1, harmful_returned_unwarned=1,
        unknown_returned=1, unknown_refused=0)
    repaired = [{**rows[0], "returned": True}, {**rows[1], "returned": False}]
    assert summarize(repaired)["useful_refused"] == 0
    assert summarize(repaired)["harmful_returned_unwarned"] == 0


@pytest.mark.asyncio
async def test_offline_profile_safety_matrix(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, record_property, monkeypatch,
):
    # A developer's oracle-ablation environment must not change this fixed panel.
    monkeypatch.delenv("MENHIR_FRONTIER_ORACLE_SUBSET", raising=False)
    adapter = stub_memory_graph_adapter
    adapter.stale_anchored_memories = lambda **kwargs: []
    service = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=adapter,
                            scoring_service=ScoringService(), read_only=True)
    report = dict(schema_version=1, qualified_for_defaults=False,
                  limits=["Nine authored single-candidate controls; no accuracy estimate",
                          "Fixed candidate pool; no retrieval/ranking or answer-quality qualification",
                          "Temporal/conflict warnings do not equal refusal",
                          "No ingest, graph, model calls or deployed enforcement evidence"],
                  controls=[asdict(c) for c in CONTROLS], runs=[])
    baseline_scores = {}
    for currentness, contradiction in OPTIONAL_GUARDS:
        for profile, (gate, anchor) in PROFILES.items():
            tuning = RetrievalTuningConfig(enable_assertion_shadow=False,
                enable_warden_gate=gate, enable_evidence_anchor=anchor,
                enable_intent_lens=True, enable_belief_gate=currentness,
                enable_contradiction_interrupt=contradiction)
            rows = []
            for case in CONTROLS:
                stub_graphiti_client.search_scored_results = [(case.name, "config", .85)]
                adapter.candidate_metadata = [dict(uuid=case.name, name="config",
                    content="Authored configuration control", namespace="tenant",
                    scope="PERSISTENT", type="SEMANTIC", freshness="ACTIVE",
                    conflict_group_id="conflict-1" if case.conflict else None,
                    conflict_status=("resolved" if case.resolved else "unresolved") if case.conflict else None)]
                adapter.candidate_provenance_rows = [dict(uuid=case.name,
                    evidence_node_kinds=["git"] if case.evidence == "git" else [],
                    anchor_projects=[case.project] if case.project else [],
                    episode_sources=["claude-code"] if case.evidence == "agent" else [])]
                # Agent-only controls must not accidentally acquire a file anchor.
                if case.evidence != "git":
                    adapter.candidate_provenance_rows[0]["anchor_projects"] = []
                adapter.temporal_fact_rows = ([dict(node_uuid=case.name, fact="Former setting",
                    expired_at="2020-01-02T00:00:00Z", valid_at="2020-01-01T00:00:00Z")]
                    if case.expired else [])
                query = "have we already tried this configuration" if case.historical else "configuration"
                result = await service.recall(query, namespace="tenant", file_context_project="menhir",
                    include_invalidated=True, source_memory_limit=0, update_access=False,
                    tuning=tuning, include_warden_status=True)
                assert not result.warden_status.metadata_gaps
                expected_state = ("applied" if gate else "computed_not_applied" if currentness
                                  else "not_run" if contradiction else "disabled")
                assert result.warden_status.state == expected_state
                warnings = []
                if result.results:
                    memory = result.results[0]
                    assert memory.uuid == case.name
                    if memory.warden_label:
                        warnings.append("warden:" + memory.warden_label)
                    if memory.breakdown.conflict_bonus == 1.0:
                        warnings.append("context:unresolved_conflict")
                    if any(f.expired_at for f in memory.temporal_facts):
                        warnings.append("temporal:expired_fact")
                returned = bool(result.results)
                score = result.results[0].final_score if returned else None
                if profile == "off" and not currentness and not contradiction:
                    baseline_scores[case.name] = score
                elif returned:
                    assert score == baseline_scores[case.name]
                # Pin current behavior, including known gaps, without relabeling gold.
                expected = not (gate and (case.name == "wrong_scope"
                    or (anchor and case.evidence != "git")
                    or (contradiction and case.name == "recorded_conflict")
                    or (currentness and contradiction and case.name == "stale_current")))
                assert returned == expected, (profile, currentness, contradiction, case.name, result)
                if gate:
                    assert result.warden_status.evaluated == 1
                    assert result.warden_status.refused == int(not returned)
                if case.conflict and returned:
                    assert ("context:unresolved_conflict" in warnings) == (not case.resolved)
                    if gate and contradiction and case.historical:
                        assert "warden:conflict" in warnings
                if case.resolved:
                    assert not warnings
                if gate and currentness and case.historical and case.expired:
                    assert "warden:historical" in warnings
                rows.append(dict(control=case.name, query=query, gold=case.gold, returned=returned,
                    final_score=score, warnings=warnings, warden=asdict(result.warden_status)))
            report["runs"].append(dict(profile=profile, currentness=currentness,
                contradiction=contradiction,
                tuning=asdict(tuning), rows=rows, counts=summarize(rows)))
    path = tmp_path / "warden-safety-controls.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    record_property("warden_safety_report", json.dumps(report, sort_keys=True, default=str))
    assert len(report["runs"]) == 12
