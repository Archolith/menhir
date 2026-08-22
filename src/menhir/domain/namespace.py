"""Memory namespace (silo) primitive.

A menhir *namespace* is a tenant/isolation boundary for memory. It maps 1:1 onto graphiti's
native ``group_id`` graph partition, which is the load-bearing isolation boundary at the engine
layer (verified: graphiti scopes entity resolution and search to a node's group_id). Menhir
additionally stamps the namespace onto nodes as defense-in-depth.

Mapping rules (see Phase 0 evidence in
``.agent/plans/menhir-memory-namespace-isolation-plan.md``):

- ``DEFAULT_NAMESPACE`` ("default") maps to graphiti group_id "" (graphiti's Neo4j default
  group), which is where all pre-existing menhir data already lives -> full backward
  compatibility.
- ``namespace=None`` means "caller did not specify a namespace" and MUST preserve today's
  global behavior: writes land in the default group; reads are NOT filtered (search all
  groups). Isolation is opt-in -- you only get a silo when you pass an explicit namespace.
"""

from __future__ import annotations

import re
from typing import Any

# Graphiti's default Neo4j group partition (see graphiti_core.helpers.get_default_group_id).
_GRAPHITI_DEFAULT_GROUP_ID = ""

#: The reserved namespace name for the shared/default silo (existing data lives here).
DEFAULT_NAMESPACE = "default"

# Mirrors graphiti_core.helpers.validate_group_id's character rule exactly. A namespace
# that fails this becomes an invalid group_id and graphiti rejects it -- but only deep in
# the background enrichment worker, long after the MCP caller that supplied it is gone.
# Checking it here, at write time, turns that into an immediate, visible error instead.
_GROUP_ID_SAFE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def normalize_namespace(value: Any) -> str:
    """Resolve any namespace-shaped value to the canonical silo name it denotes.

    CF-150: this was declared three times -- here (as the canonical one, with **no caller outside
    its own module**, see CF-147) and as a private ``_safe_namespace`` in each of
    ``todo_repository`` and ``work_artifact_repository``. All three agreed on every ``str | None``
    input, which is the finding: they agreed, and nothing kept them agreeing.

    Empty, whitespace-only and ``None`` all collapse to :data:`DEFAULT_NAMESPACE`. Case and inner
    whitespace are preserved beyond a strip -- namespaces are stamped through one path, so an exact
    match after coalescing the empty case is the correct normalized comparison.

    ``Any`` rather than ``str | None`` on purpose: this reads values straight off graph nodes, where
    a property is not guaranteed to be a string. The repositories' typed ``str | None`` callers are
    a strict subset and behave identically -- the two former helpers would raise ``AttributeError``
    on a non-string, so nothing that used to work stops working.

    **Not the same function as** :func:`stamped_namespace`. That one answers "what do I write onto a
    node", and today it deliberately passes ``''`` through unchanged (the pre-canonicalization
    spelling; see .agent/plans/menhir-default-namespace-canonicalization.md stage 2). This one
    answers "which silo does this value mean", where ``''`` means the default. Merging them would
    quietly make stage 2 of that plan happen early.
    """
    text = str(value).strip() if value is not None else ""
    return text or DEFAULT_NAMESPACE


def namespace_spellings(namespace: str | None) -> list[str] | None:
    """Every persisted spelling of *namespace*, for matching a raw node property.

    OWNER RULING 2026-08-21: ``''`` and ``'default'`` are **the same legacy/default silo**, and
    ``'default'`` is the canonical value going forward -- an explicit value is far less ambiguous
    than an empty string.

    ``namespace_to_group_ids`` already encodes that rule for graphiti's ``group_id``. This is the
    same rule for nodes matched on their own ``namespace`` property, which is not the same field:
    ``:TurnEvidence`` carries ``namespace`` and **no** ``group_id`` at all, so a group_id predicate
    silently matches nothing there. Two fields, one rule, declared once.

    ``None`` -> ``None`` (no filter), matching the sibling helpers.

    Both spellings are accepted on READ until the persisted values are migrated to the canonical
    one; a read that accepted only ``'default'`` would go blind to every existing row the moment
    the ruling was adopted.
    """
    if namespace is None:
        return None
    if namespace in (DEFAULT_NAMESPACE, _GRAPHITI_DEFAULT_GROUP_ID):
        return [DEFAULT_NAMESPACE, _GRAPHITI_DEFAULT_GROUP_ID]
    return [namespace]


def namespace_to_group_id(namespace: str | None) -> str:
    """Translate a menhir namespace to the graphiti ``group_id`` used on WRITE.

    Both ``None`` (unspecified) and ``DEFAULT_NAMESPACE`` resolve to graphiti's default group
    ("") so unspecified writes behave exactly as before. Any other value is used verbatim.
    """
    if namespace is None or namespace == DEFAULT_NAMESPACE:
        return _GRAPHITI_DEFAULT_GROUP_ID
    return namespace


def namespace_to_group_ids(namespace: str | None) -> list[str] | None:
    """Translate a menhir namespace to the graphiti ``group_ids`` filter used on READ.

    - ``None`` -> ``None`` (no filter; search every group -- preserves current behavior).
    - ``DEFAULT_NAMESPACE`` -> ``[""]`` (only the default silo).
    - other -> ``[namespace]`` (only that silo).
    """
    if namespace is None:
        return None
    if namespace == DEFAULT_NAMESPACE:
        return [_GRAPHITI_DEFAULT_GROUP_ID]
    return [namespace]


def namespace_group_id_error(namespace: str | None) -> str | None:
    """Return an error message if *namespace* cannot become a valid graphiti group_id.

    Returns ``None`` when the namespace is empty, ``DEFAULT_NAMESPACE``, or otherwise
    safe. Callers at the MCP boundary (e.g. ``add_memory``) should check this before
    queuing a write, so a bad namespace fails immediately instead of silently queuing
    and only surfacing as a ``GroupIdValidationError`` in the background enrichment
    worker -- long after the caller who supplied it is gone.
    """
    if not namespace or namespace == DEFAULT_NAMESPACE:
        return None
    if _GROUP_ID_SAFE_PATTERN.match(namespace):
        return None
    return (
        f"namespace {namespace!r} must contain only alphanumeric characters, dashes, "
        "or underscores (it becomes the graphiti group_id on write). Did you mean to "
        "pass this as bootstrap_scope instead?"
    )


def stamped_namespace(namespace: str | None) -> str:
    """The value to stamp onto menhir nodes as the defense-in-depth ``namespace`` property.

    Unspecified writes are stamped as ``DEFAULT_NAMESPACE`` so the property is always present
    on new data; absence of the property on legacy nodes is treated as the default silo by
    readers.
    """
    if namespace is None or namespace == DEFAULT_NAMESPACE:
        return DEFAULT_NAMESPACE
    return namespace
