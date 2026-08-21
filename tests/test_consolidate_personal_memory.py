"""Orchestration tests for the personal-memory consolidation task (perception prod wiring).

These cover the scheduling wrapper — dirty selection, budget cap, watermarking, guard pinning — not
the perception gate itself (tested in test_perception*). The fake LLM returns an empty extraction so
the gate commits nothing; each extract_once still costs one llm call, which drives the budget test.
"""

from __future__ import annotations

import asyncio

import pytest

from menhir.services import scheduler_tasks


class _FakeGraph:
    """Implements the surface consolidate_personal_memory touches: dirty list, episode load,
    watermark, and record_counter (perceive_and_fold's fold sink — never reached here since the
    fake LLM extracts nothing)."""

    def __init__(self, dirty, episodes_by_ns):
        self._dirty = list(dirty)
        self._episodes = episodes_by_ns
        self.marked: list[str] = []
        self.loaded: list[str] = []

    def list_dirty_namespaces(self, *, limit=200):
        return self._dirty[:limit]

    def load_user_episodes(self, namespace, *, limit=500):
        self.loaded.append(namespace)
        return self._episodes.get(namespace, [])

    def mark_consolidated(self, namespace, *, at):
        self.marked.append(namespace)

    def record_counter(self, **kwargs):
        return {"uuid": "u", "view_key": "k", "created": True}


def _eps(*bodies):
    return [{"uuid": f"e{i}", "valid_at": "2026-07-01T00:00:00Z", "content": f"user: {b}"}
            for i, b in enumerate(bodies)]


def _empty_llm(system: str, user: str) -> str:
    return "[]"  # extractor sees no measures -> gate commits nothing


@pytest.mark.unit
def test_consolidate_processes_dirty_namespaces_and_watermarks_each() -> None:
    graph = _FakeGraph(
        dirty=["lme-a", "lme-b"],
        episodes_by_ns={"lme-a": _eps("I spent forty dollars on parts"),
                        "lme-b": _eps("bought another tank today")},
    )
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=2))

    assert out["namespaces_dirty"] == 2
    assert out["namespaces_processed"] == 2
    assert graph.loaded == ["lme-a", "lme-b"]
    assert graph.marked == ["lme-a", "lme-b"]      # both watermarked (abstain still debounces)
    assert out["llm_calls"] > 0


@pytest.mark.unit
def test_budget_cap_stops_between_namespaces_and_leaves_rest_dirty() -> None:
    graph = _FakeGraph(
        dirty=["lme-a", "lme-b", "lme-c"],
        episodes_by_ns={ns: _eps("spent money on things") for ns in ("lme-a", "lme-b", "lme-c")},
    )
    # k=2 -> ~2 llm calls per namespace; a budget of 1 permits only the first namespace.
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=2, call_budget=1))

    assert out["namespaces_processed"] == 1
    assert graph.marked == ["lme-a"]               # only the processed one is watermarked
    assert "lme-b" not in graph.marked             # the rest stay dirty for the next run


@pytest.mark.unit
def test_empty_namespace_is_watermarked_without_calling_llm() -> None:
    graph = _FakeGraph(dirty=["lme-empty"], episodes_by_ns={"lme-empty": []})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=3))

    assert out["namespaces_processed"] == 1
    assert out["llm_calls"] == 0                    # no episodes -> no perception
    assert graph.marked == ["lme-empty"]           # still debounced


@pytest.mark.unit
def test_explicit_namespaces_override_dirty_query() -> None:
    graph = _FakeGraph(dirty=["should-not-be-used"], episodes_by_ns={"forced": _eps("hi there friend")})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, namespaces=["forced"], k=1))

    assert graph.loaded == ["forced"]
    assert out["namespaces_dirty"] == 1


@pytest.mark.unit
def test_scheduler_registers_consolidation_job_only_when_enabled_and_llm_present() -> None:
    from menhir.services.maintenance_scheduler import MaintenanceScheduler

    class _Ingest:
        def get_queue_depth(self): return 0

    common = dict(ingest_service=_Ingest(), graph_adapter=object())
    off = MaintenanceScheduler(**common, personal_memory_enabled=False, personal_memory_llm=_empty_llm)
    assert "consolidate_personal_memory" not in off._jobs
    no_llm = MaintenanceScheduler(**common, personal_memory_enabled=True, personal_memory_llm=None)
    assert "consolidate_personal_memory" not in no_llm._jobs
    on = MaintenanceScheduler(**common, personal_memory_enabled=True, personal_memory_llm=_empty_llm)
    assert "consolidate_personal_memory" in on._jobs


_COUNTER_ONLY_KEYS = {
    "namespaces_dirty", "namespaces_processed", "views_written", "abstained",
    "corrections_applied", "llm_calls",
}


@pytest.mark.unit
def test_flag_off_result_is_byte_identical_and_never_touches_scalar() -> None:
    # flag OFF: the result shape is EXACTLY the counter-only keys (no scalar_* additive keys), and the
    # scalar surface is never touched — _FakeGraph has no scalar methods, so any access would AttributeError.
    graph = _FakeGraph(dirty=["lme-a"], episodes_by_ns={"lme-a": _eps("I spent forty dollars on parts")})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(graph, llm_complete=_empty_llm, k=2))
    assert set(out.keys()) == _COUNTER_ONLY_KEYS
    assert not hasattr(graph, "scalar_marked")   # the scalar watermark was never stamped


@pytest.mark.unit
def test_scalar_runner_receives_the_counter_phase_call_counter(monkeypatch) -> None:
    graph = _FakeGraph(
        dirty=[],
        episodes_by_ns={"lme-a": _eps("I spent forty dollars on parts")},
    )
    captured: dict[str, object] = {}

    def _capture_scalar_runner(*args, **kwargs):
        captured["counter"] = kwargs["counting_llm"]
        captured["calls"] = kwargs["counting_llm"].calls
        captured["budget"] = kwargs["call_budget"]
        return {"scalar_namespaces_dirty": len(kwargs["scalar_targets"])}

    monkeypatch.setattr(
        scheduler_tasks,
        "run_scalar_consolidation",
        _capture_scalar_runner,
    )

    out = asyncio.run(
        scheduler_tasks.consolidate_personal_memory(
            graph,
            llm_complete=_empty_llm,
            namespaces=["lme-a"],
            k=1,
            call_budget=5,
            enable_scalar_state=True,
        )
    )

    assert captured["calls"] == out["llm_calls"]
    assert int(captured["calls"]) > 0
    assert captured["budget"] == 5
    assert out["scalar_namespaces_dirty"] == 1


