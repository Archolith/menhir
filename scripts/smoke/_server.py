"""Shared self-serve helper for smoke scripts.

Every smoke should be able to **spin up its own throwaway server** rather than
requiring an already-running service (which risks accidentally targeting a real
deployment). This wraps the shaped launcher (`scripts/dev/test_server.py`) so a
smoke can:

    with smoke_server("oauth") as base_url:
        ... run checks against base_url ...

By default it launches an isolated, throwaway server in the requested auth shape
(no repo .env, no real backend, safe port). Pass ``url=...`` (typically from a
``--url`` flag) to instead target an already-running server and skip the launch —
the opt-in path for hitting a live instance on purpose.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

# scripts/ on path so `dev.test_server` imports regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dev.test_server import launch  # noqa: E402


@contextlib.contextmanager
def smoke_server(shape: str, *, url: str | None = None, port: int | None = None, **launch_kw):
    """Yield a base URL for the smoke to hit.

    - ``url`` given  -> yield it unchanged; do NOT start a server (target a
      running instance on purpose).
    - ``url`` is None -> launch a throwaway server in *shape* (free port +
      instance-id handshake, collision-proof) and yield its base URL; torn down
      on exit.
    """
    if url:
        yield url.rstrip("/")
        return
    with launch(shape, port=port, **launch_kw) as srv:
        yield srv.base_url
