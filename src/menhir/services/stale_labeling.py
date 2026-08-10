"""Pure helper for labeling recall items with stale-anchor metadata.

Hook Center integration: enriches recall result items by matching against
stale-anchored memory rows from the graph adapter. Label-only — does not
filter, delete, expire, or down-rank recall output.
"""

from __future__ import annotations

from typing import Any

STALE_REASON = "file_changed_after_anchor"
STALE_ACTION = "verify_current_file_before_relying"
STALE_ADVISORY = (
    "This memory is anchored to a file that changed after anchoring. "
    "Inspect the current file before relying on it. "
    "If outdated, update or supersede the memory."
)
STALE_ACTION_OUTDATED = "do_not_rely_update_or_supersede"
STALE_ADVISORY_OUTDATED = (
    "This memory was verified against the current file and appears outdated. "
    "Do not rely on it as current truth. "
    "Update or supersede it."
)
STALE_ADVISORY_STILL_VALID = (
    "This memory is stale because its anchored file changed, but it was later "
    "verified against the current file. Use it with normal caution; inspect "
    "the file again if the answer is high-impact."
)
ALLOWED_VERIFICATION_OUTCOMES = (
    "still_valid",
    "outdated",
    "needs_review",
    "superseded",
)


def label_stale_anchors(
    items: list[dict[str, Any]],
    stale_anchors: list[dict[str, Any]],
) -> None:
    """Enrich each recall item with stale-anchor metadata, in place.

    Parameters
    ----------
    items:
        Recall output items as dicts. Each dict MUST contain a key that
        identifies the memory (preferred: ``uuid``). Modified in place.
    stale_anchors:
        Rows returned by ``stale_anchored_memories()``. Each must contain
        ``memory_uuid``. Optional enrichment fields: ``dirty_at``,
        ``anchored_at``, ``path``.

    Matching is by ``memory_uuid`` ↔ ``uuid`` (preferred) or by
    ``name`` (fallback when uuid keys are absent).  No fuzzy or LLM matching.

    Items with a match get:
        ``stale_anchor=true, stale_reason="file_changed_after_anchor",
        dirty_at=…, anchored_at=…, path=…``

    Items without a match get:
        ``stale_anchor=false``

    No item is removed, no score/rank is changed, no graph writes occur.
    """
    if not items or not stale_anchors:
        _assign_default(items)
        return

    stale_by_uuid: dict[str, dict[str, Any]] = {}
    stale_by_name: dict[str, dict[str, Any]] = {}
    for sa in stale_anchors:
        uid = sa.get("memory_uuid")
        if uid:
            stale_by_uuid[str(uid)] = sa
        name = sa.get("name")
        if name:
            stale_by_name[str(name)] = sa

    for item in items:
        matched = None
        uid = item.get("uuid")
        if uid and uid in stale_by_uuid:
            matched = stale_by_uuid[uid]
        if matched is None:
            name = item.get("name")
            if name and name in stale_by_name:
                matched = stale_by_name[name]
        if matched is not None:
            item["stale_anchor"] = True
            item["stale_reason"] = STALE_REASON
            item["dirty_at"] = matched.get("dirty_at")
            item["anchored_at"] = matched.get("anchored_at")
            item["path"] = matched.get("path")
        else:
            item["stale_anchor"] = False


def _assign_default(items: list[dict[str, Any]]) -> None:
    """Set ``stale_anchor=false`` on every item when there is nothing to match."""
    for item in items:
        item["stale_anchor"] = False