class _ScalarGraph(_FakeGraph):
    """Counter surface + the scalar-shadow surface the C.4.3 coordinator drives. Counter is inert
    here (no counter-dirty namespaces) so the scalar pass is isolated. Models the resumable scalar
    CURSOR exactly as the Cypher does: pages AFTER the advanced cursor on the MONOTONIC work-discovery
    key `cursor_at = coalesce(created_at, valid_at)` (NOT world-time), so a late-finalized episode
    with an earlier valid_at is still discovered. Each episode dict may carry `created_at` and/or
    `valid_at`; a perceiver_version change resets the cursor."""

    def __init__(self, *, scalar_dirty, episodes_by_ns, entities_by_ep,
                 schema_ready=True, activate_raises=None, pending=None, rebuild_fail_first=False,
                 orphans=None):
        super().__init__(dirty=[], episodes_by_ns=episodes_by_ns)
        self._scalar_dirty = list(scalar_dirty)
        self._scalar_episodes = episodes_by_ns
        self._entities = entities_by_ep
        self._schema_ready = schema_ready
        self._activate_raises = activate_raises
        self._pending = list(pending or [])
        self._rebuild_fail_first = rebuild_fail_first
        self._rebuild_failed: set = set()        # subjects that have already taken their one failure
        self._orphans = list(orphans or [])
        self._recon_repaired = 0
        self.orphan_calls: list = []
        self.recon_calls: list = []
        self.counter_marked: list[str] = []
        self.recorded: list = []
        self.self_uuids: set[str] = set()
        self.rebuilt: list[str] = []             # (subject) of SUCCESSFUL rebuilds
        self.rebuilt_ns: list = []               # parallel namespaces of successful rebuilds
        self.cursor: dict[str, tuple] = {}       # ns -> (cursor_at, uuid, perceiver_version)
        self.advances: list[tuple] = []          # (ns, cursor_at, uuid, perceiver_version)

    @staticmethod
    def _ckey(e):
        # coalesce(created_at, valid_at) — the ingestion-order work-discovery key
        return e.get("created_at") or e.get("valid_at") or ""

    # counter watermark (proves it is NOT stamped by the scalar pass)
    def mark_consolidated(self, namespace, *, at):
        self.counter_marked.append(namespace)

    # scalar cursor surface
    def list_scalar_dirty_namespaces(self, *, perceiver_version, limit=200):
        return self._scalar_dirty[:limit]

    def load_next_scalar_batch(self, namespace, *, perceiver_version, limit=500):
        eps = self._scalar_episodes.get(namespace, [])
        cur = self.cursor.get(namespace)
        if cur is not None and cur[2] != perceiver_version:
            cur = None                           # version bump resets the cursor
        after = (cur[0], cur[1]) if cur else None
        # sort by (cursor_at, uuid) with a timeless row (empty ckey) LAST, matching Neo4j NULL-last.
        ordered = sorted(eps, key=lambda e: (self._ckey(e) == "", self._ckey(e), e["uuid"]))
        rows = [{"uuid": e["uuid"], "valid_at": e.get("valid_at") or "",
                 "cursor_at": self._ckey(e), "content": e["content"]}
                for e in ordered
                if after is None or (self._ckey(e), e["uuid"]) > after]
        return rows[:limit]

    def advance_scalar_cursor(self, namespace, *, cursor_at, cursor_uuid, perceiver_version, at):
        self.cursor[namespace] = (cursor_at, cursor_uuid, perceiver_version)
        self.advances.append((namespace, cursor_at, cursor_uuid, perceiver_version))

    # coordinator adapter surface
    def ensure_self_entity(self, namespace):
        # CF-131: production ALWAYS wires the self seam, so a fake without this method makes the
        # seam decline and a first-person subject fall back to lexical name-match -- the bypass
        # CF-131 is about. Modelling it is what makes this fake match a real deployment.
        uuid = f"self-{namespace or 'default'}"
        self.self_uuids.add(uuid)
        return uuid

    def activate_scalar_state(self):
        if self._activate_raises is not None:
            raise self._activate_raises
        return {"queries_executed": 1}

    def scalar_state_schema_ready(self):
        return self._schema_ready

    def fetch_linked_entities_for_episode(self, episode_uuid):
        return list(self._entities.get(episode_uuid, []))

    def record_typed_assertion(self, assertion):
        self.recorded.append(assertion)
        real = (assertion.subject_uuid in self.self_uuids
                or any(assertion.subject_uuid == e["uuid"]
                       for ents in self._entities.values() for e in ents))
        if real:
            # adoption on the same node: clears binding_pending, sets projection_pending (View not yet
            # rebuilt), and adopts the resolved subject — mirrors the record cypher.
            for r in self._pending:
                if r.get("episode_uuid") == assertion.episode_uuid and r.get("binding_pending"):
                    r["binding_pending"] = False
                    r["projection_pending"] = True
                    r["subject_uuid"] = assertion.subject_uuid
                    break
        return {"assertion_id": f"a{len(self.recorded)}", "binding_pending": not real, "created": True}

    def pending_advisory_assertions(self, *, namespaces=None, limit=200):
        # model the repository fairness order + namespace allowlist + single global limit, over rows
        # that still need work (binding_pending OR projection_pending).
        rows = [r for r in self._pending
                if r.get("binding_pending") or r.get("projection_pending")]
        if namespaces is not None:
            rows = [r for r in rows if r.get("namespace") in namespaces]
        rows = sorted(rows, key=lambda r: (r.get("_attempted") is not None,
                                           r.get("_attempted") or "", r["assertion_id"]))
        return rows[:limit]

    def mark_binding_repair_attempted(self, assertion_ids, *, at):
        for r in self._pending:
            if r["assertion_id"] in assertion_ids:
                r["_attempted"] = at + f"#{r['assertion_id']}"   # unique, monotonic-ish per stamp
        return len(assertion_ids)

    def mark_projection_complete(self, assertion_ids):
        for r in self._pending:
            if r["assertion_id"] in assertion_ids:
                r["projection_pending"] = False
        return len(assertion_ids)

    def scalar_state_service(self, **_kwargs):
        outer = self

        class _Svc:
            def rebuild_scalar_state(self, subject_uuid, *, namespace=None, as_of=None):
                if outer._rebuild_fail_first and subject_uuid not in outer._rebuild_failed:
                    outer._rebuild_failed.add(subject_uuid)
                    raise RuntimeError("rebuild boom (first attempt)")
                outer.rebuilt.append(subject_uuid)
                outer.rebuilt_ns.append(namespace)
                return {"subject_uuid": subject_uuid, "written": 1}

            def repair_orphaned_assertions(self, *, committed_merges=None, allowed_namespaces=None,
                                           limit=200):
                outer.orphan_calls.append({"committed_merges": committed_merges,
                                           "allowed_namespaces": allowed_namespaces, "limit": limit})
                return {"orphans": len(outer._orphans), "repaired": list(outer._orphans),
                        "unresolved": [], "errored": [], "replayed_op_ids": []}

            def activate_due_assertions(self, *, namespaces=None, limit=200, as_of=None):
                return {"claimed": 0, "repaired": [], "failed": [], "examined": 0}

            def repair_incomplete_reconciliations(self, *, committed_merges=None,
                                                  committed_unmerges=None, allowed_namespaces=None):
                outer.recon_calls.append({"committed_merges": committed_merges,
                                          "committed_unmerges": committed_unmerges,
                                          "allowed_namespaces": allowed_namespaces})
                return {"repaired": outer._recon_repaired, "merge_op_ids": [],
                        "unmerge_op_ids": [], "errored_merge_op_ids": [],
                        "errored_unmerge_op_ids": [], "downstream_replayed_op_ids": []}

        return _Svc()


