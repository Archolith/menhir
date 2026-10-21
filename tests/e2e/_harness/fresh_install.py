"""Run the installed wheel the way a user does: default state dir, no harness env file.

The other harness modules always set ENV_FILE, benchmark mode and a saga mode. This one
sets none of them, so `menhir setup` / `up` / `serve` resolve config from ``~/.menhir``.
"~" is a lane-owned temp HOME so the operator's real home is never read or written.

Graph fence: every child gets the disposable graph as PROCESS env (it beats ``.env``), and
``assert_not_production`` runs on every env built here.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterable, Mapping

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from tests.e2e._harness.client import RecordingClient
from tests.e2e._harness.config import E2EConfig, assert_not_production
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.stack import (
    BackendProcess,
    InstalledMenhir,
    _readiness_verdict,
    graph_query,
)

__all__ = [
    "EFFECTIVE_SETTINGS_CODE",
    "FreshHome",
    "code_defaults",
    "edit_env",
    "effective_settings",
    "flag_env_vars",
    "flag_lines",
    "fresh_install_environment",
    "make_fresh_home",
    "projection_for_turn",
    "readme_client_config",
    "run_cli",
    "run_python",
    "start_server",
    "stock_client_session",
    "tool_json",
    "tool_text",
    "wait_episode_state",
]

_KEEP_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
_FORBIDDEN_EXTRA = frozenset(
    {
        "ENV_FILE",
        "MENHIR_STATE_DIR",
        "WORKSPACE_ROOT",
        "PYTHONPATH",
        "PYTHONHOME",
        "MENHIR_BENCHMARK_MODE",
        "MENHIR_SAGA_RECONCILE_STARTUP_MODE",
        "HOME",
        "USERPROFILE",
    }
)
_TERMINAL_STATES = {"READY", "FAILED", "MANUAL_REVIEW"}


@dataclass(frozen=True)
class FreshHome:
    root: Path
    home: Path
    cwd: Path
    other_cwd: Path

    @property
    def state_dir(self) -> Path:
        return self.home / ".menhir"

    @property
    def env_path(self) -> Path:
        return self.state_dir / ".env"


def make_fresh_home(root: Path) -> FreshHome:
    """Create the temp HOME and neutral cwds; refuse a root inside a Menhir checkout."""

    root = Path(root).resolve()
    for parent in (root, *root.parents):
        pyproject = parent / "pyproject.toml"
        try:
            if not pyproject.is_file():
                continue
            name = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("name")
        except (OSError, ValueError):
            continue
        if name == "archolith-menhir":
            raise RuntimeError(
                f"{root} is inside a Menhir checkout ({parent}); `menhir setup`/`up` would "
                "configure that checkout instead of the state dir"
            )
    fresh = FreshHome(root=root, home=root / "home", cwd=root / "work", other_cwd=root / "elsewhere")
    for directory in (fresh.home, fresh.cwd, fresh.other_cwd):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (fresh.cwd, fresh.other_cwd):
        if (directory / ".env").exists():
            raise RuntimeError(f"unexpected {directory / '.env'} in a neutral cwd")
    return fresh


def fresh_install_environment(config: E2EConfig, home: FreshHome, **extra: str) -> dict[str, str]:
    """Environment for a fresh-install child.

    Deliberately ABSENT: ENV_FILE, MENHIR_STATE_DIR, WORKSPACE_ROOT, PYTHONPATH/PYTHONHOME,
    MENHIR_BENCHMARK_MODE, MENHIR_SAGA_RECONCILE_STARTUP_MODE, feature flags, and the
    operator's HOME. The lane exists to prove a user with none of those gets a working
    install from ``~/.menhir`` alone. Only the disposable graph is injected (process env).
    """

    config.validate()
    bad = sorted(k for k in extra if k in _FORBIDDEN_EXTRA or k.startswith("NEO4J_"))
    if bad:
        raise ValueError(f"fresh-install lanes may not set {bad}; they defeat the fresh-install premise")
    env: dict[str, str] = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    env.update(
        {
            "HOME": str(home.home),
            "USERPROFILE": str(home.home),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "NEO4J_URI": config.neo4j_uri,
            "NEO4J_USER": config.neo4j_user,
            "NEO4J_PASSWORD": config.neo4j_password,
            "NEO4J_DATABASE": config.neo4j_database,
        }
    )
    env.update(extra)
    assert_not_production(env["NEO4J_URI"], what="fresh-install child graph")
    return env


def run_cli(
    config: E2EConfig,
    installed: InstalledMenhir,
    home: FreshHome,
    *args: str,
    cwd: Path | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(config.venv_script("menhir")), *args],
        cwd=str(cwd or home.cwd),
        env=fresh_install_environment(config, home),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_python(
    config: E2EConfig,
    installed: InstalledMenhir,
    home: FreshHome,
    code: str,
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(installed.venv_python), "-c", code],
        cwd=str(cwd or home.cwd),
        env=fresh_install_environment(config, home, **(extra_env or {})),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# Mirrors what `menhir serve` does first: load the env file, then build settings.
EFFECTIVE_SETTINGS_CODE = """\
import json
from menhir.env_file import load_menhir_env
path = load_menhir_env()
from menhir.config import MemorySettings
s = MemorySettings.from_env()
from menhir.config.feature_flags import FEATURES
values = {}
for flag in FEATURES.values():
    if not flag.setting or not hasattr(s, flag.setting):
        continue
    if not isinstance(flag.default, (bool, str)):
        continue
    v = getattr(s, flag.setting)
    if not (v is None or isinstance(v, (bool, str, int, float))):
        v = str(v)
    values[flag.env_var] = v
