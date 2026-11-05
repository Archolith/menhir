"""recall_timeline must list only recall-visible scalar_history Views.

Delete repair always rebuilds history, with ``recallable`` taken from the scalar-history flag. With
the flag off the rebuilt View is OPERATOR-audience, and the timeline must not hand it to an agent.
"""

from __future__ import annotations

import os
import uuid as uuidlib

import pytest

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.services.timeline_service import _typed_histories

from tests.test_scalar_deletion_live import _seed_scalar

pytestmark = [
    pytest.mark.online,
    pytest.mark.skipif(
        not os.getenv("MENHIR_TEST_NEO4J_URI"),
        reason="requires an explicit MENHIR_TEST_NEO4J_URI test instance",
    ),
]


def _record_second_assertion(repo, *, namespace: str, subject: str) -> str:
    text = "I own 40 coins"
    turn_id = TurnEvidenceRepository(repo).record_turn_evidence(
        text=text, role="user", declarant="user", namespace=namespace,
        source_kind="claude_code_hook", session_id=f"{subject}-session-2",
        prompt_id=f"{subject}-prompt-2",
    )["turn_id"]
    rec = TypedAssertionRepository(repo).record_assertion(TypedAssertion(
        subject_uuid=subject, subject_display="my coins", attribute="owned", scope="",
        value_kind="count", unit="", operation="absolute", value=40,
        stated_span=text, span_start=0, span_end=len(text), episode_uuid=turn_id,
        valid_at="2026-07-21T00:00:00+00:00", learned_at="2026-07-21T00:00:00+00:00",
        evidence_tier="user", perceiver_version="v1", namespace=namespace,
    ))
    assert rec["binding_pending"] is False
    return rec["assertion_id"]


def _current_history_audiences(repo, *, namespace: str, subject: str) -> list[str]:
    rows = repo.execute(
        "MATCH (v:Entity {view_kind:'scalar_history', view_subject_uuid:$subject, "
        "group_id:$namespace}) WHERE coalesce(v.view_current, true) "
        "RETURN v.view_audience AS audience",
        {"subject": subject, "namespace": namespace},
    )
    return [str(r["audience"]) for r in rows]


async def _timeline_scalar_histories(adapter, *, namespace: str, subject: str):
    histories, _note = await _typed_histories(
        adapter, subject_uuid=subject, gid=namespace, history_view=None,
        history_offset=None, history_limit=16,
    )
    return [h for h in histories if h.kind == "scalar"]


@pytest.fixture
def history_case(test_neo4j_repo):
    namespace = f"test-timeline-visibility-{uuidlib.uuid4().hex}"
    ids = _seed_scalar(test_neo4j_repo, namespace=namespace)
    yield test_neo4j_repo, namespace, ids
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.test_tag = $namespace OR n.namespace = $namespace "
        "OR n.group_id = $namespace DETACH DELETE n",
        params={"namespace": namespace},
    )


@pytest.mark.asyncio
async def test_recallable_history_view_is_listed(history_case):
    repo, namespace, ids = history_case
    adapter = MemoryGraphAdapter(neo4j=repo)

    assert _current_history_audiences(repo, namespace=namespace, subject=ids["subject"]) == ["RECALL"]
    histories = await _timeline_scalar_histories(adapter, namespace=namespace, subject=ids["subject"])
    assert len(histories) == 1
    assert [str(e.value) for e in histories[0].entries] == ["37"]


@pytest.mark.asyncio
async def test_operator_history_from_flag_off_delete_repair_is_not_listed(history_case):
    repo, namespace, ids = history_case
    adapter = MemoryGraphAdapter(neo4j=repo)
    second = _record_second_assertion(repo, namespace=namespace, subject=ids["subject"])

    # The adapter's delete path runs repair with the scalar-history flag at its default (off).
    assert adapter.delete_memory(second)

    assert _current_history_audiences(repo, namespace=namespace, subject=ids["subject"]) == ["OPERATOR"]
    assert await _timeline_scalar_histories(adapter, namespace=namespace, subject=ids["subject"]) == []