def _wake_llm(system: str, user: str) -> str:
    import json
    return json.dumps([dict(
        episode=0, subject="user", attribute="wake", scope="", value_kind="clock_time",
        unit="", operation="absolute", value="07:30", when="2026-07-01",
        stated_span="I wake at 07:30")])


def _wake_llm_no_when(system: str, user: str) -> str:
    import json  # no `when` -> the assertion's valid_at comes from the time-basis fallback chain
    return json.dumps([dict(
        episode=0, subject="user", attribute="wake", scope="", value_kind="clock_time",
        unit="", operation="absolute", value="07:30", stated_span="I wake at 07:30")])


def _wake_ep(uuid, *, created_at, valid_at):
    return {"uuid": uuid, "created_at": created_at, "valid_at": valid_at,
            "content": "user: I wake at 07:30"}


def _wake_eps(*uuids):
    # created_at ascending so the ingestion-order cursor is stable; valid_at parallel for simplicity
    return [_wake_ep(u, created_at=f"2026-07-0{i+1}T00:00:00Z", valid_at=f"2026-07-0{i+1}T00:00:00Z")
            for i, u in enumerate(uuids)]


@pytest.mark.unit
def test_flag_on_runs_independent_scalar_pass_and_advances_only_scalar_cursor() -> None:
    graph = _ScalarGraph(
        scalar_dirty=["lme-a"],
        episodes_by_ns={"lme-a": _wake_eps("e0")},
        entities_by_ep={"e0": [{"uuid": "ent-user", "name": "user"}]},
    )
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=3, enable_scalar_state=True))

    assert out["scalar_states_written"] == 1 and out["scalar_advisory"] == 0
    assert out["scalar_namespaces_processed"] == 1
    assert out["llm_calls"] == 3                         # includes scalar-only LLM work
    assert [a[0] for a in graph.advances] == ["lme-a"]   # scalar cursor advanced
    assert graph.counter_marked == []                    # counter watermark UNTOUCHED
    # CF-131: the episode says "I wake at 07:30", so the subject is FIRST PERSON and binds
    # through the canonical self seam, not to the per-episode "user" entity. That is the
    # whole point of the seam -- the per-episode node is a twin that _absorb_self_entity_forks
    # later DETACH DELETEs.
    assert graph.rebuilt == ["self-lme-a"]               # bound -> View rebuilt


@pytest.mark.unit
def test_flag_on_activation_refused_records_nothing_and_leaves_scalar_dirty() -> None:
    from menhir.infrastructure.typed_assertion_repository import ScalarStateActivationError
    graph = _ScalarGraph(
        scalar_dirty=["lme-a"],
        episodes_by_ns={"lme-a": _wake_eps("e0")},
        entities_by_ep={"e0": [{"uuid": "ent-user", "name": "user"}]},
        activate_raises=ScalarStateActivationError(1, 0),
    )
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=3, enable_scalar_state=True))

    assert out["scalar_states_written"] == 0
    assert graph.recorded == []                    # nothing persisted when activation refused
    assert graph.advances == []                    # cursor NOT advanced -> namespace stays scalar-dirty


@pytest.mark.unit
def test_scalar_backfill_paginates_and_does_not_strand_the_tail() -> None:
    # 3 episodes, batch size 1: the pass must walk ALL three pages and advance the cursor to the last,
    # never stamping "done" after the first page (the truncation bug).
    graph = _ScalarGraph(
        scalar_dirty=["lme-a"],
        episodes_by_ns={"lme-a": _wake_eps("e0", "e1", "e2")},
        entities_by_ep={u: [{"uuid": "ent-user", "name": "user"}] for u in ("e0", "e1", "e2")},
    )
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_batch_size=1))

    assert out["scalar_batches"] == 3                       # all three pages processed
    assert graph.cursor["lme-a"][1] == "e2"                # cursor advanced to the LAST episode
    # a follow-up run finds nothing new after the cursor -> no further advances
    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_batch_size=1))
    assert out2["scalar_batches"] == 0


