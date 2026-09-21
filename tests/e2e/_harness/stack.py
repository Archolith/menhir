"""Bring up and tear down the three-process E2E stack.

The campaign's topology is not a single server, and the plan's wording ("launch Menhir
the way a real local MCP client does") resolves to three processes:

    disposable Neo4j            provisioned here, reset between lanes
      |
    `menhir serve`              the runtime owner; the stdio bridge refuses to start
      |                         without it (mcp/lifecycle.py:62)
      |
    `python -m menhir.mcp.server`   the stdio bridge a stock MCP client spawns

Both Menhir processes run from a **non-editable install inside a dedicated venv**, which
is what makes E2E-1's "install from the candidate package, not an editable checkout"
meaningful. Running them from the checkout would also test packaging by accident and
pass when packaging is broken.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tests.e2e._harness.config import E2EConfig, assert_not_production, child_environment

__all__ = [
    "BackendProcess",
    "InstalledMenhir",
    "build_wheel",
    "graph_query",
    "run_menhir_cli",
    "install_into_venv",
    "reset_graph",
    "start_backend",
]

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class InstalledMenhir:
    """A built-and-installed Menhir, with the identity evidence the RC freeze needs."""

    venv_python: Path
    wheel_path: Path
    wheel_sha256: str
    version: str

    def as_evidence(self) -> dict[str, str]:
        return {
            "wheel": self.wheel_path.name,
            "wheel_sha256": self.wheel_sha256,
            "version": self.version,
            "interpreter": str(self.venv_python),
        }


def build_wheel(destination: Path) -> Path:
    """Build a wheel from the working tree into ``destination``.

    Gate D freezes a package hash, so the campaign must test *a wheel*, not a checkout.
    The tree should be clean when this runs; the caller records the commit alongside.
    """

    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob("*.whl"):
        stale.unlink()

    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(destination), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        # `check=True` here raised a CalledProcessError carrying only the exit code, and
        # the caller turns that into a skip. A build failure then reported nothing about
        # why the build failed, which is the one thing the reader needs.
        raise RuntimeError(
            f"`python -m build` exited {result.returncode}. "
            f"stdout: {result.stdout[-3000:]} | stderr: {result.stderr[-3000:]}"
        )
    wheels = sorted(destination.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel in {destination}, found {wheels}")
    return wheels[0]


def install_into_venv(config: E2EConfig, wheel: Path) -> InstalledMenhir:
    """Create a clean venv and install the wheel into it, non-editable.

    ``--no-cache-dir`` is not merely hygiene: a cached editable or a previously built
    artifact of the same version would make "cold install" a lie.
    """

    import hashlib

    if config.venv_dir.exists():
        shutil.rmtree(config.venv_dir)
    subprocess.run(
        [sys.executable, "-m", "venv", str(config.venv_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )

    python = config.venv_python()
    subprocess.run(
        [str(python), "-m", "pip", "install", "--upgrade", "pip", "--no-cache-dir", "-q"],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-cache-dir", "-q", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    )

    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    version = subprocess.run(
        [str(python), "-c", "import menhir, sys; sys.stdout.write(getattr(menhir, '__version__', 'unknown'))"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()

    _assert_not_running_from_checkout(python)
    return InstalledMenhir(venv_python=python, wheel_path=wheel, wheel_sha256=digest, version=version)


def _assert_not_running_from_checkout(python: Path) -> None:
    """Prove the venv imports the INSTALLED menhir, not the source checkout.

    Without this, a stray ``PYTHONPATH``, a ``.pth`` left by an earlier editable
    install, or simply running with cwd inside the repo silently turns the whole
    campaign back into a source-tree run -- and E2E-1 would report a passing cold
    install that never happened.
    """

    resolved = subprocess.run(
        [str(python), "-c", "import menhir, sys; sys.stdout.write(menhir.__file__)"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()

    location = Path(resolved).resolve()
    checkout_src = (REPO_ROOT / "src").resolve()
    if checkout_src in location.parents:
        raise RuntimeError(
            f"venv imports menhir from the source checkout ({location}), not the "
            f"installed wheel. E2E-1 cannot claim a non-editable install."
        )
    if "site-packages" not in location.parts:
        raise RuntimeError(f"menhir resolved outside site-packages: {location}")


def reset_graph(config: E2EConfig) -> None:
    """Delete every node in the disposable graph.

    Each lane starts from a fresh graph, per the plan's "disposable state directory,
    fresh graph". The production fence is re-checked here rather than trusted from
    session setup, because this function is the one that destroys data.
    """

    assert_not_production(config.neo4j_uri, what="graph reset target")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password)
    )
    try:
        with driver.session(database=config.neo4j_database) as session:
            # Batched so a large leftover graph cannot blow the transaction budget.
            while True:
                summary = session.run(
                    "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(n) AS deleted"
                ).single()
                if not summary or summary["deleted"] == 0:
                    break
    finally:
        driver.close()


def run_menhir_cli(
    config: E2EConfig,
    *args: str,
    feature_env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run the installed ``menhir`` CLI under the harness environment.

    Some acceptance criteria only exist on the operator CLI -- artifact reconciliation
    writes through `menhir artifacts reconcile --apply`, and the MCP surface is read-only
    by design. A lane that asserted only the MCP half would leave the writing half
    untested.

    cwd is the harness state directory for the same reason every other child's is:
    ``resolve_env_file`` falls back to ``./.env``, and a CLI invoked from inside a
    checkout would read the developer's env file and reach a graph this campaign never
    chose.
    """

    return subprocess.run(
        [str(config.venv_script("menhir")), *args],
        cwd=str(config.state_dir),
        env=child_environment(config, **(feature_env or {})),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def graph_query(config: E2EConfig, cypher: str, **params: object) -> list[dict]:
    """Run one read-only Cypher statement against the disposable graph.

    Some criteria are about a durable fact that no MCP surface reports -- whether a
    :CanonicalView exists, whether a MENTIONS edge crosses a group boundary. Asserting
    those through a formatted tool response would be asserting the formatter. The
    statement is checked for write clauses first: this helper exists to observe the
    graph a lane just exercised, and a lane that mutated it here would be grading its
    own answer.
    """

    forbidden = ("CREATE", "MERGE", "DELETE", "SET ", "REMOVE", "DROP", "DETACH")
    upper = cypher.upper()
    for clause in forbidden:
        if clause in upper:
            raise ValueError(f"graph_query is read-only; refusing statement containing {clause!r}")

    assert_not_production(config.neo4j_uri, what="graph read target")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password)
    )
    try:
        with driver.session(database=config.neo4j_database) as session:
            return [dict(record) for record in session.run(cypher, **params)]
    finally:
        driver.close()


