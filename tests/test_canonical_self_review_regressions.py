"""PR #46 regressions across mixed extraction and subsequent hydration calls.

Only model and I/O boundaries are injected. The declaration, binding, pruning and
hydration guards under test are the production functions, not transcribed copies.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
import graphiti_core.utils.maintenance.combined_extraction as combined

from menhir.domain.self_authority import SelfAuthorizationDecision
from menhir.domain.self_identity import (
    self_context_for_pending_episode,
    self_subject_endpoint_for_claim,
    self_uuid_for_namespace,
)
from menhir.infrastructure import graphiti_extraction_patches as extraction
from menhir.infrastructure import graphiti_model_patches as models
from menhir.infrastructure.self_binding import SelfBindMode

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_receipt():
    extraction.clear_extraction_receipt()
    yield
    extraction.clear_extraction_receipt()


def _receipt(text: str, *, authorized: bool = False):
    endpoint = self_subject_endpoint_for_claim({
        "uuid": "projection", "content": text, "source": "user", "namespace": "default",
        "diff": None, "subject_endpoint_eligible": True, "is_evidence_projection": True,
        "evidence_projection_of": "turn", "turn_evidence_count": 1,
        "turn_evidence_uuid": "turn", "turn_evidence_role": "user",
        "turn_evidence_declarant": "user", "turn_evidence_text": text,
        "turn_evidence_namespace": "default",
    })
    assert endpoint is not None
    identity = self_context_for_pending_episode(
        source="user", namespace="default", episode_uuid="projection",
        turn_evidence_uuid="turn", principal_id="owner",
    )
    receipt = extraction.begin_extraction_receipt(
        "projection", text, self_identity=identity, self_subject_endpoint=endpoint,
        self_bind_mode=SelfBindMode.ENFORCE,
        self_assertion_authorizer=SimpleNamespace(authorize=lambda proposal: SelfAuthorizationDecision(
            authorized, "owner_signature_verified" if authorized else "confirmation_no_exact_match"
        )),
    )
    receipt.graphiti_episode_uuid = "graphiti-episode"
    return receipt, endpoint


def _node(uuid: str, name: str) -> EntityNode:
    return EntityNode(
        uuid=uuid, name=name, group_id="", labels=["Entity"], summary="",
        created_at=datetime.now(timezone.utc),
    )


def _edge(source: str, target: str, fact: str) -> EntityEdge:
    return EntityEdge(
        source_node_uuid=source, target_node_uuid=target, name="OWNS", fact=fact,
        group_id="", episodes=["graphiti-episode"], created_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_payload_cannot_hide_an_unsigned_author_reference(authorized, reverse):
    receipt, endpoint = _receipt("I own postcards. I also own stamps.", authorized=authorized)
    nodes = [_node("marker", endpoint.marker), _node("postcards", "postcards"),
             _node("fallback", "user"), _node("stamps", "stamps")]
    marked = _edge("marker", "postcards", f"{endpoint.marker} owns postcards.")
    unmarked = _edge("stamps", "fallback", "Stamps belong to user.") if reverse else _edge(
        "fallback", "stamps", "user owns stamps."
    )
    edges = [marked, unmarked]
    index_map = {node.uuid: [0] for node in nodes}

    receipt.self_bind_result = extraction._record_self_binding(nodes, edges, index_map, receipt)

    # Before candidate acquisition, the ambiguous identity and its otherwise orphaned target
    # have gone. One valid marker does not turn the other edge into an ordinary fact.
    assert "fallback" not in {node.uuid for node in nodes}
    assert "stamps" not in {node.uuid for node in nodes}
    assert "fallback" not in index_map and "stamps" not in index_map
    assert edges == [marked]
    assert receipt.self_assertion_pending_edges == [marked]
    assert receipt.self_bind_result.bound
    receipt.resolved_node_identity_by_extracted_uuid["postcards"] = (
        "postcards-persistent", "postcards", ("Entity",)
    )
    receipt.resolved_node_was_persistent_by_extracted_uuid["postcards"] = True
    extraction.finalize_self_assertion_authority_after_node_resolution(receipt)
    assert edges == ([marked] if authorized else [])
    assert receipt.self_assertions_authorized == int(authorized)
    assert any(row.get("kind") == "unresolved_author_reference" for row in receipt.self_assertion_proposals)


def test_orphan_author_alias_cannot_enter_resolution_alongside_a_marker():
    receipt, endpoint = _receipt("I own postcards.")
    nodes = [_node("marker", endpoint.marker), _node("postcards", "postcards"),
             _node("fallback", "user")]
    edges = [_edge("marker", "postcards", f"{endpoint.marker} owns postcards.")]
    index_map = {node.uuid: [0] for node in nodes}

    extraction._record_self_binding(nodes, edges, index_map, receipt)

    assert "fallback" not in {node.uuid for node in nodes}
    assert "fallback" not in index_map


def test_named_user_counterpart_on_a_marked_edge_still_needs_exact_confirmation():
    receipt, endpoint = _receipt("I supervise the application user.", authorized=True)
    ordinary = _node("application-user", "user")
    nodes = [_node("marker", endpoint.marker), ordinary]
    edge = _edge("marker", ordinary.uuid, f"{endpoint.marker} supervises the application user.")
    edges = [edge]
    index_map = {node.uuid: [0] for node in nodes}

    receipt.self_bind_result = extraction._record_self_binding(nodes, edges, index_map, receipt)
    assert ordinary.uuid == "application-user" and ordinary.name == "user"
    assert edges == [edge] and receipt.self_assertion_pending_edges == [edge]
    receipt.resolved_node_identity_by_extracted_uuid[ordinary.uuid] = (
        "persistent-application-user", "user", ("Entity",)
    )
    receipt.resolved_node_was_persistent_by_extracted_uuid[ordinary.uuid] = True
    extraction.finalize_self_assertion_authority_after_node_resolution(receipt)
    assert receipt.self_assertions_authorized == 1
    assert edge.source_node_uuid == self_uuid_for_namespace("default")
    assert edge.target_node_uuid == "application-user"  # ordinary resolver maps this later


def test_pure_third_person_user_remains_ordinary():
    receipt, _ = _receipt("The application user has read access.")
    ordinary = _node("application-user", "user")
    nodes = [ordinary, _node("access", "read access")]
    edge = _edge(ordinary.uuid, "access", "The application user has read access.")
    edges = [edge]
    index_map = {node.uuid: [0] for node in nodes}

    result = extraction._record_self_binding(nodes, edges, index_map, receipt)

    assert not result.bound
    assert nodes[0] is ordinary and ordinary.uuid == "application-user"
    assert edges == [edge]
    assert not receipt.self_assertion_pending_edges


@pytest.mark.asyncio
async def test_mixed_payload_gets_one_correction_then_whole_payload_quarantine(monkeypatch):
    receipt, endpoint = _receipt("I own postcards. I also own stamps.")
    nodes = [_node("marker", endpoint.marker), _node("postcards", "postcards"),
             _node("fallback", "user"), _node("stamps", "stamps")]
    marked = _edge("marker", "postcards", f"{endpoint.marker} owns postcards.")
    unmarked = _edge("fallback", "stamps", "user owns stamps.")
    calls = []

    async def extract(*args, **kwargs):
        calls.append(kwargs["custom_extraction_instructions"])
        return list(nodes), [marked, unmarked], {node.uuid: [0] for node in nodes}

    monkeypatch.setattr(combined, "extract_nodes_and_edges", extract)
    final_nodes, final_edges, index_map = await extraction._run_graphiti_combined_extraction(
        object(), SimpleNamespace(uuid="graphiti-episode"), [], None, None, None,
    )

    assert len(calls) == 2
    assert "CORRECTION" in calls[-1]
    assert "fallback" not in {node.uuid for node in final_nodes}
    assert "fallback" not in index_map
    assert final_edges == [marked]
    assert receipt.self_assertion_pending_edges == [marked]


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword_arguments", [False, True])
async def test_fresh_later_receipt_cannot_resummarize_rejected_history(keyword_arguments):
    original = AsyncMock()
    embed = AsyncMock()
    wrapped = models._wrap_self_authority_node_hydration(original, embed)
    node = _node("postcards", "postcards")
    node.summary = "Existing ordinary description."
    node.attributes = {"ordinary": "preserved"}
    clients = SimpleNamespace(embedder=object())
    rejected = SimpleNamespace(content="I own 37 postcards.", uuid="earlier")
    earlier = extraction.begin_extraction_receipt(
        "earlier", rejected.content, self_bind_mode=SelfBindMode.ENFORCE
    )
    earlier.suppress_node_semantic_hydration = True
    await wrapped(clients, [node], rejected, [])
    extraction.clear_extraction_receipt()
    later = extraction.begin_extraction_receipt(
        "later", "The postcards are blue.", self_bind_mode=SelfBindMode.ENFORCE
    )
    assert not later.suppress_node_semantic_hydration

    async def contaminate(*args, **kwargs):
        node.summary = "The user owns 37 postcards."
        node.attributes["owner"] = "user"
        return [node]

    original.side_effect = contaminate
    current = SimpleNamespace(content=later.episode_text, uuid="later")
    if keyword_arguments:
        result = await wrapped(clients, [node], episode=current, previous_episodes=[rejected])
    else:
        result = await wrapped(clients, [node], current, [rejected])

    original.assert_not_awaited()
    assert embed.await_count == 2
    assert result == [node]
    assert node.summary == "Existing ordinary description."
    assert node.attributes == {"ordinary": "preserved"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [SelfBindMode.OFF, SelfBindMode.OBSERVE])
async def test_non_enforcing_hydration_retains_original_call_contract(mode):
    extraction.begin_extraction_receipt("ordinary", "Ordinary prose.", self_bind_mode=mode)
    original = AsyncMock(return_value=["original-result"])
    embed = AsyncMock()
    wrapped = models._wrap_self_authority_node_hydration(original, embed)
    clients, nodes, episode, previous = object(), [], object(), [object()]

    assert await wrapped(clients, nodes, episode, previous, edges=[]) == ["original-result"]
    original.assert_awaited_once_with(clients, nodes, episode, previous, edges=[])
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_hydration_enforcement_is_task_local_not_a_global_toggle():
    original = AsyncMock(side_effect=lambda clients, nodes, **kwargs: nodes)
    embed = AsyncMock()
    wrapped = models._wrap_self_authority_node_hydration(original, embed)

    async def hydrate(mode):
        extraction.begin_extraction_receipt(str(mode), "Ordinary prose.", self_bind_mode=mode)
        await asyncio.sleep(0)
        node = _node(str(mode), str(mode))
        await wrapped(SimpleNamespace(embedder=object()), [node], previous_episodes=[])

    await asyncio.gather(hydrate(SelfBindMode.ENFORCE), hydrate(SelfBindMode.OFF))
    assert original.await_count == 1
    assert original.await_args.args[1][0].uuid == "off"
    assert embed.await_count == 1
    assert embed.await_args.args[1][0].uuid == "enforce"


@pytest.mark.asyncio
async def test_quarantined_alias_never_reaches_real_node_resolver(monkeypatch) -> None:
    import graphiti_core.graphiti as graphiti_module
    import graphiti_core.utils.bulk_utils as bulk
    import graphiti_core.utils.maintenance.node_operations as operations

    receipt, endpoint = _receipt("I own postcards. I also own stamps.")
    nodes = [_node("marker", endpoint.marker), _node("postcards", "postcards"),
             _node("fallback", "user"), _node("stamps", "stamps")]
    edges = [_edge("marker", "postcards", f"{endpoint.marker} owns postcards."),
             _edge("fallback", "stamps", "user owns stamps.")]
    receipt.self_bind_result = extraction._record_self_binding(
        nodes, edges, {node.uuid: [0] for node in nodes}, receipt
    )
    persistent = _node("postcards-persistent", "postcards")
    collect = AsyncMock(return_value=[[persistent]])
    monkeypatch.setattr(operations, "_collect_candidate_nodes", collect)
    monkeypatch.setattr(models, "_existing_canonical_node", AsyncMock(return_value=nodes[0]))
    # Restore all patch-installed globals after this test; exercise the production resolver,
    # not a hand-built approximation of the candidate and finalization order.
    for module in (operations, graphiti_module, bulk):
        monkeypatch.setattr(module, "resolve_extracted_nodes", module.resolve_extracted_nodes)
    monkeypatch.setattr(operations, "_menhir_adaptive_dedupe_patched", False, raising=False)
    assert models._patch_graphiti_adaptive_dedupe()
    clients = SimpleNamespace(llm_client=AsyncMock(), embedder=AsyncMock(), driver=object())

    resolved, uuid_map, _ = await operations.resolve_extracted_nodes(clients, nodes)

    searched = collect.await_args.args[1]
    assert [node.uuid for node in searched] == ["postcards"]
    assert "fallback" not in uuid_map and "stamps" not in uuid_map
    assert [node.uuid for node in resolved] == [self_uuid_for_namespace("default")]
    assert edges == []  # the marked proposal also lacked owner confirmation
    clients.llm_client.generate_response.assert_not_awaited()


def _seed_test_context(monkeypatch, *, rows=None, state="READY"):
    from unittest.mock import MagicMock
    from tests import test_canonical_self_endpoint_e2e as e2e

    request = MagicMock(return_value={"episode_id": "seed-episode"})
    monkeypatch.setattr(e2e, "_request", request)
    session = MagicMock()
    ready = SimpleNamespace(single=lambda: {"state": state, "error": "test failure"})
    records = rows if rows is not None else [{"uuid": "persistent-cobalt", "embedding": [0.1, 0.2]}]
    session.run.side_effect = [ready, records]
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    server = SimpleNamespace(base_url="http://127.0.0.1:18123", tail_log=lambda: "test log")
    return e2e, server, driver, session, request


def test_e2e_counterpart_is_seeded_by_the_candidate_app_not_raw_create(monkeypatch) -> None:
    e2e, server, driver, session, request = _seed_test_context(monkeypatch)

    assert e2e._seed_counterpart_through_public_ingest(server, driver, "test-ns") == "persistent-cobalt"

    request.assert_called_once_with(server.base_url, "POST", "/api/memory", {
        "episode": "Project Cobalt is a software project.", "source": "claude-code",
        "session_id": "canonical-self-e2e-session", "user_id": "canonical-self-e2e",
        "namespace": "test-ns",
    })
    assert all("CREATE" not in call.args[0] for call in session.run.call_args_list)
    query = session.run.call_args_list[-1]
    assert "MENTIONS" in query.args[0] and "resolved_episode_uuid" in query.args[0]
    assert query.kwargs == {"pending": "seed-episode", "name": "Project Cobalt", "group": "test-ns"}


@pytest.mark.parametrize("vector", [None, [], [float("nan")], [True], ["0.1"]])
def test_e2e_seed_refuses_unsearchable_counterpart(monkeypatch, vector) -> None:
    e2e, server, driver, _, _ = _seed_test_context(
        monkeypatch, rows=[{"uuid": "persistent-cobalt", "embedding": vector}]
    )
    with pytest.raises(AssertionError, match="searchable name embedding"):
        e2e._seed_counterpart_through_public_ingest(server, driver, "test-ns")


@pytest.mark.parametrize("rows", [[], [{"uuid": "A"}, {"uuid": "B"}]])
def test_e2e_seed_refuses_missing_or_ambiguous_identity(monkeypatch, rows) -> None:
    e2e, server, driver, _, _ = _seed_test_context(monkeypatch, rows=rows)
    with pytest.raises(AssertionError, match="one exact episode-linked identity"):
        e2e._seed_counterpart_through_public_ingest(server, driver, "test-ns")


def test_e2e_seed_fails_visibly_when_normal_enrichment_fails(monkeypatch) -> None:
    e2e, server, driver, _, _ = _seed_test_context(monkeypatch, state="FAILED")
    with pytest.raises(AssertionError, match="counterpart seed state='FAILED'"):
        e2e._seed_counterpart_through_public_ingest(server, driver, "test-ns")
