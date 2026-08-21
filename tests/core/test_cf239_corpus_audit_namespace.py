"""CF-239: the corpus audit accepts a namespace and never passes it on.

`fetch_artifact_corpus_audit` declared `namespace` and then omitted it from the very adapter
call it exists to scope, so both the in-process transport and the HTTP route (which dispatches
onto this same RuntimeProvider method by operation name) silently dropped the pin.

Built as a minimal object that records what it received, the way the defect reproduction did:
a fake graph_adapter records kwargs and a tiny RuntimeProviderAdminOpsMixin subclass forwards
to it via `_off_loop`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.core.backend_runtime_admin_ops import RuntimeProviderAdminOpsMixin


class _RecordingAdapter:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    def fetch_artifact_corpus_audit(self, **kwargs: object) -> dict[str, object]:
        self.kwargs = dict(kwargs)
        return {"counts": {}}


class _Runtime(RuntimeProviderAdminOpsMixin):
    def __init__(self, adapter: _RecordingAdapter) -> None:
        self.built = SimpleNamespace(graph_adapter=adapter)

    async def _off_loop(self, fn, *args: object, **kwargs: object) -> object:
        return fn(*args, **kwargs)


def _runtime() -> tuple[_RecordingAdapter, _Runtime]:
    adapter = _RecordingAdapter()
    return adapter, _Runtime(adapter)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_namespace_reaches_the_adapter() -> None:
    adapter, runtime = _runtime()

    await runtime.fetch_artifact_corpus_audit(
        repo_path="/r",
        repository="repo",
        namespace="tenant-a",
    )

    assert adapter.kwargs is not None
    assert adapter.kwargs["namespace"] == "tenant-a"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unpinned_call_still_invokes_adapter_unchanged() -> None:
    adapter, runtime = _runtime()

    await runtime.fetch_artifact_corpus_audit(repo_path="/r", repository="repo")

    # Positive control: an unpinned caller must still hit the adapter, and the kwargs must be
    # exactly the full expected dict including namespace=None -- otherwise a fix that always
    # passed a hardcoded string would pass the namespace test above.
    assert adapter.kwargs == {
        "repo_path": "/r",
        "repository": "repo",
        "from_commit": None,
        "conflict_limit": 25,
        "namespace": None,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_other_arguments_still_forwarded() -> None:
    adapter, runtime = _runtime()

    await runtime.fetch_artifact_corpus_audit(
        repo_path="/r",
        repository="repo",
        from_commit="abc123",
        conflict_limit=7,
        namespace="tenant-a",
    )

    assert adapter.kwargs is not None
    assert adapter.kwargs["from_commit"] == "abc123"
    assert adapter.kwargs["conflict_limit"] == 7
    assert adapter.kwargs["repo_path"] == "/r"
    assert adapter.kwargs["repository"] == "repo"
