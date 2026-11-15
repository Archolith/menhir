"""The corpus audit walks a caller-named server directory, so it is confined like ingest.

Before this, `fetch_artifact_corpus_audit` passed `repo_path` straight to the scanner and sat in
the internal dispatch's readonly remainder, so a read-only key could name any directory the server
can read and get back its markdown paths, statuses, titles and commit ids.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.api.routes import _required_tier_for_operation
from menhir.core.backend_runtime_admin_ops import RuntimeProviderAdminOpsMixin
from menhir.core.ingest_guard import IngestPathNotAllowedError
from menhir.mcp.service_access import bind_request_tier, reset_request_tier

pytestmark = pytest.mark.unit


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def fetch_artifact_corpus_audit(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return {"counts": {}}


class _Runtime(RuntimeProviderAdminOpsMixin):
    def __init__(self, adapter: _Adapter) -> None:
        self.built = SimpleNamespace(graph_adapter=adapter)

    async def _off_loop(self, fn, *args: object, **kwargs: object) -> object:
        return fn(*args, **kwargs)


async def _audit(tier: str, repo: Path) -> _Adapter:
    adapter = _Adapter()
    token = bind_request_tier(tier)
    try:
        await _Runtime(adapter).fetch_artifact_corpus_audit(repo_path=str(repo), repository="r")
    finally:
        reset_request_tier(token)
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["readonly", "agent"])
async def test_confined_tiers_cannot_audit_outside_the_allowed_roots(
    tier: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("MENHIR_INGEST_ALLOWED_ROOTS", str(allowed))
    with pytest.raises(IngestPathNotAllowedError):
        await _audit(tier, outside)


@pytest.mark.asyncio
async def test_agent_can_audit_inside_an_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("MENHIR_INGEST_ALLOWED_ROOTS", str(tmp_path))
    adapter = await _audit("agent", repo)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_operator_keeps_its_reach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENHIR_INGEST_ALLOWED_ROOTS", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    adapter = await _audit("operator", repo)
    assert len(adapter.calls) == 1


def test_dispatch_requires_agent_tier_for_the_audit() -> None:
    assert _required_tier_for_operation("fetch_artifact_corpus_audit") == "agent"
