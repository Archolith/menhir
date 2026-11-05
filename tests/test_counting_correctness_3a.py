"""Counting lane unit 3a correctness, offline.

Pins: the one-count-per-turn fold, the occurrence kill switch, the runner's claim/fence/isolation
rules, the flag sweep, the scheduler's counting key/ordering rules, the activation schema, and the
read-side recall gate. The live Cypher side of the fence is in test_counting_fence_live.py.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from menhir.domain.scalar_state_fold import BASIS_COUNTED, fold_assertions
from menhir.infrastructure.schema import get_scalar_state_activation_queries
from menhir.infrastructure.turn_evidence_repository import CountingFenceLost
from menhir.services import scheduler_tasks
from menhir.services.counting_consolidation import (
    CountingConsolidationConfig,
    run_counting_consolidation,
    sweep_counting_flag_state,
)
from menhir.services.recall_service import RecallService
from menhir.services.scalar_state_service import ScalarStateService
from menhir.services.scoring_service import ScoringService


# --------------------------------------------------------------------------- fold helpers

def _occ(aid, episode, *, scope="", value=1, attribute="gym_visit_count", span=None, **over):
    row = dict(
        assertion_id=aid, subject_uuid="ent-self", subject_display="me", attribute=attribute,
        scope=scope, value_kind="count", unit="", operation="occurrence", value=value,
        importance="tracked", valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", evidence_tier="user", episode_uuid=episode,
        stated_span=span or f"quote {aid}", binding_pending=False)
    row.update(over)
    return row


def _states(result):
    return {s.slot_key: s for s in result.states}


_GYM = ("ent-self", "gym_visit_count", "", "count", "")
_GYM_SCOPED = ("ent-self", "gym_visit_count", "gym", "count", "")


# --------------------------------------------------------------------------- 1. one count per turn

@pytest.mark.unit
def test_same_turn_duplicate_across_scopes_counts_once_with_larger_value_and_turns_sum():
    # Fails if the same turn's rows in two scopes are both counted (double count), if the smaller
    # value survives, or if different turns are collapsed instead of summed.
    rows = [
        _occ("o-a", "ep-1", scope="", value=1, span="went to the gym"),
        _occ("o-b", "ep-1", scope="gym", value=3, span="hit the gym three times"),
        _occ("o-c", "ep-2", scope="gym", value=2),
    ]
    result = fold_assertions(rows)
    by_slot = _states(result)
    assert list(by_slot) == [_GYM_SCOPED]            # the scope="" duplicate left no slot behind
    state = by_slot[_GYM_SCOPED]
    assert state.basis == BASIS_COUNTED
    assert state.value == 5                          # ep-1 once (3) + ep-2 (2)
    assert sorted(state.contributor_ids) == ["o-b", "o-c"]


@pytest.mark.unit
@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_same_turn_duplicate_larger_value_in_blank_scope_wins_regardless_of_input_order(order):
    # Fails if survival depends on input order or prefers the scoped row over the larger value.
    rows = [
        _occ("o-a", "ep-1", scope="", value=4),
        _occ("o-b", "ep-1", scope="gym", value=2),
    ]
    if order == "reversed":
        rows.reverse()
    by_slot = _states(fold_assertions(rows))
    assert list(by_slot) == [_GYM]
    assert by_slot[_GYM].value == 4
    assert by_slot[_GYM].contributor_ids == ["o-a"]


@pytest.mark.unit
def test_same_turn_equal_values_keep_the_blank_scope_row():
    # Fails if the tie-break stops preferring scope == "" (a scoped duplicate would then win).
    rows = [
        _occ("o-a", "ep-1", scope="gym", value=1),
        _occ("o-b", "ep-1", scope="", value=1),
    ]
    by_slot = _states(fold_assertions(rows))
    assert list(by_slot) == [_GYM]
    assert by_slot[_GYM].contributor_ids == ["o-b"]


@pytest.mark.unit
def test_rows_without_episode_uuid_are_never_collapsed():
    # Fails if rows with an empty episode_uuid are grouped as "the same turn" and merged.
    rows = [
        _occ("o-a", "", scope="", value=1),
        _occ("o-b", "", scope="gym", value=1),
        _occ("o-c", "", scope="gym", value=1),
    ]
    by_slot = _states(fold_assertions(rows))
    assert set(by_slot) == {_GYM, _GYM_SCOPED}
    assert by_slot[_GYM].value == 1
    assert by_slot[_GYM_SCOPED].value == 2            # two timeless rows in one slot both count


@pytest.mark.unit
def test_dedup_leaves_stated_slots_and_other_attributes_untouched():
    # Fails if the per-turn dedup touches non-occurrence rows or occurrences of another attribute:
    # the stated slot must fold byte-identically with and without the duplicate pair present.
    stated = [
        dict(_occ("a1", "ep-9", attribute="owned", value=5, importance="", operation="absolute")),
        dict(_occ("d1", "ep-9", attribute="owned", value=1, importance="", operation="delta",
                  valid_at="2026-07-05T00:00:00+00:00")),
    ]
    dups = [
        _occ("o-a", "ep-9", scope="", value=1),
        _occ("o-b", "ep-9", scope="gym", value=3),
        # same turn id as the stated rows, other attribute: must not be absorbed into the dedup group
        _occ("o-z", "ep-9", attribute="museum_visit_count", value=1),
    ]
    plain = _states(fold_assertions(stated))
    mixed = _states(fold_assertions(stated + dups))
    owned = ("ent-self", "owned", "", "count", "")
    assert plain[owned] == mixed[owned]
    assert plain[owned].value == 6
    assert ("ent-self", "museum_visit_count", "", "count", "") in mixed
    assert mixed[_GYM_SCOPED].value == 3 and _GYM not in mixed


# --------------------------------------------------------------------------- 2. kill switch

class _Assertions:
    def __init__(self, rows):
        self._rows = rows

    def materializable_assertions_for_entity(self, subject_uuid, *, namespace=None):
        return [r for r in self._rows if r["subject_uuid"] == subject_uuid]


class _Views:
    def record_scalar_state(self, **kw):
        return {"view_key": "vk", "uuid": "v"}

    def list_scalar_state_views(self, **kw):
        return []

    def retire_scalar_state(self, **kw):
        return True

    def draw_scalar_state_provenance_edges(self, **kw):
        pass


@pytest.mark.unit
def test_include_occurrences_false_drops_occurrence_only_slots_but_not_stated_ones():
    # Fails if the kill switch lets occurrences fold (a counted state appears) or if it changes a
    # slot anchored on a stated absolute.
    occurrences_only = [_occ("o1", "ep-1"), _occ("o2", "ep-2")]
    on = fold_assertions(occurrences_only, include_occurrences=True)
    off = fold_assertions(occurrences_only, include_occurrences=False)
    assert [s.value for s in on.states] == [2]       # positive control: counting is real when on
    assert off.states == []

    stated_slot = [
        _occ("a1", "ep-3", attribute="owned", value=5, importance="", operation="absolute"),
        _occ("o3", "ep-4", attribute="owned", value=1),
    ]
    assert fold_assertions(stated_slot, include_occurrences=False) == fold_assertions(
        stated_slot, include_occurrences=True)


@pytest.mark.unit
def test_scalar_state_service_counting_disabled_fold_entity_drops_occurrences():
    # Fails if ScalarStateService ignores counting_enabled when folding (kill switch not wired).
    rows = [_occ("o1", "ep-1"), _occ("o2", "ep-2")]
    off = ScalarStateService(_Assertions(rows), _Views(), counting_enabled=False)
    on = ScalarStateService(_Assertions(rows), _Views(), counting_enabled=True)
    assert off.counting_enabled is False
    assert off.fold_entity("ent-self").states == []
    assert [s.value for s in on.fold_entity("ent-self").states] == [2]


# --------------------------------------------------------------------------- 3. runner fakes

@dataclass
class _Ep:
    uuid: str
    content: str


def _build_episodes(rows):
    return [_Ep(uuid=str(r["uuid"]), content=str(r["content"])) for r in rows]


def _row(i):
    return {"uuid": f"t{i}", "valid_at": f"2026-07-0{i}T00:00:00Z",
            "cursor_at": f"2026-07-0{i}T00:00:00Z", "content": f"I went to the gym {i}"}


class _Llm:
    def __init__(self, calls=0):
        self.calls = calls

    def __call__(self, system, user):
        self.calls += 1
        return "[]"


class _FenceGraph:
    """Records every call in `events` as (name, namespace); behaviours are scripted per test."""

    def __init__(self, pages=None, retired=None):
        self.pages = {k: list(v) for k, v in (pages or {}).items()}
        self.retired = retired or []
        self.claim_result = {"incarnation": "inc-1", "epoch": 7}
        self.claim_none_for: set[str] = set()
        self.renew_script: list[bool] = []          # consumed first; then True
        self.advance_raises: set[str] = set()
        self.events: list[tuple] = []
        self.kwargs: dict[str, list[dict]] = {}
        self.rebuilds: list[tuple[str, str | None]] = []

    def _log(self, name, ns, **kw):
        self.events.append((name, ns))
        self.kwargs.setdefault(name, []).append(kw)

    def names(self, name=None):
        return [e[0] for e in self.events] if name is None else [e for e in self.events if e[0] == name]

    def scalar_state_service(self, **kw):
        graph = self

        class _State:
            def rebuild_scalar_state(self, subject, *, namespace=None, **_kw):
                graph.events.append(("rebuild", subject))
                graph.rebuilds.append((subject, namespace))
                return {}

        return _State()

    def list_counting_dirty_namespaces(self, **kw):
        return []

    def claim_counting_namespace(self, namespace, **kw):
        self._log("claim", namespace, **kw)
        return None if namespace in self.claim_none_for else dict(self.claim_result)

    def renew_counting_claim(self, namespace, **kw):
        self._log("renew", namespace, **kw)
        return self.renew_script.pop(0) if self.renew_script else True

    def release_counting_claim(self, namespace, **kw):
        self._log("release", namespace, **kw)

    def mark_counting_flag_state(self, namespace, **kw):
        self._log("mark", namespace, **kw)

    def load_next_counting_batch(self, namespace, **kw):
        self._log("load", namespace, **kw)
        queue = self.pages.get(namespace, [])
        return queue.pop(0) if queue else []

    def prior_version_occurrences(self, namespace, turn_ids, **kw):
        return []

    def mark_projection_complete(self, assertion_ids):
        return len(assertion_ids)

    def retire_prior_version_occurrences(self, namespace, turn_ids, **kw):
        self._log("retire", namespace, **kw)
        return list(self.retired)

    def scalar_claims_for_turns(self, namespace, turn_ids, **kw):
        return []

    def advance_counting_cursor(self, namespace, **kw):
        self._log("advance", namespace, **kw)
        if namespace in self.advance_raises:
            raise CountingFenceLost("stolen")


class _FakePerception:
    instances: list["_FakePerception"] = []
    timeline: list[tuple] | None = None     # a test may point this at graph.events
    fail_namespaces: set[str] = set()

    def __init__(self, graph, state_service, **kwargs):
        _FakePerception.instances.append(self)
        self.calls: list[str] = []

    def ensure_activated(self):
        pass

    def perceive_and_persist(self, episodes, llm, **kwargs):
        ns = kwargs["namespace"]
        self.calls.append(ns)
        if _FakePerception.timeline is not None:
            _FakePerception.timeline.append(("perceive", ns))
        if ns in _FakePerception.fail_namespaces:
            raise RuntimeError("llm exploded")
        return {"bound": 1, "advisory": 0}


@pytest.fixture
def fake_perception(monkeypatch):
    import menhir.services.typed_scalar_perception as tsp

    _FakePerception.instances = []
    _FakePerception.timeline = None
    _FakePerception.fail_namespaces = set()
    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _FakePerception)
    return _FakePerception


def _run(graph, namespaces, *, batch_size=500):
    out = run_counting_consolidation(
        graph, namespaces=namespaces, counting_llm=_Llm(), build_episodes=_build_episodes,
        k=1, threshold=1.0, call_budget=None, embed=None,
        config=CountingConsolidationConfig(
            batch_size=batch_size, extract_v2=False, match=False))
    return out


def _perceptions(fake):
    return [ns for inst in fake.instances for ns in inst.calls]


# --------------------------------------------------------------------------- 3. runner fence

@pytest.mark.unit
def test_busy_namespace_is_skipped_without_any_work(fake_perception):
    # Fails if a namespace whose claim is refused is still loaded, perceived or advanced.
    graph = _FenceGraph(pages={"a": [[_row(1)]]})
    graph.claim_none_for = {"a"}
    out = _run(graph, ["a"])
    assert out["counting_namespaces_busy"] == 1
    assert out["counting_namespaces_processed"] == 0 and out["counting_namespaces_fenced"] == 0
    assert graph.names("load") == [] and graph.names("advance") == []
    assert graph.names("mark") == []
    assert graph.names("release") == []              # never owned, so nothing to release
    assert _perceptions(fake_perception) == []


@pytest.mark.unit
def test_failed_pre_perception_renew_stops_before_the_llm_step(fake_perception):
    # Fails if the fence is not re-checked before perception (a stolen claim would still spend LLM
    # calls and write assertions) or if a lost fence still advances the cursor / skips release.
    graph = _FenceGraph(pages={"a": [[_row(1)]]})
    graph.renew_script = [False]
    out = _run(graph, ["a"])
    assert _perceptions(fake_perception) == []
    assert graph.names("advance") == []
    assert out["counting_namespaces_fenced"] == 1
    assert out["counting_namespaces_failed"] == 0 and out["counting_namespaces_processed"] == 0
    assert len(graph.names("release")) == 1
    assert graph.names("renew")[0] == ("renew", "a")


@pytest.mark.unit
def test_fence_lost_on_advance_stops_the_namespace_and_still_releases(fake_perception):
    # Fails if CountingFenceLost on advance is counted as a generic failure, if the loop keeps
    # paging, or if the claim is not released.
    graph = _FenceGraph(pages={"a": [[_row(1)], [_row(2)]]})
    graph.advance_raises = {"a"}
    out = _run(graph, ["a"], batch_size=1)
    assert out["counting_namespaces_fenced"] == 1
    assert out["counting_namespaces_failed"] == 0
    assert out["counting_namespaces_processed"] == 0  # the advance never landed
    assert len(graph.names("load")) == 1              # no second page
    assert len(graph.names("advance")) == 1
    assert len(graph.names("release")) == 1


@pytest.mark.unit
def test_perception_failure_in_one_namespace_does_not_stop_the_next(fake_perception):
    # Fails if one namespace's exception aborts the run, is not counted, or skips its release.
    fake_perception.fail_namespaces = {"a"}
    graph = _FenceGraph(pages={"a": [[_row(1)]], "b": [[_row(2)]]})
    out = _run(graph, ["a", "b"])
    assert out["counting_namespaces_failed"] == 1
    assert out["counting_namespaces_fenced"] == 0
    assert out["counting_namespaces_processed"] == 1
    assert [e for e in graph.names("advance")] == [("advance", "b")]
    assert sorted(ns for _, ns in graph.names("release")) == ["a", "b"]
    assert "b" in _perceptions(fake_perception)


@pytest.mark.unit
def test_retired_subjects_are_rebuilt_in_namespace_after_the_new_reading(fake_perception):
    # Fails if prior-version retirement is not followed by a Views rebuild scoped to the namespace,
    # or if retire runs before the new reading is stored (re-version safety A: a failed LLM step
    # must leave the old rows counted).
    graph = _FenceGraph(pages={"ns-1": [[_row(1)]]}, retired=["s1", "s2"])
    fake_perception.timeline = graph.events          # one shared timeline: graph + perception
    _run(graph, ["ns-1"])
    assert graph.rebuilds == [("s1", "ns-1"), ("s2", "ns-1")]
    order = [e[0] for e in graph.events]
    assert order.index("renew") < order.index("perceive") < order.index("retire")
    assert order.index("retire") < order.index("rebuild")


@pytest.mark.unit
def test_flag_marker_is_written_create_only_by_the_runner(fake_perception):
    # Fails if the runner overwrites the marker (a failed re-enable sweep's False would be flipped
    # to True and the sweep never retried) instead of creating it only when absent.
    graph = _FenceGraph(pages={"a": [[_row(1)]]})
    _run(graph, ["a"])
    marks = graph.kwargs["mark"]
    assert marks and all(kw.get("create_only") is True for kw in marks)
    assert all(kw["enabled"] is True for kw in marks)


@pytest.mark.unit
def test_fence_tokens_from_the_claim_reach_every_fenced_write(fake_perception):
    # Fails if retire / advance / renew / release are passed anything but the run_id, incarnation
    # and epoch the claim returned (a fenced write would then be checked against the wrong owner).
    graph = _FenceGraph(pages={"a": [[_row(1)]]})
    graph.claim_result = {"incarnation": "inc-XYZ", "epoch": 41}
    _run(graph, ["a"])
    run_id = graph.kwargs["claim"][0]["run_id"]
    assert run_id
    for name in ("retire", "advance", "renew"):
        for kw in graph.kwargs[name]:
            assert kw["run_id"] == run_id, name
            assert kw["incarnation"] == "inc-XYZ", name
            assert kw["epoch"] == 41, name
    for kw in graph.kwargs["release"]:
        assert kw["run_id"] == run_id and kw["incarnation"] == "inc-XYZ"


# --------------------------------------------------------------------------- 4. sweep

class _SweepGraph:
    def __init__(self, subjects, targets=("a",), fail_on=None):
        self.subjects = sorted(subjects)
        self.targets = list(targets)
        self.fail_on = fail_on or {}                 # namespace -> subject whose rebuild raises
        self.events: list[tuple] = []
        self.afters: list[str] = []
        self.service_kwargs: list[dict] = []
        self.target_calls: list[dict] = []

    def counting_flag_sweep_targets(self, **kw):
        self.target_calls.append(kw)
        return list(self.targets)

    def counting_sweep_subjects(self, namespace, *, limit, after=""):
        self.afters.append(after)
        return [s for s in self.subjects if s > after][:limit]

    def mark_counting_flag_state(self, namespace, **kw):
        self.events.append(("mark", namespace, kw))

    def scalar_state_service(self, **kw):
        self.service_kwargs.append(kw)
        graph = self

        class _State:
            def rebuild_scalar_state(self, subject, *, namespace=None, **_kw):
                if graph.fail_on.get(namespace) == subject:
                    raise RuntimeError("rebuild failed")
                graph.events.append(("rebuild", namespace, subject))

        return _State()


@pytest.mark.unit
@pytest.mark.parametrize("count", [5, 4, 0])
@pytest.mark.parametrize("flag", [True, False])
def test_sweep_pages_every_subject_then_marks_once_without_create_only(count, flag):
    # Fails if paging drops/repeats subjects (keyset `after` mishandled, an exact-multiple page lost),
    # if the marker is set before the last page, more than once, or with create_only (a no-op flip).
    subjects = [f"s{i}" for i in range(1, count + 1)]
    graph = _SweepGraph(subjects)
    out = sweep_counting_flag_state(
        graph, enabled=flag, namespaces=None, scalar_history_enabled=False, subject_limit=2)
    rebuilt = [e[2] for e in graph.events if e[0] == "rebuild"]
    assert rebuilt == subjects
    marks = [e for e in graph.events if e[0] == "mark"]
    assert len(marks) == 1
    assert graph.events[-1][0] == "mark"
    _, ns, kw = marks[0]
    assert ns == "a" and kw["enabled"] is flag
    assert kw.get("create_only", False) is not True
    assert out["counting_sweep_subjects_rebuilt"] == count
    assert out["counting_sweep_namespaces"] == 1 and out["counting_sweep_failed"] == 0
    # keyset paging: each page starts strictly after the previous page's last subject
    if count > 2:
        assert graph.afters[0] == "" and graph.afters[1] == "s2"
    assert graph.target_calls[0]["enabled"] is flag


@pytest.mark.unit
@pytest.mark.parametrize("flag", [True, False])
def test_sweep_builds_the_state_service_with_the_sweeps_counting_flag(flag):
    # Fails if the sweep rebuilds with the process-wide flag instead of the `enabled` it is
    # sweeping toward (a kill-switch sweep would then rebuild Views with counting still on).
    graph = _SweepGraph(["s1"])
    sweep_counting_flag_state(
        graph, enabled=flag, namespaces=None, scalar_history_enabled=True)
    assert graph.service_kwargs
    assert all(kw["counting_enabled"] is flag for kw in graph.service_kwargs)
    assert all(kw["scalar_history_enabled"] is True for kw in graph.service_kwargs)


@pytest.mark.unit
def test_sweep_rebuild_failure_leaves_marker_unset_and_other_namespaces_proceed():
    # Fails if a failed rebuild still flips the marker (the sweep would never retry), is not counted,
    # or aborts the remaining namespaces.
    graph = _SweepGraph(["s1", "s2", "s3"], targets=("a", "b"), fail_on={"a": "s2"})
    out = sweep_counting_flag_state(
        graph, enabled=True, namespaces=None, scalar_history_enabled=False, subject_limit=2)
    marked = [e[1] for e in graph.events if e[0] == "mark"]
    assert marked == ["b"]
    assert out["counting_sweep_failed"] == 1 and out["counting_sweep_namespaces"] == 1


# --------------------------------------------------------------------------- 5. scheduler

class _SchedGraph:
    def __init__(self, sweep_targets=()):
        self.sweep_targets = list(sweep_targets)
        self.sweep_target_calls: list[dict] = []
        self.marks: list[tuple] = []

    def list_dirty_namespaces(self, *, limit=200):
        return []

    def load_user_episodes(self, namespace, *, limit=500):
        return []

    def mark_consolidated(self, namespace, *, at):
        pass

    def counting_flag_sweep_targets(self, **kw):
        self.sweep_target_calls.append(kw)
        return list(self.sweep_targets)

    def counting_sweep_subjects(self, namespace, *, limit, after=""):
        return [s for s in ("s1",) if s > after][:limit]

    def mark_counting_flag_state(self, namespace, **kw):
        self.marks.append((namespace, kw))

    def scalar_state_service(self, **kw):
        class _State:
            def rebuild_scalar_state(self, subject, *, namespace=None, **_kw):
                return {}

        return _State()


def _noop_llm(system, user):
    return "[]"


def _consolidate(graph, **kw):
    return asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_noop_llm, namespaces=["a"], k=1, enable_scalar_state=True, **kw))


def _patch_phases(monkeypatch, order):
    def _scalar(graph, **kw):
        order.append("scalar")
        return {"scalar_namespaces_dirty": 0}

    def _counting(graph, **kw):
        order.append("counting")
        return {"counting_namespaces_dirty": 0}

    monkeypatch.setattr(scheduler_tasks, "run_scalar_consolidation", _scalar)
    monkeypatch.setattr(scheduler_tasks, "run_counting_consolidation", _counting)


@pytest.mark.unit
def test_counting_off_with_no_sweep_targets_adds_no_counting_keys(monkeypatch):
    # Fails if a counting-off pass starts reporting counting_* keys (changes the flag-off payload)
    # or runs the counting runner.
    order: list[str] = []
    _patch_phases(monkeypatch, order)
    graph = _SchedGraph(sweep_targets=())
    out = _consolidate(graph, enable_counting=False)
    assert not [k for k in out if k.startswith("counting_")]
    assert order == ["scalar"]
    assert graph.sweep_target_calls and graph.sweep_target_calls[0]["enabled"] is False


@pytest.mark.unit
def test_counting_off_with_a_sweep_target_reports_sweep_keys(monkeypatch):
    # Fails if the kill-switch sweep is not run (or not reported) when a marker disagrees with the
    # now-off flag, or if the sweep result is dropped from the pass result.
    order: list[str] = []
    _patch_phases(monkeypatch, order)
    graph = _SchedGraph(sweep_targets=("a",))
    out = _consolidate(graph, enable_counting=False)
    assert out["counting_sweep_namespaces"] == 1
    assert out["counting_sweep_subjects_rebuilt"] == 1
    assert out["counting_sweep_failed"] == 0
    assert graph.marks and graph.marks[0][1]["enabled"] is False
    assert "counting" not in order                   # the sweep never runs the LLM lane


@pytest.mark.unit
def test_phase_order_is_scalar_then_counting(monkeypatch):
    # Fails if counting runs before scalars (it would then spend the shared budget first, or read
    # turns the scalar cursor has not passed).
    order: list[str] = []
    _patch_phases(monkeypatch, order)
    _consolidate(_SchedGraph(), enable_counting=True)
    assert order == ["scalar", "counting"]


# --------------------------------------------------------------------------- 6. schema

@pytest.mark.unit
def test_activation_queries_declare_counting_fence_uniqueness_constraints():
    # Fails if either uniqueness constraint is dropped from activation: concurrent first claims would
    # then create two watermark nodes (a split fence) or two flag markers for one namespace.
    queries = get_scalar_state_activation_queries()
    wm = r"CREATE CONSTRAINT \w+ IF NOT EXISTS FOR \(\w+:CountingConsolidationWatermark\) REQUIRE \w+\.group_id IS UNIQUE"
    fs = r"CREATE CONSTRAINT \w+ IF NOT EXISTS FOR \(\w+:CountingFlagState\) REQUIRE \w+\.namespace_key IS UNIQUE"
    assert any(re.fullmatch(wm, q.strip()) for q in queries)
    assert any(re.fullmatch(fs, q.strip()) for q in queries)


# --------------------------------------------------------------------------- 7. recall gate

def _counted_view_meta(uuid="counted-view-1"):
    return {
        "uuid": uuid, "name": "gym visits: 5", "scope": "PERSISTENT", "type": "SEMANTIC",
        "content": "gym visits: 5 (counted)", "summary": None,
        "last_accessed": datetime.now(timezone.utc), "edge_count": 1,
        "freshness": "ACTIVE", "user_flagged": False,
        "view_kind": "scalar_state", "view_current": True, "scalar_basis": "counted",
    }


def _seed_pool(client, adapter):
    client.search_scored_results = [
        ("entity-1", "Test Entity", 0.85),
        ("counted-view-1", "gym visits: 5", 0.9),
    ]
    adapter.candidate_metadata = [
        {
            "uuid": "entity-1", "name": "Test Entity", "scope": "PERSISTENT",
            "type": "SEMANTIC", "content": "test content", "summary": None,
            "last_accessed": datetime.now(timezone.utc), "edge_count": 5,
            "freshness": "ACTIVE", "user_flagged": False,
        },
        _counted_view_meta(),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("counting_enabled,expected", [(False, False), (True, True)])
async def test_counted_scalar_state_view_is_recalled_only_when_counting_enabled(
    stub_graphiti_client, stub_memory_graph_adapter, counting_enabled, expected,
):
    # Fails if the read-side kill switch is not applied (a counted View leaks into recall with
    # counting off) or is applied unconditionally (a counted View never surfaces with counting on).
    _seed_pool(stub_graphiti_client, stub_memory_graph_adapter)
    svc = RecallService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(), counting_enabled=counting_enabled)
    res = await svc.recall("how many times did I go to the gym")
    uuids = [r.uuid for r in res.results]
    assert ("counted-view-1" in uuids) is expected
    assert "entity-1" in uuids                       # the ordinary candidate is unaffected
