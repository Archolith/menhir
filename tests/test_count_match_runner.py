"""Related-count matching (unit 4R): the counting runner and flag sweep with match on and off.

A fake graph records every call in order; a fake perception service stands in for the LLM step.
Modelled on tests/test_counting_consolidation.py (helpers copied, not imported).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from menhir.domain.count_decide import DECIDE_K
from menhir.domain.count_match import RELATED_POOL_LIMIT, UNDECIDED_SWEEP_LIMIT
from menhir.services.counting_consolidation import (
    COUNTING_MATCH_PROMPT_ADDENDUM,
    COUNTING_SYSTEM_PROMPT,
    COUNTING_SYSTEM_PROMPT_V2,
    CountingConsolidationConfig,
    counting_system_prompt,
    effective_counting_perceiver_version,
    run_counting_consolidation,
    sweep_counting_flag_state,
)

_NEW_METHODS = (
    "count_occurrences_by_ids", "counted_occurrences_for_subjects",
    "undecided_count_occurrences", "record_count_decisions",
)


@dataclass
class _Ep:
    uuid: str
    content: str


def _build_episodes(rows):
    return [_Ep(uuid=str(r["uuid"]), content=str(r["content"])) for r in rows]


def _row(i):
    return {"uuid": f"t{i}", "valid_at": f"2026-07-0{i}T00:00:00Z",
            "cursor_at": f"2026-07-0{i}T00:00:00Z", "content": f"I finished kit {i}"}


def _occ(aid, day, subject="s1", **over):
    base = dict(
        assertion_id=aid, subject_uuid=subject, value_kind="count", attribute="model_kit_count",
        count_object="model kit", count_item="", valid_at=f"2026-07-{day:02d}T00:00:00+00:00",
        episode_uuid=f"ep-{aid}", count_status="",
    )
    base.update(over)
    return base


class _Svc:
    """The scalar state service the runner rebuilds through; logs into the graph's ordered log."""

    def __init__(self, graph):
        self._graph = graph

    def rebuild_scalar_state(self, subject, *, namespace=None, **kw):
        self._graph.calls.append(("rebuild", (subject,), {"namespace": namespace}))

    def rebuild_scalar_projections(self, subject, *, namespace=None, **kw):
        self._graph.calls.append(("rebuild", (subject,), {"namespace": namespace}))


class _Graph:
    def __init__(self, pages=None, *, occ_by_id=None, pool=None, undecided=None, fence_ok=True,
                 redecide=None, cosine_hits=None):
        self.pages = list(pages or [])
        self.occ_by_id = occ_by_id or {}
        self.pool = pool or []
        self.undecided = undecided or {}
        self.fence_ok = fence_ok
        self.redecide = list(redecide or [])
        self.cosine_hits = list(cosine_hits or [])
        self.calls: list[tuple[str, tuple, dict]] = []

    def _log(self, name, *a, **kw):
        self.calls.append((name, a, kw))

    def of(self, name):
        return [c for c in self.calls if c[0] == name]

    def names(self):
        return [c[0] for c in self.calls]

    def scalar_state_service(self, **kw):
        self._log("scalar_state_service", **kw)
        return _Svc(self)

    def list_counting_dirty_namespaces(self, **kw):
        self._log("list_counting_dirty_namespaces", **kw)
        return []

    def load_next_counting_batch(self, namespace, **kw):
        self._log("load_next_counting_batch", namespace, **kw)
        return self.pages.pop(0) if self.pages else []

    def advance_counting_cursor(self, namespace, **kw):
        self._log("advance_counting_cursor", namespace, **kw)

    def claim_counting_namespace(self, namespace, **kw):
        self._log("claim_counting_namespace", namespace, **kw)
        return {"incarnation": "inc-1", "epoch": 7}

    def renew_counting_claim(self, namespace, **kw):
        self._log("renew_counting_claim", namespace, **kw)
        return True

    def release_counting_claim(self, namespace, **kw):
        self._log("release_counting_claim", namespace, **kw)

    def prior_version_occurrences(self, namespace, turn_ids, **kw):
        return []

    def mark_projection_complete(self, assertion_ids):
        return len(assertion_ids)

    def retire_prior_version_occurrences(self, namespace, turn_ids, **kw):
        self._log("retire_prior_version_occurrences", namespace, list(turn_ids), **kw)
        return []

    def mark_counting_flag_state(self, namespace, **kw):
        self._log("mark_counting_flag_state", namespace, **kw)

    def scalar_claims_for_turns(self, namespace, turn_ids, **kw):
        self._log("scalar_claims_for_turns", namespace, list(turn_ids), **kw)
        return []

    # --- 4R additions
    def count_occurrences_by_ids(self, namespace, ids, **kw):
        self._log("count_occurrences_by_ids", namespace, list(ids), **kw)
        return [self.occ_by_id[i] for i in ids if i in self.occ_by_id]

    def counted_occurrences_for_subjects(self, namespace, subjects, **kw):
        self._log("counted_occurrences_for_subjects", namespace, list(subjects), **kw)
        return list(self.pool)

    def undecided_count_occurrences(self, namespace, **kw):
        self._log("undecided_count_occurrences", namespace, **kw)
        return list(self.undecided.get(namespace, []))

    def redecide_count_occurrences(self, namespace, **kw):
        self._log("redecide_count_occurrences", namespace, **kw)
        return list(self.redecide)

    def cosine_related_counted(self, namespace, probes, **kw):
        self._log("cosine_related_counted", namespace, list(probes), **kw)
        return list(self.cosine_hits)

    def record_count_decisions(self, namespace, decisions, **kw):
        self._log("record_count_decisions", namespace, list(decisions), **kw)
        return {"fence_ok": self.fence_ok}

    # --- sweep
    def counting_flag_sweep_targets(self, **kw):
        self._log("counting_flag_sweep_targets", **kw)
        return ["ns-a"]

    def counting_sweep_subjects(self, namespace, **kw):
        self._log("counting_sweep_subjects", namespace, **kw)
        return ["subj-1"]


