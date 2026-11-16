"""The note a read adds when its namespace holds no records (#671).

Reads match a namespace exactly, so ``PFCC`` or ``pfcc `` returns nothing when the data is under
``pfcc``. An empty result there looks like "no history"; the note says why it is empty.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.domain.namespace import DEFAULT_NAMESPACE

logger = logging.getLogger(__name__)


async def namespace_note(backend: Any, namespace: str | None) -> str | None:
    """A note when *namespace* holds no records, else ``None``. Best-effort: never raises."""
    ns = namespace or ""
    if ns in ("", DEFAULT_NAMESPACE):
        return None
    try:
        status = await backend.get_namespace_status(ns)
    except Exception:  # an older server, or a failed probe: no note rather than no answer
        logger.debug("get_namespace_status unavailable for %r", ns, exc_info=True)
        return None
    if not isinstance(status, dict) or status.get("known") is not False:
        return None
    # The server answers for the namespace it applied: a pinned client's pin, not what it asked.
    shown = str(status.get("namespace") or ns)
    note = (
        f"Namespace {shown!r} holds no records, so an empty result here is not evidence of no "
        "history. Namespaces are matched exactly."
    )
    similar = [str(s) for s in status.get("similar") or []]
    if similar:
        note += " Did you mean " + " or ".join(repr(s) for s in similar) + "?"
    return note
