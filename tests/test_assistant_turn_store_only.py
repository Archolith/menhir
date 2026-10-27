"""Assistant-turn policy: a raw assistant turn is stored and embedded, never extracted (#168).

Safety property: with MENHIR_ASSISTANT_TURNS_STORE_ONLY_ENABLED on, no episode whose turn role
resolves to ``assistant`` (the role extraction itself would use) reaches Graphiti extraction, and
the store-only mode lands in the same fenced write as READY. The Cypher half is proven in
`test_assistant_turn_store_only_live.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from menhir.config.feature_flags import FEATURES
from menhir.config.settings_model import MemorySettings
from menhir.domain.ingest import EXTRACTION_MODE_STORE_ONLY
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.services import ingest_worker as iw
from menhir.services.ingest_queue import IngestQueueMixin
from tests.test_store_only_ingest import (
    CLAIM,
    _Adapter,
    _intake_adapter,
    _Recording,
    _Svc,
    _queue,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def steps(monkeypatch):
    rec = SimpleNamespace(order=[])
    monkeypatch.setattr(iw, "EnrichmentContext", lambda **kw: SimpleNamespace(**kw))
    for attr, name, result in (
        ("embed_episode_content", "embed", None), ("try_reconcile_existing", "reconcile", False),
        ("run_preflight_rejection", "preflight", False), ("run_graphiti_extraction", "extraction", None),
        ("handle_enrichment_failure", "failure", None),
    ):
        monkeypatch.setattr(iw, attr, _recorder(rec, name, result))
    return rec


def _recorder(rec: SimpleNamespace, name: str, result: Any):
    async def run(*a: Any, **kw: Any) -> Any:
        rec.order.append(name)
        return result
    return run

ASSISTANT_TEXT = "Multnomah Falls is 2.4 miles round trip."
NORMAL = {k: v for k, v in CLAIM.items() if k != "extraction_mode"}


def _claim(**kw: Any) -> dict[str, Any]:
    return {**NORMAL, "turn_evidence_count": 0, "content": ASSISTANT_TEXT, **kw}


def _proven(role: str, **kw: Any) -> dict[str, Any]:
    # A linked :TurnEvidence whose text is the episode's: resolve_turn_role trusts its role.
    return _claim(**{"turn_evidence_count": 1, "turn_evidence_text": ASSISTANT_TEXT,
                     "turn_evidence_role": role, "turn_evidence_source_kind": "hook",
                     "namespace": "ns", "turn_evidence_namespace": "ns", **kw})


async def _run(claimed: dict[str, Any], *, policy: bool, finished: bool = True) -> _Svc:
    svc = _Svc(_Adapter(claimed, finished=finished))
    svc._assistant_turns_store_only = policy
    await svc._process_pending_episode("ep-1")
    return svc


def _store_only_calls(svc: _Svc) -> list[tuple[str, tuple, dict]]:
    return [c for c in svc.graph_adapter.calls if c[0] == "mark_episode_store_only"]


# --- worker gate ------------------------------------------------------------------------------

async def test_declared_assistant_turn_is_embedded_and_finished_store_only(steps) -> None:
    svc = await _run(_claim(turn_role="assistant"), policy=True)
    assert steps.order == ["embed"]
    assert not svc.budget_checked
    (_, args, kwargs), = _store_only_calls(svc)
    assert args == ("ep-1",)
    assert kwargs == {"worker_id": "worker-A", "claim_started_at": "STARTED-TOKEN",
                      "by_turn_role": True}


async def test_policy_off_extracts_the_assistant_turn(steps) -> None:
    svc = await _run(_claim(turn_role="assistant"), policy=False)
    assert steps.order[-1] == "extraction"
    assert not _store_only_calls(svc)


@pytest.mark.parametrize("role", ["user", "agent", "tool", "system", None])
async def test_other_roles_still_extract(steps, role) -> None:
    claimed = _claim(turn_role=role) if role else _claim()
    svc = await _run(claimed, policy=True)
    assert steps.order[-1] == "extraction"
    assert not _store_only_calls(svc)


async def test_undeclared_turn_proven_assistant_by_its_evidence_is_store_only(steps) -> None:
    # A client that recorded the turn but sent no role: the evidence decides, as in extraction.
    svc = await _run(_proven("assistant"), policy=True)
    assert steps.order == ["embed"]
    assert _store_only_calls(svc)[0][2]["by_turn_role"] is True


async def test_evidence_proving_a_user_turn_beats_a_declared_assistant_role(steps) -> None:
    # Counterexample for a declared-role-only check: the text is the user's own captured words.
    svc = await _run(_proven("user", turn_role="assistant"), policy=True)
    assert steps.order[-1] == "extraction"
    assert not _store_only_calls(svc)


async def test_agent_quoted_evidence_proves_nothing(steps) -> None:
    claimed = _proven("assistant", turn_evidence_source_kind="agent_quoted", turn_role="user")
    svc = await _run(claimed, policy=True)
    assert steps.order[-1] == "extraction"
    assert not _store_only_calls(svc)


async def test_evidence_projection_is_never_reclassified(steps) -> None:
    svc = await _run(_claim(turn_role="assistant", is_evidence_projection=True), policy=True)
    assert not _store_only_calls(svc)


async def test_already_store_only_episode_keeps_its_unchanged_write(steps) -> None:
    svc = await _run(dict(CLAIM, turn_role="assistant"), policy=True)
    (_, _, kwargs), = _store_only_calls(svc)
    assert "by_turn_role" not in kwargs


async def test_lost_fence_on_a_policy_finish_never_extracts(steps) -> None:
    svc = await _run(_claim(turn_role="assistant"), policy=True, finished=False)
    assert "extraction" not in steps.order
    assert _store_only_calls(svc)


# --- the fenced write -------------------------------------------------------------------------

def test_policy_write_stamps_the_mode_in_the_fenced_ready_set() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = _Recording()
    repo.mark_episode_store_only("ep-1", worker_id="w", claim_started_at="T", by_turn_role=True)
    query, params = repo.neo4j.calls[0]
    assert "n.processing_owner = $worker_id" in query
    assert "(n.extraction_mode = $store_only OR $by_turn_role)" in query
    assert "SET n.extraction_mode = $store_only," in query
    assert params["by_turn_role"] is True
    repo.neo4j = _Recording()
    repo.mark_episode_store_only("ep-1", worker_id="w", claim_started_at="T")
    assert repo.neo4j.calls[0][1]["by_turn_role"] is False


# --- intake: the bench's new request against its old one ----------------------------------------

async def test_role_only_request_differs_from_the_old_store_only_request_only_in_mode_and_digest() -> None:
    old, new = _intake_adapter(), _intake_adapter()
    await _queue(old, role="assistant", store_only=True, idempotency_key="k")
    await _queue(new, role="assistant", idempotency_key="k")
    old_kw = dict(old.create_pending_episode.call_args.kwargs)
    new_kw = dict(new.create_pending_episode.call_args.kwargs)
    assert old_kw.pop("extraction_mode") == EXTRACTION_MODE_STORE_ONLY
    assert old_kw.pop("ingest_request_sha256") != new_kw.pop("ingest_request_sha256")
    old_kw.pop("episode_uuid"), new_kw.pop("episode_uuid")
    old_kw.pop("name"), new_kw.pop("name")
    assert old_kw == new_kw
    assert new_kw["turn_role"] == "assistant"
    assert old.link_episode_admission.call_count == new.link_episode_admission.call_count == 1


# --- settings ---------------------------------------------------------------------------------

def test_settings_default_on_env_parsed_and_registered(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_ASSISTANT_TURNS_STORE_ONLY_ENABLED", raising=False)
    assert MemorySettings().assistant_turns_store_only_enabled is True
    assert MemorySettings.from_env().assistant_turns_store_only_enabled is True
    monkeypatch.setenv("MENHIR_ASSISTANT_TURNS_STORE_ONLY_ENABLED", "false")
    assert MemorySettings.from_env().assistant_turns_store_only_enabled is False
    flag = FEATURES["assistant_turns_store_only_enabled"]
    assert flag.env_var == "MENHIR_ASSISTANT_TURNS_STORE_ONLY_ENABLED" and flag.default is True


def test_configure_sets_the_worker_field() -> None:
    class _Q(IngestQueueMixin):
        pass

    q = _Q()
    q._assistant_turns_store_only = False
    q.configure(assistant_turns_store_only=True)
    assert q._assistant_turns_store_only is True