class _Llm:
    """Callable (system, user) -> str with a scripted answer; `calls` counts every call."""

    def __init__(self, response="[]"):
        self.calls = 0
        self.response = response
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, system, user):
        self.calls += 1
        self.prompts.append((system, user))
        return self.response


# N1 is the only candidate that needs the decide call (a1 has no related counts, so it is R1).
_REPEAT_R1 = '[{"new": "N1", "decision": "repeat", "target": "R1", "family": "model kit"}]'


class _Perception:
    instances: list["_Perception"] = []
    results: list[dict] = []

    def __init__(self, graph, state_service, **kwargs):
        self.init_kwargs = kwargs
        self.persisted: list[dict] = []
        _Perception.instances.append(self)

    def ensure_activated(self):
        pass

    def perceive_and_persist(self, episodes, llm, **kwargs):
        self.persisted.append(kwargs)
        results = list(_Perception.results)
        return {"bound": sum(1 for r in results if r.get("bound")), "advisory": 0,
                "results": results}


@pytest.fixture
def perception(monkeypatch):
    import menhir.services.typed_scalar_perception as tsp

    _Perception.instances = []
    _Perception.results = []
    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _Perception)
    return _Perception


def _run(graph, *, match, extract_v2=False, namespaces=("ns-1",), pv="cv1", llm=None,
         call_budget=None, embed=None, embed_version=None):
    cfg = dict(perceiver_version=pv, scalar_perceiver_version="sv1", batch_size=500,
               match=match, extract_v2=extract_v2)
    if embed_version is not None:
        cfg["embed_version"] = embed_version
    return run_counting_consolidation(
        graph, namespaces=list(namespaces), counting_llm=llm if llm is not None else _Llm(),
        build_episodes=_build_episodes,
        k=1, threshold=1.0, call_budget=call_budget, embed=embed,
        config=CountingConsolidationConfig(**cfg))


def _two_kit_graph(**kw):
    return _Graph(
        pages=[[_row(1), _row(2)]],
        occ_by_id={"a1": _occ("a1", 1), "a2": _occ("a2", 2)}, **kw)


_TWO_RESULTS = [
    {"bound": True, "assertion_id": "a1"},
    {"bound": True, "assertion_id": "a2"},
    {"bound": False, "assertion_id": "never-bound"},
    {"bound": True},  # bound but no id: ignored
]


# ------------------------------------------------------------------ C.1 match off

