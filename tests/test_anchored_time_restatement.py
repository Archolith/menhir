"""P3c: a restatement must not expire a stored world-ended fact (offline).

The stored edge carries ``time_expiry='world_end'`` and ``time_world_end`` (the stamp written
at persist). A later restatement resolves to it as a duplicate; the pinned fork keeps the
stamp across the LLM duplicate path (``preserved_attributes``) and ``MenhirEdgeExpiryHook``
answers ``WORLD_END`` from that stored evidence even when this call's own anchored-time report
has no usable result for the mention (no cue, ``plan``, resolver error). The real-resolver
cases follow tests/test_anchored_time_edge_copies.py. Tests passing ``preserved_attributes``
to Graphiti need the 0.30.2.post6 fork.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("graphiti_core")

import graphiti_core.utils.maintenance.edge_operations as edge_ops  # noqa: E402
from graphiti_core.edge_expiry import EdgeExpiryDecision  # noqa: E402
from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.nodes import EntityNode  # noqa: E402
from graphiti_core.search.search_config import SearchResults  # noqa: E402

import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.anchored_time import AnchoredTimeReport  # noqa: E402
from menhir.infrastructure.anchored_time_persist import (  # noqa: E402
    CONTRACT_PROPERTIES,
    PRESERVED_EDGE_ATTRIBUTES,
)
from tests.test_anchored_time_expiry import (  # noqa: E402
    OWNER,
    TRIP_END,
    TURN,
    _edge,
    _episode,
    _result,
)

pytestmark = pytest.mark.unit

TRIP_START = datetime(2024, 3, 27, tzinfo=timezone.utc)
MILAN_START = datetime(2024, 3, 28, tzinfo=timezone.utc)
STORED_FACT = "The user visited Rome."
PARAPHRASE = "The user had a Rome trip in late March."
STAMP = {"time_expiry": "world_end", "time_world_end": TRIP_END.isoformat(),
         "time_contract": "at1|v|m", "time_kind": "point_event"}


# --- hook level -----------------------------------------------------------------------------------

def _resolved(*, attributes=None, invalid_at=TRIP_END, uuid="stored"):
    attrs = dict(STAMP) if attributes is None else attributes
    return SimpleNamespace(uuid=uuid, invalid_at=invalid_at, expired_at=None, attributes=attrs)


def _context(resolved, *, is_duplicate=True, extracted_uuid="new"):
    return SimpleNamespace(extracted_edge=SimpleNamespace(uuid=extracted_uuid), resolved_edge=resolved,
                           is_duplicate=is_duplicate, episode=_episode())


@contextmanager
def _bound(*, results=(), status="ok", owner=OWNER, report_owner=OWNER, expiry=True, with_report=True):
    """Receipt + owner (+ this call's report) bound like add_episode does; yields the report."""
    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    binding = policy.anchored_time_owner.set(owner)
    try:
        report = None
        if with_report:
            report = AnchoredTimeReport(status=status, model="m", owner=report_owner, expiry=expiry,
                                        results=tuple(results))
            receipt.anchored_time = report
        yield report
    finally:
        policy.anchored_time_owner.reset(binding)
        policy.clear_extraction_receipt()


async def _decide(context):
    return await policy.MenhirEdgeExpiryHook().decide_edge_expiry(context)


@pytest.mark.asyncio
async def test_stored_stamp_keeps_world_end_when_the_mention_has_no_result() -> None:
    # Fails if the hook consults only this call's report (the P3c counterexample).
    with _bound(results=[_result("some-other-edge")]):
        assert await _decide(_context(_resolved())) is EdgeExpiryDecision.WORLD_END


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_receipt", "status_error", "owner_mismatch", "expiry_off"])
async def test_stored_stamp_keeps_world_end_without_a_usable_report(case) -> None:
    kwargs: dict = {}
    if case == "no_receipt":
        kwargs["with_report"] = False
    elif case == "status_error":
        kwargs["status"] = "error"
    elif case == "owner_mismatch":
        kwargs["report_owner"] = "another-call"
    elif case == "expiry_off":
        kwargs["expiry"] = False
    with _bound(**kwargs):
        assert await _decide(_context(_resolved())) is EdgeExpiryDecision.WORLD_END


@pytest.mark.asyncio
async def test_stored_stamp_keeps_world_end_when_the_mention_is_a_plan() -> None:
    # D2: a plan mention would EXPIRE on the report path; the stored evidence still wins.
    with _bound(results=[_result("new", kind="plan")]):
        assert await _decide(_context(_resolved())) is EdgeExpiryDecision.WORLD_END


@pytest.mark.asyncio
async def test_stored_stamp_without_any_binding_at_all() -> None:
    # No receipt, no owner: the evidence is the stored edge alone and must not raise.
    assert await _decide(_context(_resolved())) is EdgeExpiryDecision.WORLD_END


@pytest.mark.asyncio
async def test_invalid_at_moved_one_microsecond_expires() -> None:
    # Fails if the comparison is coarser than the exact instant (ms/seconds/date).
    moved = _resolved(invalid_at=TRIP_END + timedelta(microseconds=1))
    with _bound():
        assert await _decide(_context(moved)) is EdgeExpiryDecision.EXPIRE
    earlier = _resolved(invalid_at=TRIP_END - timedelta(microseconds=1))
    with _bound():
        assert await _decide(_context(earlier)) is EdgeExpiryDecision.EXPIRE


@pytest.mark.asyncio
@pytest.mark.parametrize("attributes", [
    {},
    {"time_world_end": TRIP_END.isoformat()},
    {"time_expiry": "world_end"},
    {"time_expiry": "something_else", "time_world_end": TRIP_END.isoformat()},
    {"time_expiry": "world_end", "time_world_end": "not a date"},
    {"time_expiry": "world_end", "time_world_end": ""},
    {"time_expiry": "world_end", "time_world_end": None},
    {"time_expiry": "world_end", "time_world_end": 12345},
    None,
    "not-a-dict",
    ["time_expiry", "time_world_end"],
])
async def test_missing_or_unusable_stamp_expires(attributes) -> None:
    resolved = SimpleNamespace(uuid="stored", invalid_at=TRIP_END, expired_at=None, attributes=attributes)
    with _bound():
        assert await _decide(_context(resolved)) is EdgeExpiryDecision.EXPIRE


@pytest.mark.asyncio
async def test_invalid_at_none_expires_even_with_a_stamp() -> None:
    with _bound():
        assert await _decide(_context(_resolved(invalid_at=None))) is EdgeExpiryDecision.EXPIRE


@pytest.mark.asyncio
async def test_new_edge_carrying_the_keys_is_not_evidence() -> None:
    # is_duplicate False: the keys came from extraction output, not from a stored stamp.
    with _bound():
        assert await _decide(_context(_resolved(uuid="new"), is_duplicate=False)) \
            is EdgeExpiryDecision.EXPIRE


@pytest.mark.asyncio
async def test_resolved_uuid_equal_to_extracted_uuid_is_not_evidence() -> None:
    with _bound():
        assert await _decide(_context(_resolved(uuid="same"), extracted_uuid="same")) \
            is EdgeExpiryDecision.EXPIRE


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", [
    "2024-04-01T02:00:00+02:00",
    "2024-03-31T19:00:00-05:00",
    "2024-04-01T00:00:00",       # naive: UTC
    "2024-04-01T00:00:00Z",
    TRIP_END.astimezone(timezone(timedelta(hours=9))),
    datetime(2024, 4, 1),        # naive datetime: UTC
    SimpleNamespace(to_native=lambda: TRIP_END),
])
async def test_same_instant_in_any_representation_keeps_world_end(stamp) -> None:
    resolved = _resolved(attributes={"time_expiry": "world_end", "time_world_end": stamp})
    with _bound():
        assert await _decide(_context(resolved)) is EdgeExpiryDecision.WORLD_END


