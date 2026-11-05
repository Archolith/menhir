"""#401 follow-up: close_todo and delete_todo refuse a pinned caller's foreign todo.

Both are reachable from `/api/internal/backend/{operation}`, which injects a namespace only into
methods that declare one; neither does, so without the backend guard a client pinned to one
namespace could close or delete another namespace's todo by uuid. supersede/resolve/reopen/link
already call `_require_own_todo`; these two did not.

Same minimal-runtime shape as tests/core/test_cf239_corpus_audit_namespace.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from menhir.core.backend_runtime_admin_ops import RuntimeProviderAdminOpsMixin

_DEFAULT = "default"


class _Adapter:
    def __init__(self, todos: dict[str, str]) -> None:
        self.todos = todos  # uuid -> namespace
        self.lookups: list[tuple[str, str | None]] = []
        self.writes: list[tuple[str, str]] = []

    def get_todo(self, uuid: str, *, namespace: str | None = None) -> dict | None:
        self.lookups.append((uuid, namespace))
        ns = self.todos.get(uuid)
        if ns is None:
            return None
        if namespace and ns not in (namespace, _DEFAULT):
            return None
        return {"uuid": uuid, "namespace": ns}

    def close_todo(self, uuid: str) -> bool:
        self.writes.append(("close_todo", uuid))
        return True

    def delete_todo(self, uuid: str) -> bool:
        self.writes.append(("delete_todo", uuid))
        return True


class _Runtime(RuntimeProviderAdminOpsMixin):
    def __init__(self, adapter: _Adapter) -> None:
        self.built = SimpleNamespace(graph_adapter=adapter)

    async def _off_loop(self, fn, *args: object, **kwargs: object) -> object:
        return fn(*args, **kwargs)


def _pinned(ns: str):
    return patch("menhir.core.tenancy.pinned_namespace", return_value=ns)


OPS = ("close_todo", "delete_todo")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("op", OPS)
async def test_pinned_caller_cannot_touch_another_namespaces_todo(op: str) -> None:
    adapter = _Adapter({"theirs": "tenant-b"})
    with _pinned("tenant-a"), pytest.raises(PermissionError, match="outside namespace tenant-a"):
        await getattr(_Runtime(adapter), op)("theirs")
    assert adapter.writes == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("owner", ["tenant-a", _DEFAULT])
async def test_pinned_caller_can_touch_own_or_default_todo(op: str, owner: str) -> None:
    adapter = _Adapter({"t1": owner})
    with _pinned("tenant-a"):
        assert await getattr(_Runtime(adapter), op)("t1") is True
    assert adapter.writes == [(op, "t1")]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("op", OPS)
async def test_unpinned_caller_is_unchanged_and_skips_the_lookup(op: str) -> None:
    adapter = _Adapter({"theirs": "tenant-b"})
    with _pinned(""):
        assert await getattr(_Runtime(adapter), op)("theirs") is True
    assert adapter.lookups == []
    assert adapter.writes == [(op, "theirs")]
