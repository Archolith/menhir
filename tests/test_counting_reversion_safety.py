"""Counting re-version safety (options A + B): a version change never drops a count because the
re-read failed, and an old row the new samples proposed over but did not agree on stays counted.

Three layers: the perception hooks (real extract, gate and persist; fake adapter), rule B on its own,
and the runner's page order (fake graph and perception).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from menhir.domain.typed_assertion import COUNTING_OPERATIONS
from menhir.infrastructure.turn_evidence_repository import CountingFenceLost
from menhir.services.counting_consolidation import (
    COUNTING_SYSTEM_PROMPT,
    CountingConsolidationConfig,
    reversion_carried,
    run_counting_consolidation,
)
from menhir.services.typed_scalar_service import TypedScalarPerceptionService
from tests.test_count_match_runner import _build_episodes, _Graph, _Llm, _row

# ------------------------------------------------------------------ perception hooks

_EPISODES = [SimpleNamespace(uuid="t1", content="I went to the gym today"),
             SimpleNamespace(uuid="t2", content="Quiet day at home")]


def _obs(span):
    return {"subject": "user", "attribute": "gym_visit_count", "scope": "", "value_kind": "count",
            "unit": "", "operation": "occurrence", "value": 1, "importance": "tracked",
            "stated_span": span}


_CLEAN = json.dumps([{"episode": 0, "observations": [_obs("went to the gym")]},
                     {"episode": 1, "observations": []}])


class _SeqLlm:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, _system, _user):
        text = self.responses[self.calls % len(self.responses)]
        self.calls += 1
        return text


class _Adapter:
    def __init__(self, log):
        self.log = log
        self.marked: list[list[str]] = []

    def activate_scalar_state(self):
        return {"ok": True}

    def scalar_state_schema_ready(self):
        return True

    def fetch_linked_entities_for_episode(self, _uuid):
        return []

    def ensure_self_entity(self, _ns):
        return "self-1"

    def record_typed_assertion(self, assertion):
        self.log.append(("record", assertion.source_key))
        return {"assertion_id": f"id-{assertion.source_key[:8]}", "binding_pending": False,
                "binding_mismatch": False}

    def mark_projection_complete(self, ids):
        self.marked.append(list(ids))


class _State:
    def __init__(self, log):
        self.log = log

    def rebuild_scalar_state(self, subject, **_kw):
        self.log.append(("rebuild", subject))
        return {"complete": True}


def _perceive(llm, *, k=2, **kw):
    log: list[tuple] = []
    adapter = _Adapter(log)
    service = TypedScalarPerceptionService(adapter, _State(log), perceiver_version="cv1+m")
    out = service.perceive_and_persist(
        _EPISODES, llm, k=k, threshold=1.0, namespace="ns-1",
        system_prompt=COUNTING_SYSTEM_PROMPT, allowed_operations=COUNTING_OPERATIONS,
        claim_ordinal=1, **kw)
    return out, log, adapter


@pytest.mark.unit
def test_deferred_rebuild_stores_rows_pending_and_returns_the_spans():
    # Fails if a deferred write still rebuilds (a View would count old and new together) or clears
    # projection_pending before the caller's rebuild, or if the spans rule B needs are missing.
    out, log, adapter = _perceive(_SeqLlm(_CLEAN), defer_rebuild=True)
    assert [e for e in log if e[0] == "rebuild"] == []
    assert adapter.marked == []
    (row,) = out["results"]
    assert row["bound"] and row["projection_incomplete"] and row["subject_uuid"] == "self-1"
    start = _EPISODES[0].content.index("went to the gym")
    span = ("t1", start, start + len("went to the gym"))
    assert out["spans"] == {"clean_turns": ["t1", "t2"], "proposed": [span, span],
                            "committed": [span]}


@pytest.mark.unit
def test_a_rule_rejection_of_the_quoted_span_still_counts_as_reading_the_turn():
    # F-04: every sample quotes a plan ("going to go to the gym tomorrow") and the parser drops it
    # as not happened. That is the new version reading the turn and finding nothing, so the turn is
    # clean and rule B can retire an old row there. Fails if the rejection marks the turn unread.
    episodes = [SimpleNamespace(uuid="t1", content="I'm going to go to the gym tomorrow")]
    reply = json.dumps([{"episode": 0, "observations": [_obs("go to the gym")]}])
    log: list[tuple] = []
    service = TypedScalarPerceptionService(_Adapter(log), _State(log), perceiver_version="cv1+m")
    out = service.perceive_and_persist(
        episodes, _SeqLlm(reply), k=2, threshold=1.0, namespace="ns-1",
        system_prompt=COUNTING_SYSTEM_PROMPT, allowed_operations=COUNTING_OPERATIONS,
        claim_ordinal=1, defer_rebuild=True)
    assert out["spans"] == {"clean_turns": ["t1"], "proposed": [], "committed": []}
    old = [_prior("plan", "t1", 0, 30)]
    assert reversion_carried(old, out["spans"], []) == []


@pytest.mark.unit
@pytest.mark.parametrize("second, clean", [
    ("not json at all", []),                                          # whole sample unreadable
    ("[]", []),                                                       # no envelopes: no coverage
    (json.dumps([{"episode": 0, "observations": [_obs("went to the gym")]}]), ["t1"]),  # t2 missing
    (json.dumps([{"episode": 0, "observations": [_obs("went to the gym")]},
                 {"episode": 1, "observations": [_obs("swam laps")]}]), ["t1"]),  # t2 row dropped
])
def test_a_turn_is_clean_only_when_every_sample_read_it_in_full(second, clean):
    # Fails if a truncated, envelope-less or partly dropped sample counts as "nothing there", which
    # would let rule B retire an old row on a turn the new version never actually read.
    out, _, _ = _perceive(_SeqLlm(_CLEAN, second), defer_rebuild=True)
    assert out["spans"]["clean_turns"] == clean


@pytest.mark.unit
def test_defaults_are_unchanged():
    # Fails if the scalar lane (no new kwargs) loses its per-row rebuild or gains a result key.
    out, log, adapter = _perceive(_SeqLlm(_CLEAN))
    assert "spans" not in out
    assert [e for e in log if e[0] == "rebuild"] == [("rebuild", "self-1")]
    assert len(adapter.marked) == 1


# ------------------------------------------------------------------ rule B

def _prior(aid, turn, start, end):
    return {"assertion_id": aid, "source_key": f"sk-{aid}", "episode_uuid": turn,
            "span_start": start, "span_end": end, "subject_uuid": "s1"}


@pytest.mark.unit
def test_rule_b_keeps_only_rows_the_new_reading_could_not_settle():
    # Fails if a vetoed overlap is retired (the F1 loss), if a replaced or clean-and-empty row is
    # kept, or if a row on an unclean turn is retired.
    prior = [
        _prior("veto", "t1", 0, 10),      # proposed over, nothing committed -> carried
        _prior("gone", "t2", 0, 10),      # clean turn, nothing proposed over it -> retired
        _prior("swap", "t3", 0, 10),      # committed overlap -> retired (replaced)
        _prior("dark", "t4", 0, 10),      # turn not cleanly read -> carried
        _prior("edge", "t2", 20, 30),     # touches a proposal only at the boundary -> retired
    ]
    spans = {"clean_turns": ["t1", "t2", "t3"],
             "proposed": [("t1", 5, 15), ("t2", 30, 40), ("t3", 2, 4)],
             "committed": [("t3", 2, 4)]}
    assert reversion_carried(prior, spans, [_stored("t3", 2, 4)]) == ["veto", "dark"]


def _stored(turn, start, end):
    return {"assertion_id": f"n-{turn}-{start}", "episode_uuid": turn,
            "span_start": start, "span_end": end}


@pytest.mark.unit
def test_rule_b_treats_a_row_without_a_span_as_covering_its_turn():
    prior = [{"assertion_id": "a", "episode_uuid": "t1"}, {"assertion_id": "b", "episode_uuid": "t2"}]
    spans = {"clean_turns": ["t1", "t2"], "proposed": [("t1", 0, 3)], "committed": []}
    assert reversion_carried(prior, spans, []) == ["a"]
    assert reversion_carried(prior, {}, []) == ["a", "b"]             # no reading: keep all


@pytest.mark.unit
def test_rule_b_replaces_only_with_what_was_stored_not_what_the_gate_committed():
    # Fails if a committed claim that was never stored (mismatch, advisory, a failed write) retires
    # the old row: the happening would count nowhere.
    prior = [_prior("old", "t1", 0, 10)]
    spans = {"clean_turns": ["t1"], "proposed": [("t1", 0, 10)], "committed": [("t1", 0, 10)]}
    assert reversion_carried(prior, spans, []) == ["old"]


@pytest.mark.unit
def test_rule_b_a_current_row_from_a_partial_earlier_run_replaces_the_old_row():
    # F-01: run 1 stored N over O and stopped before retiring O; run 2 vetoes. Fails if O is
    # carried next to N (both counted for good once the cursor passes the page).
    prior = [_prior("O", "t1", 10, 30)]
    spans = {"clean_turns": ["t1"], "proposed": [("t1", 12, 20)], "committed": []}
    assert reversion_carried(prior, spans, [_stored("t1", 10, 25)]) == []


# ------------------------------------------------------------------ F-05: carried pre-match rows

@pytest.mark.unit
@pytest.mark.parametrize("row, status", [
    ({"count_status": "", "perceiver_version": "cv1"}, "counted"),       # counted under its version
    ({"count_status": None, "perceiver_version": "cv1+x2"}, "counted"),
    ({"count_status": "", "perceiver_version": "cv1+m"}, "uncertain"),   # match row not decided yet
    ({"count_status": ""}, "uncertain"),                                 # no version: unknown
    ({"count_status": "repeat", "perceiver_version": "cv1"}, "repeat"),
])
def test_status_of_an_undecided_pre_match_row_is_counted(row, status):
    from menhir.domain.count_match import status_of

    assert status_of(row) == status


@pytest.mark.unit
def test_match_fold_still_counts_a_carried_pre_match_row():
    # Fails if a re-version to match turns a carried row (counted under its old version, never
    # decided again) into a permanent uncertain one: the count drops though nothing was retired.
    from menhir.domain.scalar_state_fold import fold_assertions
    from tests.test_count_match_decide import _occ as _match_occ

    rows = [dict(_match_occ("carried", 1, perceiver_version="cv1"), operation="occurrence",
                 value=1, scope="", unit="", evidence_tier="agent", importance="tracked"),
            dict(_match_occ("new", 5, count_status="counted"), operation="occurrence", value=1,
                 scope="", unit="", evidence_tier="agent", importance="tracked")]
    (state,) = [s for s in fold_assertions(rows, count_match=True).states
                if s.attribute == "model_kit_count"]
    assert sorted(state.contributed_occurrence_ids) == ["carried", "new"]
    assert state.count_uncertain == 0


@pytest.mark.unit
def test_a_retelling_of_a_carried_pre_match_row_is_its_repeat_and_never_swaps_it():
    # Fails if decide treats the carried row as an uncertain target (the retelling would stay
    # uncertain), or swaps it (a swap may only demote a row of the current version).
    from tests.test_count_match_decide import _occ as _match_occ
    from tests.test_count_match_decide import _rules, _v

    pool = [_match_occ("carried", 9, perceiver_version="cv1")]
    out = _rules([_match_occ("c1", 5)], pool, {"c1": _v("repeat", "carried")})
    (d,) = out.decisions
    assert (d["status"], d["target_id"]) == ("repeat", "carried")
    assert out.swaps == []


# ------------------------------------------------------------------ runner page order

class _CompleteSvc:
    """Rebuilds log into the graph and report complete, as the real service does; subjects in
    `fail_rebuild` report an incomplete rebuild."""

    def __init__(self, graph):
        self._graph = graph

    def rebuild_scalar_state(self, subject, *, namespace=None, **kw):
        self._graph.calls.append(("rebuild", (subject,), {"namespace": namespace}))
        return {"complete": subject not in self._graph.fail_rebuild}

    rebuild_scalar_projections = rebuild_scalar_state


class _RevGraph(_Graph):
    def __init__(self, *, prior=(), retire_subjects=(), stored=(), owed=(), fail_rebuild=(),
                 **kw):
        super().__init__(**kw)
        self.prior = list(prior)
        self.retire_subjects = list(retire_subjects)
        self.stored = list(stored)
        self.owed = list(owed)
        self.fail_rebuild = set(fail_rebuild)

    def scalar_state_service(self, **kw):
        self._log("scalar_state_service", **kw)
        return _CompleteSvc(self)

    def claim_counting_namespace(self, namespace, **kw):
        self._log("claim_counting_namespace", namespace, **kw)
        return {"incarnation": "inc-1", "epoch": 7, "rebuild_subjects": list(self.owed)}

    def prior_version_occurrences(self, namespace, turn_ids, **kw):
        self._log("prior_version_occurrences", namespace, list(turn_ids), **kw)
        return [p for p in self.prior if p["episode_uuid"] in turn_ids]

    def current_version_occurrence_spans(self, namespace, turn_ids, **kw):
        self._log("current_version_occurrence_spans", namespace, list(turn_ids), **kw)
        return [s for s in self.stored if s["episode_uuid"] in turn_ids]

    def retire_prior_version_occurrences(self, namespace, turn_ids, **kw):
        self._log("retire_prior_version_occurrences", namespace, list(turn_ids), **kw)
        return list(self.retire_subjects)

    def mark_projection_complete(self, assertion_ids):
        self._log("mark_projection_complete", list(assertion_ids))
        return len(assertion_ids)


class _RevPerception:
    script: dict = {}

    def __init__(self, graph, state_service, **kwargs):
        self.graph = graph

    def ensure_activated(self):
        pass

    def perceive_and_persist(self, episodes, llm, **kwargs):
        s = _RevPerception.script
        self.graph._log("perceive", defer_rebuild=kwargs.get("defer_rebuild"))
        llm.calls += int(s.get("spend", 0))
        if s.get("raise") is not None:
            raise s["raise"]
        for sk in s.get("writes", []):
            kwargs["write_guard"]()
            self.graph._log("record", sk)
        results = list(s.get("results", []))
        return {"bound": sum(1 for r in results if r.get("bound")), "advisory": 0,
                "results": results, "decisions": 1, "committed": 1, "spans": s.get("spans", {})}


@pytest.fixture
def rev(monkeypatch):
    import menhir.services.typed_scalar_perception as tsp

    _RevPerception.script = {}
    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _RevPerception)
    return _RevPerception


def _run(graph, *, match=False, call_budget=None, batch_size=500):
    return run_counting_consolidation(
        graph, namespaces=["ns-1"], counting_llm=_Llm(), build_episodes=_build_episodes,
        k=1, threshold=1.0, call_budget=call_budget, embed=None,
        config=CountingConsolidationConfig(
            perceiver_version="cv1", scalar_perceiver_version="sv1", batch_size=batch_size,
            match=match))


_OLD = [_prior("old-1", "t1", 0, 10), _prior("old-2", "t1", 20, 30)]


@pytest.mark.unit
@pytest.mark.parametrize("exc, counter", [
    (RuntimeError("llm timeout"), "counting_namespaces_failed"),
    (CountingFenceLost("lease lost mid-LLM"), "counting_namespaces_fenced"),
])
def test_a_failed_reading_leaves_the_old_rows_counted(rev, exc, counter):
    # Fails if anything is retired or rebuilt before the new reading is stored (F2), or if the
    # cursor moves past a page whose reading failed.
    rev.script = {"raise": exc}
    graph = _RevGraph(pages=[[_row(1)]], prior=_OLD)
    out = _run(graph)
    assert graph.of("retire_prior_version_occurrences") == []
    assert graph.of("rebuild") == []
    assert graph.of("advance_counting_cursor") == []
    assert out[counter] == 1


@pytest.mark.unit
def test_budget_stop_reads_and_retires_nothing_on_the_unread_page(rev):
    # Fails if a page the budget stopped before has its old rows retired.
    rev.script = {"spend": 3, "spans": {"clean_turns": ["t1"], "proposed": [], "committed": []}}
    graph = _RevGraph(pages=[[_row(1)], [_row(2)]], prior=_OLD + [_prior("old-3", "t2", 0, 5)])
    _run(graph, call_budget=3, batch_size=1)
    assert [c[1][1] for c in graph.of("prior_version_occurrences")] == [["t1"]]
    assert [c[1][1] for c in graph.of("retire_prior_version_occurrences")] == [["t1"]]
    assert len(graph.of("advance_counting_cursor")) == 1


@pytest.mark.unit
def test_one_retire_after_every_write_hands_heads_to_the_stored_rows(rev):
    # F-03: fails if anything is retired before a write (a failed write would then leave the head
    # with no current row and the re-read could not carry it), or if the retire does not hand the
    # old row's head to the stored new row in the same call, or if rule B ignores what was stored.
    rev.script = {
        "writes": ["sk-old-1", "sk-new"],
        "results": [{"bound": True, "assertion_id": "n1", "subject_uuid": "s1"}],
        "spans": {"clean_turns": ["t1"], "proposed": [("t1", 0, 10), ("t1", 22, 25)],
                  "committed": [("t1", 0, 10)]},
    }
    graph = _RevGraph(pages=[[_row(1)]], prior=_OLD, stored=[_stored("t1", 0, 10)])
    out = _run(graph)
    names = graph.names()
    (retire,) = graph.of("retire_prior_version_occurrences")
    assert retire[1] == ("ns-1", ["t1"])
    assert retire[2]["keep_ids"] == ["old-2"]                         # vetoed overlap carried
    assert retire[2]["promote_ids"] == ["n1"]
    assert names.index("retire_prior_version_occurrences") > len(names) - 1 - names[::-1].index(
        "record")
    (read,) = graph.of("current_version_occurrence_spans")
    assert read[2]["include_ids"] == ["n1"]
    assert out["counting_reversion_carried"] == 1


@pytest.mark.unit
def test_a_page_with_no_prior_rows_reads_nothing_back_and_promotes_nothing(rev):
    rev.script = {"results": [{"bound": True, "assertion_id": "n1", "subject_uuid": "s1"}],
                  "spans": {"clean_turns": ["t1"], "proposed": [], "committed": []}}
    graph = _RevGraph(pages=[[_row(1)]])
    _run(graph)
    assert graph.of("current_version_occurrence_spans") == []
    (retire,) = graph.of("retire_prior_version_occurrences")
    assert "promote_ids" not in retire[2]


def _released(graph):
    (call,) = graph.of("release_counting_claim")
    return call[2].get("rebuilt_subjects")


@pytest.mark.unit
def test_subjects_an_earlier_owner_retired_are_rebuilt_first_and_cleared_on_release(rev):
    # F-02: the watermark lists s9 (an earlier owner retired its row and stopped before the
    # rebuild). Fails if this owner does not rebuild it before reading pages, or never clears it.
    rev.script = {"spans": {"clean_turns": ["t1"], "proposed": [], "committed": []}}
    graph = _RevGraph(pages=[[_row(1)]], owed=["s9"])
    _run(graph)
    names = graph.names()
    assert graph.calls[names.index("rebuild")][1] == ("s9",)
    assert names.index("rebuild") < names.index("load_next_counting_batch")
    assert _released(graph) == ["s9"]


@pytest.mark.unit
def test_a_subject_whose_rebuild_failed_stays_owed(rev):
    # F-02: fails if a retired subject whose rebuild did not complete is cleared from the
    # watermark (its View would keep counting the retired row with nothing left to rebuild it).
    rev.script = {"spans": {"clean_turns": ["t1"], "proposed": [], "committed": []}}
    graph = _RevGraph(pages=[[_row(1)]], prior=_OLD, retire_subjects=["s1", "s2"],
                      fail_rebuild={"s2"})
    _run(graph)
    assert _released(graph) == ["s1"]


@pytest.mark.unit
def test_no_rebuild_owed_releases_without_clearing(rev):
    rev.script = {"spans": {"clean_turns": ["t1"], "proposed": [], "committed": []}}
    graph = _RevGraph(pages=[[_row(1)]])
    _run(graph)
    assert _released(graph) is None


@pytest.mark.unit
@pytest.mark.parametrize("match", [False, True])
def test_one_rebuild_per_subject_after_retire_then_decide(rev, match):
    # Fails if a subject is rebuilt before the leftover retire (a View would briefly count old and
    # new rows together), rebuilt twice, never marked complete, or decided before the retire.
    rev.script = {
        "writes": ["sk-new"],
        "results": [{"bound": True, "assertion_id": "n1", "subject_uuid": "s1"},
                    {"bound": True, "assertion_id": "n2", "subject_uuid": "s2"}],
        "spans": {"clean_turns": ["t1"], "proposed": [], "committed": []},
    }
    graph = _RevGraph(pages=[[_row(1)]], prior=_OLD, retire_subjects=["s1", "s3"])
    _run(graph, match=match)
    names = graph.names()
    rest = len(names) - 1 - names[::-1].index("retire_prior_version_occurrences")
    rebuilds = [(i, c[1][0]) for i, c in enumerate(graph.calls) if c[0] == "rebuild"]
    page_rebuilds = [s for i, s in rebuilds if i > rest and i < names.index("advance_counting_cursor")]
    assert sorted(page_rebuilds) == ["s1", "s2", "s3"]
    assert all(i > rest for i, _ in rebuilds)
    assert graph.of("mark_projection_complete") == [
        ("mark_projection_complete", (["n1"],), {}), ("mark_projection_complete", (["n2"],), {})]
    if match:
        assert names.index("count_occurrences_by_ids") > rest


@pytest.mark.unit
def test_each_page_retires_only_its_own_turns(rev):
    # Fails if a later page's retire can reach a row carried on an earlier page.
    rev.script = {"spans": {"clean_turns": ["t1", "t2"], "proposed": [], "committed": []}}
    graph = _RevGraph(pages=[[_row(1)], [_row(2)]], prior=_OLD)
    _run(graph, batch_size=1)
    turns = [c[1][1] for c in graph.of("retire_prior_version_occurrences")]
    assert turns == [["t1"], ["t2"]]
