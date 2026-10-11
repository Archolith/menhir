"""P3 expiry: a fact's own end (invalid_at) is world time, not supersession (offline).

Covers the hook record, MenhirEdgeExpiryHook on Graphiti's real resolver (P3b), the client
wiring and version guard, the persist rows and statement choice, the gates, the "ended" recall
role and the flag-off identity. The graph counterexamples (restated duplicate, contradiction,
replay) run against Neo4j in tests/test_anchored_time_expiry_live.py.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.nodes import EpisodeType, EpisodicNode  # noqa: E402
from graphiti_core.utils.maintenance.edge_operations import resolve_extracted_edge  # noqa: E402

import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.anchored_time import AnchoredTimeReport, AnchoredTimeResult  # noqa: E402
from menhir.infrastructure.anchored_time_persist import (  # noqa: E402
    CONTRACT_PROPERTIES,
    EXPIRY_PERSIST_CYPHER,
    LOCK_PROPERTY,
    PERSIST_CYPHER,
    contract_rows,
    persist_anchored_time,
)
from menhir.infrastructure.anchored_time_resolver import AnchoredTimeResolver  # noqa: E402

pytestmark = pytest.mark.unit

SPEECH = datetime(2024, 4, 10, 18, 30, tzinfo=timezone.utc)
TRIP_END = datetime(2024, 4, 1, tzinfo=timezone.utc)
TURN = "user: I was in Rome from March 27 to April 1 and I lived in Paris until 2021, planning Oslo."
OUTPUT = json.dumps({
    "facts": [
        {"i": 0, "expression": "from March 27 to April 1", "basis": "explicit_date",
         "kind": "point_event", "offset": None, "calendar": None, "date": "2024-03-27",
         "anchor": None},
        {"i": 1, "expression": "until 2021", "basis": "none", "kind": "state", "offset": None,
         "calendar": None, "date": None, "anchor": None},
        {"i": 2, "expression": None, "basis": "none", "kind": "plan", "offset": None,
         "calendar": None, "date": None, "anchor": None},
    ],
    "missing_events": [],
})


def _edge(fact: str, uuid: str, *, valid_at=SPEECH, invalid_at=None, expired_at=None) -> EntityEdge:
    return EntityEdge(
        uuid=uuid, group_id="ns", source_node_uuid="n-user", target_node_uuid=f"n-{uuid}",
        created_at=SPEECH, name="RELATES_TO", fact=fact, episodes=["ep-1"], valid_at=valid_at,
        invalid_at=invalid_at, expired_at=expired_at, reference_time=SPEECH,
    )


def _edges() -> list[EntityEdge]:
    return [
        _edge("The user was in Rome.", "e0", valid_at=datetime(2024, 3, 27, tzinfo=timezone.utc),
              invalid_at=TRIP_END),
        _edge("The user lived in Paris.", "e1", valid_at=datetime(2015, 1, 1, tzinfo=timezone.utc),
              invalid_at=datetime(2021, 1, 1, tzinfo=timezone.utc)),
        _edge("The user plans a trip to Oslo.", "e2", invalid_at=datetime(2024, 6, 1, tzinfo=timezone.utc)),
    ]


class _Completions:
    def __init__(self, content: str) -> None:
        self.content = content

    async def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


def _resolver(content: str = OUTPUT) -> AnchoredTimeResolver:
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(content)))
    return AnchoredTimeResolver(client, base_url="https://api.openai.com/v1", model="m", timeout_s=5.0)


async def _apply(edges, *, expiry: bool, content: str = OUTPUT) -> AnchoredTimeReport:
    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    try:
        await policy._apply_anchored_time(_resolver(content), receipt,
                                          SimpleNamespace(uuid="g-1", valid_at=SPEECH), edges,
                                          expiry=expiry)
        return receipt.anchored_time
    finally:
        policy.clear_extraction_receipt()


def _result(uuid: str = "e0", **overrides) -> AnchoredTimeResult:
    fields = dict(
        edge_uuid=uuid, fact_index=0, expression="from March 27 to April 1", basis="explicit_date",
        kind="point_event", granularity="day", window_start=date(2024, 3, 27),
        window_end=date(2024, 4, 1), anchor_ref=None, anchor_offset=None, planned_window=None,
        guard=None, written=False, reason="graphiti_inside_window", graphiti_valid_at=SPEECH,
        new_valid_at=None,
    )
    fields.update(overrides)
    return AnchoredTimeResult(**fields)


# --- hook record ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hook_records_world_end_for_point_events_and_states_only() -> None:
    edges = _edges()
    before = [e.model_dump() for e in edges]
    report = await _apply(edges, expiry=True)
    assert report.status == "ok" and report.expiry is True
    by_uuid = {r.edge_uuid: r for r in report.results}
    assert by_uuid["e0"].world_end == TRIP_END
    assert by_uuid["e1"].world_end == datetime(2021, 1, 1, tzinfo=timezone.utc)  # state
    assert by_uuid["e2"].world_end is None  # a plan's end is not an event's end
    for old, edge in zip(before, edges):
        new = edge.model_dump()
        old.pop("valid_at"), new.pop("valid_at")
        assert old == new  # the hook never touches invalid_at or expired_at: paths 2/3 see today's input


@pytest.mark.asyncio
async def test_hook_flag_off_records_nothing() -> None:
    report = await _apply(_edges(), expiry=False)
    assert report.status == "ok" and report.expiry is False
    assert all(r.world_end is None for r in report.results)


@pytest.mark.asyncio
async def test_hook_gates_unclassified_unended_and_already_expired_edges() -> None:
    no_items = json.dumps({"facts": [], "missing_events": []})
    report = await _apply(_edges(), expiry=True, content=no_items)
    # A fact the resolver returned no item for defaults to point_event; it is not classified.
    assert {r.reason for r in report.results} == {"no_item"}
    assert all(r.world_end is None for r in report.results)

    edges = _edges()
    edges[0].invalid_at = None
    edges[1].expired_at = SPEECH
    report = await _apply(edges, expiry=True)
    assert all(r.world_end is None for r in report.results)


@pytest.mark.asyncio
async def test_hook_failure_or_skip_records_nothing() -> None:
    report = await _apply(_edges(), expiry=True, content="not json")
    assert report.status != "ok" and report.results == ()
    receipt = policy.begin_extraction_receipt("ep-1", "user: no time words here at all.")
    try:
        await policy._apply_anchored_time(_resolver(), receipt,
                                          SimpleNamespace(uuid="g-1", valid_at=SPEECH), _edges(),
                                          expiry=True)
        assert receipt.anchored_time.status == "skipped" and receipt.anchored_time.results == ()
    finally:
        policy.clear_extraction_receipt()


@pytest.mark.asyncio
async def test_extraction_hook_threads_the_flag(monkeypatch) -> None:
    edges = _edges()
    node = SimpleNamespace(name="user", uuid="n-user")

    async def fake_extract(clients, episode, previous_episodes, **kwargs):
        return [node], edges, {"n-user": [0]}

    class _Clients:
        llm_client = object()

        def model_copy(self, *, update):
            copy = _Clients()
            copy.llm_client = update["llm_client"]
            return copy

    monkeypatch.setattr(policy, "extract_nodes_and_edges", fake_extract)
    context = SimpleNamespace(
        clients=_Clients(), episode=SimpleNamespace(uuid="g-1", valid_at=SPEECH), previous_episodes=[],
        entity_types=None, excluded_entity_types=None, edge_type_map={}, edge_types=None,
        custom_extraction_instructions=None,
    )
    for flag in (True, False):
        receipt = policy.begin_extraction_receipt("ep-1", TURN)
        try:
            hook = policy.MenhirExtractionHook(anchored_time=_resolver(), anchored_time_expiry=flag)
            await hook.extract_single_episode(context)
        finally:
            policy.clear_extraction_receipt()
        assert receipt.anchored_time.expiry is flag
        assert (receipt.anchored_time.results[0].world_end is not None) is flag
    # Without a resolver the flag has no effect: no report at all.
    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    try:
        await policy.MenhirExtractionHook(anchored_time_expiry=True).extract_single_episode(context)
    finally:
        policy.clear_extraction_receipt()
    assert receipt.anchored_time is None


# --- MenhirEdgeExpiryHook on Graphiti's real resolver (P3b) -------------------------------------

class _DedupeLLM:
    def __init__(self, contradicted: list[int], duplicates: list[int] | None = None) -> None:
        self.contradicted = contradicted
        self.duplicates = duplicates or []

    async def generate_response(self, *args, **kwargs):
        return {"duplicate_facts": self.duplicates, "contradicted_facts": self.contradicted}


def _episode() -> EpisodicNode:
    return EpisodicNode(uuid="ep-1", name="ep", group_id="ns", source=EpisodeType.message,
                        source_description="chat", content=TURN, valid_at=SPEECH, created_at=SPEECH)


OWNER = "owner-1"


async def _resolve(edge, related, existing, llm, *, results=None, hook=True, owner=OWNER,
                   report_owner=OWNER, status="ok", expiry=True):
    """resolve_extracted_edge inside an add_episode-like scope: receipt + owner bound."""
    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    binding = policy.anchored_time_owner.set(owner)
    try:
        receipt.anchored_time = AnchoredTimeReport(
            status=status, model="m", owner=report_owner, expiry=expiry,
            results=tuple(results if results is not None else [_result(edge.uuid)]))
        kwargs = {"edge_expiry_hook": policy.MenhirEdgeExpiryHook()} if hook else {}
        resolved, invalidated, _ = await resolve_extracted_edge(
            llm, edge, related, existing, _episode(), **kwargs)
        return resolved, invalidated, receipt.anchored_time
    finally:
        policy.anchored_time_owner.reset(binding)
        policy.clear_extraction_receipt()


def _other() -> EntityEdge:
    return _edge("The user likes pasta.", "x0", valid_at=datetime(2020, 1, 1, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_without_hook_path_one_expires_the_ended_edge() -> None:
    # Baseline (flag off passes no hook): upstream expires an edge for carrying its own end.
    resolved, invalidated, _ = await _resolve(_edges()[0], [], [_other()], _DedupeLLM([]), hook=False)
    assert invalidated == [] and resolved.expired_at is not None and resolved.invalid_at == TRIP_END


@pytest.mark.asyncio
async def test_world_end_keeps_a_new_ended_edge_live() -> None:
    resolved, invalidated, report = await _resolve(_edges()[0], [], [_other()], _DedupeLLM([]))
    assert invalidated == [] and resolved.expired_at is None and resolved.invalid_at == TRIP_END
    assert report.world_end_kept == 1


@pytest.mark.asyncio
async def test_world_end_keeps_a_restated_duplicate_live() -> None:
    # P3-1: the restatement resolves to the stored ended edge; it is not re-expired.
    stored = _edge("The user visited Rome.", "stored", valid_at=datetime(2024, 3, 27, tzinfo=timezone.utc),
                   invalid_at=TRIP_END)
    new = _edges()[0]
    resolved, invalidated, report = await _resolve(new, [stored], [], _DedupeLLM([], [0]))
    assert resolved.uuid == "stored" and invalidated == []
    assert resolved.expired_at is None and resolved.invalid_at == TRIP_END
    assert report.world_end_kept == 1
    # Without the hook the same restatement expires the stored edge (the P3-1 bug).
    stored2 = _edge("The user visited Rome.", "stored", valid_at=datetime(2024, 3, 27, tzinfo=timezone.utc),
                    invalid_at=TRIP_END)
    resolved, _, _ = await _resolve(_edges()[0], [stored2], [], _DedupeLLM([], [0]), hook=False)
    assert resolved.expired_at is not None


@pytest.mark.asyncio
async def test_newer_contradiction_inside_the_window_truncates_and_expires() -> None:
    # Residual 1 closed: path 1 no longer masks path 2.
    newer = _edge("The user was in Milan instead.", "x2", valid_at=datetime(2024, 3, 28, tzinfo=timezone.utc))
    resolved, invalidated, _ = await _resolve(_edges()[0], [], [newer], _DedupeLLM([0]))
    assert resolved.expired_at is not None and resolved.invalid_at == newer.valid_at
    assert invalidated == []


@pytest.mark.asyncio
async def test_contradiction_after_the_end_does_not_supersede() -> None:
    later = _edge("The user is in Milan.", "x3", valid_at=datetime(2024, 4, 5, tzinfo=timezone.utc))
    resolved, _, _ = await _resolve(_edges()[0], [], [later], _DedupeLLM([0]))
    assert resolved.expired_at is None and resolved.invalid_at == TRIP_END


@pytest.mark.asyncio
async def test_path_three_on_older_overlapping_edge_is_unchanged() -> None:
    older = _edge("The user was in Milan.", "x1", valid_at=datetime(2024, 3, 1, tzinfo=timezone.utc))
    resolved, invalidated, _ = await _resolve(_edges()[0], [], [older], _DedupeLLM([0]))
    assert [e.uuid for e in invalidated] == ["x1"] and invalidated[0].expired_at is not None
    assert resolved.expired_at is None and resolved.invalid_at == TRIP_END


@pytest.mark.asyncio
async def test_no_candidates_never_reaches_the_hook() -> None:
    resolved, _, report = await _resolve(_edges()[0], [], [], _DedupeLLM([]))
    assert resolved.expired_at is None and report.world_end_kept == 0


@pytest.mark.parametrize("case", [
    "plan", "no_item", "no_result", "owner_mismatch", "unowned_call", "status_not_ok",
    "expiry_off", "broken_report",
])
@pytest.mark.asyncio
async def test_adapter_answers_expire_unless_this_call_classified_an_ended_fact(case) -> None:
    edge = _edges()[0]
    kwargs: dict = {}
    if case == "plan":
        kwargs["results"] = [_result("e0", kind="plan")]
    elif case == "no_item":
        kwargs["results"] = [_result("e0", reason="no_item")]
    elif case == "no_result":
        kwargs["results"] = [_result("other")]
    elif case == "owner_mismatch":
        kwargs["report_owner"] = "another-call"  # a concurrent call's report on a shared receipt
    elif case == "unowned_call":
        kwargs["owner"], kwargs["report_owner"] = None, None
    elif case == "status_not_ok":
        kwargs["status"] = "skipped"
    elif case == "expiry_off":
        kwargs["expiry"] = False
    elif case == "broken_report":
        kwargs["results"] = [object()]  # attribute errors are caught: EXPIRE
    resolved, _, report = await _resolve(edge, [], [_other()], _DedupeLLM([]), **kwargs)
    assert resolved.expired_at is not None and report.world_end_kept == 0


@pytest.mark.asyncio
async def test_adapter_without_receipt_answers_expire() -> None:
    from graphiti_core.edge_expiry import EdgeExpiryContext, EdgeExpiryDecision

    edge = _edges()[0]
    context = EdgeExpiryContext(extracted_edge=edge, resolved_edge=edge, is_duplicate=False,
                                episode=_episode())
    assert await policy.MenhirEdgeExpiryHook().decide_edge_expiry(context) is EdgeExpiryDecision.EXPIRE


@pytest.mark.asyncio
async def test_adapter_falls_back_to_the_resolved_edge_classification() -> None:
    # Bulk pass 1 can resolve to another edge of the same episode (fork F4).
    from graphiti_core.edge_expiry import EdgeExpiryContext, EdgeExpiryDecision

    extracted, sibling = _edges()[0], _edges()[1]
    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    binding = policy.anchored_time_owner.set(OWNER)
    try:
        receipt.anchored_time = AnchoredTimeReport(status="ok", model="m", owner=OWNER, expiry=True,
                                                   results=(_result("e1", kind="state"),))
        context = EdgeExpiryContext(extracted_edge=extracted, resolved_edge=sibling,
                                    is_duplicate=True, episode=_episode())
        assert (await policy.MenhirEdgeExpiryHook().decide_edge_expiry(context)
                is EdgeExpiryDecision.WORLD_END)
        # The new mention's own result wins when present.
        receipt.anchored_time.results = (_result("e0", kind="plan"), _result("e1", kind="state"))
        assert (await policy.MenhirEdgeExpiryHook().decide_edge_expiry(context)
                is EdgeExpiryDecision.EXPIRE)
    finally:
        policy.anchored_time_owner.reset(binding)
        policy.clear_extraction_receipt()


# --- client wiring and version guard -------------------------------------------------------------

def test_client_passes_the_hook_only_with_the_flag_on() -> None:
    from graphiti_core import Graphiti

    from menhir.infrastructure.graphiti_client import edge_expiry_hook_kwargs

    assert edge_expiry_hook_kwargs(False, Graphiti) == {}
    on = edge_expiry_hook_kwargs(True, Graphiti)
    assert list(on) == ["edge_expiry_hook"]
    assert isinstance(on["edge_expiry_hook"], policy.MenhirEdgeExpiryHook)


def test_version_guard_refuses_the_flag_on_a_fork_without_the_hook() -> None:
    from menhir.infrastructure.graphiti_client import edge_expiry_hook_kwargs

    class OldGraphiti:
        def __init__(self, uri=None, *, candidate_filter_hook=None):
            pass

    assert edge_expiry_hook_kwargs(False, OldGraphiti) == {}  # flag off: old fork is fine
    with pytest.raises(RuntimeError, match=r"archolith-graphiti-core==0\.30\.2\.post3"):
        edge_expiry_hook_kwargs(True, OldGraphiti)


def test_installed_fork_accepts_the_hook() -> None:
    from graphiti_core.edge_expiry import EdgeExpiryHook

    assert isinstance(policy.MenhirEdgeExpiryHook(), EdgeExpiryHook)


# --- persist rows and statement ------------------------------------------------------------------

def _report(*results: AnchoredTimeResult, expiry: bool = True) -> AnchoredTimeReport:
    return AnchoredTimeReport(status="ok", model="m", results=tuple(results),
                              owner=policy.anchored_time_owner.get(), expiry=expiry)


def test_rows_carry_world_end_only_when_recorded_and_flag_on() -> None:
    ended = _result("e0", world_end=TRIP_END)
    plain = _result("e1")
    rows = {r["uuid"]: r for r in contract_rows(_report(ended, plain), ["e0", "e1"], date(2024, 4, 10))}
    assert rows["e0"]["time_world_end"] == "2024-04-01T00:00:00+00:00"
    assert (rows["e0"]["time_world_end_s"], rows["e0"]["time_world_end_ns"]) == (int(TRIP_END.timestamp()), 0)
    assert set(rows["e1"]) == {"uuid", *CONTRACT_PROPERTIES}
    # Flag off: identical to the P2 row even if a result somehow carried a world end.
    off = contract_rows(_report(ended, plain, expiry=False), ["e0", "e1"], date(2024, 4, 10))
    assert all(set(r) == {"uuid", *CONTRACT_PROPERTIES} for r in off)


def test_naive_world_end_is_treated_as_utc() -> None:
    naive = _result("e0", world_end=datetime(2024, 4, 1))
    row = contract_rows(_report(naive), ["e0"], None)[0]
    assert (row["time_world_end_s"], row["time_world_end_ns"]) == (int(TRIP_END.timestamp()), 0)


def test_world_end_row_dropped_for_duplicates() -> None:
    # The extracted uuid resolved to an existing edge: not among the returned uuids.
    assert contract_rows(_report(_result("e0", world_end=TRIP_END)), ["existing"], None) == []


def test_expiry_statement_shape() -> None:
    q = EXPIRY_PERSIST_CYPHER
    assert q.count("r.time_contract IS NULL") == 2  # eligibility before and after the lock
    assert q.index(f"SET r.{LOCK_PROPERTY} = true") < q.index("r.time_expiry = 'world_end'")
    # P3b: stamp only, on a still-live edge; nothing is ever un-expired.
    assert "r.expired_at IS NULL" in q
    assert "r.expired_at =" not in q and "expired_at = null" not in q
    assert "datetime(r.invalid_at).epochSeconds = row.time_world_end_s" in q
    assert "datetime(r.invalid_at).nanosecond = row.time_world_end_ns" in q
    assert "epochMillis" not in q
    assert "r.time_expiry = 'world_end'" in q
    assert "SET r.invalid_at" not in q and "r.valid_at" not in q
    assert q.startswith(PERSIST_CYPHER.split("RETURN")[0])  # P2 contract write unchanged


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute_query(self, query, params=None, **kwargs):
        self.calls.append((query, params))
        record = {"written": len(params["rows"])}
        if "stamped" in query:
            record["stamped"] = 1
        return SimpleNamespace(records=[record])


@pytest.mark.asyncio
async def test_persist_uses_p2_statement_unless_a_world_end_was_recorded() -> None:
    driver = _Driver()
    rep = _report(_result("e0"))
    await persist_anchored_time(driver, rep, episode_uuid="ep-1", group_id="ns", edge_uuids=["e0"],
                                speech_date=None)
    assert driver.calls[0][0] == PERSIST_CYPHER and rep.stamped == 0

    driver = _Driver()
    rep = _report(_result("e0", world_end=TRIP_END))
    await persist_anchored_time(driver, rep, episode_uuid="ep-1", group_id="ns", edge_uuids=["e0"],
                                speech_date=None)
    assert driver.calls[0][0] == EXPIRY_PERSIST_CYPHER
    assert (rep.persist, rep.persisted, rep.stamped) == ("ok", 1, 1)

    driver = _Driver()
    rep = _report(_result("e0", world_end=TRIP_END), expiry=False)
    await persist_anchored_time(driver, rep, episode_uuid="ep-1", group_id="ns", edge_uuids=["e0"],
                                speech_date=None)
    assert driver.calls[0][0] == PERSIST_CYPHER and rep.stamped == 0


# --- settings -------------------------------------------------------------------------------------

def test_flag_defaults_off_and_reaches_tuning(monkeypatch) -> None:
    from menhir.config.feature_flags import FEATURES
    from menhir.config.settings_model import MemorySettings

    monkeypatch.delenv("MENHIR_ANCHORED_TIME_EXPIRY", raising=False)
    off = MemorySettings.from_env()
    assert off.anchored_time_expiry_enabled is False
    assert off.retrieval_tuning().enable_anchored_time_expiry is False
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_EXPIRY", "true")
    on = MemorySettings.from_env()
    assert on.anchored_time_expiry_enabled is True
    assert on.retrieval_tuning().enable_anchored_time_expiry is True
    flag = FEATURES["anchored_time_expiry_enabled"]
    assert (flag.env_var, flag.default, flag.recall_affecting) == ("MENHIR_ANCHORED_TIME_EXPIRY", False, True)
    assert flag.requires == ("anchored_time_resolver_enabled",)


# --- recall role ---------------------------------------------------------------------------------

def _rows() -> list[dict]:
    return [
        {"node_uuid": "n1", "fact": "trip", "valid_at": "2024-03-27T00:00:00Z[UTC]",
         "invalid_at": "2024-04-01T00:00:00Z[UTC]", "created_at": "2024-04-10T18:31:00Z",
         "expired_at": None},
        {"node_uuid": "n1", "fact": "future", "valid_at": "2024-04-01T00:00:00Z",
         "invalid_at": "2099-01-01T00:00:00Z", "created_at": "2024-04-10T18:32:00Z", "expired_at": None},
        {"node_uuid": "n1", "fact": "superseded", "valid_at": "2020-01-01T00:00:00Z",
         "invalid_at": "2021-01-01T00:00:00Z", "created_at": "2024-04-10T18:33:00Z",
         "expired_at": "2024-04-10T18:33:00Z"},
        {"node_uuid": "n1", "fact": "open", "valid_at": "2020-01-01T00:00:00Z", "invalid_at": None,
         "created_at": "2024-04-10T18:34:00Z", "expired_at": None},
    ]


def test_ended_role_for_unexpired_facts_whose_end_passed() -> None:
    from menhir.services.recall_policies import _build_temporal_facts

    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    facts = {f.fact: f for f in _build_temporal_facts(_rows(), ended=True, now=now)["n1"]}
    assert (facts["trip"].temporal_role, facts["trip"].is_current_belief) == ("ended", True)
    assert facts["future"].temporal_role == "current_belief"
    assert (facts["superseded"].temporal_role, facts["superseded"].is_current_belief) == (
        "superseded_belief", False)
    assert facts["open"].temporal_role == "current_belief"
    # The boundary is inclusive: an end at exactly now has passed.
    at_end = _build_temporal_facts(_rows(), ended=True, now=datetime(2024, 4, 1, tzinfo=timezone.utc))
    assert {f.fact: f.temporal_role for f in at_end["n1"]}["trip"] == "ended"
    just_before = _build_temporal_facts(_rows(), ended=True,
                                        now=datetime(2024, 4, 1, tzinfo=timezone.utc) - timedelta(seconds=1))
    assert {f.fact: f.temporal_role for f in just_before["n1"]}["trip"] == "current_belief"


def test_recall_flag_off_is_identical() -> None:
    from menhir.services.recall_policies import _build_temporal_facts

    assert _build_temporal_facts(_rows(), ended=False) == _build_temporal_facts(_rows())
    assert {f.temporal_role for f in _build_temporal_facts(_rows())["n1"]} == {
        "current_belief", "superseded_belief"}


def test_context_line_marks_ended_and_is_unchanged_otherwise() -> None:
    from menhir.services.context_builder import _source_time_lines
    from menhir.services.recall_policies import _build_temporal_facts

    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    on = SimpleNamespace(temporal_facts=_build_temporal_facts(_rows(), ended=True, now=now)["n1"])
    off = SimpleNamespace(temporal_facts=_build_temporal_facts(_rows())["n1"])
    on_lines, off_lines = _source_time_lines(on), _source_time_lines(off)
    trip_on = next(line for line in on_lines if "| trip |" in line)
    assert trip_on.endswith("belief: current belief, ended 2024-04-01T00:00:00Z[UTC]")
    trip_off = next(line for line in off_lines if "| trip |" in line)
    assert trip_off.endswith("belief: current belief")
    assert [line for line in on_lines if "| trip |" not in line] == [
        line for line in off_lines if "| trip |" not in line]


def test_result_default_has_no_world_end() -> None:
    # P1/P2 constructors (no world_end) keep working and compare equal to before.
    assert _result().world_end is None
    assert replace(_result(), world_end=None) == _result()


def test_world_end_keeps_full_precision() -> None:
    # Codex P3 review: epochMillis let a contradiction that moved invalid_at by 800 us pass.
    end = TRIP_END + timedelta(microseconds=123456)
    row = contract_rows(_report(_result("e0", world_end=end)), ["e0"], None)[0]
    assert (row["time_world_end_s"], row["time_world_end_ns"]) == (int(TRIP_END.timestamp()), 123456000)
    # Before the epoch Neo4j floors epochSeconds and keeps nanosecond-of-second non-negative.
    old = datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=timezone.utc)
    row = contract_rows(_report(_result("e0", world_end=old)), ["e0"], None)[0]
    assert (row["time_world_end_s"], row["time_world_end_ns"]) == (-1, 500000000)
