"""Related-count matching (unit 4R, PR 2): the decide step with a fake LLM.

Pure prompt / parse / vote / code-rule tests, runner tests against a fake graph, and the
decisions-plus-fold end-to-end. Helpers are copied from tests/test_count_match_runner.py and
tests/test_count_match_fold.py, not imported.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from menhir.domain.count_decide import (
    COSINE_MIN,
    DECIDE_K,
    MAX_DECIDE_ATTEMPTS,
    REDECIDE_LIMIT,
    apply_decide_rules,
    build_decide_prompt,
    key_text,
    parse_decide_response,
    vote,
)
from menhir.domain.count_match import RelatedSet, related_sets
from menhir.domain.scalar_state_fold import fold_assertions
from menhir.services.counting_consolidation import (
    CountingConsolidationConfig,
    run_counting_consolidation,
)

CUR = "cv1+m"


def _occ(aid, day, subject="s1", **over):
    base = dict(
        assertion_id=aid, subject_uuid=subject, value_kind="count", attribute="model_kit_count",
        count_object="model kit", count_action="", count_item="", stated_span="",
        valid_at=f"2026-07-{day:02d}T00:00:00+00:00", episode_uuid=f"ep-{aid}",
        count_status="", perceiver_version=CUR,
    )
    base.update(over)
    return base


def _counted(aid, day, **over):
    return _occ(aid, day, count_status="counted", **over)


def _v(decision, target="", family="model kit", tally="3/3"):
    return (decision, target, family, tally)


def _rules(candidates, pool, voted, **kw):
    sets = related_sets(candidates, pool)
    return apply_decide_rules(sets, voted, decided=kw.pop("decided", True),
                              current_version=kw.pop("current_version", CUR))


def _by_id(outcome):
    return {d["assertion_id"]: d for d in outcome.decisions}


# ------------------------------------------------------------------ 1. build_decide_prompt

@pytest.mark.unit
def test_prompt_handles_order_related_refs_and_no_assertion_ids():
    # Fails if candidates are not numbered in set order, a page candidate related to a later one is
    # shown as an R row instead of its N handle, an assertion id leaks, or a field is not shown.
    pool = [_counted("zz-pool-1", 1, stated_span="I built the Hornet", count_action="built",
                     count_item="Hornet")]
    cands = [
        _occ("zz-cand-a", 3, stated_span="finished the Tiger kit last week", count_action="finished",
             count_item="Tiger"),
        _occ("zz-cand-b", 5, stated_span="that kit I finished", count_action="finished"),
    ]
    sets = related_sets(cands, pool)
    prompt = build_decide_prompt(sets)

    assert prompt.handles["N1"] == "zz-cand-a" and prompt.handles["N2"] == "zz-cand-b"
    assert prompt.candidate_handles == {"zz-cand-a": "N1", "zz-cand-b": "N2"}
    assert prompt.handles["R1"] == "zz-pool-1"
    assert set(prompt.handles) == {"N1", "N2", "R1"}
    lines = prompt.user.splitlines()
    assert lines.index("  related: R1") == lines.index(
        next(ln for ln in lines if ln.startswith("NEW N1:"))) + 1
    # N2 is related to the earlier page candidate N1 (by its N handle) and to the pool row
    assert "  related: N1, R1" in lines
    assert not any(ln.startswith("RELATED N") for ln in lines)
    assert sum(1 for ln in lines if ln.startswith("RELATED R1:")) == 1
    for aid in ("zz-cand-a", "zz-cand-b", "zz-pool-1"):
        assert aid not in prompt.user
    for shown in ("finished the Tiger kit last week", "I built the Hornet", "2026-07-03",
                  "2026-07-05", "2026-07-01", '"model kit"', '"finished"', '"Tiger"', '"Hornet"'):
        assert shown in prompt.user


@pytest.mark.unit
def test_prompt_shares_one_r_handle_across_candidates():
    # Fails if the same pool row gets a fresh R handle per candidate.
    pool = [_counted("p-shared", 1)]
    cands = [_occ("c-x", 3, subject="s1"), _occ("c-y", 4, subject="s1")]
    prompt = build_decide_prompt(related_sets(cands, pool))
    assert [h for h in prompt.handles if h.startswith("R")] == ["R1"]


# ------------------------------------------------------------------ 2. parse_decide_response

def _two_cand_prompt():
    pool = [_counted("p1", 1)]
    return build_decide_prompt(related_sets([_occ("c1", 3), _occ("c2", 5)], pool))


@pytest.mark.unit
def test_parse_tolerates_prose_and_maps_handles_to_assertion_ids():
    # Fails if prose around the array breaks parsing or a handle is not mapped back to its id.
    prompt = _two_cand_prompt()
    text = (
        "Sure, here is my answer:\n"
        '[{"new": "N1", "decision": "repeat", "target": "R1", "family": "model kit"},'
        ' {"new": "N2", "decision": "same_event", "target": "N1", "family": 7}]\n'
        "Hope that helps [really]."
    )
    # the trailing "[really]" makes the outer slice invalid JSON -> the sample abstains
    assert parse_decide_response(text, prompt) == {}
    text = text.replace("Hope that helps [really].", "Hope that helps.")
    assert parse_decide_response(text, prompt) == {
        "c1": ("repeat", "p1", "model kit"),
        "c2": ("same_event", "c1", ""),  # non-string family -> ""
    }


@pytest.mark.unit
@pytest.mark.parametrize("item", [
    '{"new": "N9", "decision": "counted", "target": "", "family": "x"}',      # unknown handle
    '{"new": "R1", "decision": "counted", "target": "", "family": "x"}',      # R is not a candidate
    '{"new": "N1", "decision": "maybe", "target": "", "family": "x"}',        # unknown decision
    '{"new": "N1", "decision": "repeat", "target": "", "family": "x"}',       # repeat, no target
    '{"new": "N1", "decision": "repeat", "target": "R9", "family": "x"}',     # unknown target handle
    '{"new": "N1", "decision": "repeat", "target": "N1", "family": "x"}',     # target is itself
    '{"new": "N2", "decision": "same_event", "target": "N2", "family": "x"}', # same, same_event
    '"just a string"',
])
def test_parse_drops_invalid_entries(item):
    # Fails if any malformed entry is kept (it would then be voted on and stored).
    assert parse_decide_response(f"[{item}]", _two_cand_prompt()) == {}


@pytest.mark.unit
def test_parse_first_duplicate_wins_and_valid_neighbours_survive():
    # Fails if a later duplicate overwrites the first answer, or one bad entry drops the rest.
    text = ('[{"new": "N1", "decision": "counted", "target": "", "family": "a"},'
            ' {"new": "N1", "decision": "repeat", "target": "R1", "family": "b"},'
            ' {"new": "N9", "decision": "counted"},'
            ' {"new": "N2", "decision": "counted", "target": "", "family": "c"}]')
    assert parse_decide_response(text, _two_cand_prompt()) == {
        "c1": ("counted", "", "a"), "c2": ("counted", "", "c")}


@pytest.mark.unit
@pytest.mark.parametrize("text", ["", "no array here", "[not json]", '{"new": "N1"}', "]["])
def test_parse_unparseable_text_abstains(text):
    assert parse_decide_response(text, _two_cand_prompt()) == {}


# ------------------------------------------------------------------ 3. vote

@pytest.mark.unit
def test_vote_two_of_three_wins_with_tally():
    # Fails if the threshold is not 2 of 3 or the tally string is wrong.
    s = [{"c": ("repeat", "X", "kit")}, {"c": ("counted", "", "kit")}, {"c": ("repeat", "X", "kit")}]
    assert vote(s, ["c"], k=3) == {"c": ("repeat", "X", "kit", "2/3")}


@pytest.mark.unit
def test_vote_same_decision_different_target_does_not_agree():
    # Fails if agreement ignores the target.
    s = [{"c": ("repeat", "A", "")}, {"c": ("repeat", "B", "")}, {"c": ("repeat", "C", "")}]
    assert vote(s, ["c"], k=3) == {}
    s = [{"c": ("repeat", "A", "")}, {"c": ("repeat", "B", "")}, {"c": ("repeat", "A", "")}]
    assert vote(s, ["c"], k=3)["c"][:2] == ("repeat", "A")


@pytest.mark.unit
def test_vote_three_way_split_has_no_entry():
    s = [{"c": ("counted", "", "")}, {"c": ("repeat", "X", "")}, {"c": ("same_event", "X", "")}]
    assert vote(s, ["c"], k=3) == {}


@pytest.mark.unit
def test_vote_abstaining_samples_count_against():
    # Fails if the threshold is taken over parsed samples only (1 of 1 would wrongly win).
    s = [{"c": ("counted", "", "")}, {}, {}]
    assert vote(s, ["c"], k=3) == {}
    s = [{"c": ("counted", "", "")}, {"c": ("counted", "", "")}, {}]
    assert vote(s, ["c"], k=3)["c"][3] == "2/3"


@pytest.mark.unit
def test_vote_family_is_modal_among_winning_samples_only():
    # Fails if the family is taken from a losing sample or is not the mode.
    s = [{"c": ("repeat", "X", "kit")}, {"c": ("repeat", "X", "model kit")},
         {"c": ("repeat", "X", "model kit")}]
    assert vote(s, ["c"], k=3)["c"][2] == "model kit"
    s = [{"c": ("counted", "", "")}, {"c": ("counted", "", " ")}, {"c": ("repeat", "X", "zzz")}]
    assert vote(s, ["c"], k=3)["c"][2] == ""  # the loser's family is never used
    s = [{"c": ("counted", "", "x")}, {"c": ("counted", "", "y")}, {"c": ("repeat", "X", "z")}]
    assert vote(s, ["c"], k=3)["c"][2] == "x"  # tie -> first appearance


# ------------------------------------------------------------------ 4. apply_decide_rules

@pytest.mark.unit
def test_retelling_in_a_later_page_is_a_repeat_of_the_pool_row():
    # Fails if a later retelling is stored counted (double count) or loses its target id.
    out = _rules([_occ("c1", 5)], [_counted("p1", 1)], {"c1": _v("repeat", "p1")})
    d, = out.decisions
    assert (d["status"], d["target_id"], d["reason"], d["vote"]) == (
        "repeat", "p1", "decided_repeat", "3/3")
    assert out.swaps == [] and out.skipped == []


def _truncated_setup():
    # ten pool rows share two tokens with the candidate; the real match shares one -> rank 11
    pool = [_counted(f"p{i:02d}", 1) for i in range(10)]
    pool.append(_counted("p99-true", 1, count_object="kit", attribute="kit_count"))
    sets = related_sets([_occ("c1", 5)], pool)
    rs, = sets
    assert rs.truncated and "p99-true" not in {r["assertion_id"] for r in rs.related}
    return sets


@pytest.mark.unit
def test_truncated_candidate_voted_counted_is_uncertain():
    # Fails if a candidate whose related set was cut is allowed to become counted.
    out = apply_decide_rules(_truncated_setup(), {"c1": _v("counted")}, decided=True,
                             current_version=CUR)
    d, = out.decisions
    assert d["status"] == "uncertain" and d["reason"] == "truncated" and d["truncated"] is True


@pytest.mark.unit
def test_truncated_candidate_voted_repeat_of_a_sent_row_is_a_repeat():
    # Fails if truncation also blocks a repeat of a row that was actually sent.
    out = apply_decide_rules(_truncated_setup(), {"c1": _v("repeat", "p03")}, decided=True,
                             current_version=CUR)
    d, = out.decisions
    assert d["status"] == "repeat" and d["target_id"] == "p03" and d["truncated"] is True


@pytest.mark.unit
def test_no_agreement_is_uncertain():
    out = _rules([_occ("c1", 5)], [_counted("p1", 1)], {})
    d, = out.decisions
    assert d["status"] == "uncertain" and d["reason"] == "no_agreement"


@pytest.mark.unit
def test_target_outside_the_related_set_or_other_subject_is_uncertain():
    # Fails if a hallucinated or cross-subject target is stored.
    out = _rules([_occ("c1", 5)], [_counted("p1", 1)], {"c1": _v("repeat", "ghost")})
    assert out.decisions[0]["status"] == "uncertain"
    assert out.decisions[0]["reason"] == "target_not_related"
    other = _counted("p-other", 1, subject="s2")
    rs = RelatedSet(candidate=_occ("c1", 5), related=(other,), truncated=False)
    out = apply_decide_rules([rs], {"c1": _v("repeat", "p-other")}, decided=True,
                             current_version=CUR)
    assert out.decisions[0]["status"] == "uncertain"
    assert out.decisions[0]["reason"] == "target_not_related"


@pytest.mark.unit
def test_repeat_of_an_earlier_page_candidate_resolves_to_the_root():
    # Fails if the chain is stored c2 -> c1 (c1 is itself a repeat; c2 would then be orphaned).
    out = _rules([_occ("c1", 3), _occ("c2", 5)], [_counted("p1", 1)],
                 {"c1": _v("repeat", "p1"), "c2": _v("repeat", "c1")})
    got = _by_id(out)
    assert (got["c1"]["status"], got["c1"]["target_id"]) == ("repeat", "p1")
    assert (got["c2"]["status"], got["c2"]["target_id"]) == ("repeat", "p1")


@pytest.mark.unit
def test_repeat_of_an_uncertain_page_candidate_is_uncertain():
    # Fails if a repeat is stored against a target that is itself undecided.
    out = _rules([_occ("c1", 3), _occ("c2", 5)], [_counted("p1", 1)], {"c2": _v("repeat", "c1")})
    got = _by_id(out)
    assert got["c1"]["status"] == "uncertain" and got["c1"]["reason"] == "no_agreement"
    assert got["c2"]["status"] == "uncertain" and got["c2"]["reason"] == "target_uncertain"


@pytest.mark.unit
@pytest.mark.parametrize("status", ["repeat", "same_event"])
def test_earlier_dated_retelling_is_one_swap_not_a_plain_decision(status):
    # Fails if an earlier-dated retelling is stored as a plain repeat of a later row (the earlier
    # one should be counted and the later the repeat), or the swap loses its status.
    out = _rules([_occ("c1", 3)], [_counted("p1", 10)], {"c1": _v(status, "p1", tally="2/3")})
    assert out.decisions == []
    swap, = out.swaps
    assert (swap["candidate_id"], swap["target_id"], swap["status"]) == ("c1", "p1", status)
    assert swap["vote"] == "2/3"


@pytest.mark.unit
def test_earlier_dated_target_from_another_perceiver_version_is_a_plain_repeat():
    # Fails if a swap is attempted against a row of another version (it is not a current row).
    pool = [_counted("p1", 10, perceiver_version="cv0+m")]
    out = _rules([_occ("c1", 3)], pool, {"c1": _v("repeat", "p1")})
    assert out.swaps == []
    d, = out.decisions
    assert d["status"] == "repeat" and d["target_id"] == "p1"


@pytest.mark.unit
def test_two_earlier_candidates_never_make_two_swaps_on_one_target():
    # Fails if both are swapped onto p1 (two counted rows would then claim the same happening).
    out = _rules([_occ("c1", 3), _occ("c2", 4)], [_counted("p1", 10)],
                 {"c1": _v("repeat", "p1"), "c2": _v("repeat", "p1")})
    swap, = out.swaps
    assert swap["candidate_id"] == "c1" and swap["target_id"] == "p1"
    d, = out.decisions
    assert d["assertion_id"] == "c2" and d["status"] == "repeat" and d["target_id"] == "c1"


@pytest.mark.unit
def test_vote_family_used_only_when_it_is_a_known_family():
    # Fails if an arbitrary model-chosen family is stored, or a related row's family is refused.
    pool = [_counted("p1", 1, count_family="scale model", count_object="scale model")]
    cand = _occ("c1", 5)  # object "model kit"; shares the token "model" with the pool row
    out = _rules([cand], pool, {"c1": _v("counted", family="Scale Models")})
    assert out.decisions[0]["family"] == "scale model"
    out = _rules([cand], pool, {"c1": _v("counted", family="model kits")})
    assert out.decisions[0]["family"] == "model kit"  # the candidate's own family
    out = _rules([cand], pool, {"c1": _v("counted", family="spaceship")})
    assert out.decisions[0]["family"] == "model kit"
    out = _rules([cand], pool, {"c1": _v("counted", family="")})
    assert out.decisions[0]["family"] == "model kit"


@pytest.mark.unit
def test_decide_not_run_skips_related_candidates_but_counts_unrelated_ones():
    # Fails if a budget-skipped candidate is stored (it must stay undecided) or an unrelated one
    # is held back with it.
    out = _rules([_occ("c-rel", 5), _occ("c-free", 6, count_object="guitar", attribute="guitar_count")],
                 [_counted("p1", 1)], {}, decided=False)
    assert out.skipped == ["c-rel"]
    d, = out.decisions
    assert d["assertion_id"] == "c-free" and d["status"] == "counted" and d["reason"] == "no_related"


# ------------------------------------------------------------------ 5. runner

@dataclass
class _Ep:
    uuid: str
    content: str


def _build_episodes(rows):
    return [_Ep(uuid=str(r["uuid"]), content=str(r["content"])) for r in rows]


def _row(i):
    return {"uuid": f"t{i}", "valid_at": f"2026-07-0{i}T00:00:00Z",
            "cursor_at": f"2026-07-0{i}T00:00:00Z", "content": f"I finished kit {i}"}


class _Svc:
    def __init__(self, graph):
        self._graph = graph

    def rebuild_scalar_state(self, subject, *, namespace=None, **kw):
        self._graph.calls.append(("rebuild", (subject,), {"namespace": namespace}))

    def rebuild_scalar_projections(self, subject, *, namespace=None, **kw):
        self._graph.calls.append(("rebuild", (subject,), {"namespace": namespace}))


class _Graph:
    def __init__(self, pages=None, *, occ_by_id=None, pool=None, undecided=None, redecide=None,
                 cosine_hits=None, record_result=None):
        self.pages = list(pages or [])
        self.occ_by_id = occ_by_id or {}
        self.pool = pool or []
        self.undecided = undecided or {}
        self.redecide = list(redecide or [])
        self.cosine_hits = list(cosine_hits or [])
        self.record_result = record_result or {"fence_ok": True}
        self.calls: list[tuple[str, tuple, dict]] = []

    def _log(self, name, *a, **kw):
        self.calls.append((name, a, kw))

    def of(self, name):
        return [c for c in self.calls if c[0] == name]

    def scalar_state_service(self, **kw):
        return _Svc(self)

    def list_counting_dirty_namespaces(self, **kw):
        return []

    def load_next_counting_batch(self, namespace, **kw):
        return self.pages.pop(0) if self.pages else []

    def advance_counting_cursor(self, namespace, **kw):
        self._log("advance_counting_cursor", namespace, **kw)

    def claim_counting_namespace(self, namespace, **kw):
        self._log("claim_counting_namespace", namespace, **kw)
        return {"incarnation": "inc-1", "epoch": 7}

    def renew_counting_claim(self, namespace, **kw):
        return True

    def release_counting_claim(self, namespace, **kw):
        pass

    def prior_version_occurrences(self, namespace, turn_ids, **kw):
        return []

    def mark_projection_complete(self, assertion_ids):
        return len(assertion_ids)

    def retire_prior_version_occurrences(self, namespace, turn_ids, **kw):
        return []

    def mark_counting_flag_state(self, namespace, **kw):
        pass

    def scalar_claims_for_turns(self, namespace, turn_ids, **kw):
        return []

    def count_occurrences_by_ids(self, namespace, ids, **kw):
        return [self.occ_by_id[i] for i in ids if i in self.occ_by_id]

    def counted_occurrences_for_subjects(self, namespace, subjects, **kw):
        return list(self.pool)

    def related_counted_occurrences(self, namespace, probes, **kw):
        return [{**r, "probe_ids": [p["id"] for p in probes]} for r in self.pool]

    def undecided_count_occurrences(self, namespace, **kw):
        return list(self.undecided.get(namespace, []))

    def redecide_count_occurrences(self, namespace, **kw):
        self._log("redecide_count_occurrences", namespace, **kw)
        return list(self.redecide)

    def cosine_related_counted(self, namespace, probes, **kw):
        self._log("cosine_related_counted", namespace, list(probes), **kw)
        return list(self.cosine_hits)

    def record_count_decisions(self, namespace, decisions, **kw):
        self._log("record_count_decisions", namespace, list(decisions), **kw)
        return dict(self.record_result)


class _Llm:
    def __init__(self, response="[]"):
        self.calls = 0
        self.response = response
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, system, user):
        self.calls += 1
        self.prompts.append((system, user))
        return self.response


class _Perception:
    results: list[dict] = []

    def __init__(self, graph, state_service, **kwargs):
        pass

    def ensure_activated(self):
        pass

    def perceive_and_persist(self, episodes, llm, **kwargs):
        results = list(_Perception.results)
        return {"bound": sum(1 for r in results if r.get("bound")), "advisory": 0,
                "results": results}


@pytest.fixture
def perception(monkeypatch):
    import menhir.services.typed_scalar_perception as tsp

    _Perception.results = []
    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _Perception)
    return _Perception


def _bound(*ids):
    return [{"bound": True, "assertion_id": i} for i in ids]


def _run(graph, llm, *, call_budget=None, embed=None, embed_version=None):
    cfg = dict(perceiver_version="cv1", scalar_perceiver_version="sv1", batch_size=500, match=True,
               extract_v2=False)
    if embed_version is not None:
        cfg["embed_version"] = embed_version
    return run_counting_consolidation(
        graph, namespaces=["ns-1"], counting_llm=llm, build_episodes=_build_episodes,
        k=1, threshold=1.0, call_budget=call_budget, embed=embed,
        config=CountingConsolidationConfig(**cfg))


def _recorded(graph):
    out = {}
    for c in graph.of("record_count_decisions"):
        for d in c[1][1]:
            out[d["assertion_id"]] = d
    return out


_REPEAT_R1 = '[{"new": "N1", "decision": "repeat", "target": "R1", "family": "model kit"}]'


@pytest.mark.unit
def test_no_related_counts_means_no_decide_call(perception):
    # Fails if the LLM is called for candidates that have nothing to compare against.
    perception.results = _bound("a1", "a2")
    graph = _Graph(pages=[[_row(1), _row(2)]], occ_by_id={
        "a1": _occ("a1", 1), "a2": _occ("a2", 2, count_object="guitar", attribute="guitar_count")})
    llm = _Llm()
    out = _run(graph, llm)
    assert llm.calls == 0
    assert {a: d["status"] for a, d in _recorded(graph).items()} == {"a1": "counted", "a2": "counted"}
    assert all(d["redecide"] is False for d in _recorded(graph).values())
    assert out["counting_decided_counted"] == 2 and out["counting_decided_deferred"] == 0


def _three_row_graph():
    return _Graph(pages=[[_row(1), _row(2), _row(3)]], occ_by_id={
        "a1": _occ("a1", 1), "a2": _occ("a2", 2),
        "a3": _occ("a3", 3, count_object="guitar", attribute="guitar_count")})


@pytest.mark.unit
def test_budget_that_cannot_cover_a_decide_round_defers_related_candidates(perception):
    # Fails if the decide call runs past the budget, a deferred candidate is stored (counted or
    # otherwise), or an unrelated candidate is held back with it.
    perception.results = _bound("a1", "a2", "a3")
    graph = _three_row_graph()
    llm = _Llm(_REPEAT_R1)
    out = _run(graph, llm, call_budget=DECIDE_K - 1)
    assert llm.calls == 0
    assert set(_recorded(graph)) == {"a1", "a3"}
    assert all(d["status"] == "counted" for d in _recorded(graph).values())
    assert out["counting_decided_deferred"] == 1 and out["counting_decided_counted"] == 2


@pytest.mark.unit
def test_budget_that_exactly_covers_a_decide_round_decides(perception):
    # Fails if the budget check is off by one (a round that fits exactly must run).
    perception.results = _bound("a1", "a2", "a3")
    graph = _three_row_graph()
    llm = _Llm(_REPEAT_R1)
    out = _run(graph, llm, call_budget=DECIDE_K)
    assert llm.calls == DECIDE_K
    assert _recorded(graph)["a2"]["status"] == "repeat"
    assert out["counting_decided_deferred"] == 0


@pytest.mark.unit
def test_redecide_pass_marks_decisions_and_excludes_this_run(perception):
    # Fails if re-decided rows are written without redecide=True, or the pass does not exclude the
    # rows this run already decided, or ignores the attempt cap / limit.
    redo = [_occ("r1", 5, count_status="uncertain"),
            _occ("r2", 6, count_object="guitar", attribute="guitar_count", count_status="uncertain")]
    graph = _Graph(pages=[], pool=[_counted("p1", 1)], redecide=redo)
    llm = _Llm(_REPEAT_R1)
    _run(graph, llm)
    run_id = graph.of("claim_counting_namespace")[0][2]["run_id"]
    call, = graph.of("redecide_count_occurrences")
    assert call[2]["exclude_run_id"] == run_id
    assert call[2]["max_attempts"] == MAX_DECIDE_ATTEMPTS
    assert call[2]["limit"] == REDECIDE_LIMIT and call[2]["perceiver_version"] == CUR
    got = _recorded(graph)
    assert {a: d["status"] for a, d in got.items()} == {"r1": "repeat", "r2": "counted"}
    assert got["r1"]["target_id"] == "p1"
    assert all(d["redecide"] is True for d in got.values())
    assert llm.calls == DECIDE_K


@pytest.mark.unit
def test_repeat_whose_target_is_gone_is_counted_again_on_redecide(perception):
    # Fails if a repeat of a deleted target stays a repeat/uncertain: the happening would vanish.
    orphan = _occ("r1", 5, count_status="repeat", count_target_id="deleted-target")
    graph = _Graph(pages=[], pool=[], redecide=[orphan])
    llm = _Llm()
    out = _run(graph, llm)
    d = _recorded(graph)["r1"]
    assert d["status"] == "counted" and d["reason"] == "no_related" and d["redecide"] is True
    assert llm.calls == 0 and out["counting_decided_counted"] == 1
    assert [c[1] for c in graph.of("rebuild")] == [("s1",)]


@pytest.mark.unit
@pytest.mark.parametrize("swapped", [0, 1])
def test_swaps_are_passed_only_when_present_and_tallied_from_the_record(perception, swapped):
    # Fails if swaps= is passed when empty, a swap is split into plain decisions, or the swapped
    # tally comes from the number of requested swaps instead of what the graph reports written.
    perception.results = _bound("a1")
    graph = _Graph(pages=[[_row(1)]], occ_by_id={"a1": _occ("a1", 3)},
                   pool=[_counted("p1", 10)], record_result={"fence_ok": True, "swapped": swapped})
    out = _run(graph, _Llm(_REPEAT_R1))
    rec, = graph.of("record_count_decisions")
    assert rec[1][1] == []  # no plain decision for the swapped candidate
    swap, = rec[2]["swaps"]
    assert (swap["candidate_id"], swap["target_id"], swap["status"]) == ("a1", "p1", "repeat")
    assert swap["redecide"] is False
    assert out["counting_decided_swapped"] == swapped


@pytest.mark.unit
def test_no_swaps_kwarg_when_there_are_none(perception):
    perception.results = _bound("a1", "a2")
    graph = _Graph(pages=[[_row(1), _row(2)]],
                   occ_by_id={"a1": _occ("a1", 1), "a2": _occ("a2", 2)})
    out = _run(graph, _Llm(_REPEAT_R1))
    rec, = graph.of("record_count_decisions")
    assert "swaps" not in rec[2]
    assert out["counting_decided_swapped"] == 0 and out["counting_decided_repeat"] == 1


def _vec(text):
    return [float(len(text)), float(sum(map(ord, text)) % 101), 1.0]


def _hornet_graph(**kw):
    return _Graph(pages=[[_row(1), _row(2)]], occ_by_id={
        "a1": _occ("a1", 1, count_action="finished", count_item="Hornet"),
        "a2": _occ("a2", 2, count_action="finished", count_item="Tiger")}, **kw)


@pytest.mark.unit
def test_key_embeddings_are_stored_and_probed_with_embed_version(perception):
    # Fails if the embedded text is not "object action item", the vector or version is not carried
    # on the decision, or the cosine probe is skipped / wrongly parameterised.
    perception.results = _bound("a1", "a2")
    graph = _hornet_graph()
    seen: list[str] = []

    def embed(text):
        seen.append(text)
        return _vec(text)

    _run(graph, _Llm(_REPEAT_R1), embed=embed, embed_version="e1")
    want = {"a1": "model kit finished Hornet", "a2": "model kit finished Tiger"}
    assert key_text(_occ("a1", 1, count_action="finished", count_item="Hornet")) == want["a1"]
    assert sorted(seen) == sorted(want.values())
    got = _recorded(graph)
    for aid, text in want.items():
        assert got[aid]["key_embedding"] == _vec(text) and got[aid]["embed_version"] == "e1"
    call, = graph.of("cosine_related_counted")
    assert call[1][0] == "ns-1"
    assert {p["id"]: p["vector"] for p in call[1][1]} == {a: _vec(t) for a, t in want.items()}
    assert call[2]["embed_version"] == "e1" and call[2]["min_cosine"] == COSINE_MIN


@pytest.mark.unit
def test_without_embed_version_there_is_no_embedding_and_no_cosine_call(perception):
    # Fails if embeddings are computed or stored while the embed version is unset.
    perception.results = _bound("a1", "a2")
    graph = _hornet_graph()
    seen: list[str] = []
    _run(graph, _Llm(_REPEAT_R1), embed=lambda t: seen.append(t) or _vec(t), embed_version=None)
    assert seen == [] and graph.of("cosine_related_counted") == []
    assert all(d["key_embedding"] is None and d["embed_version"] is None
               for d in _recorded(graph).values())


def _hit(cosine):
    return {"assertion_id": "h1", "probe_id": "a1", "cosine": cosine, "subject_uuid": "s1",
            "value_kind": "count", "attribute": "airplane_count", "count_object": "airplane replica",
            "count_status": "counted", "perceiver_version": CUR,
            "valid_at": "2026-06-01T00:00:00+00:00", "episode_uuid": "ep-h1"}


@pytest.mark.unit
def test_cosine_hit_with_no_shared_token_reaches_the_decide_call(perception):
    # Fails if cosine hits from the graph are not merged into the related pool.
    perception.results = _bound("a1")
    graph = _Graph(pages=[[_row(1)]], cosine_hits=[_hit(0.9)], occ_by_id={
        "a1": _occ("a1", 1, count_object="plane", attribute="plane_count")})
    llm = _Llm(_REPEAT_R1)
    _run(graph, llm, embed=_vec, embed_version="e1")
    assert llm.calls == DECIDE_K
    assert "airplane replica" in llm.prompts[0][1] and "h1" not in llm.prompts[0][1]
    d = _recorded(graph)["a1"]
    assert d["status"] == "repeat" and d["target_id"] == "h1"


@pytest.mark.unit
def test_cosine_hit_below_the_threshold_is_not_related(perception):
    # Fails if any returned hit is treated as related regardless of its cosine.
    perception.results = _bound("a1")
    graph = _Graph(pages=[[_row(1)]], cosine_hits=[_hit(COSINE_MIN - 0.2)], occ_by_id={
        "a1": _occ("a1", 1, count_object="plane", attribute="plane_count")})
    llm = _Llm(_REPEAT_R1)
    _run(graph, llm, embed=_vec, embed_version="e1")
    assert llm.calls == 0 and _recorded(graph)["a1"]["status"] == "counted"


@pytest.mark.unit
def test_embed_that_raises_does_not_fail_the_namespace(perception):
    # Fails if an embedding error aborts the namespace instead of dropping the cosine signal.
    perception.results = _bound("a1", "a2")
    graph = _hornet_graph()

    def boom(text):
        raise RuntimeError("embedder down")

    out = _run(graph, _Llm(_REPEAT_R1), embed=boom, embed_version="e1")
    assert out["counting_namespaces_failed"] == 0 and out["counting_namespaces_processed"] == 1
    got = _recorded(graph)
    assert got["a2"]["status"] == "repeat" and got["a2"]["key_embedding"] is None


# ------------------------------------------------------------------ 6. decisions + fold

def _fold_row(i, day, **over):
    base = dict(
        assertion_id=f"o{i}", subject_uuid="ent-self", subject_display="me",
        attribute="model_kit_count", scope="", value_kind="count", unit="", operation="occurrence",
        value=1, importance="notable", valid_at=f"2026-07-{day:02d}T00:00:00+00:00",
        learned_at=f"2026-07-{day:02d}T00:00:00+00:00", evidence_tier="user",
        episode_uuid=f"ep-{i}", binding_pending=False, perceiver_version=CUR,
        count_object="model kit", count_item="", count_action="", count_status="",
        count_target_id="", count_family="",
    )
    base.update(over)
    return base


@pytest.mark.unit
def test_reversioned_retelling_decided_repeat_folds_to_total_one():
    # Fails if a decided repeat still adds to the total after the fold (total would be 2), or if
    # the decision stores no usable target (the fold would then report it uncertain, not absorb it).
    a = _fold_row(1, 1, count_status="counted")
    b = _fold_row(2, 5)
    out = apply_decide_rules(related_sets([b], [a]), {"o2": _v("repeat", "o1")}, decided=True,
                             current_version=CUR)
    d, = out.decisions
    b_decided = dict(b, count_status=d["status"], count_target_id=d["target_id"],
                     count_family=d["family"])
    result = fold_assertions([a, b_decided], count_match=True)
    assert not result.abstentions
    state, = result.states
    assert state.value == 1 and state.count_uncertain == 0
    assert "o2" in state.absorbed_occurrence_ids

    # control: had the decision been "counted", the same fold must give 2
    out2 = apply_decide_rules(related_sets([b], [a]), {"o2": _v("counted")}, decided=True,
                              current_version=CUR)
    b_counted = dict(b, count_status=out2.decisions[0]["status"])
    state2, = fold_assertions([a, b_counted], count_match=True).states
    assert state2.value == 2