@dataclass
class BackendProcess:
    """A running ``menhir serve``, with its captured output."""

    process: subprocess.Popen[str]
    log_path: Path
    url: str
    host: str = "127.0.0.1"
    port: int = 0
    #: The ``/api/ready`` body that was accepted. Recorded as evidence: "degraded" with
    #: no provider is a legitimate configuration for some lanes and a silent
    #: misconfiguration for others, and only the capability flags tell them apart.
    ready_payload: dict | None = None

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def terminate(self) -> str:
        """Stop the backend and return whatever it wrote.

        Graceful first: E2E-1 asserts "graceful shutdown leaves no corrupt state", so a
        kill as the default would make that assertion untestable.
        """

        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=30)
        self.wait_port_released()
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def wait_port_released(self, timeout: float = 30.0) -> bool:
        """Block until nothing is listening on the backend port, or the timeout elapses.

        A dead process is not the same as a free port. `menhir serve` refuses to start
        when its port is already bound -- "already in use; another server owns it,
        exiting (code 3)" -- and the campaign starts a fresh backend PER LANE, so lane N+1
        was binding while lane N's socket was still winding down.

        This never reproduced on Windows and failed every run on Linux, which is the
        shape of a socket-teardown timing difference rather than a defect in either lane.
        Waiting here rather than sleeping in the caller keeps the guarantee attached to
        the thing that owns the port.
        """

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(1.0)
                if probe.connect_ex((self.host, self.port)) != 0:
                    return True
            time.sleep(0.25)
        return False

    def kill(self) -> str:
        """Stop the backend the way a crash does, with no shutdown path taken.

        E2E-7 needs the ungraceful stop specifically. ``terminate()`` gives the process a
        chance to flush, drain and mark work FAILED, which is exactly the behaviour under
        test -- using it here would prove the recovery path works when the process was
        allowed to prepare for it, which is not the scenario that produces a false READY.

        On Windows ``SIGKILL`` does not exist; ``Popen.kill`` maps to TerminateProcess,
        which is likewise unblockable and uncatchable, so the lane gets the same
        no-cleanup guarantee on both platforms.
        """

        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=30)
        self.wait_port_released()
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def start_backend(
    config: E2EConfig,
    installed: InstalledMenhir,
    *,
    log_path: Path,
    feature_env: dict[str, str] | None = None,
    require_enrichment: bool = False,
) -> BackendProcess:
    """Start ``menhir serve`` from the installed package and wait for ``/api/ready``.

    The cwd is the harness state directory, never the checkout: ``resolve_env_file``
    falls back to ``./.env``, so a child started inside the repo would read the
    developer's env file even though ``ENV_FILE`` is set to ours.

    ``feature_env`` is the lane's feature combination. It must be passed here AND to the
    stdio bridge: the backend owns the runtime (workers, retrieval, lifecycle) while the
    bridge is a separate process with its own settings resolution, so applying the combo
    to only one of them would leave the campaign reporting a configuration that was
    never fully in effect.

    WHAT "READY" MEANS HERE
    -----------------------
    ``/api/ready`` reports ``status: "ready"`` only when ``enrichment_ready`` is true,
    which requires a reachable model. Lanes that deliberately declare no provider --
    E2E-4 and E2E-5 -- therefore never see it: the backend comes up
    ``degraded / degraded_queue_only``, with ``neo4j_ready`` and ``queue_writes_ready``
    true and the LLM capabilities false. That is the configuration those lanes intend,
    not a failure, and waiting for ``ready`` deadlocks them.

    But accepting ``degraded`` unconditionally would be worse than the deadlock. A lane
    that DID ask for a provider comes up degraded when the provider failed to wire up,
    and the lane would then run against a backend that cannot enrich and report "the
    episode never reached READY" as a product defect. So the caller states what it
    needs: ``require_enrichment`` is true exactly when a provider was requested, and a
    degraded backend is accepted only when the capabilities the lane depends on are
    present.
    """

    config.state_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # A fresh port per backend. See `E2EConfig.reserve_backend_port`: reusing one makes
    # every backend after the first exit 3 on Linux.
    config.reserve_backend_port()
    handle = log_path.open("w", encoding="utf-8")

    process = subprocess.Popen(
        [str(installed.venv_python), "-m", "menhir.main", "serve"],
        cwd=str(config.state_dir),
        env=child_environment(config, **(feature_env or {})),
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
                f"`menhir serve` exited with code {process.returncode} before becoming "
                f"ready. Log:\n{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        try:
            with urllib.request.urlopen(f"{backend.url}/api/ready", timeout=5) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode("utf-8"))
                    accepted, why = _readiness_verdict(payload, require_enrichment)
                    if accepted:
                        backend.ready_payload = payload
                        return backend
                    last_error = why
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_error = repr(exc)
        time.sleep(1.0)

    backend.terminate()
    raise TimeoutError(
        f"`menhir serve` did not become usable within {config.backend_ready_timeout}s "
        f"(require_enrichment={require_enrichment}; {last_error}).\n"
        f"Log:\n{log_path.read_text(encoding='utf-8', errors='replace')[-4000:]}"
    )


