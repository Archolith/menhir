"""Throwaway self-served server for the Hook Center stale-anchor lane smoke.

Extracted verbatim from ``hook_center_stale_lane_smoke.py``: mount the REAL router
over the REAL adapter. No full runtime bootstrap, no embedder, no scheduler.
``_spawn_server`` stays in the facade because it re-invokes the script itself via
``__file__``.
"""

from __future__ import annotations

import argparse


# ---------------------------------------------------------------------------
# Throwaway server (self-serve): mount the REAL router over the REAL adapter.
# No full runtime bootstrap, no embedder, no scheduler.
# ---------------------------------------------------------------------------


def _quiet_neo4j_logs() -> None:
    """Silence the neo4j driver's benign 'UnknownPropertyKey' server notifications —
    the throwaway DB has no indexes/props yet, and the smoke's own output is the signal."""
    import logging
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)


def _serve(args: argparse.Namespace) -> int:
    from types import SimpleNamespace
    _quiet_neo4j_logs()
    import uvicorn
    from fastapi import FastAPI
    from menhir.api.routes import router
    from menhir.infrastructure.neo4j import Neo4jRepository
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    neo = Neo4jRepository(args.neo4j_uri, args.neo4j_database, args.neo4j_user, args.neo4j_password)
    adapter = MemoryGraphAdapter(neo)
    app = FastAPI()
    app.include_router(router)
    # Routes only need runtime_ctx.built.graph_adapter (auth disabled: no keys configured).
    app.state.runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=adapter),
        capabilities=None,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0
