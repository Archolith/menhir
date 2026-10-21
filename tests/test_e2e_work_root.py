"""Keep local RC evidence outside pytest's session cleanup directory."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.e2e import conftest as e2e_fixtures


def test_default_e2e_evidence_survives_pytest_scratch_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    with TemporaryDirectory() as checkout:
        repo_root = Path(checkout)
        monkeypatch.setattr(e2e_fixtures, "REPO_ROOT", repo_root)
        monkeypatch.setattr(e2e_fixtures, "validate_registry", lambda _: None)
        monkeypatch.delenv("MENHIR_E2E_WORK_ROOT", raising=False)

        config = e2e_fixtures.e2e_config.__wrapped__()
        assert config.work_root == repo_root / ".e2e-work"
        assert not config.evidence_dir.is_relative_to(tmp_path_factory.getbasetemp())
        marker = config.evidence_dir / "sample-result.json"
        marker.write_text("{}", encoding="utf-8")
        assert marker.read_text(encoding="utf-8") == "{}"