@pytest.mark.unit
def test_scalar_budget_cap_stops_mid_backfill_and_leaves_cursor_mid_namespace() -> None:
    # budget of 1 llm call permits only the first page; the cursor advances to e0 and the tail stays
    # dirty for the next run rather than being skipped.
    graph = _ScalarGraph(
        scalar_dirty=["lme-a"],
        episodes_by_ns={"lme-a": _wake_eps("e0", "e1", "e2")},
        entities_by_ep={u: [{"uuid": "ent-user", "name": "user"}] for u in ("e0", "e1", "e2")},
    )
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_batch_size=1,
        call_budget=1))
    assert out["scalar_batches"] == 1
    assert graph.cursor["lme-a"][1] == "e0"                # stopped after the first page
    # resume: a subsequent unbudgeted run continues from the cursor through the remaining pages
    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_batch_size=1))
    assert out2["scalar_batches"] == 2                      # e1, e2
    assert graph.cursor["lme-a"][1] == "e2"


def _pending_row(**over):
    base = dict(
        assertion_id="a-sk-p", source_key="sk-p", subject_uuid="unbound:sk-p", subject_display="Alice",
        binding_pending=True, projection_pending=False, attribute="wake", scope="",
        value_kind="clock_time", unit="", operation="absolute", value="07:30",
        stated_span="wake at 07:30", episode_uuid="ep-p", span_start=0, span_end=13,
        claim_ordinal=0, valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", time_basis="explicit", perceiver_version="v1",
        namespace="ns")
    base.update(over)
    return base


@pytest.mark.unit
def test_scheduler_runs_pending_binding_repair_after_backfill() -> None:
    # the repair pass is wired into the scheduled job (not just callable): an advisory that has become
    # uniquely bindable is repaired + rebuilt, and the result reports it.
    graph = _ScalarGraph(
        scalar_dirty=[],                       # no backfill work; isolate the repair pass
        episodes_by_ns={},
        entities_by_ep={"ep-p": [{"uuid": "ent-user", "name": "Alice"}]},
        pending=[_pending_row(subject_display="Alice", episode_uuid="ep-p")],
    )
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True))
    assert out["scalar_repaired"] == 1 and out["scalar_repair_pending"] == 0
    assert graph.rebuilt == ["ent-user"]       # the repaired claim's View was rebuilt
    assert graph.recorded and graph.recorded[0].subject_uuid == "ent-user"


@pytest.mark.unit
def test_scheduler_repair_no_ops_when_advisory_still_unbindable() -> None:
    graph = _ScalarGraph(
        scalar_dirty=[], episodes_by_ns={}, entities_by_ep={"ep-p": []},   # not resolvable
        pending=[_pending_row(episode_uuid="ep-p")])
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True))
    assert out["scalar_repaired"] == 0 and out["scalar_repair_pending"] == 1
    assert graph.rebuilt == []


@pytest.mark.unit
def test_scheduler_repair_is_eventually_complete_past_an_unresolved_first_page() -> None:
    # fairness regression: limit=2, rows 1-2 unresolvable, row 3 uniquely bindable. run 1 scans 1-2
    # (stamps them); run 2 must REACH and repair row 3 (unattempted-first); later runs revisit 1-2.
    graph = _ScalarGraph(
        scalar_dirty=[], episodes_by_ns={},
        entities_by_ep={"ep-1": [], "ep-2": [], "ep-3": [{"uuid": "ent-user", "name": "Alice"}]},
        pending=[
            _pending_row(assertion_id="a1", source_key="s1", episode_uuid="ep-1"),
            _pending_row(assertion_id="a2", source_key="s2", episode_uuid="ep-2"),
            _pending_row(assertion_id="a3", source_key="s3", episode_uuid="ep-3", subject_display="Alice"),
        ],
    )
    out1 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_repair_limit=2))
    assert out1["scalar_repaired"] == 0                     # only rows 1-2 seen, neither bindable
    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_repair_limit=2))
    assert out2["scalar_repaired"] == 1                     # row 3 reached + repaired
    assert graph.rebuilt == ["ent-user"]


@pytest.mark.unit
def test_scheduler_repair_respects_explicit_namespace_override() -> None:
    # targeting: an explicit namespaces=["tenant-a"] override must never repair/rebuild tenant-b.
    graph = _ScalarGraph(
        scalar_dirty=[], episodes_by_ns={"tenant-a": []},
        entities_by_ep={"ep-a": [{"uuid": "ent-a", "name": "Alice"}],
                        "ep-b": [{"uuid": "ent-b", "name": "Alice"}]},
        pending=[
            _pending_row(assertion_id="a-a", source_key="s-a", episode_uuid="ep-a",
                         subject_display="Alice", namespace="tenant-a"),
            _pending_row(assertion_id="a-b", source_key="s-b", episode_uuid="ep-b",
                         subject_display="Alice", namespace="tenant-b"),
        ],
    )
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, namespaces=["tenant-a"]))
    assert out["scalar_repaired"] == 1
    assert graph.rebuilt == ["ent-a"]                      # only tenant-a rebuilt
    assert graph.rebuilt_ns == ["tenant-a"]                # rebuilt in the row's own namespace
    assert all(a.subject_uuid != "ent-b" for a in graph.recorded)   # tenant-b never recorded


@pytest.mark.unit
def test_scheduler_repair_multi_namespace_override_is_globally_bounded() -> None:
    # 3 allowlisted namespaces, limit 2: a SINGLE fair query scans at most 2 rows (never limit-per-ns),
    # and the third namespace's row is reached on a later run — no per-namespace multiplication, no
    # first-namespace starvation.
    graph = _ScalarGraph(
        scalar_dirty=[], episodes_by_ns={"a": [], "b": [], "c": []},
        entities_by_ep={"ep-a": [], "ep-b": [], "ep-c": [{"uuid": "ent-c", "name": "Alice"}]},
        pending=[
            _pending_row(assertion_id="a-a", source_key="s-a", episode_uuid="ep-a", namespace="a"),
            _pending_row(assertion_id="a-b", source_key="s-b", episode_uuid="ep-b", namespace="b"),
            _pending_row(assertion_id="a-c", source_key="s-c", episode_uuid="ep-c", namespace="c",
                         subject_display="Alice"),
        ],
    )
    out1 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True,
        namespaces=["a", "b", "c"], scalar_repair_limit=2))
    assert out1["scalar_repaired"] == 0                    # only a,b scanned (both unbindable)
    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True,
        namespaces=["a", "b", "c"], scalar_repair_limit=2))
    assert out2["scalar_repaired"] == 1 and graph.rebuilt == ["ent-c"]   # c reached on the next run


