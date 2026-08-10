"""Structural anchoring — bridge semantic memories to structural code entities.

Provides path extraction, normalization, structural entity resolution,
anchor edge creation, and reverse lookup for the structural-semantic
integration pipeline.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

from menhir.infrastructure.neo4j import Neo4jRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------

FILE_PATH_PATTERN = re.compile(
    r'(?:[\w.-]+/)+[\w.-]+\.(?:py|ts|tsx|js|jsx|java|kt|go|rs|yaml|toml|json|sql|sh)',
    re.MULTILINE,
)

BARE_FILENAME_PATTERN = re.compile(
    r'\b([\w.-]+\.(?:py|ts|tsx|js|jsx|java|kt|go|rs|yaml|toml|json|sql|sh))\b',
)


def extract_file_paths(text: str) -> list[str]:
    """Extract file path references from text.

    Full paths (e.g. ``src/menhir/services/foo.py``) take priority.
    Bare filenames (e.g. ``foo.py``) are only included when they are not
    already a tail segment of a returned full path.
    """
    if not text:
        return []

    full_paths = list(dict.fromkeys(FILE_PATH_PATTERN.findall(text)))
    bare_names = list(dict.fromkeys(BARE_FILENAME_PATTERN.findall(text)))

    # Deduplicate: exclude bare names that are tails of full paths
    full_tails = {p.rsplit("/", 1)[-1] for p in full_paths}
    unique_bare = [b for b in bare_names if b not in full_tails]

    return full_paths + unique_bare


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

def normalize_to_repo_relative(
    path: str,
    known_roots: list[str] | None = None,
) -> str:
    """Convert any file reference to repo-relative forward-slash form.

    Handles:
    - Absolute Windows paths: ``C:\\Users\\...\\src\\foo.py`` -> ``src/foo.py``
    - Absolute Unix paths: ``/mnt/c/Users/.../src/foo.py`` -> ``src/foo.py``
    - Git Bash paths: ``/c/Users/.../src/foo.py`` -> ``src/foo.py``
    - Already-relative paths: ``src/foo.py`` -> ``src/foo.py`` (passthrough)
    - Backslash paths: ``src\\foo.py`` -> ``src/foo.py``

    ``known_roots``: project root_path values. Strips the longest matching prefix.
    """
    # Normalize separators
    normalized = path.replace("\\", "/")

    if known_roots:
        # Sort by length descending so longest prefix wins
        sorted_roots = sorted(
            (r.replace("\\", "/").rstrip("/") for r in known_roots),
            key=len,
            reverse=True,
        )
        lower_normalized = normalized.lower()
        for root in sorted_roots:
            lower_root = root.lower()
            if lower_normalized.startswith(lower_root + "/"):
                normalized = normalized[len(root) + 1:]
                break
            if lower_normalized == lower_root:
                return "."

    # Strip leading slash if still absolute
    normalized = normalized.lstrip("/")

    # Handle Windows drive letters: C:/Users/... -> strip drive
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[2:].lstrip("/")

    # Handle Git Bash /c/Users style (single letter directory at root)
    parts = normalized.split("/")
    if len(parts) >= 2 and len(parts[0]) == 1 and parts[0].isalpha():
        normalized = "/".join(parts[1:])

    return normalized


# ---------------------------------------------------------------------------
# Narrative / diff content splitting
# ---------------------------------------------------------------------------

_DIFF_SEPARATOR = "\n\n--- git diff ---\n"


def split_narrative_and_diff(episode_body: str) -> tuple[str, str]:
    """Split composed episode body into (narrative_text, diff_text).

    Returns ``(full_body, '')`` if no diff separator found.
    """
    idx = episode_body.find(_DIFF_SEPARATOR)
    if idx < 0:
        return (episode_body, "")
    return (episode_body[:idx], episode_body[idx + len(_DIFF_SEPARATOR):])


# ---------------------------------------------------------------------------
# Structural entity resolution (Neo4j)
# ---------------------------------------------------------------------------

def resolve_structural_entities(
    neo4j: Neo4jRepository,
    candidate_paths: list[str],
    project_filter: str | None = None,
) -> list[dict[str, str]]:
    """Resolve file paths to structural Entity nodes.

    Returns ``[{uuid, project, path}]`` for each matched structural entity.
    """
    if not candidate_paths:
        return []

    project_clause = "AND n.structure_project = $project" if project_filter else ""
    rows = neo4j.execute(
        f"""
        UNWIND $paths AS candidate
        MATCH (n:Entity)
        WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test']
          AND (n.structure_path = candidate
               OR (NOT candidate CONTAINS '/'
                   AND (n.structure_path ENDS WITH '/' + candidate
                        OR n.structure_path = candidate)))
          {project_clause}
        RETURN DISTINCT n.uuid AS uuid,
               n.structure_project AS project,
               n.structure_path AS path
        """,
        params={
            "paths": candidate_paths,
            **({"project": project_filter} if project_filter else {}),
        },
    )
    return [
        {"uuid": str(r["uuid"]), "project": str(r["project"]), "path": str(r["path"])}
        for r in rows
        if r.get("uuid")
    ]


# ---------------------------------------------------------------------------
# Anchor edge creation (Neo4j)
# ---------------------------------------------------------------------------

def create_anchor_edges(
    neo4j: Neo4jRepository,
    semantic_uuids: list[str],
    structural_uuids: list[str],
    *,
    anchor_source: str = "narrative_path",
    weight: float = 1.0,
) -> int:
    """Create ANCHORED_TO edges from semantic entities to structural entities.

    Returns count of edges created.
    """
    if not semantic_uuids or not structural_uuids:
        return 0

    pairs = [
        {
            "sem": sem_uuid,
            "struct": struct_uuid,
            "edge_uuid": str(uuid4()),
            "anchor_source": anchor_source,
            "weight": weight,
        }
        for sem_uuid in semantic_uuids
        for struct_uuid in structural_uuids
    ]

    rows = neo4j.execute(
        """
        UNWIND $pairs AS pair
        MATCH (sem:Entity {uuid: pair.sem}), (struct:Entity {uuid: pair.struct})
        WHERE sem.structure_role IS NULL AND struct.structure_role IS NOT NULL
        MERGE (sem)-[r:ANCHORED_TO]->(struct)
        ON CREATE SET
            r.uuid = pair.edge_uuid,
            r.source = 'structural-anchor',
            r.anchor_source = pair.anchor_source,
            r.weight = pair.weight,
            r.created_at = datetime(),
            r.fact = 'Memory linked to code file: ' + struct.structure_path
        RETURN count(r) AS created
        """,
        params={"pairs": pairs},
    )
    return int(rows[0].get("created", 0)) if rows else 0


# ---------------------------------------------------------------------------
# Reverse lookup: find semantic entities linked to structural entities
# ---------------------------------------------------------------------------

def find_cross_linked_semantic_entities(
    neo4j: Neo4jRepository,
    structural_uuids: list[str],
) -> list[str]:
    """Find semantic entity UUIDs that are ANCHORED_TO the given structural entities."""
    if not structural_uuids:
        return []

    rows = neo4j.execute(
        """
        UNWIND $struct_uuids AS sid
        MATCH (sem:Entity)-[:ANCHORED_TO]->(struct:Entity {uuid: sid})
        WHERE sem.structure_role IS NULL
        RETURN DISTINCT sem.uuid AS uuid
        """,
        params={"struct_uuids": structural_uuids},
    )
    return [str(r["uuid"]) for r in rows if r.get("uuid")]