@pytest.mark.asyncio
async def test_invalid_at_in_another_zone_matches_by_instant() -> None:
    chicago = TRIP_END.astimezone(timezone(timedelta(hours=-5)))
    with _bound():
        assert await _decide(_context(_resolved(invalid_at=chicago))) is EdgeExpiryDecision.WORLD_END


@pytest.mark.asyncio
async def test_naive_stamp_is_utc_not_local_or_offset() -> None:
    # A naive stamp that is NOT the UTC instant must not match.
    resolved = _resolved(attributes={"time_expiry": "world_end", "time_world_end": "2024-04-01T02:00:00"})
    with _bound():
        assert await _decide(_context(resolved)) is EdgeExpiryDecision.EXPIRE


@pytest.mark.asyncio
async def test_report_path_still_counts_world_end_kept() -> None:
    # Report-based WORLD_END is unchanged: no stamp on the resolved edge, a classified mention.
    unstamped = _resolved(attributes={})
    with _bound(results=[_result("new")]) as report:
        assert await _decide(_context(unstamped, is_duplicate=False, extracted_uuid="new")) \
            is EdgeExpiryDecision.WORLD_END
        assert report.world_end_kept == 1


@pytest.mark.asyncio
async def test_stored_evidence_path_with_a_report_does_not_raise_or_touch_it() -> None:
    with _bound(results=[_result("some-other-edge")]) as report:
        assert await _decide(_context(_resolved())) is EdgeExpiryDecision.WORLD_END
        assert report.results[0].edge_uuid == "some-other-edge"


