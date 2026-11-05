"""CF-272: Menhir defaults the graphiti fork's telemetry off; the operator can re-enable it.

Each case builds a real ``Graphiti`` through ``GraphitiClient.from_settings`` in a subprocess
(pytest not imported, so the fork's own pytest suppression does not apply). Only ``posthog`` is
replaced, by a recording stub, so nothing is ever sent. The fork's ``is_telemetry_enabled`` and
``capture_event`` stay real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_PROBE = """
import json, os, sys, types
from pathlib import Path
captured = []
stub = types.ModuleType("posthog")
stub.capture = lambda **kw: captured.append(kw.get("event"))
sys.modules["posthog"] = stub
from menhir.config import MemorySettings
from menhir.infrastructure.graphiti_client import GraphitiClient
if os.environ.get("PROBE_LOAD_ENV"):
    from menhir.env_file import load_menhir_env
    load_menhir_env()
GraphitiClient.from_settings(MemorySettings.from_env())
anon = Path.home() / ".cache" / "graphiti" / "telemetry_anon_id"
print(json.dumps({"captures": captured, "anon": anon.exists(), "pytest": "pytest" in sys.modules}))
"""

_SCRUB = ("PYTHONPATH", "GRAPHITI_TELEMETRY_ENABLED", "ENV_FILE", "MENHIR_STATE_DIR", "WORKSPACE_ROOT")


def _run(*, dotenv: str | None = None, load_env: bool = False, process_value: str | None = None) -> dict:
    with tempfile.TemporaryDirectory() as home:
        env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
        env.update(
            HOME=home,
            USERPROFILE=home,
            MENHIR_STATE_DIR=str(Path(home) / "state"),
            NEO4J_URI="bolt://127.0.0.1:1",
        )
        if process_value is not None:
            env["GRAPHITI_TELEMETRY_ENABLED"] = process_value
        if load_env:
            env["PROBE_LOAD_ENV"] = "1"
        cwd = Path(home) / "cwd"
        cwd.mkdir()
        if dotenv is not None:
            (cwd / ".env").write_text(dotenv + "\n", encoding="utf-8")
        out = subprocess.run(
            [sys.executable, "-c", _PROBE], env=env, cwd=cwd, capture_output=True, text=True, timeout=180
        )
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["pytest"] is False
    return result


def test_unset_variable_sends_nothing_and_writes_no_anon_id() -> None:
    result = _run()
    assert result["captures"] == []
    assert result["anon"] is False


def test_dotenv_false_on_a_path_that_does_not_load_dotenv_sends_nothing() -> None:
    result = _run(dotenv="GRAPHITI_TELEMETRY_ENABLED=false")
    assert result["captures"] == []
    assert result["anon"] is False


def test_process_env_true_re_enables_capture() -> None:
    result = _run(process_value="true")
    assert result["captures"] == ["graphiti_initialized"]
    assert result["anon"] is True


def test_dotenv_true_loaded_before_construction_re_enables_capture() -> None:
    result = _run(dotenv="GRAPHITI_TELEMETRY_ENABLED=true", load_env=True)
    assert result["captures"] == ["graphiti_initialized"]
    assert result["anon"] is True
