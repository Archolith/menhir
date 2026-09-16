"""MCP-bundled remote project snapshot ingest.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md``.

Shipped so far: the frozen wire contract (:mod:`menhir.snapshot.protocol`), the selection policy
(:mod:`menhir.snapshot.policy`), and the client-side bundler (:mod:`menhir.snapshot.bundler`)
behind ``menhir sync --check``. Nothing here reaches the network or the graph: the MCP receive
tools are plan phase P2 and are gated on P0's measured transport limits.
"""

from __future__ import annotations

__all__ = ["bundler", "policy", "protocol"]