# --- client wiring --------------------------------------------------------------------------------

class _OldGraphiti:
    def __init__(self, uri=None, *, candidate_filter_hook=None, edge_expiry_hook=None):
        pass


class _NewGraphiti:
    def __init__(self, uri=None, *, edge_expiry_hook=None, preserved_edge_attributes=None):
        pass


def test_preserved_kwargs_wiring_and_version_guard() -> None:
    from menhir.infrastructure.graphiti_client import preserved_edge_attributes_kwargs as kw

    wanted = {"preserved_edge_attributes": PRESERVED_EDGE_ATTRIBUTES}
    assert kw(False, False, _OldGraphiti) == {}
    assert kw(False, False, _NewGraphiti) == {}
    assert kw(True, False, _OldGraphiti) == {}  # resolver only: the old fork is fine
    for resolver in (False, True):
        with pytest.raises(RuntimeError, match=r"archolith-graphiti-core==0\.30\.2\.post6"):
            kw(resolver, True, _OldGraphiti)
    assert kw(True, False, _NewGraphiti) == wanted
    assert kw(True, True, _NewGraphiti) == wanted


def test_preserved_attributes_cover_the_contract_and_the_stamp() -> None:
    names = set(PRESERVED_EDGE_ATTRIBUTES)
    assert set(CONTRACT_PROPERTIES) <= names
    assert {"time_expiry", "time_world_end"} <= names
    assert len(names) == len(PRESERVED_EDGE_ATTRIBUTES)


def test_installed_graphiti_accepts_the_preserved_attributes_kwarg() -> None:
    # Needs archolith-graphiti-core 0.30.2.post6; expected to fail on post5 until the pin bump.
    from graphiti_core import Graphiti

    from menhir.infrastructure.graphiti_client import preserved_edge_attributes_kwargs

    assert preserved_edge_attributes_kwargs(True, True, Graphiti) == {
        "preserved_edge_attributes": PRESERVED_EDGE_ATTRIBUTES}


# --- Graphiti's real resolver ---------------------------------------------------------------------

def _stored() -> EntityEdge:
    edge = _edge(STORED_FACT, "stored", valid_at=TRIP_START, invalid_at=TRIP_END)
    edge.target_node_uuid = "n-rome"
    edge.attributes = dict(STAMP)
    return edge


def _mention(fact: str, uuid: str) -> EntityEdge:
    edge = _edge(fact, uuid, valid_at=TRIP_START, invalid_at=TRIP_END)
    edge.target_node_uuid = "n-rome"
    return edge


