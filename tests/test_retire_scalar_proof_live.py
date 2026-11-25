"""Characterization: retiring every SOURCE memory of a scalar-state View changes nothing about it.

Menhir will add a visibility-only "retired" state for old memories. The safety property pinned here:
marking every source of a scalar View (its :TurnEvidence, the admitting :Episodic, the subject
:Entity and a plain value :Entity) retired leaves the View's value, counts, history, receipt /
MENTIONS provenance and default-recall visibility untouched, through every re-fold path
(rebuild_scalar_projections, run_scalar_consolidation, run_counting_consolidation, the View
projection repair pass), with the SAME View uuid (no new version).

The marker is two NEW properties, `memory_retired` and `memory_retired_at`, set on source nodes
only. The bare `retired` property already means "retired View" on View nodes, so it is not used
as the source marker; test 4 flips it on the View to prove the visibility snapshot can detect a
change. Today nothing reads the marker, so these pass; they fail if a future change makes the
scalar pipeline (fold, counts, history, receipt, recall predicate) react to retired sources.

Online: needs the test Neo4j instance. Everything is built through production write paths
(TurnEvidenceRepository, TypedAssertionRepository, record_count_decisions, the rebuilds); no View
node is written by hand.
"""

from __future__ import annotations

import uuid as uuidlib
from typing import Any

import pytest

from menhir.domain.recall_visibility import default_recall_visibility_cypher
from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.services.counting_consolidation import (
    CountingConsolidationConfig,
    run_counting_consolidation,
)
from menhir.services.scalar_consolidation import (
    ScalarConsolidationConfig,
    run_scalar_consolidation,
)

pytestmark = pytest.mark.online

_PV_COUNTING = "v1+x2+m"  # effective counting version with extraction v2 and match on
_T1 = "2026-07-01T00:00:00+00:00"
_T2 = "2026-07-10T00:00:00+00:00"
# Properties a no-op refresh may legitimately restamp; everything else must be byte-identical.
# `last_accessed` is the read-time access stamp (fetching a View touches it); it is not state.
_VOLATILE = {"updated_at", "refreshed_at", "last_rebuilt_at", "last_seen_at", "last_accessed"}


class _NoLlm:
    """The re-fold paths must never need a model: any call is a failure of the test premise."""

    calls = 0

    def __call__(self, system: str, user: str) -> str:  # pragma: no cover - failure path
        raise AssertionError("re-fold path called the LLM")


# ---------------------------------------------------------------------------------- seeding