@pytest.mark.unit
def test_match_off_touches_none_of_the_new_surface(perception):
    # Fails if any 4R graph call, kwarg, version suffix, prompt text or result key leaks while off.
    perception.results = list(_TWO_RESULTS)
    graph = _two_kit_graph()
    out = _run(graph, match=False)
    assert out["counting_batches"] == 1
    assert not set(graph.names()) & set(_NEW_METHODS)
    assert all("match" not in kw for _, _, kw in graph.of("mark_counting_flag_state"))
    assert graph.of("mark_counting_flag_state")
    assert all("count_match" not in kw for _, _, kw in graph.of("scalar_state_service"))
    assert not [k for k in out if k.startswith("counting_decided")]
    inst, = perception.instances
    assert inst.init_kwargs["perceiver_version"] == "cv1"
    assert inst.persisted[0]["system_prompt"] is COUNTING_SYSTEM_PROMPT
    assert COUNTING_MATCH_PROMPT_ADDENDUM not in inst.persisted[0]["system_prompt"]
    for name in ("load_next_counting_batch", "retire_prior_version_occurrences",
                 "advance_counting_cursor"):
        assert graph.of(name)[0][2]["perceiver_version"] == "cv1"
    assert "rebuild" not in graph.names()


@pytest.mark.unit
def test_bridge_resolution_reaches_perception_only_under_match(perception):
    _run(_two_kit_graph(), match=False)
    assert "counting_match" not in perception.instances[-1].persisted[0]
    _run(_two_kit_graph(), match=True)
    assert perception.instances[-1].persisted[0]["counting_match"] is True


# ------------------------------------------------------------------ C.2 match on

@pytest.mark.unit
@pytest.mark.parametrize("extract_v2,version", [(False, "cv1+m"), (True, "cv1+x2+m")])
def test_match_on_decides_records_then_rebuilds(perception, extract_v2, version):
    # Fails if match is not threaded (version/prompt/service), if decisions are written with the
    # wrong claim identity, if the rebuild happens before the record, or the tally is wrong.
    perception.results = list(_TWO_RESULTS)
    graph = _two_kit_graph()
    llm = _Llm(_REPEAT_R1)
    out = _run(graph, match=True, extract_v2=extract_v2, llm=llm)

    inst, = perception.instances
    assert inst.init_kwargs["perceiver_version"] == version
    prompt = inst.persisted[0]["system_prompt"]
    assert COUNTING_MATCH_PROMPT_ADDENDUM in prompt
    assert prompt == counting_system_prompt(extract_v2, True)
    assert graph.of("scalar_state_service")[0][2]["count_match"] is True
    assert graph.of("mark_counting_flag_state")[0][2]["match"] is True
    assert graph.of("load_next_counting_batch")[0][2]["perceiver_version"] == version

    by_ids, = graph.of("count_occurrences_by_ids")
    assert by_ids[1] == ("ns-1", ["a1", "a2"])  # only bound rows that carry an id
    assert by_ids[2]["perceiver_version"] == version

    claim_run_id = graph.of("claim_counting_namespace")[0][2]["run_id"]
    rec, = graph.of("record_count_decisions")
    assert rec[1][0] == "ns-1"
    kw = rec[2]
    assert kw["run_id"] == claim_run_id and kw["incarnation"] == "inc-1" and kw["epoch"] == 7
    assert kw["perceiver_version"] == version
    decisions = {d["assertion_id"]: d for d in rec[1][1]}
    assert decisions["a1"]["status"] == "counted" and decisions["a1"]["reason"] == "no_related"
    # PR 2: a2 is related to a1, so one decide round (DECIDE_K samples) settles it as a repeat of a1.
    assert llm.calls == DECIDE_K
    assert decisions["a2"]["status"] == "repeat" and decisions["a2"]["target_id"] == "a1"
    assert decisions["a2"]["reason"] == "decided_repeat" and decisions["a2"]["vote"] == f"{DECIDE_K}/{DECIDE_K}"

    names = graph.names()
    assert names.index("record_count_decisions") < names.index("rebuild")
    # rebuilt after the record, before the cursor moved
    assert names.index("rebuild") < names.index("advance_counting_cursor")
    assert graph.of("rebuild")[0][1] == ("s1",) and graph.of("rebuild")[0][2]["namespace"] == "ns-1"
    assert out["counting_decided_counted"] == 1 and out["counting_decided_uncertain"] == 0
    assert out["counting_decided_repeat"] == 1
    assert out["counting_namespaces_processed"] == 1


# ------------------------------------------------------------------ C.3 skip rule