async def _resolve_batch(monkeypatch, mentions, responses, *, results=(), status="ok",
                         preserved=PRESERVED_EDGE_ATTRIBUTES):
    """Each search returns a fresh copy of the stored, stamped trip; Milan is the other candidate."""
    milan = _edge("The user was in Milan instead.", "milan", valid_at=MILAN_START)

    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    async def fake_search(clients, query, group_ids, config, search_filter):
        if search_filter.edge_uuids is not None:
            return SearchResults(edges=[_stored()])
        return SearchResults(edges=[_stored(), milan])

    async def generate_response(messages, **kwargs):
        text = str(messages)
        return next(resp for fact, resp in responses.items() if fact in text)

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    monkeypatch.setattr(edge_ops, "semaphore_gather", immediate_gather)
    monkeypatch.setattr(edge_ops, "create_entity_edge_embeddings", AsyncMock(return_value=None))
    monkeypatch.setattr(edge_ops, "_extract_edge_timestamps", AsyncMock(return_value=None))
    monkeypatch.setattr(EntityEdge, "get_between_nodes", AsyncMock(return_value=[]))
    monkeypatch.setattr(edge_ops, "search", fake_search)

    clients = SimpleNamespace(driver=MagicMock(), llm_client=llm, embedder=MagicMock(),
                              cross_encoder=MagicMock())
    nodes = [EntityNode(uuid="n-user", name="user", group_id="ns", labels=["Entity"]),
             EntityNode(uuid="n-rome", name="Rome", group_id="ns", labels=["Entity"])]

    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    binding = policy.anchored_time_owner.set(OWNER)
    try:
        receipt.anchored_time = AnchoredTimeReport(
            status=status, model="m", owner=OWNER, expiry=True, results=tuple(results))
        kwargs = {} if preserved is None else {"preserved_attributes": preserved}
        resolved, invalidated, _ = await edge_ops.resolve_extracted_edges(
            clients, mentions, _episode(), nodes, {}, {},
            edge_expiry_hook=policy.MenhirEdgeExpiryHook(), **kwargs)
    finally:
        policy.anchored_time_owner.reset(binding)
        policy.clear_extraction_receipt()
    return [edge for edge in resolved + invalidated if edge.uuid == "stored"]


def _duplicate_of_stored(fact: str) -> dict:
    return {fact: {"duplicate_facts": [0], "contradicted_facts": []}}


def _assert_live_and_stamped(edge) -> None:
    assert edge.expired_at is None and edge.invalid_at == TRIP_END
    for key in ("time_expiry", "time_world_end", "time_contract"):
        assert edge.attributes.get(key) == STAMP[key], key


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "error"])
async def test_restatement_without_a_report_result_keeps_the_stored_edge_live(monkeypatch, status):
    # Paraphrase so the LLM duplicate path (which clears attributes) runs, not the exact-fact
    # fast path. The report has no result for the mention (no cue) or the resolver errored.
    mention = _mention(PARAPHRASE, "m-restate")
    copies = await _resolve_batch(monkeypatch, [mention], _duplicate_of_stored(PARAPHRASE),
                                  results=(), status=status)
    assert len(copies) == 1
    _assert_live_and_stamped(copies[0])
    assert copies[0].attributes["time_kind"] == "point_event"


@pytest.mark.asyncio
async def test_control_without_preserved_attributes_the_restatement_expires(monkeypatch):
    # The counterexample: the duplicate path clears the stamp, the hook has no evidence, upstream
    # expires. Documents why Menhir passes the set.
    mention = _mention(PARAPHRASE, "m-restate")
    copies = await _resolve_batch(monkeypatch, [mention], _duplicate_of_stored(PARAPHRASE),
                                  results=(), preserved=None)
    assert len(copies) == 1
    assert copies[0].expired_at is not None
    assert copies[0].attributes == {}


@pytest.mark.asyncio
async def test_overlapping_contradiction_still_wins_over_the_stamp(monkeypatch):
    mention = _mention(PARAPHRASE, "m-contra")
    responses = {PARAPHRASE: {"duplicate_facts": [0], "contradicted_facts": [1]}}
    copies = await _resolve_batch(monkeypatch, [mention], responses, results=())
    assert len(copies) == 1
    assert copies[0].expired_at is not None
    assert copies[0].invalid_at == MILAN_START


@pytest.mark.asyncio
@pytest.mark.parametrize("exact_first", [True, False])
async def test_two_restatements_of_one_stored_edge_all_stay_live_and_stamped(monkeypatch, exact_first):
    exact = _mention(STORED_FACT, "m-exact")
    paraphrase = _mention(PARAPHRASE, "m-para")
    mentions = [exact, paraphrase] if exact_first else [paraphrase, exact]
    copies = await _resolve_batch(monkeypatch, mentions, _duplicate_of_stored(PARAPHRASE), results=())
    assert len(copies) == 2
    for edge in copies:
        _assert_live_and_stamped(edge)
