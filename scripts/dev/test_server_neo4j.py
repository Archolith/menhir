"""Throwaway Neo4j sidecar for backend-backed test-server launches.

Either reuses an explicitly provided external test Neo4j (``MENHIR_TEST_NEO4J_*``)
or runs a disposable ``neo4j:5-community`` Docker container that is removed on
exit.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass

from .test_server_util import free_port


@dataclass
class _Neo4jSidecar:
    uri: str
    user: str
    password: str
    container: str | None  # None when reusing an external Neo4j (nothing to tear down)


@contextlib.contextmanager
def _throwaway_neo4j(*, wait_s: float = 90.0, allow_external: bool = True):
    """Provide a throwaway Neo4j for backend-backed launches.

    Fast path: if ``MENHIR_TEST_NEO4J_URI`` is set, reuse that Neo4j (with
    ``MENHIR_TEST_NEO4J_USER`` / ``MENHIR_TEST_NEO4J_PASSWORD``) and start nothing.
    Otherwise start a disposable ``neo4j:5-community`` container on an ephemeral
    bolt port, wait until it accepts connections, and remove it on exit.
    """
    ext = os.getenv("MENHIR_TEST_NEO4J_URI")
    if ext:
        if not allow_external:
            raise RuntimeError(
                "public OAuth test profiles refuse MENHIR_TEST_NEO4J_URI reuse; "
                "unset it so the launcher creates a disposable Docker Neo4j"
            )
        yield _Neo4jSidecar(
            uri=ext,
            user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
            password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "neo4j"),
            container=None,
        )
        return

    if shutil.which("docker") is None:
        raise RuntimeError(
            "backend='neo4j' needs Docker (or set MENHIR_TEST_NEO4J_URI to an "
            "existing throwaway Neo4j). Docker was not found on PATH."
        )
    bolt = free_port()
    name = f"menhir-smoke-neo4j-{secrets.token_hex(4)}"
    password = "smokethrowaway"
    run = subprocess.run(  # noqa: S603
        ["docker", "run", "-d", "--rm", "--name", name,
         "-p", f"127.0.0.1:{bolt}:7687",
         "-e", f"NEO4J_AUTH=neo4j/{password}",
         "-e", "NEO4J_server_memory_heap_max__size=512m",
         "neo4j:5-community"],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        raise RuntimeError(f"failed to start throwaway Neo4j: {run.stderr.strip()}")
    uri = f"bolt://127.0.0.1:{bolt}"
    try:
        _wait_for_neo4j(uri, "neo4j", password, wait_s)
        yield _Neo4jSidecar(uri=uri, user="neo4j", password=password, container=name)
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(["docker", "rm", "-f", name],  # noqa: S603
                           capture_output=True, text=True, timeout=30)


def _wait_for_neo4j(uri: str, user: str, password: str, timeout_s: float) -> None:
    """Block until the Neo4j at *uri* answers a trivial query, or time out."""
    from neo4j import GraphDatabase  # local import: only needed for backend launches

    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password))
            try:
                driver.verify_connectivity()
                with driver.session() as s:
                    s.run("RETURN 1").consume()
                return
            finally:
                driver.close()
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            time.sleep(1.0)
    raise TimeoutError(f"throwaway Neo4j not ready after {timeout_s}s: {last_err}")
