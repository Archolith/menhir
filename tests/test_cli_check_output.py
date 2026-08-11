"""The post-install dependency check must explain its exit status."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from menhir.cli import app


@pytest.mark.unit
def test_check_prints_each_runtime_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "menhir.core.collect_runtime_failures",
        lambda settings, require_venv: ["Neo4j unavailable.", "Embedder unavailable."],
    )

    result = CliRunner().invoke(app, ["check"])

    assert result.exit_code == 1
    assert "[FAIL] Neo4j unavailable." in result.output
    assert "[FAIL] Embedder unavailable." in result.output


@pytest.mark.unit
def test_check_confirms_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "menhir.core.collect_runtime_failures",
        lambda settings, require_venv: [],
    )

    result = CliRunner().invoke(app, ["check"])

    assert result.exit_code == 0
    assert "[OK] Menhir runtime dependencies are ready." in result.output
