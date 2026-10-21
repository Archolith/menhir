"""Live source-derived retention policy for semantic memory entities."""

from __future__ import annotations


RETENTION_SOURCE_RELATIONSHIP = "RETENTION_SOURCE"


def _validate_variable(variable: str) -> None:
    if not variable or not variable.isidentifier():
        raise ValueError(f"variable must be a Cypher identifier, got {variable!r}")


def canonical_tenant_cypher(variable: str) -> str:
    """Return a Cypher expression that treats legacy ``''`` as ``default``."""

    _validate_variable(variable)
    raw = f"coalesce({variable}.namespace, {variable}.group_id, '')"
    return f"CASE WHEN {raw} IN ['', 'default'] THEN 'default' ELSE {raw} END"


def same_tenant_cypher(left: str, right: str) -> str:
    """Return the tenant-equality predicate used by retention provenance."""

    return f"{canonical_tenant_cypher(left)} = {canonical_tenant_cypher(right)}"


def source_retention_protected_cypher(variable: str = "n") -> str:
    """Whether an Entity has a currently flagged, tenant-consistent source episode."""

    _validate_variable(variable)
    return (
        "EXISTS { "
        f"MATCH (retention_source:Episodic)-[:{RETENTION_SOURCE_RELATIONSHIP}]->({variable}) "
        "WHERE coalesce(retention_source.user_flagged, false) "
        f"AND {same_tenant_cypher('retention_source', variable)} "
        "}"
    )


def destructive_retention_allowed_cypher(variable: str = "n") -> str:
    """Guard automatic harmful mutation using direct and live source intent."""

    _validate_variable(variable)
    return (
        f"coalesce({variable}.user_flagged, false) = false "
        f"AND NOT {source_retention_protected_cypher(variable)}"
    )