@pytest.mark.unit
def test_scheduler_repair_recovers_from_a_crashed_rebuild() -> None:
    # crash recovery: adoption succeeds, the first rebuild raises -> the row stays projection_pending
    # (not lost from the repair set), and a later run retries the rebuild without re-recording.
    graph = _ScalarGraph(
        scalar_dirty=[], episodes_by_ns={},
        entities_by_ep={"ep-p": [{"uuid": "ent-user", "name": "Alice"}]},
        pending=[_pending_row(subject_display="Alice", episode_uuid="ep-p")],
        rebuild_fail_first=True,
    )
    out1 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True))
    assert out1["scalar_repaired"] == 0                    # adoption happened, rebuild raised
    assert len(graph.recorded) == 1                        # the advisory was re-recorded once
    assert graph._pending[0]["projection_pending"] is True  # still selectable for retry

    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True))
    assert out2["scalar_repaired"] == 1 and graph.rebuilt == ["ent-user"]
    assert len(graph.recorded) == 1                        # NOT re-recorded on the projection retry
    assert graph._pending[0]["projection_pending"] is False  # cleared after success


@pytest.mark.unit
def test_scheduler_runs_reconciliation_and_orphan_passes_and_reports_them() -> None:
    # both journal-driven passes are wired: receiptless reconciliation (finds the no-orphan
    # rebind-ok/rebuild-failed case) AND the fair orphan-rebind pass; both results are reported.
    graph = _ScalarGraph(
        scalar_dirty=[], episodes_by_ns={}, entities_by_ep={}, orphans=["dead-ent"])
    graph._recon_repaired = 2
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True))
    assert out["scalar_recon_repaired"] == 2                        # receiptless reconciliation ran
    assert out["scalar_orphans_repaired"] == 1 and out["scalar_orphans_unresolved"] == 0
    assert len(graph.recon_calls) == 1 and len(graph.orphan_calls) == 1
    # reconciliation is fed committed_unmerges too (not merges alone)
    assert graph.recon_calls[0]["committed_unmerges"] is not None


@pytest.mark.unit
def test_scheduler_repair_passes_forward_namespace_allowlist() -> None:
    graph = _ScalarGraph(
        scalar_dirty=[], episodes_by_ns={"tenant-a": []}, entities_by_ep={}, orphans=[])
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, namespaces=["tenant-a", "tenant-a"]))
    assert graph.orphan_calls[0]["allowed_namespaces"] == {"tenant-a"}   # deduped allowlist forwarded


@pytest.mark.unit
def test_flag_off_omits_repair_keys() -> None:
    graph = _ScalarGraph(scalar_dirty=[], episodes_by_ns={}, entities_by_ep={}, orphans=["x"])
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1))               # flag OFF
    assert "scalar_orphans_repaired" not in out and "scalar_recon_repaired" not in out
    assert graph.orphan_calls == [] and graph.recon_calls == []


# ---- required cursor-correctness regressions (2nd review round 3) -------------------------------

@pytest.mark.unit
def test_late_finalized_episode_with_earlier_world_time_is_still_discovered() -> None:
    # e0 processed (finalized July10). Then e1 FINALIZES later (created July11) but its world-time is
    # July1 (earlier). A world-time cursor would sort e1 BEHIND e0 and strand it forever; the
    # ingestion-order (created_at) cursor discovers it.
    graph = _ScalarGraph(
        scalar_dirty=["ns"],
        episodes_by_ns={"ns": [_wake_ep("e0", created_at="2026-07-10T00:00:00Z",
                                        valid_at="2026-07-10T00:00:00Z")]},
        entities_by_ep={u: [{"uuid": "ent-user", "name": "user"}] for u in ("e0", "e1")},
    )
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_batch_size=10))
    assert graph.cursor["ns"][1] == "e0"
    graph._scalar_episodes["ns"].append(
        _wake_ep("e1", created_at="2026-07-11T00:00:00Z", valid_at="2026-07-01T00:00:00Z"))
    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_batch_size=10))
    assert out2["scalar_batches"] == 1 and graph.cursor["ns"][1] == "e1"   # discovered + processed


@pytest.mark.unit
def test_same_world_time_lower_uuid_later_arrival_is_not_stranded() -> None:
    # e_z: world-time July5, finalized July10, uuid "z" (processed). e_a: SAME world-time July5 but
    # finalizes LATER (July11) with a LOWER uuid "a". A (valid_at, uuid) cursor strands "a" (<"z");
    # the (created_at, uuid) cursor discovers it (July11 > July10).
    graph = _ScalarGraph(
        scalar_dirty=["ns"],
        episodes_by_ns={"ns": [_wake_ep("z", created_at="2026-07-10T00:00:00Z",
                                        valid_at="2026-07-05T00:00:00Z")]},
        entities_by_ep={u: [{"uuid": "ent-user", "name": "user"}] for u in ("z", "a")},
    )
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True))
    assert graph.cursor["ns"][1] == "z"
    graph._scalar_episodes["ns"].append(
        _wake_ep("a", created_at="2026-07-11T00:00:00Z", valid_at="2026-07-05T00:00:00Z"))
    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True))
    assert out2["scalar_batches"] == 1 and graph.cursor["ns"][1] == "a"