print(json.dumps({"env_file": str(path) if path else "", "values": values}))
"""


def code_defaults() -> dict[str, object]:
    """Host-side twin of EFFECTIVE_SETTINGS_CODE: flag defaults declared in code."""

    from menhir.config import MemorySettings
    from menhir.config.feature_flags import FEATURES

    return {
        flag.env_var: flag.default
        for flag in FEATURES.values()
        if flag.setting and hasattr(MemorySettings, flag.setting) and isinstance(flag.default, (bool, str))
    }


def effective_settings(
    config: E2EConfig,
    installed: InstalledMenhir,
    home: FreshHome,
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict:
    result = run_python(
        config, installed, home, EFFECTIVE_SETTINGS_CODE, cwd=cwd, extra_env=extra_env
    )
    if result.returncode != 0:
        raise AssertionError(
            f"effective-settings probe exited {result.returncode}: {result.stderr[-3000:]}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"effective-settings probe printed nothing; stderr: {result.stderr[-3000:]}")
    return json.loads(lines[-1])


def flag_env_vars() -> frozenset[str]:
    from menhir.config.feature_flags import FEATURES

    return frozenset(flag.env_var for flag in FEATURES.values())


def _line_key(line: str) -> str | None:
    """Key of a ``KEY=value`` or commented ``# KEY=value`` line, else None."""

    stripped = line.strip()
    if stripped.startswith("#"):
        stripped = stripped[1:].strip()
    if "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key or None


def _active_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip() or None


def flag_lines(path: Path) -> list[str]:
    names = flag_env_vars()
    text = Path(path).read_bytes().decode("utf-8")
    return [line for line in text.split("\n") if _line_key(line) in names]


def edit_env(
    path: Path,
    *,
    set_values: Mapping[str, str] | None = None,
    remove: Iterable[str] = (),
) -> None:
    """Edit an env file like a user: in place, comments and other lines untouched."""

    path = Path(path)
    text = path.read_bytes().decode("utf-8")
    lines = text.split("\n")
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines.pop()

    drop = set(remove)
    if drop:
        lines = [line for line in lines if _active_key(line) not in drop]
    for key, value in (set_values or {}).items():
        for index, line in enumerate(lines):
            if _active_key(line) == key:
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")

    if trailing:
        lines.append("")
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def readme_client_config(python: Path, backend_url: str) -> dict:
    return {
        "mcpServers": {
            "memory": {
                "type": "stdio",
                "command": str(python),
                "args": ["-m", "menhir.mcp.server"],
                "env": {"MENHIR_BACKEND_URL": backend_url},
            }
        }
    }