#: Capabilities every lane needs regardless of provider: a graph to write to, and a
#: backend willing to accept writes. Without these there is nothing to test.
_BASELINE_CAPABILITIES = ("neo4j_ready", "queue_writes_ready")


def _readiness_verdict(payload: dict, require_enrichment: bool) -> tuple[bool, str]:
    """Decide whether this ``/api/ready`` payload is usable, and say why if not.

    Returns ``(accepted, reason)``. The reason is carried into the timeout message so a
    failure names the missing capability rather than dumping the payload and leaving the
    reader to work out which field mattered.
    """

    status = str(payload.get("status") or "")
    capabilities = payload.get("capabilities") or {}

    if status == "starting":
        return False, "still starting (runtime not initialized)"

    missing = [name for name in _BASELINE_CAPABILITIES if not capabilities.get(name)]
    if missing:
        return False, f"missing baseline capabilities {missing}; failures={payload.get('failures')}"

    if require_enrichment and not capabilities.get("enrichment_ready"):
        # The provider the lane asked for did not come up. Failing here, rather than
        # letting the lane run, is what keeps a harness misconfiguration from being
        # written into the evidence as a product defect.
        return False, (
            "a provider was requested but enrichment_ready is false; "
            f"failures={payload.get('failures')} auth={payload.get('provider_auth_failure')}"
        )

    if status in {"ready", "ok"}:
        return True, "ready"
    if status == "degraded":
        return True, "degraded but sufficient for this lane"
    return False, f"unrecognized status {status!r}"
