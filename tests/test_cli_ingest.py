"""CLI coverage for typed project-identity outcomes during wiki ingestion."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from menhir.cli import app


def _wiki(tmp_path):
    wiki = tmp_path / "sage" / "wiki"
    concepts = wiki / "concepts"
    concepts.mkdir(parents=True)
    document = concepts / "identity.md"
    document.write_text("# Identity\n", encoding="utf-8")
    return wiki, document


def _install_backend(monkeypatch, outcome):
    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value=outcome)
    backend.aclose = AsyncMock()
    monkeypatch.setattr(
        "menhir.config.MemorySettings.from_env",
        lambda: MagicMock(api_host="127.0.0.1", api_port=8090),
    )
    monkeypatch.setattr("menhir.core.backend_impl.BackendClient", lambda *a, **kw: backend)
    monkeypatch.setattr("menhir.cli.configure_logging", lambda: None)
    return backend


@pytest.mark.unit
def test_ingest_wiki_forwards_identity_retry_and_counts_only_written_entities(
    tmp_path, monkeypatch
) -> None:
    wiki, _ = _wiki(tmp_path)
    backend = _install_backend(
        monkeypatch,
        {
            "entity_written": True,
            "structure_project": "sage",
            "structure_path": "identity.md",
        },
    )

    result = CliRunner().invoke(
        app,
        [
            "ingest-wiki",
            str(wiki),
            "--identity-action",
            "adopt",
            "--adopt-project-id",
            "project-id-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingested 1 documents (0 unresolved, 0 errors)" in result.output
    kwargs = backend.ingest_document.await_args.kwargs
    assert kwargs["identity_action"] == "adopt"
    assert kwargs["adopt_project_id"] == "project-id-1"
    backend.aclose.assert_awaited_once()


@pytest.mark.unit
def test_ingest_wiki_reports_needs_decision_as_unresolved_and_exits_nonzero(
    tmp_path, monkeypatch
) -> None:
    wiki, document = _wiki(tmp_path)
    backend = _install_backend(
        monkeypatch,
        {
            "status": "needs_decision",
            "reason": "identity_file_missing",
            "directory": str(wiki.parent),
            "candidates": [
                {
                    "project_id": "existing-id",
                    "display_name": "sage",
                    "entity_count": 42,
                    "last_scan": "2026-08-25T00:00:00Z",
                    "recorded_root_path": "C:/canonical/sage",
                }
            ],
            "retry_with": {
                "identity_action": "adopt|new",
                "adopt_project_id": "<project_id from candidates, required for adopt>",
            },
        },
    )

    result = CliRunner().invoke(app, ["ingest-wiki", str(wiki)])

    assert result.exit_code == 1
    assert "Identity decision required for 1 document(s):" in result.output
    assert str(document.resolve()) in result.output
    assert '"project_id": "existing-id"' in result.output
    assert '"identity_action": "adopt|new"' in result.output
    assert '"adopt_project_id"' in result.output
    assert "Ingested 0 documents (1 unresolved, 0 errors)" in result.output
    backend.aclose.assert_awaited_once()
