"""The one snapshot receive mode a server operates in.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P2B).

P2A read ``MENHIR_SNAPSHOT_RECEIVE_MODE`` with ``os.getenv`` inside the tool module and compared it
to a string literal. That was deliberate and temporary: one phase, one surface, off by default.
It does not survive contact with three more modes, because "is this on?" stops being a single
question. `shadow` extracts an archive and scans it but writes no graph; `write` does both. A
string compared in each place that cares is how `receive` ends up extracting somewhere.

So the mode is declared once, here, next to `auth_mode` and for the same reason: the question
"what is this server allowed to do?" should have exactly one answer that every caller reads.

**Unknown values resolve to OFF, and that is load-bearing.** A typo in a deployment's environment
must not enable a receive surface, and it must not crash a server either -- a snapshot mode nobody
set is not a reason to refuse to boot. Resolving to OFF means the failure is "the feature is not
there", which is both the safe direction and the one an operator notices immediately.
"""

from __future__ import annotations

import os
from enum import Enum

__all__ = [
    "SNAPSHOT_RECEIVE_MODE_ENV",
    "SnapshotReceiveMode",
    "resolve_snapshot_receive_mode",
    "snapshot_receive_mode",
]

SNAPSHOT_RECEIVE_MODE_ENV = "MENHIR_SNAPSHOT_RECEIVE_MODE"

#: P2A's spelling for what is now `receive`. Kept so an operator config and the remote-sim stack
#: written against P2A do not silently fall back to OFF, which would look like the tools vanishing.
#: Remove once P2B ships and nothing sets it.
_LEGACY_STAGING = "staging"


class SnapshotReceiveMode(str, Enum):
    """What a server may do with an uploaded snapshot.

    The order is a ladder: each mode permits everything the one before it does, plus one step.
    The properties exist so callers ask about the CAPABILITY they need rather than comparing
    against a mode name -- a comparison like ``mode == WRITE`` in an extraction guard is correct
    until `shadow` is added, and then silently wrong.
    """

    OFF = "off"
    RECEIVE = "receive"
    SHADOW = "shadow"
    WRITE = "write"

    @property
    def accepts_uploads(self) -> bool:
        """Whether begin/chunk/status/abort are registered and invocable at all."""
        return self is not SnapshotReceiveMode.OFF

    @property
    def extracts_archives(self) -> bool:
        """Whether an uploaded archive may be opened. P3 implements this; nothing does yet."""
        return self in {SnapshotReceiveMode.SHADOW, SnapshotReceiveMode.WRITE}

    @property
    def writes_graph(self) -> bool:
        """Whether extraction results may reach the graph. P4 implements this; nothing does yet."""
        return self is SnapshotReceiveMode.WRITE


def resolve_snapshot_receive_mode(raw: str | None) -> SnapshotReceiveMode:
    """Resolve a configured value to a mode, failing closed.

    Case and surrounding whitespace are forgiven because they are typing accidents, not intent.
    An unrecognised word is not forgiven into anything except OFF.
    """
    text = (raw or "").strip().lower()
    if not text:
        return SnapshotReceiveMode.OFF
    if text == _LEGACY_STAGING:
        return SnapshotReceiveMode.RECEIVE
    try:
        return SnapshotReceiveMode(text)
    except ValueError:
        return SnapshotReceiveMode.OFF


def snapshot_receive_mode() -> SnapshotReceiveMode:
    """Read the mode from the environment at call time.

    Read per call, not cached at import: registration state is a process-start fact and this is a
    runtime one. A cached value would make a test holding a tool instance, or a process whose
    environment changed, act on a mode the server no longer has.
    """
    return resolve_snapshot_receive_mode(os.getenv(SNAPSHOT_RECEIVE_MODE_ENV))
