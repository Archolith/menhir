"""Structural anchoring — bridge semantic memories to structural code entities.

Provides path extraction, normalization, structural entity resolution,
anchor edge creation, and reverse lookup for the structural-semantic
integration pipeline.
"""

from __future__ import annotations

import logging
import re
from itertools import islice, product
from typing import Any
from uuid import uuid4

from menhir.domain.structure_paths import (
    is_absolute_spelling,
    normalize_query_path,
    path_relative_to_root,
)
from menhir.infrastructure.neo4j import Neo4jRepository

logger = logging.getLogger(__name__)

MAX_ANCHOR_EDGES_PER_EPISODE = 256

# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------

_FILE_EXTENSIONS = r"py|tsx?|jsx?|java|kt|go|rs|yaml|toml|json|sql|sh"
FILE_PATH_PATTERN = re.compile(
    rf"""(?P<quote>[`"'])(?P<quoted>(?:[A-Za-z]:)?[/\\][^`"'\r\n]+?\.(?:{_FILE_EXTENSIONS}))(?P=quote)"""
    rf"|(?P<unquoted>(?:(?:[A-Za-z]:)?[/\\]{{1,2}}(?:[\w.-]+[/\\])*|(?:[\w.-]+[/\\])+)"
    rf"[\w.-]+\.(?:{_FILE_EXTENSIONS})\b)"
)

BARE_FILENAME_PATTERN = re.compile(
    rf"\b([\w.-]+\.(?:{_FILE_EXTENSIONS}))\b",
)


def extract_file_paths(text: str) -> list[str]:
    """Extract file path references from text.

    Full paths (e.g. ``src/menhir/services/foo.py``) take priority.
    Bare filenames (e.g. ``foo.py``) are only included when they are not
    already a tail segment of a returned full path.
    """
    if not text:
        return []

    full_paths = list(dict.fromkeys(
        match["quoted"] or match["unquoted"] for match in FILE_PATH_PATTERN.finditer(text)
    ))
    bare_names = list(dict.fromkeys(BARE_FILENAME_PATTERN.findall(FILE_PATH_PATTERN.sub(" ", text))))

    # Deduplicate: exclude bare names that are tails of full paths
    full_tails = {p.replace("\\", "/").rsplit("/", 1)[-1] for p in full_paths}
    unique_bare = [b for b in bare_names if b not in full_tails]

    return full_paths + unique_bare


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

def resolve_file_reference(
    path: str,
    projects: list[dict[str, str]],
    project: str | None = None,
) -> tuple[str | None, str] | None:
    """Resolve an absolute reference to its deepest indexed root and project."""
    if project and sum(entry.get("name") == project for entry in projects) > 1:
        return None
    if not is_absolute_spelling(path):
        relative = normalize_query_path(path)
        if "/" not in relative and path.strip().replace("\\", "/").startswith("./"):
            relative = f"./{relative}"
        return project, relative

    matches = []
    for entry in projects:
        name = entry.get("name")
        root = entry.get("root_path")
        if name and root and (project is None or name == project):
            relative = path_relative_to_root(path, root)
            if relative:
                matches.append((name, relative))
    if not matches:
        return None

    # Roots matching the same absolute path are nested or identical. The deepest
    # one removes the longest prefix, including across equivalent drive spellings.
    shortest = min(len(relative) for _, relative in matches)
    best = {(name, relative) for name, relative in matches if len(relative) == shortest}
    if len(best) != 1:
        return None
    name, relative = best.pop()
    if sum(entry.get("name") == name for entry in projects) != 1:
        return None
    # Preserve a root-level path's explicit directory so it cannot match a basename in a subfolder.
    return name, relative if "/" in relative else f"./{relative}"


# ---------------------------------------------------------------------------
# Narrative / diff content splitting
# ---------------------------------------------------------------------------

_DIFF_SEPARATOR = "\n\n--- git diff ---\n"


def extract_diff_file_paths(diff: str) -> list[str]:
    """Strip Git's a/ and b/ prefixes only in diff file headers."""
    paths = []
    for line in diff.splitlines():
        references = extract_file_paths(line)
        if line.startswith(("diff --git ", "--- ", "+++ ")):
            references = [p[2:] if p.startswith(("a/", "b/")) else p for p in references]
        paths.extend(references)
    return list(dict.fromkeys(paths))


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
    project_filter: str | list[str] | None = None,
) -> list[dict[str, str]]:
    """Resolve each file reference only when it identifies one indexed entity."""
    if not candidate_paths or project_filter == []:
        return []

    projects = [project_filter] if isinstance(project_filter, str) else project_filter
    project_clause = "AND n.structure_project IN $projects" if projects is not None else ""
    rows = neo4j.execute(
        f"""
        UNWIND $paths AS reference
        WITH reference, CASE WHEN reference STARTS WITH './' THEN substring(reference, 2)
                             ELSE reference END AS candidate
        CALL (reference, candidate) {{
            MATCH (n:Entity)
            WHERE n.structure_role IN ['file', 'File', 'entrypoint', 'config', 'test']
              AND n.uuid IS NOT NULL
              AND (n.structure_path = candidate
                   OR (NOT reference CONTAINS '/' AND n.structure_path ENDS WITH '/' + candidate))
              {project_clause}
            WITH n LIMIT 2
            RETURN collect(n) AS matches
        }}
        WITH matches WHERE size(matches) = 1
        WITH matches[0] AS n
        RETURN DISTINCT n.uuid AS uuid,
               n.structure_project AS project,
               n.structure_path AS path
        ORDER BY project, path, uuid
        """,
        params={
            "paths": list(dict.fromkeys(candidate_paths)),
            **({"projects": projects} if projects is not None else {}),
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
    max_edges: int = MAX_ANCHOR_EDGES_PER_EPISODE,
) -> int:
    """Create ANCHORED_TO edges from semantic entities to structural entities.

    Returns the number of pairs processed, including pre-existing edges.
    """
    if not semantic_uuids or not structural_uuids or max_edges <= 0:
        return 0

    semantic = list(dict.fromkeys(semantic_uuids))
    structural = list(dict.fromkeys(structural_uuids))
    if len(semantic) * len(structural) > max_edges:
        logger.info("Structural anchor pairs capped at %d for %s", max_edges, anchor_source)
    pairs = [
        {
            "sem": sem_uuid,
            "struct": struct_uuid,
            "edge_uuid": str(uuid4()),
            "anchor_source": anchor_source,
            "weight": weight,
        }
        for struct_uuid, sem_uuid in islice(product(structural, semantic), max_edges)
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