@pytest.mark.unit
def test_rows_with_a_decision_are_not_decided_again(perception):
    # Fails if a replayed row is re-decided (its stored decision must stand).
    perception.results = list(_TWO_RESULTS)
    graph = _Graph(pages=[[_row(1), _row(2)]], occ_by_id={
        "a1": _occ("a1", 1, count_status="repeat", count_target_id="old"),
        "a2": _occ("a2", 2),
    })
    _run(graph, match=True)
    rec, = graph.of("record_count_decisions")
    assert [d["assertion_id"] for d in rec[1][1]] == ["a2"]


@pytest.mark.unit
def test_page_of_only_decided_rows_records_nothing(perception):
    perception.results = list(_TWO_RESULTS)
    graph = _Graph(pages=[[_row(1), _row(2)]], occ_by_id={
        "a1": _occ("a1", 1, count_status="counted"),
        "a2": _occ("a2", 2, count_status="uncertain"),
    })
    out = _run(graph, match=True)
    assert graph.of("record_count_decisions") == []
    assert graph.of("counted_occurrences_for_subjects") == []
    assert "rebuild" not in graph.names()
    assert out["counting_decided_counted"] == 0 and out["counting_decided_uncertain"] == 0


# ------------------------------------------------------------------ C.4 fence

@pytest.mark.unit
def test_fence_not_ok_on_record_fences_namespace_and_holds_the_cursor(perception):
    # Fails if a refused record still advances the cursor, rebuilds, or counts as processed/failed.
    perception.results = list(_TWO_RESULTS)
    graph = _two_kit_graph(fence_ok=False)
    out = _run(graph, match=True, llm=_Llm(_REPEAT_R1))
    assert out["counting_namespaces_fenced"] == 1
    assert out["counting_namespaces_failed"] == 0
    assert out["counting_namespaces_processed"] == 0
    assert out["counting_decided_counted"] == 0 and out["counting_decided_uncertain"] == 0
    assert graph.of("advance_counting_cursor") == []
    assert "rebuild" not in graph.names()
    assert graph.of("release_counting_claim")


# ------------------------------------------------------------------ C.5 undecided sweep

@pytest.mark.unit
def test_undecided_sweep_runs_once_per_namespace_even_with_no_new_pages(perception):
    # Fails if leftovers are only decided when the page loop did work, or decided more than once.
    graph = _Graph(pages=[], undecided={
        "ns-a": [_occ("u1", 1, subject="s1")],
        "ns-b": [_occ("u2", 1, subject="s2")],
    })
    out = _run(graph, match=True, namespaces=("ns-a", "ns-b"))
    assert perception.instances[0].persisted == []          # no page was perceived
    sweeps = graph.of("undecided_count_occurrences")
    assert [c[1][0] for c in sweeps] == ["ns-a", "ns-b"]
    assert sweeps[0][2] == {"perceiver_version": "cv1+m", "limit": UNDECIDED_SWEEP_LIMIT}
    recs = graph.of("record_count_decisions")
    assert [c[1][0] for c in recs] == ["ns-a", "ns-b"]
    assert [[d["assertion_id"] for d in c[1][1]] for c in recs] == [["u1"], ["u2"]]
    assert [c[1] for c in graph.of("rebuild")] == [("s1",), ("s2",)]
    assert out["counting_decided_counted"] == 2


@pytest.mark.unit
def test_undecided_sweep_runs_after_the_page_loop_in_a_pass_with_pages(perception):
    perception.results = list(_TWO_RESULTS)
    graph = _two_kit_graph(undecided={"ns-1": [_occ("u1", 3, subject="s9")]})
    _run(graph, match=True, llm=_Llm(_REPEAT_R1))
    recs = graph.of("record_count_decisions")
    assert [[d["assertion_id"] for d in c[1][1]] for c in recs] == [["a1", "a2"], ["u1"]]
    names = graph.names()
    assert names.index("advance_counting_cursor") < names.index("undecided_count_occurrences")


@pytest.mark.unit
def test_match_off_never_sweeps_undecided(perception):
    graph = _Graph(pages=[], undecided={"ns-1": [_occ("u1", 1)]})
    _run(graph, match=False)
    assert graph.of("undecided_count_occurrences") == []


# ------------------------------------------------------------------ C.6 full pool

