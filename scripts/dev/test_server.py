"""Launch a throwaway Menhir server in a selectable auth *shape* for fast testing.

Why this exists
---------------
Testing the auth/OAuth surface against a live server kept requiring hand-rolled
env + `menhir.cli serve` invocations. Two hazards made that error-prone:

1. A plain `serve` inherits the repo ``.env`` (``load_dotenv(ENV_FILE or None)``
   searches upward), so it boots ``startup_mode=full`` against the **real Neo4j
   backend** and with the **dev bearer keys** — violating the "never touch the
   real backend / never :8090" test rule.
2. Each auth mode needs a different, fiddly env combination.

This launcher removes both. It runs the server with a **fully isolated env**
(no repo ``.env``, no inherited shell vars, a dead/throwaway Neo4j so startup
degrades instead of touching real data) on a **safe port** (default 8099, and
it refuses 8090), in one of a few named *shapes*. Startup is backgrounded in the
server, so ``/api/health`` (auth-exempt) answers immediately even while the
backend init fails in the background — which is exactly what auth-path testing
needs.

No shape requires a secret: bearer keys are fixed test values and OAuth signing
keys are generated locally (joserfc). The client-token / AS SQLite stores go in
a temp dir that is deleted at teardown.

Shapes
------
- ``no-auth``      : no keys configured (loopback dev mode).
- ``static``       : static bearer keys (operator/agent/readonly = known values).
- ``client-token`` : per-client token tier enabled (bootstrap + admin gate).
- ``oauth``        : OAuth resource-server mode. ``--jwks-uri`` selectable;
                     defaults to a dead URI so the IdP-outage path (503) is
                     exercisable.
- ``oauth-as``     : embedded authorization server enabled (DCR/authorize/token).

Usage
-----
CLI (foreground; Ctrl-C to stop, auto-teardown)::

    python scripts/dev/test_server.py --shape client-token --port 8099

For a disposable ChatGPT OAuth acceptance server behind a secure tunnel::

    python scripts/dev/test_server.py --shape oauth-as --backend neo4j \
        --public-base-url https://example-tunnel.example \
        --oauth-refresh --oauth-access-ttl 120 --interactive-control

The OAuth consent key for this throwaway shape is the fixed ``test-operator-key``.

Programmatic (used by scripts/smoke/auth_shapes_smoke.py)::

    from scripts.dev.test_server import launch, TEST_KEYS
    with launch("static", port=8099) as srv:
        ...  # srv.base_url, srv.keys

Safety
------
- Refuses ``--port 8090`` (the real server).
- Never reads the repo ``.env``; the subprocess env is built from scratch.
- Neo4j points at a dead port by default (``--backend none``) so no real graph
  is touched; pass ``--backend neo4j`` to use a disposable Docker Neo4j.
- A public test profile refuses inherited ``MENHIR_TEST_NEO4J_URI`` reuse.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO

if __package__:
    from .test_server_container import (
        _container_command,
        _remove_container,
        resolve_container_image,
    )
    from .test_server_env import _shape_env
    from .test_server_neo4j import _throwaway_neo4j
    from .test_server_util import (
        DEAD_JWKS_URI,
        FORBIDDEN_PORTS,
        REPO_ROOT,
        SHAPES,
        TEST_KEYS,
        _remove_workdir,
        _terminate,
        _validated_public_origin,
        _wait_for_health,
        free_port,
    )
else:  # direct `python scripts/dev/test_server.py` runs have no package context
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.dev.test_server_container import (
        _container_command,
        _remove_container,
        resolve_container_image,
    )
    from scripts.dev.test_server_env import _shape_env
    from scripts.dev.test_server_neo4j import _throwaway_neo4j
    from scripts.dev.test_server_util import (
        DEAD_JWKS_URI,
        FORBIDDEN_PORTS,
        REPO_ROOT,
        SHAPES,
        TEST_KEYS,
        _remove_workdir,
        _terminate,
        _validated_public_origin,
        _wait_for_health,
        free_port,
    )


@dataclass
class RunningServer:
    shape: str
    port: int
    base_url: str
    proc: subprocess.Popen
    workdir: Path
    keys: dict[str, str] = field(default_factory=dict)
    log_path: Path | None = None
    instance_id: str = ""
    # Populated for backend="neo4j" launches: the throwaway Neo4j the server runs
    # against, so a smoke can write graph fixtures directly to the same database.
    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = ""
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)
    log_file: TextIO | None = field(default=None, repr=False)
    creationflags: int = 0
    preexec_fn: Callable[[], None] | None = field(default=None, repr=False)
    health_timeout_s: float = 30.0

    def tail_log(self, n: int = 40) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])

    def restart(self) -> dict:
        """Restart only Menhir, preserving its stores and throwaway Neo4j."""
        if not self.command or self.log_file is None:
            raise RuntimeError("server restart state is unavailable")
        _terminate(self.proc)
        self.log_file.flush()
        self.proc = subprocess.Popen(  # noqa: S603
            list(self.command),
            cwd=str(self.workdir),
            env=self.env,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            creationflags=self.creationflags,
            preexec_fn=self.preexec_fn,
        )
        return _wait_for_health(
            self.base_url,
            self.health_timeout_s,
            self.proc,
            expect_instance_id=self.instance_id,
        )


@contextlib.contextmanager
def launch(shape: str, *, port: int | None = None, host: str = "127.0.0.1",
           backend: str = "none", jwks_uri: str = DEAD_JWKS_URI,
           oauth: dict[str, str] | None = None,
           public_base_url: str | None = None,
           oauth_refresh: bool = False,
           oauth_access_ttl_s: int | None = None,
           python_executable: str | None = None,
           container_image: str | None = None,
           health_timeout_s: float = 30.0, quiet: bool = True):
    """Context manager that starts a shaped throwaway server and tears it down.

    Yields a :class:`RunningServer`. The server is killed and its temp workdir
    removed on exit (even on exception). ``port`` defaults to a free ephemeral
    port so concurrent/adjacent servers never collide; pass one only when you
    need a fixed port.
    """
    if port is None:
        port = free_port()
    if port in FORBIDDEN_PORTS:
        raise ValueError(f"refusing to use port {port} (reserved for the real server)")
    if public_base_url is not None:
        public_base_url = _validated_public_origin(public_base_url)

    with contextlib.ExitStack() as stack:
        # Backend-backed launches bring up a throwaway Neo4j first (and a full
        # startup takes longer, so give health a bigger budget by default).
        neo4j: tuple[str, str, str] | None = None
        if backend == "neo4j":
            sidecar = stack.enter_context(
                _throwaway_neo4j(
                    allow_external=public_base_url is None and container_image is None
                )
            )
            neo4j = (sidecar.uri, sidecar.user, sidecar.password)
            if health_timeout_s < 90.0:
                health_timeout_s = 90.0

        workdir = Path(tempfile.mkdtemp(prefix=f"menhir-test-{shape}-{port}-"))
        # Cleanup failures are material here: this directory contains OAuth
        # signing keys and token stores, so never silently leave it behind.
        stack.callback(_remove_workdir, workdir)
        log_path = workdir / "server.log"
        instance_id = f"smoke-{shape}-{secrets.token_hex(8)}"
        env = _shape_env(shape, port=port, host=host, workdir=workdir,
                         jwks_uri=jwks_uri, backend=backend, instance_id=instance_id,
                         neo4j=neo4j, oauth=oauth,
                         public_base_url=public_base_url,
                         oauth_refresh=oauth_refresh,
                         oauth_access_ttl_s=oauth_access_ttl_s)
        base_url = f"http://{host}:{port}"

        container_name = ""
        process_env = env
        if container_image:
            if backend != "neo4j":
                raise ValueError("container-image tests require backend='neo4j'")
            if python_executable:
                raise ValueError("container_image and python_executable are mutually exclusive")
            container_name = f"menhir-smoke-app-{secrets.token_hex(4)}"
            cmd, process_env = _container_command(
                image=container_image, name=container_name, port=port,
                workdir=workdir, env=env,
            )
        elif python_executable:
            py = Path(python_executable)
            if not py.is_file():
                raise FileNotFoundError(f"requested Python interpreter does not exist: {py}")
            cmd = [str(py), "-m", "menhir.cli", "serve", "--host", host, "--port", str(port)]
        else:
            py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
            if not py.exists():
                py = Path(sys.executable)
            cmd = [str(py), "-m", "menhir.cli", "serve", "--host", host, "--port", str(port)]

        log_f = stack.enter_context(log_path.open("w", encoding="utf-8"))
        # New process group so we can signal the whole tree on teardown.
        creationflags = 0
        preexec = None
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            preexec = os.setsid  # type: ignore[assignment]

        # Run from the isolated workdir (NOT the repo) so cwd-relative dotenv
        # auto-loading cannot pick up the repo's real keys. menhir is venv-installed,
        # so `python -m menhir.cli` resolves regardless of cwd.
        proc = subprocess.Popen(  # noqa: S603
            cmd, cwd=str(workdir), env=process_env, stdout=log_f, stderr=subprocess.STDOUT,
            creationflags=creationflags, preexec_fn=preexec,
        )
        srv = RunningServer(shape=shape, port=port, base_url=base_url, proc=proc,
                            workdir=workdir, keys=dict(TEST_KEYS), log_path=log_path,
                            instance_id=instance_id,
                            neo4j_uri=neo4j[0] if neo4j else "",
                            neo4j_user=neo4j[1] if neo4j else "",
                            neo4j_password=neo4j[2] if neo4j else "",
                            command=tuple(cmd) if not container_image else (),
                            env=process_env, log_file=log_f,
                            creationflags=creationflags, preexec_fn=preexec,
                            health_timeout_s=health_timeout_s)
        if container_name:
            stack.callback(_remove_container, container_name, srv.proc)
        else:
            stack.callback(lambda: _terminate(srv.proc))
        health = _wait_for_health(base_url, health_timeout_s, proc,
                                  expect_instance_id=instance_id)
        if not quiet:
            print(f"[test_server] shape={shape} backend={backend} up at {base_url} "
                  f"(startup_mode={health.get('startup_mode')})", file=sys.stderr)
        yield srv


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Launch a throwaway Menhir server in an auth shape.")
    ap.add_argument("--shape", choices=SHAPES, required=True)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--backend", choices=("none", "neo4j"), default="none",
                    help="'none' = auth-only, no Neo4j (fast). 'neo4j' = full scope "
                    "against a throwaway Neo4j (public profiles refuse external reuse).")
    ap.add_argument("--jwks-uri", default=DEAD_JWKS_URI,
                    help="OAuth shape only. Default is a dead URI (exercises the 503 outage path).")
    ap.add_argument(
        "--public-base-url",
        default=None,
        help="Externally visible origin for OAuth metadata (for example, an HTTPS tunnel URL).",
    )
    ap.add_argument(
        "--oauth-refresh",
        action="store_true",
        help="OAuth-AS shape only. Enable persistent refresh-token issuance and rotation.",
    )
    ap.add_argument(
        "--oauth-access-ttl",
        type=int,
        default=None,
        help="OAuth-AS shape only. Override access-token lifetime in seconds.",
    )
    ap.add_argument(
        "--use-current-python",
        action="store_true",
        help="Launch Menhir with this interpreter (useful with uv run --isolated --frozen).",
    )
    ap.add_argument(
        "--interactive-control",
        action="store_true",
        help="Accept 'restart' and 'stop' commands on stdin while preserving test state.",
    )
    ap.add_argument("--health-timeout", type=float, default=30.0)
    args = ap.parse_args(argv)

    if (args.oauth_refresh or args.oauth_access_ttl is not None) and args.shape != "oauth-as":
        ap.error("--oauth-refresh and --oauth-access-ttl require --shape oauth-as")
    if args.oauth_access_ttl is not None and args.oauth_access_ttl <= 0:
        ap.error("--oauth-access-ttl must be positive")
    if args.public_base_url and args.shape not in {"oauth", "oauth-as"}:
        ap.error("--public-base-url requires an OAuth shape")

    with launch(args.shape, port=args.port, host=args.host, backend=args.backend,
                jwks_uri=args.jwks_uri, public_base_url=args.public_base_url,
                oauth_refresh=args.oauth_refresh,
                oauth_access_ttl_s=args.oauth_access_ttl,
                python_executable=sys.executable if args.use_current_python else None,
                health_timeout_s=args.health_timeout, quiet=False) as srv:
        print(f"Menhir test server [{srv.shape}] running at {srv.base_url}")
        if srv.shape == "static":
            print(f"  keys: operator={TEST_KEYS['operator']} "
                  f"agent={TEST_KEYS['agent']} readonly={TEST_KEYS['readonly']}")
        elif srv.shape == "oauth-as":
            print(f"  throwaway OAuth consent key: {TEST_KEYS['operator']}")
        print("  Ctrl-C to stop (auto-teardown).")
        try:
            if args.interactive_control:
                print("  Commands: restart (preserves OAuth/Neo4j state), stop")
                while srv.proc.poll() is None:
                    try:
                        command = input().strip().lower()
                    except EOFError:
                        break
                    if command == "restart":
                        health = srv.restart()
                        print(
                            "[test_server] restarted; OAuth and Neo4j state preserved "
                            f"(startup_mode={health.get('startup_mode')})",
                            file=sys.stderr,
                        )
                    elif command in {"stop", "quit", "exit"}:
                        break
                    elif command:
                        print("[test_server] commands: restart, stop", file=sys.stderr)
            else:
                while srv.proc.poll() is None:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[test_server] stopping...", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