@pytest.mark.unit
def test_timeless_world_time_advances_cursor_and_persists_learned_fallback() -> None:
    # valid_at absent but created_at present + the model gives no `when`: the cursor advances on
    # created_at, and the assertion's time_basis is learned_fallback — NOT episode_reference from a
    # conflated ingestion timestamp.
    graph = _ScalarGraph(
        scalar_dirty=["ns"],
        episodes_by_ns={"ns": [_wake_ep("e0", created_at="2026-07-10T00:00:00Z", valid_at=None)]},
        entities_by_ep={"e0": [{"uuid": "ent-user", "name": "user"}]},
    )
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm_no_when, k=1, enable_scalar_state=True))
    assert graph.cursor["ns"][0] == "2026-07-10T00:00:00Z"     # advanced via created_at
    assert graph.recorded and graph.recorded[0].time_basis == "learned_fallback"


@pytest.mark.unit
def test_perceiver_version_bump_resets_cursor_and_revisits_history() -> None:
    graph = _ScalarGraph(
        scalar_dirty=["ns"], episodes_by_ns={"ns": _wake_eps("e0", "e1")},
        entities_by_ep={u: [{"uuid": "ent-user", "name": "user"}] for u in ("e0", "e1")},
    )
    # batch_size=1 so each episode is its own page (the LLM stub grounds one proposal per page).
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True,
        scalar_state_perceiver_version="v1", scalar_batch_size=1))
    assert graph.cursor["ns"][2] == "v1" and graph.cursor["ns"][1] == "e1"
    assert len(graph.recorded) == 2
    # a version bump resets the cursor -> both episodes are revisited under v2
    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True,
        scalar_state_perceiver_version="v2", scalar_batch_size=1))
    assert graph.cursor["ns"][2] == "v2" and out2["scalar_batches"] == 2
    assert len(graph.recorded) == 4


@pytest.mark.unit
def test_timeless_row_does_not_block_earlier_valid_rows_and_is_bounded() -> None:
    # a page containing a valid row e0 and a fully timeless row ex (no created_at, no valid_at). ex
    # sorts LAST; the pass advances the cursor through e0 (valid) and does not stall on ex — earlier
    # valid work is never blocked. A follow-up run makes no further progress (bounded, no loop).
    graph = _ScalarGraph(
        scalar_dirty=["ns"],
        episodes_by_ns={"ns": [
            _wake_ep("e0", created_at="2026-07-01T00:00:00Z", valid_at="2026-07-01T00:00:00Z"),
            _wake_ep("ex", created_at=None, valid_at=None)]},
        entities_by_ep={u: [{"uuid": "ent-user", "name": "user"}] for u in ("e0", "ex")},
    )
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_batch_size=10))
    assert graph.cursor["ns"][1] == "e0"                       # advanced past the valid row
    out2 = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_wake_llm, k=1, enable_scalar_state=True, scalar_batch_size=10))
    assert out2["scalar_batches"] == 0                         # bounded: no re-processing loop