class _Case:
    """One namespace, one subject, the source nodes and the production objects that fold them."""

    def __init__(self, repo: Any, namespace: str) -> None:
        self.repo = repo
        self.ns = namespace
        self.subject = f"retire-subject-{uuidlib.uuid4().hex}"
        self.value_entity = f"retire-value-{uuidlib.uuid4().hex}"
        self.turns: list[str] = []
        self.episodes: list[str] = []
        # Counting on (as in a counting deployment) so every re-fold path folds occurrences too;
        # with it off the scalar runner would retire a counted View as an undesired slot.
        self.adapter = MemoryGraphAdapter(neo4j=repo, counting_enabled=True)
        self.adapter.bootstrap_phase_one()
        self.adapter.activate_scalar_state()
        self.assertions = TypedAssertionRepository(repo)
        repo.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $ns})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            WITH f
            CREATE (s:Entity {uuid: $subject, name: 'my coins', group_id: $ns, test_tag: $ns})
            CREATE (v:Entity {uuid: $value, name: 'a handful of coins', group_id: $ns,
                              test_tag: $ns})
            """,
            {"ns": namespace, "subject": self.subject, "value": self.value_entity},
        )

    def turn(self, text: str) -> str:
        """A user turn captured as evidence and admitted by a finalized Episodic that mentions the
        subject and the plain value entity."""
        n = len(self.turns)
        turn_id = TurnEvidenceRepository(self.repo).record_turn_evidence(
            text=text, role="user", declarant="user", namespace=self.ns,
            source_kind="claude_code_hook", session_id=f"{self.subject}-session",
            prompt_id=f"{self.subject}-prompt-{n}")["turn_id"]
        episode = f"retire-episode-{n}-{uuidlib.uuid4().hex}"
        self.repo.execute(
            """
            MATCH (f:EvidenceNamespaceFence {namespace_key: $ns})
            MATCH (t:TurnEvidence {turn_id: $turn})
            MATCH (s:Entity {uuid: $subject}), (v:Entity {uuid: $value})
            CREATE (e:Episodic {uuid: $episode, name: 'episode', group_id: $ns, namespace: $ns,
                                processing_state: 'COMPLETED', evidence_finalized: true,
                                evidence_generation: f.generation, test_tag: $ns})
            CREATE (e)-[:ADMITTED_ON]->(t)
            CREATE (e)-[:MENTIONS]->(s)
            CREATE (e)-[:MENTIONS]->(v)
            """,
            {"ns": self.ns, "turn": turn_id, "subject": self.subject,
             "value": self.value_entity, "episode": episode},
        )
        self.turns.append(turn_id)
        self.episodes.append(episode)
        return turn_id

    def absolute(self, turn_id: str, text: str, span: str, value: int, valid_at: str, *,
                 semantics: str = "ordinary", attribute: str = "owned") -> str:
        start = text.index(span)
        rec = self.assertions.record_assertion(TypedAssertion(
            subject_uuid=self.subject, subject_display="my coins", attribute=attribute, scope="",
            value_kind="count", unit="", operation="absolute", value=value, stated_span=span,
            span_start=start, span_end=start + len(span), episode_uuid=turn_id,
            valid_at=valid_at, learned_at=valid_at, evidence_tier="user",
            perceiver_version="v1", namespace=self.ns, absolute_semantics=semantics))
        assert rec["binding_pending"] is False
        return str(rec["assertion_id"])

    def occurrence(self, turn_id: str, text: str, span: str, valid_at: str, *,
                   attribute: str, count_object: str) -> str:
        start = text.index(span)
        rec = self.assertions.record_assertion(TypedAssertion(
            subject_uuid=self.subject, subject_display="my coins", attribute=attribute, scope="",
            value_kind="count", unit="", operation="occurrence", value=1, stated_span=span,
            span_start=start, span_end=start + len(span), claim_ordinal=1, episode_uuid=turn_id,
            valid_at=valid_at, learned_at=valid_at, evidence_tier="user",
            perceiver_version=_PV_COUNTING, namespace=self.ns, importance="tracked",
            count_object=count_object))
        assert rec["binding_pending"] is False
        return str(rec["assertion_id"])

    def decide_counted(self, assertion_ids: list[str]) -> None:
        """Record the count decisions through the production fenced write (no LLM)."""
        claim = self.adapter.claim_counting_namespace(
            self.ns, run_id="seed-run", lease_seconds=600, now=_T2)
        decisions = [{"assertion_id": a, "status": "counted", "reason": "decided_new",
                      "family": "coin", "vote": "3/3"} for a in assertion_ids]
        out = self.adapter.record_count_decisions(
            self.ns, decisions, perceiver_version=_PV_COUNTING, run_id="seed-run",
            incarnation=claim["incarnation"], epoch=claim["epoch"])
        assert out["fence_ok"] is True and out["written"] == len(assertion_ids)
        self.adapter.release_counting_claim(
            self.ns, run_id="seed-run", incarnation=claim["incarnation"])

    def build(self) -> dict[str, Any]:
        """The production rebuild the consolidation passes use (state + history, count match)."""
        service = self.adapter.scalar_state_service(scalar_history_enabled=True)
        out = service.rebuild_scalar_projections(
            self.subject, namespace=self.ns, history_enabled=True)
        assert out["complete"] is True, out
        return out

    # ---------------------------------------------------------------------- re-fold paths

    def refold_all_paths(self) -> None:
        """Every production path that re-folds from the TypedAssertion log or re-checks Views."""
        self.build()
        scalar = run_scalar_consolidation(
            self.adapter, scalar_targets=[self.ns], namespaces=[self.ns],
            counting_llm=_NoLlm(), build_episodes=lambda rows: [], k=3, threshold=2 / 3,
            call_budget=None, embed=None,
            config=ScalarConsolidationConfig(scalar_history_enabled=True))
        assert scalar["scalar_namespaces_failed"] == 0, scalar
        counting = run_counting_consolidation(
            self.adapter, namespaces=[self.ns], counting_llm=_NoLlm(),
            build_episodes=lambda rows: [], k=3, threshold=2 / 3, call_budget=None, embed=None,
            config=CountingConsolidationConfig(scalar_history_enabled=True))
        assert counting["counting_namespaces_failed"] == 0, counting
        assert counting["counting_namespaces_busy"] == 0, counting
        self.adapter.run_view_projection_repairs(
            [self.ns], owner_id=f"retire-{uuidlib.uuid4().hex}", scalar_history_enabled=True)
        self.build()

    # ---------------------------------------------------------------------- retire marker

    def retire_sources(self) -> int:
        """Set the NEW marker on source nodes only: turns, episodes, subject and value entity."""
        rows = self.repo.execute(
            """
            MATCH (n)
            WHERE (n:TurnEvidence AND n.turn_id IN $turns)
               OR (n:Episodic AND n.uuid IN $episodes)
               OR (n:Entity AND n.uuid IN [$subject, $value] AND NOT coalesce(n.is_view, false))
            SET n.memory_retired = true, n.memory_retired_at = datetime()
            RETURN count(n) AS marked
            """,
            {"turns": self.turns, "episodes": self.episodes, "subject": self.subject,
             "value": self.value_entity},
        )
        marked = int(rows[0]["marked"])
        assert marked == len(self.turns) + len(self.episodes) + 2
        # Premise guard: the marker is on sources only, never on a View or an assertion row.
        stray = self.repo.execute(
            "MATCH (n) WHERE n.memory_retired IS NOT NULL AND "
            "(n.view_kind IS NOT NULL OR n:TypedAssertion OR n:TypedAssertionHead) "
            "RETURN count(n) AS stray")[0]["stray"]
        assert stray == 0
        return marked

    # ---------------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        repo = self.repo
        views = repo.execute(
            "MATCH (v:Entity {view_kind: 'scalar_state', view_subject_uuid: $s, group_id: $ns}) "
            "RETURN v.uuid AS uuid, properties(v) AS props ORDER BY v.view_key, v.uuid",
            {"s": self.subject, "ns": self.ns})
        snap_views: dict[str, Any] = {}
        for row in views:
            props = {k: _norm(v) for k, v in dict(row["props"]).items() if k not in _VOLATILE}
            snap_views[row["uuid"]] = {
                "props": props,
                "mentions": sorted(r["id"] for r in repo.execute(
                    "MATCH (e)-[:MENTIONS]->(:Entity {uuid:$u}) "
                    "RETURN coalesce(e.uuid, e.turn_id) AS id", {"u": row["uuid"]})),
                "visible": _visible(repo, row["uuid"]),
            }
        history = repo.execute(
            "MATCH (h:Entity {view_kind: 'scalar_history', view_subject_uuid: $s, group_id: $ns}) "
            "OPTIONAL MATCH (h)-[:HISTORY_ENTRY]->(a:TypedAssertion) "
            "RETURN h.uuid AS uuid, coalesce(h.view_current, true) AS current, "
            "h.view_key AS view_key, properties(h) AS props, "
            "collect(DISTINCT a.assertion_id) AS entries ORDER BY h.view_key, h.uuid",
            {"s": self.subject, "ns": self.ns})
        snap_history = [
            {"uuid": r["uuid"], "current": r["current"], "view_key": r["view_key"],
             "entries": sorted(r["entries"]),
             "props": {k: _norm(v) for k, v in dict(r["props"]).items() if k not in _VOLATILE}}
            for r in history]
        rows = self.assertions.assertions_for_entity(
            self.subject, include_superseded=True, namespace=self.ns)
        raw = repo.execute(
            "MATCH (a:TypedAssertion {subject_uuid: $s, namespace: $ns}) "
            "RETURN a.assertion_id AS id, a.value_json AS value_json, a.value AS value, "
            "a.superseded AS superseded, a.operation AS operation, "
            "a.count_status AS count_status, a.count_target_id AS count_target_id, "
            "a.count_reason AS count_reason, a.count_attempts AS count_attempts, "
            "a.count_run_id AS count_run_id, a.count_family AS count_family, "
            "toString(a.count_decided_at) AS count_decided_at, "
            "a.binding_pending AS binding_pending ORDER BY a.assertion_id",
            {"s": self.subject, "ns": self.ns})
        current = [
            (str(w["view_key"]) if (w := self.adapter.fetch_current_scalar_view_for_slot(
                subject_uuid=self.subject, attribute=attr, scope="", value_kind="count",
                unit="", namespace=self.ns)) else None, w["uuid"] if w else None)
            for attr in sorted({str(v["props"].get("ss_attribute")) for v in snap_views.values()})]
        return {
            "views": snap_views,
            "history": snap_history,
            "current_by_slot": current,
            "assertions": [dict(r) for r in raw],
            "fold_input": [
                {k: r.get(k) for k in ("assertion_id", "episode_uuid", "value", "operation",
                                       "superseded", "count_status", "count_target_id")}
                for r in rows],
        }


def _norm(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _visible(raw: Any, view_uuid: str) -> bool:
    rows = raw.execute(
        f"MATCH (n:Entity {{uuid:$u}}) RETURN {default_recall_visibility_cypher('n')} AS ok",
        {"u": view_uuid})
    return bool(rows and rows[0]["ok"])


@pytest.fixture
def case(test_neo4j_repo):
    namespace = f"test-retire-proof-{uuidlib.uuid4().hex}"
    c = _Case(test_neo4j_repo, namespace)
    yield c
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.test_tag = $ns OR n.namespace = $ns OR n.group_id = $ns "
        "OR n.namespace_key = $ns OR n.subject_uuid = $subject DETACH DELETE n",
        {"ns": namespace, "subject": c.subject})


def _assert_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    # Same View uuids, same count: the rebuild created no new version.
    assert list(after["views"]) == list(before["views"])
    for view_uuid, was in before["views"].items():
        now = after["views"][view_uuid]
        assert now["props"] == was["props"]          # value (ss_*), receipt, current/retired flags
        assert now["mentions"] == was["mentions"]    # incoming MENTIONS provenance
        assert now["visible"] is was["visible"]
    assert after["history"] == before["history"]
    assert after["current_by_slot"] == before["current_by_slot"]
    assert after["assertions"] == before["assertions"]
    assert after["fold_input"] == before["fold_input"]


# ---------------------------------------------------------------------------------- tests

def test_retiring_all_sources_leaves_scalar_view_unchanged(case):
    t1 = case.turn("I have 30 coins")
    t2 = case.turn("Actually I have 37 coins now")
    case.absolute(t1, "I have 30 coins", "I have 30 coins", 30, _T1)
    case.absolute(t2, "Actually I have 37 coins now", "Actually I have 37 coins", 37, _T2,
                  semantics="correction")
    case.build()

    before = case.snapshot()
    (uuid_,) = [u for u, v in before["views"].items() if v["props"].get("ss_attribute") == "owned"]
    view = before["views"][uuid_]
    # Premise (read back from the store, not the seed): a real, current, recall-visible View
    # holding the correction, whose receipt is the anchoring (second) episode and equals its
    # MENTIONS set. The superseded first source is reachable through history only.
    assert str(view["props"]["ss_value"]) == "37"
    assert view["props"]["view_current"] is True
    assert view["visible"] is True
    assert view["props"]["episode_uuids"] == [case.episodes[1]]
    assert view["mentions"] == [case.episodes[1]]
    assert [h for h in before["history"] if h["current"]] and \
        len(before["history"][0]["entries"]) == 2

    case.retire_sources()
    case.refold_all_paths()

    after = case.snapshot()
    _assert_unchanged(before, after)
    assert after["views"][uuid_]["visible"] is True


def test_retired_source_with_live_reason_still_counts(case):
    text = ("I used to have 30 coins, I sold some because I needed rent; now I have 37")
    t = case.turn(text)
    case.absolute(t, text, "I used to have 30 coins", 30, _T1)
    case.absolute(t, text, "now I have 37", 37, _T2)
    sold = case.occurrence(t, text, "I sold some", _T1, attribute="coins_sold_count",
                           count_object="coin")
    case.decide_counted([sold])
    case.build()

    before = case.snapshot()
    by_attr = {v["props"].get("ss_attribute"): v for v in before["views"].values()}
    assert str(by_attr["owned"]["props"]["ss_value"]) == "37"
    # the reason is a counted occurrence; its View is keyed by the counted object, not our attribute
    (sold_uuid, sold_view), = [(u, v) for u, v in before["views"].items()
                               if v["props"].get("scalar_basis") == "counted"]
    assert str(sold_view["props"]["ss_value"]) == "1" and sold_view["visible"] is True
    row = next(a for a in before["assertions"] if a["id"] == sold)
    assert row["count_status"] == "counted" and row["count_decided_at"]

    case.retire_sources()
    case.refold_all_paths()

    after = case.snapshot()
    _assert_unchanged(before, after)
    row_after = next(a for a in after["assertions"] if a["id"] == sold)
    assert (row_after["count_status"], row_after["count_decided_at"]) == (
        "counted", row["count_decided_at"])
    assert after["views"][sold_uuid]["visible"] is True


def test_retiring_sources_of_a_counted_view_keeps_counts(case):
    texts = ["I went to the gym today", "I went to the gym again", "I went to the gym a third time"]
    ids = []
    for i, text in enumerate(texts):
        t = case.turn(text)
        ids.append(case.occurrence(
            t, text, text, f"2026-07-0{i + 1}T00:00:00+00:00", attribute="gym_visit_count",
            count_object="gym visit"))
    case.decide_counted(ids)
    case.build()

    before = case.snapshot()
    counted = [v for v in before["views"].values()
               if v["props"].get("scalar_basis") == "counted"]
    assert len(counted) == 1
    assert str(counted[0]["props"]["ss_value"]) == "3"
    assert counted[0]["props"].get("scalar_basis") == "counted"
    assert counted[0]["visible"] is True
    assert {a["count_status"] for a in before["assertions"]} == {"counted"}
    assert all(a["count_decided_at"] for a in before["assertions"])

    case.retire_sources()
    case.refold_all_paths()

    after = case.snapshot()
    _assert_unchanged(before, after)
    assert [a["count_status"] for a in after["assertions"]] == ["counted"] * 3
    assert [a["count_decided_at"] for a in after["assertions"]] == [
        a["count_decided_at"] for a in before["assertions"]]


def test_flipping_the_views_own_retired_flag_is_detected_by_the_snapshot(case):
    # Sensitivity guard: the snapshot's visibility check CAN see a change. Retiring the sources
    # does not hide the View; flipping `retired` on the View itself does.
    t1 = case.turn("I have 30 coins")
    t2 = case.turn("Actually I have 37 coins now")
    case.absolute(t1, "I have 30 coins", "I have 30 coins", 30, _T1)
    case.absolute(t2, "Actually I have 37 coins now", "Actually I have 37 coins", 37, _T2,
                  semantics="correction")
    case.build()
    before = case.snapshot()
    view_uuid = next(u for u, v in before["views"].items()
                     if v["props"].get("ss_attribute") == "owned")
    assert before["views"][view_uuid]["visible"] is True

    case.retire_sources()
    assert _visible(case.repo, view_uuid) is True

    case.repo.execute("MATCH (v:Entity {uuid: $u}) SET v.retired = true", {"u": view_uuid})
    assert _visible(case.repo, view_uuid) is False
    after = case.snapshot()
    assert after["views"][view_uuid]["visible"] is False
    with pytest.raises(AssertionError):
        _assert_unchanged(before, after)
