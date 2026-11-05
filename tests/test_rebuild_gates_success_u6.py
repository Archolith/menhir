"""U6: a rebuild result gates success (CF-266, CF-290) and a rebuild folds like the writer (CF-267,
CF-270). Offline."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from menhir.domain.scalar_state_fold import fold_assertions
from menhir.domain.typed_assertion import normalize_scalar
from menhir.infrastructure.projection_coverage_repository import ProjectionCoverageRepository
from menhir.infrastructure.typed_assertion_write_repository import TypedAssertionWriteMixin
from menhir.services.counting_consolidation import (
    CountingConsolidationConfig,
    run_counting_consolidation,
    sweep_counting_flag_state,
)
from menhir.services.projection_coverage_service import ProjectionCoverageService
from menhir.services.scalar_state_service import ScalarStateService
from menhir.services.typed_scalar_persistence import repair_pending_bindings

pytestmark = pytest.mark.unit

NS = "tenant-a"
SUBJECT = "entity-1"


# ---------------------------------------------------------------- CF-266: the sweep

class _SweepGraph:
    def __init__(self, subjects, results):
        self.subjects = sorted(subjects)
        self.results = results  # subject -> result dict or Exception
        self.marks: list[str] = []
        self.rebuilt: list[str] = []

    def counting_flag_sweep_targets(self, **kw):
        return ["a"]

    def counting_sweep_subjects(self, namespace, *, limit, after=""):
        return [s for s in self.subjects if s > after][:limit]

    def mark_counting_flag_state(self, namespace, **kw):
        self.marks.append(namespace)

    def scalar_state_service(self, **kw):
        graph = self

        class _State:
            def rebuild_scalar_state(self, subject, *, namespace=None, **_kw):
                graph.rebuilt.append(subject)
                result = graph.results[subject]
                if isinstance(result, Exception):
                    raise result
                return result

        return _State()


def test_sweep_with_one_incomplete_subject_leaves_namespace_unmarked():
    graph = _SweepGraph(["s1", "s2", "s3"], {
        "s1": {"complete": True}, "s2": {"complete": False}, "s3": {"complete": True}})
    out = sweep_counting_flag_state(
        graph, enabled=True, namespaces=None, scalar_history_enabled=False)
    assert graph.marks == []
    assert out["counting_sweep_namespaces"] == 0
    assert out["counting_sweep_failed"] == 1
    assert out["counting_sweep_subjects_rebuilt"] == 2


def test_sweep_raising_subject_does_not_abort_the_others_and_leaves_namespace_unmarked():
    graph = _SweepGraph(["s1", "s2", "s3"], {
        "s1": {"complete": True}, "s2": RuntimeError("boom"), "s3": {"complete": True}})
    out = sweep_counting_flag_state(
        graph, enabled=True, namespaces=None, scalar_history_enabled=False)
    assert graph.rebuilt == ["s1", "s2", "s3"]
    assert graph.marks == []
    assert out["counting_sweep_failed"] == 1 and out["counting_sweep_subjects_rebuilt"] == 2


def test_sweep_with_all_complete_marks_namespace():
    graph = _SweepGraph(["s1"], {"s1": {"complete": True}})
    out = sweep_counting_flag_state(
        graph, enabled=True, namespaces=None, scalar_history_enabled=False)
    assert graph.marks == ["a"] and out["counting_sweep_namespaces"] == 1


# ---------------------------------------------------------------- CF-290: durable decision obligation

@dataclass
class _Ep:
    uuid: str
    content: str


def _build_episodes(rows):
    return [_Ep(uuid=str(r["uuid"]), content=str(r["content"])) for r in rows]


def _page_row(i):
    return {"uuid": f"t{i}", "valid_at": f"2026-07-0{i}T00:00:00Z",
            "cursor_at": f"2026-07-0{i}T00:00:00Z", "content": f"I finished kit {i}"}


def _occ(aid, subject="s1"):
    return dict(assertion_id=aid, subject_uuid=subject, value_kind="count",
                attribute="model_kit_count", count_object="model kit", count_item="",
                valid_at="2026-07-01T00:00:00+00:00", episode_uuid=f"ep-{aid}", count_status="")


class _Crash(BaseException):
    """A process death: not an Exception, so no handler in the runner swallows it."""


class _Svc:
    """Rebuild sink; `plan` is a list of outcomes consumed per call, then healthy."""

    def __init__(self, plan=()):
        self.plan = list(plan)
        self.calls: list[str] = []

    def rebuild_scalar_state(self, subject, *, namespace=None, **kw):
        self.calls.append(subject)
        outcome = self.plan.pop(0) if self.plan else {"complete": True}
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    rebuild_scalar_projections = rebuild_scalar_state


class _Graph:
    """A fake with the durable projection_pending flag: record_count_decisions sets it (as the
    Cypher does), mark_projection_complete clears it, and the cursor only moves on advance."""

    def __init__(self, pages, renew_ok_calls=None):
        self.pages = [list(p) for p in pages]
        self.renew_ok_calls = renew_ok_calls
        self.renews = 0
        self.pending: set[str] = set()
        self.cursor_advances = 0
        self.svc = _Svc()
        self.occ = {"a1": _occ("a1")}

    def scalar_state_service(self, **kw):
        return self.svc

    def list_counting_dirty_namespaces(self, **kw):
        return []

    def load_next_counting_batch(self, namespace, **kw):
        return list(self.pages[0]) if self.pages else []

    def advance_counting_cursor(self, namespace, **kw):
        self.cursor_advances += 1
        self.pages.pop(0)

    def claim_counting_namespace(self, namespace, **kw):
        return {"incarnation": "inc-1", "epoch": 7}

    def renew_counting_claim(self, namespace, **kw):
        self.renews += 1
        return self.renew_ok_calls is None or self.renews <= self.renew_ok_calls

    def release_counting_claim(self, namespace, **kw):
        pass

    def prior_version_occurrences(self, namespace, turn_ids, **kw):
        return []

    def retire_prior_version_occurrences(self, namespace, turn_ids, **kw):
        return []

    def mark_counting_flag_state(self, namespace, **kw):
        pass

    def scalar_claims_for_turns(self, namespace, turn_ids, **kw):
        return []

    def count_occurrences_by_ids(self, namespace, ids, **kw):
        return [self.occ[i] for i in ids if i in self.occ]

    def counted_occurrences_for_subjects(self, namespace, subjects, **kw):
        return []

    def related_counted_occurrences(self, namespace, probes, **kw):
        return []

    def undecided_count_occurrences(self, namespace, **kw):
        return []

    def redecide_count_occurrences(self, namespace, **kw):
        return []

    def cosine_related_counted(self, namespace, probes, **kw):
        return []

    def record_count_decisions(self, namespace, decisions, **kw):
        self.pending |= {d["assertion_id"] for d in decisions}
        return {"fence_ok": True}

    def mark_projection_complete(self, assertion_ids):
        self.pending -= set(assertion_ids)
        return len(assertion_ids)


class _Perception:
    raise_on_call: int | None = None
    calls = 0

    def __init__(self, graph, state_service, **kwargs):
        pass

    def ensure_activated(self):
        pass

    def perceive_and_persist(self, episodes, llm, **kwargs):
        _Perception.calls += 1
        if _Perception.raise_on_call == _Perception.calls:
            raise RuntimeError("llm exploded")
        return {"bound": 1, "advisory": 0, "results": [{"bound": True, "assertion_id": "a1"}]}


@pytest.fixture
def perception(monkeypatch):
    import menhir.services.typed_scalar_perception as tsp

    _Perception.raise_on_call = None
    _Perception.calls = 0
    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _Perception)
    return _Perception


class _Llm:
    calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return "[]"


def _run(graph):
    return run_counting_consolidation(
        graph, namespaces=["ns-1"], counting_llm=_Llm(), build_episodes=_build_episodes,
        k=1, threshold=1.0, call_budget=None, embed=None,
        config=CountingConsolidationConfig(
            perceiver_version="cv1", scalar_perceiver_version="sv1", batch_size=1, match=True))


def _heal(graph, *, fail_first=False):
    """What the scalar repair pass does with a flagged row (the real `repair_pending_bindings`)."""
    rebuilt: list[str] = []

    def rebuild(subject, ns):
        rebuilt.append(subject)
        if fail_first and len(rebuilt) == 1:
            return {"complete": False}
        return {"complete": True}

    rows = [{"assertion_id": a, "source_key": a, "subject_uuid": graph.occ[a]["subject_uuid"],
             "namespace": "ns-1", "binding_pending": False, "projection_pending": True}
            for a in sorted(graph.pending)]
    out = repair_pending_bindings(
        rows, linked_entities_for_episode=lambda e: [], record_assertion=lambda a: {},
        rebuild_scalar_state=rebuild, mark_projection_complete=graph.mark_projection_complete)
    return out, rebuilt


@pytest.mark.parametrize("failure", [
    RuntimeError("boom"), {"complete": False}], ids=["raises", "incomplete"])
def test_failed_rebuild_after_decision_leaves_flag_and_a_later_pass_heals(perception, failure):
    graph = _Graph([[_page_row(1)]])
    graph.svc.plan = [failure]  # the post-decision rebuild fails
    out = _run(graph)
    assert out["counting_namespaces_failed"] == 0
    assert graph.cursor_advances == 1          # the flag carries the obligation, so the cursor moves
    assert graph.pending == {"a1"}
    _, rebuilt = _heal(graph)
    assert rebuilt == ["s1"] and graph.pending == set()


def test_successful_rebuild_after_decision_clears_the_flag(perception):
    graph = _Graph([[_page_row(1)]])
    _run(graph)
    assert graph.pending == set()


def test_crash_between_decision_and_rebuild_leaves_flag_and_a_later_pass_heals(perception):
    graph = _Graph([[_page_row(1)]])
    graph.svc.plan = [_Crash()]
    with pytest.raises(_Crash):
        _run(graph)
    assert graph.pending == {"a1"} and graph.cursor_advances == 0
    _, rebuilt = _heal(graph)
    assert rebuilt == ["s1"] and graph.pending == set()


def test_repair_that_also_fails_keeps_the_flag_until_a_pass_succeeds(perception):
    graph = _Graph([[_page_row(1)]])
    graph.svc.plan = [{"complete": False}]
    _run(graph)
    out, _ = _heal(graph, fail_first=True)
    assert out["errored"] == 1 and graph.pending == {"a1"}
    _heal(graph)
    assert graph.pending == set()


def test_fence_lost_on_a_later_page_leaves_flag_and_a_later_pass_heals(perception):
    graph = _Graph([[_page_row(1)], [_page_row(2)]], renew_ok_calls=2)
    graph.svc.plan = [{"complete": False}]
    out = _run(graph)
    assert out["counting_namespaces_fenced"] == 1
    assert graph.pending == {"a1"}
    _heal(graph)
    assert graph.pending == set()


def test_llm_exception_on_a_later_page_leaves_flag_and_a_later_pass_heals(perception):
    perception.raise_on_call = 2
    graph = _Graph([[_page_row(1)], [_page_row(2)]])
    graph.svc.plan = [{"complete": False}]
    out = _run(graph)
    assert out["counting_namespaces_failed"] == 1
    assert graph.pending == {"a1"}
    _heal(graph)
    assert graph.pending == set()


def test_decision_statement_sets_projection_pending_with_the_decision():
    from menhir.infrastructure.count_decision_repository import _SET_DECISION

    assert "a.projection_pending = true" in _SET_DECISION
    assert "a.count_status = d.status" in _SET_DECISION


def test_owed_retry_isolates_a_raising_subject():
    from menhir.services.counting_consolidation import _rebuild_subjects

    svc = _Svc([{"complete": True}, RuntimeError("boom"), {"complete": True}])
    done = _rebuild_subjects(svc, "ns-1", {"s1", "s2", "s3"}, history=False)
    assert svc.calls == ["s1", "s2", "s3"] and done == {"s1", "s3"}


# ---------------------------------------------------------------- CF-267: future-dated rows

def _row(aid, valid_at, value=10):
    return {
        "assertion_id": aid, "assertion_key": f"ak-{aid}", "source_key": f"sk-{aid}",
        "subject_uuid": SUBJECT, "subject_display": "Entity One", "namespace": NS,
        "attribute": "score", "scope": "", "value_kind": "count", "unit": "",
        "operation": "absolute", "value": value, "valid_at": valid_at,
        "learned_at": "2026-01-01T00:00:00+00:00", "episode_uuid": f"ep-{aid}",
        "evidence_tier": "user", "perceiver_version": "v1", "absolute_semantics": "ordinary",
        "superseded": False, "binding_pending": False, "projection_pending": False,
    }


class _Assertions:
    def __init__(self, rows):
        self.rows = rows

    def materializable_assertions_for_entity(self, subject_uuid, *, namespace=None):
        return [dict(r) for r in self.rows]


class _Views:
    def __init__(self):
        self.written: list[dict] = []

    def record_scalar_state(self, **kw):
        self.written.append(kw)
        return {"view_key": "vk", "created": True}

    def list_scalar_state_views(self, **kw):
        return []

    def retire_scalar_state(self, **kw):
        return True


def _service(rows):
    views = _Views()
    return ScalarStateService(_Assertions(rows), views, counting_enabled=True), views


FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
PAST = "2026-01-01T00:00:00+00:00"


def test_rebuild_scalar_state_without_as_of_excludes_future_dated_assertion():
    svc, views = _service([_row("past", PAST, 10), _row("future", FUTURE, 99)])
    svc.rebuild_scalar_state(SUBJECT, namespace=NS)
    assert [w["value"] for w in views.written] == [10]


def test_history_off_counting_rebuild_keeps_future_dated_assertion_out_of_view():
    from menhir.services.counting_consolidation import _rebuild_subject

    svc, views = _service([_row("past", PAST, 10), _row("future", FUTURE, 99)])
    _rebuild_subject(svc, SUBJECT, NS, history=False)
    assert [w["value"] for w in views.written] == [10]


def test_projections_rebuild_without_as_of_excludes_future_dated_assertion():
    svc, views = _service([_row("past", PAST, 10), _row("future", FUTURE, 99)])
    svc.rebuild_scalar_projections(SUBJECT, namespace=NS)
    assert [w["value"] for w in views.written] == [10]


# ---------------------------------------------------------------- CF-270: coverage audit, real row shape

def _alias_set(text: str) -> set[str]:
    return set(re.findall(r"\bAS\s+(\w+)", text))


class _FakeNeo4j:
    """Returns each query only the columns it RETURNs: a field the query omits never reaches the
    service, however complete the fixture row is."""

    def __init__(self, assertion_rows, view_rows):
        self.assertion_rows = assertion_rows
        self.view_rows = view_rows

    def execute(self, query, params=None):
        source = self.assertion_rows if "TypedAssertionHead" in query else self.view_rows
        aliases = _alias_set(query.split("RETURN", 1)[1])
        return [{a: row.get(a) for a in aliases} for row in source]


def _audit_row(**over):
    row = _row("x", PAST, 1)
    row.update({
        "claim_key": "ck", "stated_span": None, "time_basis": None, "superseded_by": None,
        "activation_pending": False, "head_present": True, "head_subject_uuid": SUBJECT,
        "head_current_count": 1, "head_current_assertion_id": None,
        "head_current_subject_uuid": SUBJECT, "importance": "notable",
        "count_object": "", "count_item": "", "count_action": "", "count_status": "",
        "count_target_id": "", "count_family": "", "source_episode_uuid": "", "turn_id": "",
        "grounding_id": None,
    })
    row.update(over)
    row["head_current_assertion_id"] = row["assertion_id"]
    return row


def _occurrence(aid, status):
    return _audit_row(
        assertion_id=aid, operation="occurrence", attribute="model_kit_count",
        count_status=status, count_object="model kit", episode_uuid=f"ep-{aid}",
        source_key=f"sk-{aid}")


def _view_for(state):
    return {
        "uuid": "view-1", "view_key": "vk-1", "subject_uuid": state.subject_uuid, "namespace": NS,
        "attribute": state.attribute, "scope": state.scope, "value_kind": state.value_kind,
        "unit": state.unit, "view_value": normalize_scalar(state.value), "valid_at": state.valid_at,
        "scalar_contributors": list(state.contributor_ids),
        "scalar_effective_tier": state.effective_tier,
        "episode_uuids": list(state.episode_uuids), "view_current": True,
    }


def _service_for(rows, views, *, counting_enabled=True, count_match=True):
    repo = ProjectionCoverageRepository(_FakeNeo4j(rows, views))
    return ProjectionCoverageService(
        repo, counting_enabled=counting_enabled, count_match=count_match)


def test_audit_query_returns_every_column_the_writer_query_returns():
    writer = _alias_set(inspect.getsource(TypedAssertionWriteMixin.assertions_for_entity))
    audit = _alias_set(inspect.getsource(ProjectionCoverageRepository.assertions_for_projection_audit))
    assert writer - audit == set()


def test_coverage_audit_of_correct_match_mode_view_is_clean_on_real_row_shape():
    rows = [_occurrence("o1", "counted"), _occurrence("o2", "uncertain")]
    fold = fold_assertions(rows, as_of=datetime.now(timezone.utc), count_match=True)
    assert fold.states, "match-mode fold must produce a counted state"
    report = _service_for(rows, [_view_for(fold.states[0])]).audit_entity(SUBJECT, namespace=NS)
    assert report.clean, report


def test_coverage_audit_with_counting_off_ignores_occurrences_like_the_writer():
    rows = [_occurrence("o1", "counted")]
    report = _service_for(rows, [], counting_enabled=False, count_match=True).audit_entity(
        SUBJECT, namespace=NS)
    assert report.clean, report


def test_coverage_audit_ignores_future_dated_assertion_like_the_writer():
    rows = [_audit_row(assertion_id="past", source_key="sk-past"),
            _audit_row(assertion_id="future", source_key="sk-future", value=99, valid_at=FUTURE)]
    fold = fold_assertions(rows[:1], as_of=datetime.now(timezone.utc))
    report = _service_for(rows, [_view_for(fold.states[0])]).audit_entity(SUBJECT, namespace=NS)
    assert report.clean, report