@pytest.mark.unit
def test_scheduler_factory_wires_scalar_state_flag_and_version(monkeypatch) -> None:
    # the production path is the scheduler factory, not a direct task call: it MUST forward the
    # enable flag + perceiver version, or the feature is permanently off in the maintenance job.
    from menhir.services import maintenance_scheduler as ms

    captured: dict = {}

    async def _fake_consolidate(*_a, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(ms, "consolidate_personal_memory", _fake_consolidate)

    class _Ingest:
        def get_queue_depth(self):
            return 0

    sched = ms.MaintenanceScheduler(
        ingest_service=_Ingest(), graph_adapter=object(),
        personal_memory_enabled=True, personal_memory_llm=_empty_llm,
        scalar_state_enabled=True, scalar_state_perceiver_version="v2")
    asyncio.run(sched._make_consolidate_personal_memory())

    assert captured["enable_scalar_state"] is True
    assert captured["scalar_state_perceiver_version"] == "v2"


@pytest.mark.unit
def test_scheduler_factory_defaults_scalar_state_off(monkeypatch) -> None:
    from menhir.services import maintenance_scheduler as ms

    captured: dict = {}

    async def _fake_consolidate(*_a, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(ms, "consolidate_personal_memory", _fake_consolidate)

    class _Ingest:
        def get_queue_depth(self):
            return 0

    sched = ms.MaintenanceScheduler(
        ingest_service=_Ingest(), graph_adapter=object(),
        personal_memory_enabled=True, personal_memory_llm=_empty_llm)
    asyncio.run(sched._make_consolidate_personal_memory())

    assert captured["enable_scalar_state"] is False


@pytest.mark.unit
def test_make_sync_chat_returns_none_without_key(monkeypatch) -> None:
    from menhir.infrastructure import sync_llm
    from menhir.infrastructure.providers import ProviderConfig

    class _Cfg:
        api_key = ""
        chat_model = ""
        base_url = ""

    monkeypatch.setattr(ProviderConfig, "for_chat", classmethod(lambda cls, s: _Cfg()))
    assert sync_llm.make_sync_chat(object()) is None


@pytest.mark.unit
def test_make_sync_chat_honors_model_override(monkeypatch) -> None:
    # the consolidation job runs on a dedicated model (gpt-4o-mini) without touching the global
    # chat model; make_sync_chat(model=...) must swap only the model name, same provider client.
    import openai

    from menhir.infrastructure import sync_llm
    from menhir.infrastructure.observability import reset_llm_usage_callback, set_llm_usage_callback
    from menhir.infrastructure.providers import ProviderConfig, ProviderKind

    class _Cfg:
        api_key = "k"
        chat_model = "global-model"
        # A CONCRETE local endpoint, not "". CF-188: an empty base_url is the local sentinel, and
        # handing it to the OpenAI SDK resolves to api.openai.com, so make_sync_chat now refuses
        # it rather than egressing memory text. Deliberately NOT the default :8081 base, which
        # would route this stub through the real llama scheduler.
        base_url = "http://127.0.0.1:9099/v1"
        kind = ProviderKind.LOCAL

    used: dict = {}

    class _Resp:
        usage = type(
            "Usage",
            (),
            {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
        )()

        class _Choice:
            class _Msg:
                content = "ok"
            message = _Msg()
        choices = [_Choice()]

    class _Completions:
        def create(self, *, model, messages, temperature, max_tokens):
            used["model"] = model
            return _Resp()

    class _OpenAI:
        def __init__(self, **kw):
            self.chat = type("C", (), {"completions": _Completions()})()

    monkeypatch.setattr(ProviderConfig, "for_chat", classmethod(lambda cls, s: _Cfg()))
    monkeypatch.setattr(openai, "OpenAI", _OpenAI)

    usage_events = []
    token = set_llm_usage_callback(usage_events.append)
    try:
        chat = sync_llm.make_sync_chat(object(), model="override-model")
        assert chat is not None and chat("sys", "usr") == "ok"
    finally:
        reset_llm_usage_callback(token)
    assert used["model"] == "override-model"
    assert usage_events[-1].input_tokens == 20
    assert usage_events[-1].output_tokens == 4
    assert usage_events[-1].total_tokens == 24

    used.clear()
    sync_llm.make_sync_chat(object())("sys", "usr")   # no override -> provider's global model
    assert used["model"] == "global-model"


@pytest.mark.unit
def test_settings_reads_personal_memory_chat_model(monkeypatch) -> None:
    from menhir.config import MemorySettings

    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_CHAT_MODEL", "gpt-4o-mini")
    assert MemorySettings.from_env().personal_memory_consolidation_chat_model == "gpt-4o-mini"


@pytest.mark.unit
def test_settings_reads_verify_retries(monkeypatch) -> None:
    from menhir.config import MemorySettings

    # default is 0 (behaviour unchanged); env override is read as an int.
    monkeypatch.delenv("MENHIR_PERSONAL_MEMORY_VERIFY_RETRIES", raising=False)
    assert MemorySettings.from_env().personal_memory_consolidation_verify_retries == 0
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_VERIFY_RETRIES", "1")
    assert MemorySettings.from_env().personal_memory_consolidation_verify_retries == 1


@pytest.mark.unit
def test_verify_retries_threads_to_perceive(monkeypatch) -> None:
    """The consolidation task forwards its verify_retries to perceive_and_fold (default 0)."""
    seen = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        class _R:
            committed: list = []
            abstained: list = []
        return _R()

    monkeypatch.setattr(scheduler_tasks, "perceive_and_fold", _spy, raising=False)
    import menhir.services.perception as perception
    monkeypatch.setattr(perception, "perceive_and_fold", _spy)

    graph = _FakeGraph(dirty=["ns-a"], episodes_by_ns={"ns-a": _eps("spent thirty on a helmet")})
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=2, verify_retries=2))
    assert seen.get("verify_retries") == 2

    seen.clear()
    graph2 = _FakeGraph(dirty=["ns-a"], episodes_by_ns={"ns-a": _eps("spent thirty on a helmet")})
    asyncio.run(scheduler_tasks.consolidate_personal_memory(graph2, llm_complete=_empty_llm, k=2))
    assert seen.get("verify_retries") == 0  # default unchanged


@pytest.mark.unit
def test_settings_reads_sum_grounding(monkeypatch) -> None:
    from menhir.config import MemorySettings

    # promoted to default True (2026-07-08, cross-check-quality pack); env can still disable it.
    monkeypatch.delenv("MENHIR_PERSONAL_MEMORY_SUM_GROUNDING", raising=False)
    assert MemorySettings.from_env().personal_memory_consolidation_sum_grounding is True
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SUM_GROUNDING", "0")
    assert MemorySettings.from_env().personal_memory_consolidation_sum_grounding is False
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SUM_GROUNDING", "1")
    assert MemorySettings.from_env().personal_memory_consolidation_sum_grounding is True


@pytest.mark.unit
def test_sum_grounding_threads_to_perceive(monkeypatch) -> None:
    """The consolidation task forwards sum_grounding to perceive_and_fold as enable_sum_grounding."""
    seen = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        class _R:
            committed: list = []
            abstained: list = []
        return _R()

    monkeypatch.setattr(scheduler_tasks, "perceive_and_fold", _spy, raising=False)
    import menhir.services.perception as perception
    monkeypatch.setattr(perception, "perceive_and_fold", _spy)

    graph = _FakeGraph(dirty=["ns-a"], episodes_by_ns={"ns-a": _eps("spent thirty on a helmet")})
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=2, sum_grounding=True))
    assert seen.get("enable_sum_grounding") is True

    seen.clear()
    graph2 = _FakeGraph(dirty=["ns-a"], episodes_by_ns={"ns-a": _eps("spent thirty on a helmet")})
    asyncio.run(scheduler_tasks.consolidate_personal_memory(graph2, llm_complete=_empty_llm, k=2))
    assert seen.get("enable_sum_grounding") is False  # default unchanged


@pytest.mark.unit
def test_all_bias_guards_pinned_on(monkeypatch) -> None:
    """Decision 5: the prod wrapper must pin cross-check + coref + verify ON (an automatic write must
    never commit a confident-but-wrong total). Assert perceive_and_fold is called with all three."""
    seen = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        class _R:
            committed: list = []
            abstained: list = []
        return _R()

    monkeypatch.setattr(scheduler_tasks, "perceive_and_fold", _spy, raising=False)
    # perceive_and_fold is imported inside the task; patch at its source too.
    import menhir.services.perception as perception
    monkeypatch.setattr(perception, "perceive_and_fold", _spy)

    graph = _FakeGraph(dirty=["lme-a"], episodes_by_ns={"lme-a": _eps("spent thirty on a helmet")})
    asyncio.run(scheduler_tasks.consolidate_personal_memory(graph, llm_complete=_empty_llm, k=2))

    assert seen.get("enable_cross_check") is True
    assert seen.get("enable_coref") is True
    assert seen.get("enable_verify") is True


# ---- event-history consolidation wiring (task: narrow event wiring slice) ----------------------

