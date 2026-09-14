"""`menhir up`: composes setup, compose, wait, preflight report, serve."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from menhir.cli import app
from menhir.cli import up as up_module
from menhir.cli.up import (
    UpError,
    compose_neo4j_up,
    neo4j_is_loopback,
    render_report,
    tier_report,
    wait_for_neo4j,
)
from menhir.config import MemorySettings
from menhir.core.runtime_preflight import RuntimeCapabilities

pytestmark = [pytest.mark.unit]


def _caps(**overrides) -> RuntimeCapabilities:
    base = dict(
        venv_ready=True,
        graphiti_dependency_ready=True,
        neo4j_ready=True,
        graphiti_llm_ready=True,
        embedder_ready=True,
        reranker_ready=True,
        failures=(),
    )
    base.update(overrides)
    return RuntimeCapabilities(**base)


def test_neo4j_is_loopback() -> None:
    assert neo4j_is_loopback("bolt://localhost:7687")
    assert neo4j_is_loopback("bolt://127.0.0.1:7687")
    assert not neo4j_is_loopback("bolt://neo4j:7687")
    assert not neo4j_is_loopback("bolt://192.168.1.5:7687")


def test_wait_for_neo4j_probes_immediately_then_polls_until_answer() -> None:
    answers = iter(["unreachable", "unreachable", "ok"])
    slept: list[float] = []
    ticks = iter([0.0, 0.0, 2.0, 2.0, 4.0, 4.0])
    status = wait_for_neo4j(
        uri="bolt://x", user="u", password="p", timeout_s=10.0,
        probe=lambda *a: next(answers), sleep=slept.append, clock=lambda: next(ticks),
    )
    assert status == "ok"
    assert slept == [2.0, 2.0]


def test_wait_for_neo4j_stops_at_once_when_credentials_are_refused() -> None:
    slept: list[float] = []
    status = wait_for_neo4j(
        uri="bolt://x", user="u", password="p", timeout_s=60.0,
        probe=lambda *a: "unauthorized", sleep=slept.append, clock=lambda: 0.0,
    )
    assert status == "unauthorized"
    assert slept == [], "a wrong password never becomes right by waiting"


def test_wait_for_neo4j_gives_up_at_deadline() -> None:
    slept: list[float] = []
    ticks = iter([0.0, 0.0, 3.0, 6.0, 9.0, 12.0])
    status = wait_for_neo4j(
        uri="bolt://x", user="u", password="p", timeout_s=5.0,
        probe=lambda *a: "unreachable", sleep=slept.append, clock=lambda: next(ticks),
    )
    assert status == "unreachable"
    assert len(slept) >= 1


def test_wait_for_neo4j_zero_timeout_is_a_single_probe() -> None:
    calls: list[int] = []

    def _probe(*a) -> str:
        calls.append(1)
        return "unreachable"

    status = wait_for_neo4j(
        uri="bolt://x", user="u", password="p", timeout_s=0.0,
        probe=_probe, sleep=lambda s: None, clock=lambda: 0.0,
    )
    assert status == "unreachable"
    assert calls == [1]


def test_compose_neo4j_up_requires_compose_file_and_docker(tmp_path: Path) -> None:
    with pytest.raises(UpError, match="docker-compose.yml"):
        compose_neo4j_up(tmp_path)

    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    def _missing(*a, **k):
        raise FileNotFoundError("docker")

    with pytest.raises(UpError, match="not on PATH"):
        compose_neo4j_up(tmp_path, run=_missing)

    def _fails(cmd, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom\nno such service: neo4j")

    with pytest.raises(UpError, match="no such service"):
        compose_neo4j_up(tmp_path, run=_fails)

    seen: list[list[str]] = []

    def _ok(cmd, **k):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    assert "docker compose up -d neo4j" in compose_neo4j_up(tmp_path, run=_ok)
    assert seen[0][:3] == ["docker", "compose", "-f"]
    assert seen[0][-3:] == ["up", "-d", "neo4j"]


def test_tier_report_names_the_env_keys_behind_each_miss() -> None:
    settings = MemorySettings(graphiti_provider="openai", chat_provider="openai")
    lines = tier_report(
        _caps(neo4j_ready=False, graphiti_llm_ready=False, embedder_ready=False), settings
    )
    def _line(rows, prefix):
        return next(r for r in rows if r.capability.startswith(prefix))

    assert _line(lines, "neo4j: reachable").ready is False
    assert "NEO4J_PASSWORD" in _line(lines, "neo4j: reachable").hint
    assert "OPENAI_API_KEY" in _line(lines, "llm: graphiti extraction").hint
    assert "OPENAI_EMBED_MODEL" in _line(lines, "llm: embeddings").hint
    assert _line(lines, "python: graphiti_core importable").ready is True

    local = tier_report(_caps(embedder_ready=False), MemorySettings(graphiti_provider="local"))
    assert "LOCAL_LLM_EMBED_MODEL" in _line(local, "llm: embeddings").hint


def test_render_report_marks_misses_and_states_the_mode() -> None:
    settings = MemorySettings(graphiti_provider="local")
    text = render_report(tier_report(_caps(graphiti_llm_ready=False), settings), "degraded_reads_only")
    assert "[MISS] llm: graphiti extraction  <- LOCAL_LLM_BASE_URL" in text
    assert "[ok  ] neo4j: reachable" in text
    assert text.endswith("recall works; new memories queue until the LLM is reachable.")


def _checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "menhir"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "archolith-menhir"\n', encoding="utf-8")
    (repo / ".env.example").write_text(
        "NEO4J_URI=bolt://localhost:7687\nNEO4J_USER=neo4j\nNEO4J_PASSWORD=\n"
        "LLM_CHAT_PROVIDER=local\nGRAPHITI_LLM_PROVIDER=local\n",
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch):
    for name in ("ENV_FILE", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_up_check_reports_and_exits_without_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_env
) -> None:
    repo = _checkout(tmp_path)
    served: list[object] = []
    monkeypatch.setattr("menhir.core.runtime_preflight.probe_neo4j", lambda *a: "unreachable")
    monkeypatch.setattr("menhir.core.collect_runtime_capabilities", lambda settings: _caps(neo4j_ready=False))

    import menhir.cli as cli_module

    monkeypatch.setattr(cli_module, "serve", lambda **kw: served.append(kw))

    result = CliRunner().invoke(app, ["up", "--check", "--repo", str(repo), "--compose-neo4j"])

    assert result.exit_code == 1, result.output
    assert served == []
    assert "would run: docker compose up -d neo4j (skipped by --check)" in result.output
    assert "[MISS] neo4j: reachable" in result.output
    assert "startup mode: unavailable" in result.output
    assert "NEO4J_PASSWORD=password" in (repo / ".env").read_text(encoding="utf-8")


def test_up_starts_compose_waits_and_hands_off_to_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_env
) -> None:
    repo = _checkout(tmp_path)
    (repo / "docker-compose.yml").write_text("services: {neo4j: {}}\n", encoding="utf-8")
    served: list[object] = []
    composed: list[Path] = []
    monkeypatch.setattr(up_module, "compose_neo4j_up", lambda checkout: composed.append(checkout) or "started")
    monkeypatch.setattr("menhir.core.runtime_preflight.probe_neo4j", lambda *a: "ok")
    monkeypatch.setattr("menhir.core.collect_runtime_capabilities", lambda settings: _caps())

    import menhir.cli as cli_module

    monkeypatch.setattr(cli_module, "serve", lambda **kw: served.append(kw))

    result = CliRunner().invoke(app, ["up", "--repo", str(repo), "--compose-neo4j", "--port", "8123"])

    assert result.exit_code == 0, result.output
    assert composed == [repo.resolve()]
    assert served == [{"host": None, "port": 8123}]
    assert "startup mode: full" in result.output


def test_up_refuses_compose_when_the_shell_pins_a_remote_neo4j(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_env
) -> None:
    """--compose-neo4j rewrites .env to localhost, but the shell environment outranks .env:
    an exported remote NEO4J_URI must make the compose request refuse rather than start a
    container nobody will connect to."""
    repo = _checkout(tmp_path)
    monkeypatch.setenv("NEO4J_URI", "bolt://db.internal:7687")

    result = CliRunner().invoke(app, ["up", "--check", "--repo", str(repo), "--compose-neo4j"])

    assert result.exit_code == 1
    assert "only applies to a loopback NEO4J_URI" in result.output
    # .env itself was still normalised for the next run
    assert "NEO4J_URI=bolt://localhost:7687" in (repo / ".env").read_text(encoding="utf-8")


def test_up_loads_the_checkout_env_not_the_cwd_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_env
) -> None:
    """A bystander .env in the current directory must not configure the checkout being started."""
    repo = _checkout(tmp_path)
    (repo / ".env").write_text(
        "NEO4J_URI=bolt://from-checkout:7687\nNEO4J_USER=neo4j\nNEO4J_PASSWORD=x\n",
        encoding="utf-8",
    )
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / ".env").write_text("NEO4J_URI=bolt://from-cwd:7687\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    seen: list[str] = []

    def _probe(uri: str, user: str, password: str) -> str:
        seen.append(uri)
        return "unreachable"

    monkeypatch.setattr("menhir.core.runtime_preflight.probe_neo4j", _probe)
    monkeypatch.setattr("menhir.core.collect_runtime_capabilities", lambda settings: _caps(neo4j_ready=False))

    CliRunner().invoke(app, ["up", "--check", "--repo", str(repo)])

    assert seen and all(u == "bolt://from-checkout:7687" for u in seen), seen
