"""#872: merge proposals never block the event loop; no dead scalar hook; no private adapter access.

Counterexamples against 4794b41f: ``_handle_merge_proposal`` called fetch_entity_merge_metadata and
the three veto checks synchronously on the loop thread; ``on_merge_committed`` was a constructor
parameter no production caller set; enrichment passed ``ctx.graph_adapter._correlation``.
"""

from __future__ import annotations

import inspect
import re
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import menhir
from menhir.services.correlation_service import CorrelationService

pytestmark = pytest.mark.unit

_REPO_READS = (
    "fetch_entity_merge_metadata",
    "check_ineligible_node_veto",
    "check_co_mention_veto",
    "check_anchor_project_veto",
)


@pytest.mark.asyncio
async def test_merge_proposal_reads_run_off_the_event_loop_thread() -> None:
    loop_thread = threading.get_ident()
    threads: dict[str, int] = {}
    repo = MagicMock()

    def _record(name, value):
        def _call(*_args, **_kwargs):
            threads[name] = threading.get_ident()
            return value
        return _call

    repo.fetch_entity_merge_metadata.side_effect = _record(
        "fetch_entity_merge_metadata",
        [{"uuid": "s", "name": "S", "scope": "PERSISTENT"},
         {"uuid": "a", "name": "A", "scope": "SESSION"}],
    )
    for name in _REPO_READS[1:]:
        getattr(repo, name).side_effect = _record(name, False)
    llm = MagicMock()
    llm.confirm_same_entity = AsyncMock(return_value=True)
    svc = CorrelationService(repo, MagicMock(), llm=llm)

    assert await svc._handle_merge_proposal("s", "a", 0.97) == "merged"

    assert set(threads) == set(_REPO_READS)
    on_loop = [name for name, ident in threads.items() if ident == loop_thread]
    assert on_loop == [], f"blocking repository calls on the event loop: {on_loop}"


@pytest.mark.asyncio
async def test_a_veto_read_off_the_loop_still_vetoes() -> None:
    repo = MagicMock()
    repo.fetch_entity_merge_metadata.return_value = []
    repo.check_ineligible_node_veto.return_value = False
    repo.check_co_mention_veto.return_value = True
    repo.check_anchor_project_veto.return_value = False
    llm = MagicMock()
    llm.confirm_same_entity = AsyncMock(return_value=True)
    svc = CorrelationService(repo, MagicMock(), llm=llm)

    assert await svc._handle_merge_proposal("s", "a", 0.97) == "conflict"
    llm.confirm_same_entity.assert_not_awaited()


def test_the_unwired_scalar_hook_is_gone() -> None:
    params = inspect.signature(CorrelationService.__init__).parameters
    assert "on_merge_committed" not in params
    assert not hasattr(CorrelationService(MagicMock(), MagicMock()), "_on_merge_committed")


def test_no_private_correlation_repo_access_outside_the_adapter() -> None:
    src = Path(menhir.__file__).parent
    pattern = re.compile(r"\._correlation\b(?!_)")
    offenders = []
    for path in src.rglob("*.py"):
        if path.name == "memory_graph_adapter.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pattern.search(code) and "``" not in code:
                offenders.append(f"{path.relative_to(src)}:{lineno}")
    assert offenders == []