@asynccontextmanager
async def stock_client_session(
    config_path: Path,
    home: FreshHome,
    evidence: LaneEvidence,
    *,
    label: str,
    server: str = "memory",
) -> AsyncIterator[RecordingClient]:
    """Launch the bridge from a client-config JSON file, as a stock MCP client does."""

    entry = json.loads(Path(config_path).read_text(encoding="utf-8"))["mcpServers"][server]
    env = {
        **get_default_environment(),
        "HOME": str(home.home),
        "USERPROFILE": str(home.home),
        **entry.get("env", {}),
    }
    params = StdioServerParameters(
        command=entry["command"],
        args=list(entry["args"]),
        env=env,
        cwd=str(home.other_cwd),
    )
    evidence.record_stack(**{f"{label}_client_config": entry})
    errlog_path = evidence.directory / f"{label}-stdio.log"
    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                client = RecordingClient(session, evidence)
                await client.initialize()
                yield client


def start_server(
    config: E2EConfig,
    installed: InstalledMenhir,
    home: FreshHome,
    evidence: LaneEvidence,
    *,
    command: str,
    cwd: Path,
    name: str,
) -> BackendProcess:
    """Start ``menhir up`` or ``menhir serve`` and wait for ``/api/ready``."""

    config.reserve_backend_port()
    log_path = evidence.directory / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        str(config.venv_script("menhir")),
        command,
        "--host",
        config.backend_host,
        "--port",
        str(config.backend_port),
    ]
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=fresh_install_environment(config, home),
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    backend = BackendProcess(
        process=process,
        log_path=log_path,
        url=config.backend_url,
        host=config.backend_host,
        port=config.backend_port,
    )

    deadline = time.monotonic() + config.backend_ready_timeout
    last_error = "never probed"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            handle.flush()
            raise RuntimeError(
                f"`menhir {command}` exited with code {process.returncode} before becoming "
                f"ready. Log:\n{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        try:
            with urllib.request.urlopen(f"{backend.url}/api/ready", timeout=5) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode("utf-8"))
                    accepted, why = _readiness_verdict(payload, require_enrichment=True)
                    if accepted:
                        backend.ready_payload = payload
                        evidence.record_stack(
                            **{
                                f"{name}_ready": payload,
                                f"{name}_argv": argv,
                                f"{name}_cwd": str(cwd),
                            }
                        )
                        return backend
                    last_error = why
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_error = repr(exc)
        time.sleep(1.0)

    backend.terminate()
    raise TimeoutError(
        f"`menhir {command}` did not become usable within {config.backend_ready_timeout}s "
        f"(require_enrichment=True; {last_error}).\n"
        f"Log:\n{log_path.read_text(encoding='utf-8', errors='replace')[-4000:]}"
    )


def wait_episode_state(config: E2EConfig, episode_uuid: str, *, timeout: float = 300.0) -> str:
    """Poll an episode until it reaches a terminal state; return the last state seen."""

    deadline = time.monotonic() + timeout
    state = "MISSING"
    while True:
        rows = graph_query(
            config,
            "MATCH (e:Episodic {uuid:$uuid}) RETURN e.processing_state AS state",
            uuid=episode_uuid,
        )
        state = str(rows[0]["state"]) if rows and rows[0].get("state") is not None else "MISSING"
        if state in _TERMINAL_STATES or time.monotonic() >= deadline:
            return state
        time.sleep(2.0)


def projection_for_turn(config: E2EConfig, turn_id: str) -> dict | None:
    rows = graph_query(
        config,
        "MATCH (p:Episodic) WHERE p.evidence_projection_of = $turn "
        "RETURN p.uuid AS uuid, p.processing_state AS state",
        turn=turn_id,
    )
    return rows[0] if rows else None


def tool_text(result) -> str:
    return "\n".join(getattr(item, "text", "") or "" for item in getattr(result, "content", None) or [])


def tool_json(result) -> dict:
    try:
        parsed = json.loads(tool_text(result))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
