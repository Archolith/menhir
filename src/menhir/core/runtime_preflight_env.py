"""Project virtualenv interpreter checks for menhir runtime preflight."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def expected_venv_python() -> Path:
    """Return the canonical interpreter path for the project virtual environment."""

    repo_root = Path(__file__).resolve().parents[3]
    if os.name == "nt":
        return repo_root / ".venv" / "Scripts" / "python.exe"
    return repo_root / ".venv" / "bin" / "python"


def check_expected_python_runtime(executable: str | None = None) -> bool:
    """Require the MCP server to run from the project virtualenv interpreter."""

    active = Path(executable or sys.executable).resolve()
    expected = expected_venv_python().resolve()
    if active != expected:
        logger.error(
            "Expected menhir MCP to run from %s but active interpreter is %s",
            expected,
            active,
        )
        return False
    return True