@pytest.mark.unit
@pytest.mark.parametrize("pool_size,status", [
    (RELATED_POOL_LIMIT - 1, "counted"),
    (RELATED_POOL_LIMIT, "uncertain"),
])
def test_a_full_related_pool_forces_every_candidate_uncertain(perception, pool_size, status):
    # Fails if a pool cut off at the limit is trusted as complete (a related row may be missing).
    perception.results = [{"bound": True, "assertion_id": "a1"}]
    pool = [_occ(f"p{i}", 1, subject="elsewhere") for i in range(pool_size)]  # none related
    graph = _Graph(pages=[[_row(1)]], occ_by_id={"a1": _occ("a1", 1)}, pool=pool)
    out = _run(graph, match=True)
    pool_call, = graph.of("counted_occurrences_for_subjects")
    assert pool_call[1] == ("ns-1", ["s1"]) and pool_call[2]["limit"] == RELATED_POOL_LIMIT
    rec, = graph.of("record_count_decisions")
    d, = rec[1][1]
    assert d["status"] == status
    if status == "uncertain":
        assert d["reason"] == "truncated"
        assert out["counting_decided_uncertain"] == 1 and out["counting_decided_counted"] == 0


# ------------------------------------------------------------------ C.7 flag sweep

def _kw_shapes(graph):
    return {name: sorted(graph.of(name)[0][2]) for name in
            ("counting_flag_sweep_targets", "mark_counting_flag_state", "scalar_state_service")}


@pytest.mark.unit
def test_flag_sweep_threads_match_when_enabled():
    graph = _Graph()
    out = sweep_counting_flag_state(
        graph, enabled=True, namespaces=None, scalar_history_enabled=False, match=True)
    assert graph.of("counting_flag_sweep_targets")[0][2]["match"] is True
    assert graph.of("mark_counting_flag_state")[0][2]["match"] is True
    assert graph.of("scalar_state_service")[0][2]["count_match"] is True
    assert out["counting_sweep_namespaces"] == 1 and out["counting_sweep_subjects_rebuilt"] == 1
    # marker is set only after the rebuild
    assert graph.names().index("rebuild") < graph.names().index("mark_counting_flag_state")


@pytest.mark.unit
def test_flag_sweep_match_with_counting_disabled_equals_match_off():
    # Fails if match leaks into a sweep that is turning counting off.
    g_on, g_off = _Graph(), _Graph()
    r_on = sweep_counting_flag_state(
        g_on, enabled=False, namespaces=None, scalar_history_enabled=False, match=True)
    r_off = sweep_counting_flag_state(
        g_off, enabled=False, namespaces=None, scalar_history_enabled=False)
    assert r_on == r_off
    assert _kw_shapes(g_on) == _kw_shapes(g_off)
    assert "match" not in g_on.of("counting_flag_sweep_targets")[0][2]
    assert "match" not in g_on.of("mark_counting_flag_state")[0][2]
    assert "count_match" not in g_on.of("scalar_state_service")[0][2]


@pytest.mark.unit
def test_flag_sweep_default_is_match_off():
    graph = _Graph()
    sweep_counting_flag_state(graph, enabled=True, namespaces=None, scalar_history_enabled=False)
    assert "match" not in graph.of("counting_flag_sweep_targets")[0][2]
    assert "count_match" not in graph.of("scalar_state_service")[0][2]


# ------------------------------------------------------------------ C.8 versions and prompt

@pytest.mark.unit
def test_effective_perceiver_version_suffixes():
    assert effective_counting_perceiver_version("v1", False) == "v1"
    assert effective_counting_perceiver_version("v1", True) == "v1+x2"
    assert effective_counting_perceiver_version("v1", False, True) == "v1+m"
    assert effective_counting_perceiver_version("v1", True, True) == "v1+x2+m"


@pytest.mark.unit
def test_counting_system_prompt_addendum_only_under_match():
    assert counting_system_prompt(False) is COUNTING_SYSTEM_PROMPT
    assert counting_system_prompt(True) is COUNTING_SYSTEM_PROMPT_V2
    assert counting_system_prompt(False, True) == COUNTING_SYSTEM_PROMPT + COUNTING_MATCH_PROMPT_ADDENDUM
    assert counting_system_prompt(True, True) == COUNTING_SYSTEM_PROMPT_V2 + COUNTING_MATCH_PROMPT_ADDENDUM
