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
from enum import StrEnum
from typing import Any

# Graphiti's default Neo4j group partition (see graphiti_core.helpers.get_default_group_id).
_GRAPHITI_DEFAULT_GROUP_ID = ""

#: The reserved namespace name for the shared/default silo (existing data lives here).
DEFAULT_NAMESPACE = "default"

#: The one parameter name for the tenancy filter. CF-127 found seven spellings in use
#: ($namespace, $ns, $namespaces, $group_id, $group_ids, $ns_stamped, $namespace_norm); a builder
#: that let callers choose would have made it eight.
_TENANT_PARAM = "tenant_namespaces"

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


class TenancyScheme(StrEnum):
    """Which tenancy key a label is partitioned by. There are exactly two, and they are blind to
    each other -- that blindness is CF-127's whole finding.

    ``StrEnum``, not ``class X(str, Enum)``: the latter formats as ``"TenancyScheme.MEMORY"`` under
    ``str()`` and f-strings, which would land inside a Cypher fragment (trap T20).
    """

    #: Memory entities: partitioned by ``namespace`` (menhir's stamp) over ``group_id``
    #: (graphiti's real partition key). Both may be present, absent, or disagree on legacy rows.
    MEMORY = "memory"

    #: Structure entities: NOT namespace-partitioned at all. See :func:`tenant_scope_cypher`.
    STRUCTURE = "structure"


def tenant_scope_params(namespace: str | None) -> dict[str, list[str] | None]:
    """The parameter dict that :func:`tenant_scope_cypher`'s fragment expects.

    ``None`` (caller did not scope) yields ``None``, which makes the fragment a no-op -- the
    opt-in isolation contract at the top of this module.
    """
    return {_TENANT_PARAM: namespace_spellings(namespace)}


def tenant_scope_cypher(variable: str = "n", *, scheme: TenancyScheme = TenancyScheme.MEMORY) -> str:
    """One Cypher predicate for "this row belongs to the caller's silo" (CF-127).

    CF-127 counted 107 hand-written tenancy predicates across 23 files in seven parameter
    spellings, with no shared helper -- while the OTHER cross-cutting Cypher concern
    (``non_structural_memory_cypher``) had exactly the named, imported, reusable predicate this
    one lacked. Four separately-filed bugs (CF-104, CF-106, CF-63, CF-126) are the same omission
    by four different authors, each of whom had to independently remember that scoping is needed,
    which of two keys applies, and which spelling the surrounding file uses.

    **The fragment is a no-op when unscoped, on purpose.** It reads
    ``($tenant_namespaces IS NULL OR ...)``, so a caller ALWAYS includes it and passes ``None``
    for the unscoped case. "Forgot to add the predicate" is the failure this exists to prevent,
    so the predicate must not be the thing you can forget.

    **It matches BOTH persisted spellings of the default silo, which the existing idiom does
    not.** ``coalesce(n.namespace, n.group_id, 'default')`` -- the spelling at
    ``correlation_queries.py:147`` -- returns ``''`` for a legacy row with no ``namespace`` and
    ``group_id = ''``, because ``''`` is not NULL and coalesce never reaches its default. Compared
    against ``'default'`` that is FALSE, so it misses every such row; there are 33,442 of them on
    this deployment. Comparing against :func:`namespace_spellings` instead makes both spellings
    match, which is also what keeps this correct while the ``''`` -> ``'default'`` migration is
    only partly applied.

    Raises for :attr:`TenancyScheme.STRUCTURE` rather than returning a fragment. Structure
    entities wear the same ``:Entity`` label but are **deliberately a single shared silo**, keyed
    on ``(structure_project, structure_path)`` and written with ``group_id = ''`` and no
    ``namespace`` at all (owner ruling 2026-08-21). Handing back a namespace predicate for them
    would silently match nothing; handing back an empty string would look like "no scoping needed
    here" at every call site. Refusing makes the contract arrive at the moment an author assumes
    otherwise.

    **Known residual of that ruling, unfixed and deliberate:** because the MERGE identity is
    ``(structure_project, structure_path)`` with no tenancy component, two callers scanning
    differently-named repositories that share a directory basename still overwrite each other's
    ``content``, ``name`` and ``structure_role``.
    """
    if scheme is TenancyScheme.STRUCTURE:
        raise ValueError(
            "structure entities are a single shared silo keyed on (structure_project, "
            "structure_path); they carry no namespace to scope on. Filter on structure_project "
            "instead, and see tenant_scope_cypher's docstring for why this refuses."
        )
    if not variable or not variable.isidentifier():
        raise ValueError(f"variable must be a Cypher identifier, got {variable!r}")
    return (
        f"(${_TENANT_PARAM} IS NULL OR "
        f"coalesce({variable}.namespace, {variable}.group_id, '') IN ${_TENANT_PARAM})"
    )


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
