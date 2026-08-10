"""Compatibility predicates for structural memory rows.

Current structural scan nodes carry ``structure_role``.  Older project-scan
rows predate that property and can still be identified by their deterministic
``Directory:``, ``File:``, or ``Project:`` content shape.  Keep the Cypher and
Python response-boundary checks here so the two definitions cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping


LEGACY_STRUCTURAL_CONTENT_PREFIXES = ("directory:", "file:", "project:")
STRUCTURE_ROLES = frozenset(
    {
        "project",
        "directory",
        "file",
        "entrypoint",
        "config",
        "test",
        "dependency",
        "endpoint",
        "document",
    }
)


def legacy_structural_memory_cypher(variable: str = "n") -> str:
    """Return the positive predicate for an unmarked legacy structure row."""

    prefixes = ", ".join(f"'{prefix}'" for prefix in LEGACY_STRUCTURAL_CONTENT_PREFIXES)
    # Checks the `sources` LIST as well as the legacy `source` string. A merged node's `source` now
    # holds only the LOWEST-TIER contributor, so a legacy structure row that absorbed an
    # agent-written duplicate no longer says 'project-scan' there -- and without the list check it
    # would stop being recognised as structure and start surfacing in recall as if it were a memory.
    # The string check is kept for nodes written before `sources` existed.
    return (
        f"(coalesce({variable}.source, '') CONTAINS 'project-scan' "
        f"OR any(s IN coalesce({variable}.sources, []) WHERE s CONTAINS 'project-scan')) "
        f"AND any(prefix IN [{prefixes}] WHERE "
        f"toLower(trim(coalesce({variable}.content, ''))) STARTS WITH prefix)"
    )


def non_structural_memory_cypher(variable: str = "n") -> str:
    """Return a Cypher predicate that excludes current and legacy structure rows, and evidence
    projections.

    Evidence projections are `:Episodic` nodes carrying a captured turn's verbatim text, minted so
    that entities exist in the user's own vocabulary (see
    `EpisodeLifecycleRepository.create_evidence_projection`). They are an ENTITY SOURCE, not memory:
    ADR 0001 keeps raw turns out of recall and decay, and surfacing one here would put raw chat in
    front of a caller as though someone had chosen to remember it. The entities extracted FROM a
    projection are recallable and are the entire point; the projection episode itself is not.

    Excluded here rather than at each call site because this predicate is the shared filter for every
    listing/lookup shape in memory_queries -- one edit covers them all, and a new query that forgets
    the rule is the failure mode worth designing out.
    """

    return (
        f"{variable}.structure_role IS NULL "
        f"AND NOT coalesce({variable}.is_evidence_projection, false) "
        f"AND NOT ({legacy_structural_memory_cypher(variable)})"
    )


def _has_project_scan_provenance(row: Mapping[str, object]) -> bool:
    """Does this row carry project-scan provenance in EITHER representation?

    Mirrors `legacy_structural_memory_cypher` exactly. Checking only `source` was correct until merge
    started deriving that field: it now holds only the LOWEST-tier contributor, so a legacy structure
    row that absorbed an agent-written duplicate says `claude-code` there while `project-scan` lives
    on in `sources`. The Cypher predicate was taught to read the list; this was not, so the database
    filtered such a row out as structure while this boundary check declared it an ordinary memory --
    the exact drift the module docstring promises cannot happen.
    """
    if "project-scan" in str(row.get("source") or "").casefold():
        return True
    sources = row.get("sources")
    if isinstance(sources, (list, tuple)):
        return any("project-scan" in str(s).casefold() for s in sources)
    return False


def infer_legacy_structure_role(row: Mapping[str, object]) -> str | None:
    """Infer the deterministic role of an unmarked legacy project-scan row."""

    if row.get("structure_role") is not None:
        return str(row["structure_role"]).strip().casefold() or None
    if not _has_project_scan_provenance(row):
        return None
    content = str(row.get("content") or "").strip().casefold()
    for prefix in LEGACY_STRUCTURAL_CONTENT_PREFIXES:
        if content.startswith(prefix):
            return prefix.removesuffix(":")
    return None


def is_structural_memory_row(row: Mapping[str, object]) -> bool:
    """Return whether a materialized memory row is current or legacy structure."""

    return infer_legacy_structure_role(row) is not None
