"""Counting lane (scalar counting plan, unit 3), offline.

Covers the scheduler gate (flag / scalar prerequisite), the shared call budget, the runner's paging
and cursor rules, the occurrence "actually happened" guard, claim_ordinal identity, the counted-View
provenance edges and the wipe sites that must know the :CountingConsolidationWatermark label.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.domain.typed_assertion import COUNTING_OPERATIONS
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.services import scheduler_tasks
from menhir.services import counting_consolidation as cc
from menhir.services.counting_consolidation import (
    COUNTING_CLAIM_ORDINAL,
    COUNTING_SYSTEM_PROMPT,
    CountingConsolidationConfig,
    build_counting_context,
    run_counting_consolidation,
)
from menhir.services.scalar_state_service import ScalarStateService
from menhir.services.typed_scalar_perception import (
    TypedScalarProposal,
    extract_typed_scalars_once,
)

_COUNTING_METHODS = (
    "list_counting_dirty_namespaces", "load_next_counting_batch",
    "advance_counting_cursor", "scalar_claims_for_turns",
)


# --------------------------------------------------------------------------- scheduler gate

class _CounterGraph:
    """Counter surface only (no dirty namespaces, one empty namespace) + recorders for every counting
    method, so any counting access is observable."""

    def __init__(self):
        self.counting_calls: list[str] = []
        self.sweep_reads: list[dict] = []
        for name in _COUNTING_METHODS:
            setattr(self, name, self._recorder(name))

    def counting_flag_sweep_targets(self, **kw):
        # The kill-switch sweep's one read; no markers -> nothing else is touched.
        self.sweep_reads.append(kw)
        return []

    def _recorder(self, name):
        def _rec(*_a, **_kw):
            self.counting_calls.append(name)
            return []
        return _rec

    def list_dirty_namespaces(self, *, limit=200):
        return []

    def load_user_episodes(self, namespace, *, limit=500):
        return []

    def mark_consolidated(self, namespace, *, at):
        pass

    def record_counter(self, **kwargs):
        return {"uuid": "u", "view_key": "k", "created": True}


def _noop_llm(system: str, user: str) -> str:
    return "[]"


def _patch_scalar(monkeypatch):
    monkeypatch.setattr(
        scheduler_tasks, "run_scalar_consolidation", lambda *a, **kw: {"scalar_namespaces_dirty": 0})


def _patch_counting_runner(monkeypatch):
    seen: list[dict] = []

    def _fake(graph, **kwargs):
        seen.append(kwargs)
        return {"counting_namespaces_dirty": 0}

    monkeypatch.setattr(scheduler_tasks, "run_counting_consolidation", _fake)
    return seen


@pytest.mark.unit
def test_flag_off_never_runs_counting_or_touches_its_adapter_methods(monkeypatch):
    # Fails if the scheduler runs counting without `enable_counting` (or stamps counting_* keys).
    _patch_scalar(monkeypatch)
    seen = _patch_counting_runner(monkeypatch)
    graph = _CounterGraph()
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_noop_llm, namespaces=["a"], k=1,
        enable_scalar_state=True, enable_counting=False))
    assert not [k for k in out if k.startswith("counting_")]
    assert seen == [] and graph.counting_calls == []


@pytest.mark.unit
def test_counting_without_scalar_state_does_not_run(monkeypatch):
    # Fails if the `enable_scalar_state` prerequisite is dropped.
    seen = _patch_counting_runner(monkeypatch)
    graph = _CounterGraph()
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_noop_llm, namespaces=["a"], k=1,
        enable_scalar_state=False, enable_counting=True))
    assert not [k for k in out if k.startswith("counting_")]
    assert seen == [] and graph.counting_calls == []


@pytest.mark.unit
def test_counting_runs_after_scalar_sharing_counter_budget_and_versions(monkeypatch):
    # Positive control for the two tests above (so the gate cannot be vacuously "never runs"), and
    # proves counting shares the scalar phase's _CallCounter/budget and gets its own version.
    order: list[str] = []
    scalar_kwargs: dict = {}

    def _scalar(graph, **kw):
        order.append("scalar")
        scalar_kwargs.update(kw)
        kw["counting_llm"].calls += 3   # the scalar phase spends calls
        return {"scalar_namespaces_dirty": 0}

    seen: list[dict] = []

    def _counting(graph, **kw):
        order.append("counting")
        seen.append(kw)
        return {"counting_namespaces_dirty": 7}

    monkeypatch.setattr(scheduler_tasks, "run_scalar_consolidation", _scalar)
    monkeypatch.setattr(scheduler_tasks, "run_counting_consolidation", _counting)
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        _CounterGraph(), llm_complete=_noop_llm, namespaces=["a"], k=1, call_budget=9,
        enable_scalar_state=True, scalar_state_perceiver_version="sv2",
        enable_counting=True, counting_perceiver_version="cv3"))
    assert order == ["scalar", "counting"]
    assert out["counting_namespaces_dirty"] == 7
    kw = seen[0]
    assert kw["counting_llm"] is scalar_kwargs["counting_llm"]
    assert kw["counting_llm"].calls == 3 and kw["call_budget"] == 9
    assert kw["config"].perceiver_version == "cv3"
    assert kw["config"].scalar_perceiver_version == "sv2"
    assert kw["namespaces"] == ["a"]


# --------------------------------------------------------------------------- runner fakes

@dataclass
class _Ep:
    uuid: str
    content: str


def _build_episodes(rows):
    return [_Ep(uuid=str(r["uuid"]), content=str(r["content"])) for r in rows]


class _RunnerGraph:
    def __init__(self, pages=None, claims=None, dirty=None):
        self.pages = list(pages or [])
        self.claims = claims or []
        self.dirty = dirty or []
        self.calls: list[tuple[str, tuple, dict]] = []

    def _log(self, name, *a, **kw):
        self.calls.append((name, a, kw))

    def of(self, name):
        return [c for c in self.calls if c[0] == name]

    def scalar_state_service(self, **kw):
        return object()

    def list_counting_dirty_namespaces(self, **kw):
        self._log("list_counting_dirty_namespaces", **kw)
        return list(self.dirty)

    def load_next_counting_batch(self, namespace, **kw):
        self._log("load_next_counting_batch", namespace, **kw)
        return self.pages.pop(0) if self.pages else []

    def advance_counting_cursor(self, namespace, **kw):
        self._log("advance_counting_cursor", namespace, **kw)

    def claim_counting_namespace(self, namespace, **kw):
        self._log("claim_counting_namespace", namespace, **kw)
        return {"incarnation": "inc-1", "epoch": 1}

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
        return list(self.claims)


class _Llm:
    def __init__(self, calls=0):
        self.calls = calls

    def __call__(self, system, user):
        self.calls += 1
        return "[]"


class _FakePerception:
    instances: list["_FakePerception"] = []
    spend_per_call = 0

    def __init__(self, graph, state_service, **kwargs):
        self.init_kwargs = kwargs
        self.persisted: list[dict] = []
        _FakePerception.instances.append(self)

    def ensure_activated(self):
        pass

    def perceive_and_persist(self, episodes, llm, **kwargs):
        llm.calls += _FakePerception.spend_per_call
        self.persisted.append({"episodes": episodes, "llm": llm, **kwargs})
        return {"bound": 2, "advisory": 1}


@pytest.fixture
def fake_perception(monkeypatch):
    import menhir.services.typed_scalar_perception as tsp

    _FakePerception.instances = []
    _FakePerception.spend_per_call = 0
    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _FakePerception)
    return _FakePerception


def _row(i, cursor="2026-07-0{d}T00:00:00Z"):
    return {"uuid": f"t{i}", "valid_at": f"2026-07-0{i}T00:00:00Z",
            "cursor_at": cursor.format(d=i) if cursor else "", "content": f"I went to the gym {i}"}


def _run(graph, *, llm=None, namespaces=None, budget=None, batch_size=500, scalar_pv="sv1", pv="cv1"):
    llm = llm or _Llm()
    out = run_counting_consolidation(
        graph, namespaces=namespaces, counting_llm=llm, build_episodes=_build_episodes,
        k=1, threshold=1.0, call_budget=budget, embed=None,
        config=CountingConsolidationConfig(
            perceiver_version=pv, scalar_perceiver_version=scalar_pv, batch_size=batch_size))
    return out, llm


# --------------------------------------------------------------------------- budget

@pytest.mark.unit
def test_exhausted_shared_budget_blocks_all_counting_work(fake_perception):
    # Fails if counting ignores the budget the scalar phase already consumed.
    graph = _RunnerGraph(pages=[[_row(1)]])
    out, llm = _run(graph, llm=_Llm(calls=5), namespaces=["a", "b"], budget=5)
    assert graph.of("load_next_counting_batch") == []
    assert graph.of("advance_counting_cursor") == []
    assert graph.of("scalar_claims_for_turns") == []
    assert all(not inst.persisted for inst in fake_perception.instances)
    assert llm.calls == 5 and out["counting_llm_calls"] == 0
    assert out["counting_batches"] == 0 and out["counting_namespaces_processed"] == 0


@pytest.mark.unit
def test_budget_exhausted_mid_namespace_stops_paging_after_the_page_that_ran(fake_perception):
    # One page spends the remaining budget: that page's cursor advances (it ran), and no further page
    # nor the next namespace is loaded.
    fake_perception.spend_per_call = 2
    graph = _RunnerGraph(pages=[[_row(1), _row(2)], [_row(3)]])
    out, llm = _run(graph, namespaces=["a", "b"], budget=2, batch_size=2)
    assert len(graph.of("load_next_counting_batch")) == 1
    adv = graph.of("advance_counting_cursor")
    assert len(adv) == 1 and adv[0][2]["cursor_uuid"] == "t2"
    assert out["counting_llm_calls"] == 2 and out["counting_namespaces_processed"] == 1


# --------------------------------------------------------------------------- paging / cursor

@pytest.mark.unit
def test_runner_passes_counting_lane_arguments_and_advances_to_last_row(fake_perception):
    # Fails if the lane args (prompt / occurrence-only / ordinal 1 / scalar context) are dropped, if the
    # scalar version is not given to the loader, or if the cursor does not land on the LAST row with the
    # COUNTING version.
    claim = {"episode_uuid": "t2", "subject": "user", "attribute": "wake_time", "operation": "absolute",
             "value_json": json.dumps("07:30"), "stated_span": "wake at 7:30"}
    graph = _RunnerGraph(pages=[[_row(1), _row(2), _row(3)]], claims=[claim], dirty=["ns-1"])
    out, _ = _run(graph, scalar_pv="sv9", pv="cv4", batch_size=10)

    listed = graph.of("list_counting_dirty_namespaces")[0][2]
    assert listed["scalar_perceiver_version"] == "sv9" and listed["perceiver_version"] == "cv4"
    load = graph.of("load_next_counting_batch")[0]
    assert load[1] == ("ns-1",) or load[1][0] == "ns-1"
    assert load[2]["scalar_perceiver_version"] == "sv9" and load[2]["perceiver_version"] == "cv4"
    assert load[2]["limit"] == 10

    inst, = fake_perception.instances
    assert inst.init_kwargs["perceiver_version"] == "cv4"
    p, = inst.persisted
    assert p["system_prompt"] is COUNTING_SYSTEM_PROMPT
    assert p["allowed_operations"] == frozenset({"occurrence"})
    assert p["claim_ordinal"] == 1 == COUNTING_CLAIM_ORDINAL
    assert p["namespace"] == "ns-1"
    # context carries the scalar claim, keyed by the episode index the model sees (t2 -> [1])
    assert p["context"].startswith("ALREADY CAPTURED")
    assert "[1] subject=user attribute=wake_time operation=absolute value=07:30" in p["context"]
    assert p["episode_reference_time"]("t2") == "2026-07-02T00:00:00Z"

    adv, = graph.of("advance_counting_cursor")
    assert adv[1] == ("ns-1",)
    assert adv[2]["cursor_at"] == "2026-07-03T00:00:00Z" and adv[2]["cursor_uuid"] == "t3"
    assert adv[2]["perceiver_version"] == "cv4"
    assert out["counting_occurrences_stored"] == 2 and out["counting_advisory"] == 1
    assert out["counting_batches"] == 1 and out["counting_namespaces_processed"] == 1


@pytest.mark.unit
def test_full_page_continues_and_short_page_stops(fake_perception):
    # Fails if the loop stops after a full page (strands the tail) or keeps reading after a short page.
    graph = _RunnerGraph(pages=[[_row(1), _row(2)], [_row(3)], [_row(4)]])
    _run(graph, namespaces=["a"], batch_size=2)
    assert len(graph.of("load_next_counting_batch")) == 2     # full page then short page, then stop
    assert [c[2]["cursor_uuid"] for c in graph.of("advance_counting_cursor")] == ["t2", "t3"]


@pytest.mark.unit
def test_page_without_cursor_at_does_not_advance(fake_perception):
    # Fails if a timeless page advances the cursor (fail-closed rule) or loops on it forever.
    graph = _RunnerGraph(pages=[[_row(1, cursor=""), _row(2, cursor="")], [_row(3)]])
    _run(graph, namespaces=["a"], batch_size=2)
    assert graph.of("advance_counting_cursor") == []
    assert len(graph.of("load_next_counting_batch")) == 1


@pytest.mark.unit
def test_cursor_skips_trailing_timeless_rows_and_uses_last_orderable(fake_perception):
    graph = _RunnerGraph(pages=[[_row(1), _row(2), _row(3, cursor="")]])
    _run(graph, namespaces=["a"], batch_size=10)
    adv, = graph.of("advance_counting_cursor")
    assert adv[2]["cursor_uuid"] == "t2"


@pytest.mark.unit
def test_explicit_namespaces_override_dirty_listing_and_dedupe(fake_perception):
    graph = _RunnerGraph(pages=[[_row(1)], [_row(2)]], dirty=["never"])
    out, _ = _run(graph, namespaces=["a", "a", "b"])
    assert graph.of("list_counting_dirty_namespaces") == []
    assert [c[1][0] for c in graph.of("load_next_counting_batch")] == ["a", "b"]
    assert [c[1][0] for c in graph.of("advance_counting_cursor")] == ["a", "b"]
    assert out["counting_namespaces_dirty"] == 2


# --------------------------------------------------------------------------- context

@pytest.mark.unit
def test_build_counting_context_maps_episode_index_and_skips_off_page_claims():
    eps = [_Ep("e-a", "x"), _Ep("e-b", "y")]
    claims = [
        {"episode_uuid": "e-b", "subject": "user", "attribute": "cars", "operation": "absolute",
         "value_json": "3", "stated_span": "3 cars"},
        {"episode_uuid": "e-gone", "subject": "user", "attribute": "dogs", "operation": "delta",
         "value_json": "1", "stated_span": "1 dog"},
    ]
    ctx = build_counting_context(eps, claims)
    assert "[1] subject=user attribute=cars" in ctx
    assert "dogs" not in ctx and "[0]" not in ctx


@pytest.mark.unit
def test_build_counting_context_empty_when_nothing_matches():
    eps = [_Ep("e-a", "x")]
    assert build_counting_context(eps, []) == ""
    assert build_counting_context(eps, [{"episode_uuid": "zzz", "attribute": "q"}]) == ""


# --------------------------------------------------------------------------- occurrence guard

_GYM_ROW = dict(
    episode=0, subject="user", attribute="gym_visit_count", scope="", value_kind="count",
    unit="", operation="occurrence", importance="tracked", value=1,
)


def _gym(content, span):
    drops: list[str] = []
    ep = _Ep("ep-1", content)
    out = extract_typed_scalars_once(
        [ep], lambda s, u: json.dumps([dict(_GYM_ROW, stated_span=span)]),
        on_drop=drops.append, allowed_operations=COUNTING_OPERATIONS, claim_ordinal=1)
    return out, drops


@pytest.mark.unit
@pytest.mark.parametrize("content,span", [
    ("I'm going to the gym tomorrow.", "going to the gym tomorrow"),
    ("If I go to the gym I will be tired.", "go to the gym"),
    ("I didn't go to the gym.", "go to the gym"),
])
def test_occurrence_guard_drops_planned_hypothetical_and_negated(content, span):
    out, drops = _gym(content, span)
    assert out == []
    assert drops == ["occurrence_not_happened"]


@pytest.mark.unit
def test_occurrence_that_happened_is_kept_with_ordinal_one():
    out, drops = _gym("I went to the gym today.", "went to the gym today")
    assert drops == []
    p, = out
    assert p.operation == "occurrence" and p.claim_ordinal == 1 and p.importance == "tracked"


# --------------------------------------------------------------------------- identity

@pytest.mark.unit
def test_same_span_in_two_lanes_has_distinct_source_keys():
    base = dict(
        subject_text="user", attribute="gym_visit_count", scope="", value_kind="count", unit="",
        operation="occurrence", value=1, stated_span="went to the gym", episode_uuid="ep-1",
        span_start=2, span_end=17, when=None, importance="tracked")
    a = TypedScalarProposal(**base, claim_ordinal=0)
    b = TypedScalarProposal(**base, claim_ordinal=1)
    assert a.source_key != b.source_key


# --------------------------------------------------------------------------- provenance edges

def _arow(**over):
    base = dict(
        assertion_id="x", subject_uuid="ent-self", subject_display="me", attribute="gym_visit_count",
        scope="", value_kind="count", unit="", operation="occurrence", value=1, importance="tracked",
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="user", episode_uuid="ep-1", binding_pending=False)
    base.update(over)
    return base


class _Assertions:
    def __init__(self, rows):
        self._rows = rows

    def materializable_assertions_for_entity(self, subject_uuid, *, namespace=None):
        return [r for r in self._rows if r["subject_uuid"] == subject_uuid]


class _Views:
    def __init__(self):
        self.edges: list[dict] = []

    def record_scalar_state(self, **kw):
        return {"view_key": f"vk-{kw['attribute']}", "uuid": f"view-{kw['attribute']}"}

    def list_scalar_state_views(self, *, subject_uuid, namespace=None):
        return []

    def retire_scalar_state(self, *, view_key):
        return True

    def draw_scalar_state_provenance_edges(self, **kw):
        self.edges.append(kw)


@pytest.mark.unit
def test_counted_view_draws_occurrence_edges_and_stated_view_is_unchanged():
    rows = [
        # counted slot: o1, o2 count; o3 is absorbed by delta d3 on the same turn (ep-3)
        _arow(assertion_id="o1", episode_uuid="ep-1", valid_at="2026-07-01T00:00:00+00:00"),
        _arow(assertion_id="o2", episode_uuid="ep-2", valid_at="2026-07-02T00:00:00+00:00"),
        _arow(assertion_id="o3", episode_uuid="ep-3", valid_at="2026-07-03T00:00:00+00:00"),
        _arow(assertion_id="d3", operation="delta", value=2, importance="", episode_uuid="ep-3",
              valid_at="2026-07-03T00:00:00+00:00"),
        # stated slot: absolute + delta + a stray occurrence that must NOT show up in the edges
        _arow(assertion_id="a1", attribute="owned", operation="absolute", value=5, importance="",
              episode_uuid="ep-4", valid_at="2026-07-04T00:00:00+00:00"),
        _arow(assertion_id="sd1", attribute="owned", operation="delta", value=1, importance="",
              episode_uuid="ep-5", valid_at="2026-07-05T00:00:00+00:00"),
        _arow(assertion_id="so1", attribute="owned", operation="occurrence", episode_uuid="ep-6",
              valid_at="2026-07-06T00:00:00+00:00"),
    ]
    views = _Views()
    ScalarStateService(_Assertions(rows), views).rebuild_scalar_state("ent-self")
    by_view = {e["view_uuid"]: e for e in views.edges}

    counted = by_view["view-gym_visit_count"]
    assert counted["anchor_id"] == ""
    assert sorted(counted["contributed_delta_ids"]) == ["o1", "o2"]
    assert counted["superseded_anchor_ids"] == ["o3"]

    stated = by_view["view-owned"]
    assert stated["anchor_id"] == "a1"
    assert stated["contributed_delta_ids"] == ["sd1"]
    assert "so1" not in stated["contributed_delta_ids"] + stated["superseded_anchor_ids"]


# --------------------------------------------------------------------------- wipe sites

class _RecNeo4j:
    def __init__(self, default):
        self.default = default
        self.executed: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        return self.default


@pytest.mark.unit
def test_purge_scalar_state_nodes_deletes_counting_watermarks():
    fake = _RecNeo4j([{"assertions": 0, "heads": 0, "rebinds": 0, "receipts": 0,
                       "projection_repairs": 0, "views": 0, "watermarks": 0}])
    TypedAssertionRepository(fake).purge_scalar_state_nodes()
    q, _ = fake.executed[-1]
    assert re.search(r"MATCH \(\w+:CountingConsolidationWatermark\) DETACH DELETE \w+", q)


_SRC = Path(cc.__file__).resolve().parents[1] / "infrastructure"


def _wipe_label_counts(path: Path) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8")
    # the watermark-reset blocks (`w:X` predicates), not a `{group_id: ...}` read/MERGE pattern. The
    # namespace cascade's `n:` doomed list is not counted: its reset block already deletes every
    # watermark keyed by the namespace, counting included.
    pat = r"\bw:{label}\b(?!\s*\{{)"
    return (len(re.findall(pat.format(label="EventConsolidationWatermark"), text)),
            len(re.findall(pat.format(label="CountingConsolidationWatermark"), text)))


@pytest.mark.unit
@pytest.mark.parametrize("fname", ["memory_queries.py", "turn_evidence_repository.py"])
def test_every_watermark_wipe_site_that_names_event_also_names_counting(fname):
    # Fails if a namespace-delete / watermark-reset query is added or edited without the new label:
    # a stale counting cursor would then survive the wipe and the namespace would look consolidated.
    events, counting = _wipe_label_counts(_SRC / fname)
    assert events >= 1
    assert counting == events