def _event_runner_capture(*, consume_calls=0):
    """Factory for a run_event_consolidation stand-in that records what it was handed and optionally
    spends LLM budget (via the shared counting_llm) so the caller can assert cross-phase accounting."""
    captured: dict[str, object] = {}

    def _runner(graph_adapter, *, event_targets, counting_llm, call_budget, config):
        captured["targets"] = list(event_targets)
        captured["counter"] = counting_llm
        captured["calls_before"] = counting_llm.calls
        captured["budget"] = call_budget
        captured["version"] = config.perceiver_version
        captured["batch"] = config.batch_size
        for _ in range(consume_calls):
            counting_llm("system", "user")
        captured["calls_after"] = counting_llm.calls
        return {
            "event_namespaces_dirty": len(event_targets),
            "event_namespaces_processed": len(event_targets),
            "event_batches": 1,
            "event_proposals": 1,
            "event_assertions_recorded": 1,
            "event_assertions_created": 1,
            "event_views_rebuilt": 1,
            "event_drop_reasons": {},
            "event_errors": {},
            "event_llm_calls": captured["calls_after"] - captured["calls_before"],
        }

    return _runner, captured


@pytest.mark.unit
def test_event_flag_off_never_discover_or_runs_event_and_adds_no_event_keys(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_select(*a, **kw):
        captured.setdefault("discover", True)
        return ["should-not-be-used"]

    monkeypatch.setattr(scheduler_tasks, "select_event_targets", _fake_select)
    monkeypatch.setattr(
        scheduler_tasks, "run_event_consolidation",
        lambda *a, **kw: captured.setdefault("ran", True) or {},
    )
    graph = _FakeGraph(dirty=["project-a"], episodes_by_ns={"project-a": _eps("I spent forty dollars")})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=2))

    assert "discover" not in captured and "ran" not in captured     # never touched when off
    assert not any(k.startswith("event_") for k in out)             # no event_* result keys
    assert set(out.keys()) == _COUNTER_ONLY_KEYS                     # byte-identical counter shape


@pytest.mark.unit
def test_event_flag_on_discover_shares_llm_and_merges_metrics(monkeypatch) -> None:
    async def _fake_select(*a, **kw):
        return ["ev-a", "ev-b"]

    monkeypatch.setattr(scheduler_tasks, "select_event_targets", _fake_select)
    _runner, captured = _event_runner_capture(consume_calls=3)
    monkeypatch.setattr(scheduler_tasks, "run_event_consolidation", _runner)

    graph = _FakeGraph(dirty=["ctr-a"], episodes_by_ns={"ctr-a": _eps("spent money")})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=2,
        call_budget=50,
        enable_event_history=True,
        event_history_perceiver_version="v2",
        event_batch_size=123,
    ))

    assert captured["targets"] == ["ev-a", "ev-b"]                   # independent event-dirty targets
    assert captured["version"] == "v2"                               # configured version flows through
    assert captured["batch"] == 123                                  # configured batch flows through
    assert captured["budget"] == 50                                  # shares the same call budget
    # the runner spent 3 calls; total llm_calls = counter phase + event phase
    assert out["llm_calls"] == captured["calls_after"]
    assert out["llm_calls"] > 0
    assert captured["calls_after"] - captured["calls_before"] == 3   # event spent on the same counter
    assert out["event_namespaces_dirty"] == 2                        # event metrics merged
    assert out["event_views_rebuilt"] == 1


@pytest.mark.unit
def test_event_explicit_namespaces_bypass_dirty_discovery(monkeypatch) -> None:
    called: dict = {}

    async def _fake_select(*a, **kw):
        called.setdefault("discover", True)
        return kw.get("namespaces") or []

    monkeypatch.setattr(scheduler_tasks, "select_event_targets", _fake_select)
    _runner, captured = _event_runner_capture()
    monkeypatch.setattr(scheduler_tasks, "run_event_consolidation", _runner)

    graph = _FakeGraph(dirty=["ignored"], episodes_by_ns={})
    asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=1,
        namespaces=["tenant-a"],
        enable_event_history=True,
    ))

    # select_event_targets IS called with the explicit namespaces (the bypass happens inside the
    # real select_event_targets via its `namespaces is not None` branch); the runner gets them whole.
    assert called["discover"] is True
    assert captured["targets"] == ["tenant-a"]


@pytest.mark.unit
def test_event_only_call_with_counter_and_scalar_disabled(monkeypatch) -> None:
    async def _fake_select(*a, **kw):
        return ["ev-a"]

    monkeypatch.setattr(scheduler_tasks, "select_event_targets", _fake_select)
    _runner, captured = _event_runner_capture(consume_calls=2)
    monkeypatch.setattr(scheduler_tasks, "run_event_consolidation", _runner)

    graph = _FakeGraph(dirty=["ctr-should-be-skipped"],
                       episodes_by_ns={"ctr-should-be-skipped": _eps("spent money")})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=2,
        enable_counter_state=False,
        enable_scalar_state=False,
        enable_event_history=True,
    ))

    assert graph.loaded == []                        # counter phase skipped entirely
    assert graph.marked == []                        # counter watermark untouched
    assert captured["targets"] is not None
    assert out["namespaces_processed"] == 0          # no counter namespaces
    assert out["llm_calls"] == 2                     # only the event phase spent LLM calls


@pytest.mark.unit
def test_event_runner_sees_consumed_budget_same_counter_no_fresh_object(monkeypatch) -> None:
    async def _fake_select(*a, **kw):
        return ["ev-a"]

    monkeypatch.setattr(scheduler_tasks, "select_event_targets", _fake_select)
    _runner, captured = _event_runner_capture()
    monkeypatch.setattr(scheduler_tasks, "run_event_consolidation", _runner)

    # counter phase consumes llm calls first (k=2 -> per-namespace calls), then the event runner must
    # observe the SAME counting_llm whose `calls` already reflects that spend.
    graph = _FakeGraph(dirty=["ctr-a"], episodes_by_ns={"ctr-a": _eps("spent money")})
    out = asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=2,
        enable_counter_state=True,
        enable_scalar_state=False,
        enable_event_history=True,
    ))

    assert captured["calls_before"] > 0                            # the consumed counter budget is visible
    assert captured["calls_after"] == out["llm_calls"]             # same counter, refreshed total
    assert "counter" in captured                                   # a shared object, not a fresh one
