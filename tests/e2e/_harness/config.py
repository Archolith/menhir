"""E2E stack configuration and the production-graph fence.

WHY THIS MODULE EXISTS SEPARATELY FROM ``tests/conftest.py``
------------------------------------------------------------
``tests/conftest.py`` has a session-autouse guard (``force_all_tests_onto_test_neo4j``)
that rewrites ``NEO4J_URI`` in the *pytest process* so no test can reach the operator's
real graph. That guard does not reach this harness, because every process the E2E
campaign drives is a **child process** running an **installed** Menhir:

    pytest (guarded)
      └── menhir serve            (separate process, own env, own .env resolution)
            └── python -m menhir.mcp.server   (separate process again)

``menhir.env_file.resolve_env_file`` reads ``ENV_FILE`` if set, else ``./.env`` relative
to the child's *current working directory*. A child launched with an inherited
environment and a cwd inside the checkout therefore loads the developer's real ``.env``
and connects to production -- with the parent's guard reporting nothing, because the
parent never made that connection.

So the fence has to be re-established at the process boundary, which is what
:func:`child_environment` and :func:`assert_not_production` do. The rule this module
enforces is narrow and absolute: **a child process in this harness never inherits the
operator's environment, and never resolves an env file the harness did not write.**
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    "E2EConfig",
    "ProductionGraphRefused",
    "assert_not_production",
    "child_environment",
    "normalize_bolt_uri",
]

# Environment variables that must never survive into a child process, because each one
# can silently redirect it at something real: the operator's graph, the operator's
# per-client identity policy, or an env file outside the harness's scratch directory.
_SCRUBBED_KEYS = (
    "ENV_FILE",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
    "MENHIR_BACKEND_URL",
    "MENHIR_BACKEND_TOKEN",
    "MENHIR_CLIENT_NAMESPACES",
    "MENHIR_CLIENT_TOOLS",
    "MENHIR_KNOWN_CLIENTS",
    "MENHIR_API_HOST",
    "MENHIR_API_PORT",
    "MENHIR_LOG_DIR",
    "MENHIR_DATA_DIR",
    "MENHIR_SNAPSHOT_RECEIVE_MODE",
    # PYTHONPATH matters more than it looks: pytest.ini sets `pythonpath = src`, so an
    # inherited PYTHONPATH would put the working-tree checkout ahead of the installed
    # wheel and quietly turn E2E-1's "non-editable install" into a source-tree run.
    "PYTHONPATH",
    "PYTHONHOME",
)

#: Scrubbed keys a lane is nevertheless allowed to set deliberately. ``MENHIR_BACKEND_URL``
#: is here because the stdio bridge requires it and E2E-8 needs to point a bridge at a
#: wrong/absent backend to prove the failure is loud.
_LANE_OVERRIDABLE = frozenset({"MENHIR_BACKEND_URL", "MENHIR_CLIENT_NAMESPACES", "MENHIR_SNAPSHOT_RECEIVE_MODE"})


class ProductionGraphRefused(RuntimeError):
    """Raised when any harness component resolves to a graph that could be production."""


def normalize_bolt_uri(uri: str) -> str:
    """Normalize a Bolt/Neo4j URI for comparison.

    Comparison is by (host, port) with the scheme dropped, because ``bolt://``,
    ``neo4j://`` and ``bolt+s://`` against the same host:port are the same database, and
    a fence that compares raw strings is trivially defeated by changing the scheme.
    """

    raw = (uri or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"bolt://{raw}"
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    # Loopback spellings are the same machine; treat them as one so a fence cannot be
    # slipped by swapping localhost for 127.0.0.1.
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = "127.0.0.1"
    port = parts.port
    if port is None:
        port = 7687
    return f"{host}:{port}"


def assert_not_production(uri: str, *, what: str) -> None:
    """Refuse a URI that is, or might be, the operator's production graph.

    ``what`` names the component being configured so a refusal says which one.

    Three separate refusals, because each covers a different way of being wrong:

    1. It matches ``MENHIR_PROD_NEO4J_URI_SNAPSHOT`` -- the parent conftest records the
       real pre-override production URI there, so this is a direct hit.
    2. It matches whatever ``NEO4J_URI`` was in the *ambient* environment. The parent
       conftest has already rewritten the in-process copy, so this reads the snapshot
       first and only falls back to the live value.
    3. It is the default Bolt port 7687 on loopback. The test instance is :7688 by
       convention throughout this repo; :7687 is the operator's. This one is a blunt
       heuristic on purpose -- the cost of a false refusal is an env var, and the cost
       of a false accept is the 2026-07-12 incident again.
    """

    candidate = normalize_bolt_uri(uri)
    if not candidate:
        raise ProductionGraphRefused(
            f"{what}: no graph URI resolved. Refusing to start rather than let the "
            "child process fall back to its own .env discovery."
        )

    prod_snapshot = normalize_bolt_uri(os.environ.get("MENHIR_PROD_NEO4J_URI_SNAPSHOT", ""))
    if prod_snapshot and candidate == prod_snapshot:
        raise ProductionGraphRefused(
            f"{what}: {uri} is the PRODUCTION graph recorded in "
            f"MENHIR_PROD_NEO4J_URI_SNAPSHOT. The E2E campaign runs destructive, "
            f"unscoped resets. Refusing."
        )

    if candidate == normalize_bolt_uri("bolt://127.0.0.1:7687"):
        raise ProductionGraphRefused(
            f"{what}: {uri} is the default Bolt port on loopback, which is this "
            f"workstation's production instance by convention. The E2E graph must be a "
            f"dedicated disposable instance (:7689 by default). Refusing."
        )


@dataclass(frozen=True)
class E2EConfig:
    """Resolved configuration for one E2E campaign run.

    Every field is explicit. Nothing here falls back to ambient operator configuration,
    because a fallback is exactly how a child process ends up somewhere real.
    """

    #: Disposable Neo4j for the campaign. Deliberately NOT :7688 -- that is the unit
    #: suite's instance, and an E2E lane resetting it mid-run would corrupt a parallel
    #: ``pytest --run-online`` into failures that look like product defects.
    neo4j_uri: str = field(default_factory=lambda: os.getenv("MENHIR_E2E_NEO4J_URI", "bolt://127.0.0.1:7689"))
    neo4j_user: str = field(default_factory=lambda: os.getenv("MENHIR_E2E_NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: os.getenv("MENHIR_E2E_NEO4J_PASSWORD", "testpassword"))
    neo4j_database: str = field(default_factory=lambda: os.getenv("MENHIR_E2E_NEO4J_DATABASE", "neo4j"))

    #: Loopback HTTP backend (`menhir serve`) that the stdio bridge talks to.
    backend_host: str = "127.0.0.1"
    backend_port: int = field(default_factory=lambda: int(os.getenv("MENHIR_E2E_BACKEND_PORT", "8199")))

    #: Root for the campaign's scratch state: venv, env file, fixture repos, evidence.
    work_root: Path = field(default_factory=lambda: Path(os.getenv("MENHIR_E2E_WORK_ROOT", "")) if os.getenv("MENHIR_E2E_WORK_ROOT") else Path())

    #: Seconds to wait for `menhir serve` to answer /api/ready before failing the lane.
    backend_ready_timeout: float = 120.0

    @property
    def backend_url(self) -> str:
        return f"http://{self.backend_host}:{self.backend_port}"

    @property
    def venv_dir(self) -> Path:
        return self.work_root / "venv"

    @property
    def env_file(self) -> Path:
        """The ONLY env file any child process in this harness is allowed to read."""
        return self.work_root / "e2e.env"

    @property
    def state_dir(self) -> Path:
        return self.work_root / "state"

    @property
    def evidence_dir(self) -> Path:
        return self.work_root / "evidence"

    @property
    def fixtures_dir(self) -> Path:
        return self.work_root / "fixtures"

    def venv_python(self) -> Path:
        """Interpreter inside the campaign venv (the installed, non-editable Menhir)."""
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"

    def venv_script(self, name: str) -> Path:
        if os.name == "nt":
            return self.venv_dir / "Scripts" / f"{name}.exe"
        return self.venv_dir / "bin" / name

    def validate(self) -> None:
        """Fail closed before anything is started. Call once per session."""

        assert_not_production(self.neo4j_uri, what="E2E graph")
        if self.work_root == Path():
            raise ValueError("E2EConfig.work_root was never resolved to a real directory")
        if self.backend_port == 8100:
            raise ValueError(
                "MENHIR_E2E_BACKEND_PORT is 8100, the documented default for the "
                "operator's own `menhir serve`. Pick a dedicated port (8199 default) so "
                "the campaign cannot drive the running production backend."
            )

    @property
    def absent_env_file(self) -> Path:
        """A path that must never exist, for lanes that want no env file at all.

        The #118 stdio harness pointed ``ENV_FILE`` at an intentionally absent path
        rather than at a written one. That is the stronger guarantee where a lane needs
        nothing from a file: ``resolve_env_file`` returns it, ``load_menhir_env`` finds
        no file, and nothing is loaded. The written file remains the default because
        most lanes do want the graph settings in it; this is here for the ones that
        want to prove behavior with no file present.
        """

        return self.work_root / "intentionally-absent.env"

    def write_env_file(self) -> Path:
        """Write the harness-owned env file the children read via ``ENV_FILE``.

        Children also receive these values as real environment variables. That is
        deliberate redundancy, not an accident: ``load_menhir_env`` calls dotenv with
        ``override=False``, so an explicit environment variable wins over the file. The
        file covers any code path that reads the file directly; the variables cover any
        path that skips the file. Neither alone covers both.
        """

        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Generated by tests/e2e -- disposable campaign configuration.",
            "# Never point this at an instance holding real memories.",
            f"NEO4J_URI={self.neo4j_uri}",
            f"NEO4J_USER={self.neo4j_user}",
            f"NEO4J_PASSWORD={self.neo4j_password}",
            f"NEO4J_DATABASE={self.neo4j_database}",
            f"MENHIR_API_HOST={self.backend_host}",
            f"MENHIR_API_PORT={self.backend_port}",
            f"MENHIR_LOG_DIR={self.state_dir / 'logs'}",
            "MENHIR_AUTH_MODE=none",
            "",
        ]
        self.env_file.write_text("\n".join(lines), encoding="utf-8")
        return self.env_file


def child_environment(config: E2EConfig, **extra: str) -> dict[str, str]:
    """Build the environment for a harness child process.

    Allow-list, not deny-list. A deny-list over ``os.environ`` keeps whatever the next
    Menhir release starts reading, and the failure mode of missing one is a child that
    quietly reaches something real.
    """

    config.validate()

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL")
    env: dict[str, str] = {k: os.environ[k] for k in keep if k in os.environ}

    # Provider credentials are passed through only when the lane needs a live model.
    # Lanes that do not need one must not receive them, so an accidental live call
    # fails loudly instead of quietly spending budget.
    env.update(
        {
            "ENV_FILE": str(config.env_file),
            "NEO4J_URI": config.neo4j_uri,
            "NEO4J_USER": config.neo4j_user,
            "NEO4J_PASSWORD": config.neo4j_password,
            "NEO4J_DATABASE": config.neo4j_database,
            "MENHIR_API_HOST": config.backend_host,
            "MENHIR_API_PORT": str(config.backend_port),
            "MENHIR_LOG_DIR": str(config.state_dir / "logs"),
            "MENHIR_AUTH_MODE": "none",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            # Blanked rather than omitted, from the #118 stdio harness. The allow-list
            # already keeps the operator's keys out, but an empty value is a positive
            # statement that this child has no credential, so a code path that reads one
            # and silently behaves differently cannot pick up a stale export.
            "MENHIR_AGENT_KEY": "",
            "MENHIR_READONLY_KEY": "",
            "MENHIR_OPERATOR_KEY": "",
            "MENHIR_API_KEY": "",
            # Also from that harness: benchmark mode keeps the run off shared
            # rate-limited paths, and `observe` stops saga reconciliation from acting on
            # a graph the lane is about to assert against.
            "MENHIR_BENCHMARK_MODE": "1",
            "MENHIR_SAGA_RECONCILE_STARTUP_MODE": "observe",
        }
    )
    # `extra` carries the per-lane feature combination and anything a lane pins
    # deliberately. It is applied last so a lane can override a harness default, but it
    # cannot escape the graph fence below.
    env.update(extra)

    # The allow-list above means a scrubbed key can only be present if `extra` put it
    # there. Assert that explicitly rather than silently dropping it: a lane that sets
    # PYTHONPATH is doing something the campaign's "installed package" premise forbids,
    # and it should fail rather than be quietly corrected.
    smuggled = sorted(key for key in _SCRUBBED_KEYS if key in extra and key not in _LANE_OVERRIDABLE)
    if smuggled:
        raise ValueError(
            f"lane attempted to set {smuggled} on a child process. These redirect the "
            "child at real infrastructure or at the source checkout; the campaign's "
            "isolation and non-editable-install claims depend on them staying harness-owned."
        )

    # Re-check after `extra` has been applied: a lane that overrides NEO4J_URI to probe
    # isolation must still not be able to point a child at production.
    assert_not_production(env["NEO4J_URI"], what="child process graph")
    return env


def docker_available() -> bool:
    """True when a Docker CLI is on PATH (the disposable graph is provisioned with it)."""

    return shutil.which("docker") is not None
