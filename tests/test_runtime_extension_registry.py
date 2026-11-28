"""Registry consistency for the product graph entry points."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from examples.decision_timeline import EXTENSION
from menhir import extensions as extension_module
from menhir.api import server
from menhir.cli import bootstrap as hook_bootstrap
from menhir.config import MemorySettings
from menhir.core import bootstrap
from menhir.explorer.app import create_app
from menhir.extensions import RuntimeExtensions


@pytest.fixture(autouse=True)
def isolated_process_registry(monkeypatch) -> None:
    monkeypatch.setattr(extension_module, "_process_registry", extension_module._UNBOUND)


@pytest.mark.parametrize("first", ["hook", "explorer"])
@pytest.mark.asyncio
async def test_hook_and_explorer_share_the_first_registry(monkeypatch, first: str) -> None:
    registry = RuntimeExtensions((EXTENSION,))
    graph = object()
    monkeypatch.setattr(hook_bootstrap, "Neo4jRepository", lambda **_kwargs: graph)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("graph-only test")

    monkeypatch.setattr(bootstrap.GraphitiClient, "from_settings", unavailable)
    settings = MemorySettings(backend_url="")
    hook = hook_bootstrap.build_hook_services(settings, extensions=registry) if first == "hook" else None
    app = create_app(settings=settings, repo=graph, extensions=registry if first == "explorer" else None)
    async with app.router.lifespan_context(app):
        if hook is None:
            hook = hook_bootstrap.build_hook_services(settings)
        explorer = app.state.candidate_service.graph_adapter
        for adapter in (hook.graph_adapter, explorer):
            assert adapter.view_kinds is registry.view_kinds
            assert adapter._views.KINDS == registry.view_kinds
    assert bootstrap.build_graph_adapter(graph, settings).view_kinds is registry.view_kinds


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.asyncio
async def test_other_graph_surfaces_reject_conflicting_active_registry_before_connecting(
    monkeypatch, registered: bool,
) -> None:
    settings = MemorySettings(backend_url="")
    first_registry = RuntimeExtensions((EXTENSION,)) if registered else RuntimeExtensions(())
    first_adapter = bootstrap.build_graph_adapter(object(), settings, extensions=first_registry)
    if not registered:
        assert first_adapter.view_kinds is None
        assert EXTENSION.view_kind.name not in first_adapter._views.KINDS
    requested = RuntimeExtensions((EXTENSION,))

    def unexpected(*_args, **_kwargs):
        pytest.fail("conflicting graph surface should fail before constructing a repository")

    monkeypatch.setattr(hook_bootstrap, "Neo4jRepository", unexpected)
    monkeypatch.setattr("menhir.explorer.app._repo_from_settings", unexpected)
    monkeypatch.setattr(server, "create_mcp_streamable_http_app", unexpected)
    with pytest.raises(ValueError, match="different runtime extension registry"):
        server.create_app(settings=settings, extensions=requested)
    with pytest.raises(ValueError, match="different runtime extension registry"):
        hook_bootstrap.build_hook_services(settings, extensions=requested)
    with pytest.raises(ValueError, match="different runtime extension registry"):
        requested.view_repository(object())
    app = create_app(settings=settings, extensions=requested)
    with pytest.raises(ValueError, match="different runtime extension registry"):
        async with app.router.lifespan_context(app):
            pytest.fail("conflicting Explorer registry was accepted")


def test_product_adapter_and_view_constructors_are_owned_by_the_factories() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "menhir"
    owners = {
        "MemoryGraphAdapter": "core/bootstrap.py",
        "ViewRepository": "infrastructure/view_repository.py",
    }
    calls = {name: [] for name in owners}
    for path in source.rglob("*.py"):
        contents = path.read_text(encoding="utf-8")
        if not any(name in contents for name in owners):
            continue
        tree = ast.parse(contents, filename=str(path))
        aliases = {
            item.asname or item.name: item.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for item in node.names if item.name in owners
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = aliases.get(node.func.id, node.func.id)
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            if name in owners:
                calls[name].append(path.relative_to(source).as_posix())
    assert calls == {name: [owner] for name, owner in owners.items()}
