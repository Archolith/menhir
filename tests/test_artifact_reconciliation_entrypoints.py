"""Repository identity is explicit at every reconciliation entry point."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from menhir.cli.artifacts import artifacts_app
from menhir.core.runtime import _run_startup_artifact_reconcile
from menhir.mcp.tools.ops.audit_artifact_corpus import audit_artifact_corpus


@pytest.mark.unit
@pytest.mark.parametrize("command", ["audit", "reconcile"])
def test_graph_backed_cli_commands_require_repository(command: str) -> None:
    result = CliRunner().invoke(artifacts_app, [command, "--repo", "."])
    assert result.exit_code == 2
    assert "repository" in result.output.lower()


@pytest.mark.unit
def test_mcp_audit_requires_repository_argument() -> None:
    parameter = inspect.signature(audit_artifact_corpus).parameters["repository"]
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.unit
def test_startup_reconciliation_skips_without_repository_identity(tmp_path) -> None:
    class _Adapter:
        called = False

        def fetch_artifact_corpus_audit(self, **kwargs):
            self.called = True
            raise AssertionError("startup must not audit without repository identity")

    adapter = _Adapter()
    settings = SimpleNamespace(
        artifact_reconcile_mode="audit",
        artifact_reconcile_repo=str(tmp_path),
        artifact_reconcile_repository="",
    )
    asyncio.run(
        _run_startup_artifact_reconcile(
            SimpleNamespace(graph_adapter=adapter), settings
        )
    )
    assert adapter.called is False


@pytest.mark.unit
def test_startup_audit_passes_explicit_repository_identity(tmp_path) -> None:
    class _Adapter:
        kwargs = None

        def fetch_artifact_corpus_audit(self, **kwargs):
            self.kwargs = kwargs
            return {
                "counts": {"entries": 0, "sources": 0, "by_kind": {}},
                "plan_digest": "digest",
            }

    adapter = _Adapter()
    settings = SimpleNamespace(
        artifact_reconcile_mode="audit",
        artifact_reconcile_repo=str(tmp_path),
        artifact_reconcile_repository="menhir",
    )
    asyncio.run(
        _run_startup_artifact_reconcile(
            SimpleNamespace(graph_adapter=adapter), settings
        )
    )
    assert adapter.kwargs == {
        "repo_path": str(tmp_path),
        "repository": "menhir",
    }
