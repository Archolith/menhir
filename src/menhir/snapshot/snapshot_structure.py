"""Write a scanned snapshot's structure into a view root.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P4).
Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

`promotion` owns the order; this is the step it was holding a place for. Everything written here
is invisible until `canonical_view.publish_root` moves the pointer, and reclaimable by
`view_root.purge_root` if it never does.

## Why this does not reuse `StructureGraphWriter`

The local scan writer and this one look like the same job and are not. It `MERGE`s in place on
`(structure_project, structure_path)` and then deletes whatever the scan no longer sees; this
`CREATE`s into an empty root and never deletes anything, because a file that disappeared is simply
absent from the next root. Parameterising one algorithm into two would mean every future change to
the local path reasoning about a caller that must not prune -- and the local path is the one that
must not break.

## Why the label is different, and why that is the safety property

**Every local read and prune in `structure_queries` matches the plain structure-entity label.** If
snapshot nodes carried it they would be reachable by that module's merge on
`(structure_project, structure_path)` -- so a local scan of a project with the same display name
could mutate them -- and deletable by `_delete_stale_role_entities_multi`, which removes rows for
paths the local scan did not see. A remote snapshot would then be pruned by an unrelated local
scan, which is #99's failure with a new cause.

So snapshot nodes carry `:SnapshotEntity` and never `:Entity`. The separation is structural rather
than a filter someone has to remember to write: no query matching `:Entity` can reach these nodes,
including every query that has not been written yet.

## Why these nodes carry no `group_id`

CF-215 requires every writer of the plain structure-entity label to stamp `group_id`, because that
property carries the tenant namespace those nodes are recalled under. Snapshot nodes are not
recalled by any of those queries -- they are scoped by `(view_root, project_id)` and reachable only
through a published view -- so stamping a namespace here would be inventing a second, weaker
tenancy key beside the one P5 is going to define. P5 adds a structural namespace key for every
structural entity and is where these acquire one; until then the absence is deliberate, and it
fails closed, since a tenancy filter looking for a `group_id` finds no snapshot node rather than
the wrong one.

Nothing here reads, writes or imports `project_scanner` or `structure_queries`. The scan result is
an input, produced by the SAME `ProjectScanner` the local path uses so a snapshot and a local scan
describe a project identically, and `symbol_structure_path` is imported from the domain layer
because that helper exists precisely because two copies of it diverged once.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator

from menhir.domain.utils import symbol_structure_path
from menhir.snapshot.view_root import SNAPSHOT_NODE_LABEL, VIEW_ROOT_PROPERTY

__all__ = [
    "DEFAULT_WRITE_BATCH",
    "WriteReport",
    "snapshot_structure_writer",
    "write_snapshot_structure",
]

#: Rows per statement. Bounded for the same reason the purge is: one statement carrying an entire
#: project's structure is a long lock, and the lease has to be renewable between batches.
DEFAULT_WRITE_BATCH = 500

_NODE = f"{SNAPSHOT_NODE_LABEL}"


class WriteReport(dict):
    """Counts only. Carries no path, no filename and no file content, per invariant 5."""


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _node_rows(scan: Any, root_id: str, project_id: str) -> list[dict[str, Any]]:
    """Every structural node this snapshot contributes, as one flat list.

    Built in full before anything is written, so the write is a sequence of uniform batches rather
    than five separate passes each needing its own renewal and failure reasoning.
    """
    rows: list[dict[str, Any]] = [
        {
            "path": ".",
            "role": "project",
            "name": getattr(scan, "name", "") or "",
            "content": getattr(scan, "description", "") or "",
            "extra": {
                "stack": getattr(scan, "stack", "") or "",
                # The three coverage counts travel with the root. A reader that only fetches the
                # project node can still tell whether a negative structural answer is trustworthy,
                # which is the same reason the local path persists them.
                "files_discovered": int(getattr(scan, "files_discovered", 0) or 0),
                "files_eligible": int(getattr(scan, "files_eligible", 0) or 0),
                "files_indexed": int(getattr(scan, "files_indexed", 0) or 0),
                "partial_index": bool(getattr(scan, "partial_index", False)),
            },
        }
    ]
    rows += [
        {
            "path": d.rel_path,
            "role": "directory",
            "name": d.rel_path.rstrip("/").split("/")[-1],
            "content": getattr(d, "purpose", "") or "",
            "extra": {},
        }
        for d in scan.directories
    ]
    rows += [
        {
            "path": f.rel_path,
            "role": f.role,
            "name": f.rel_path.split("/")[-1],
            "content": f.description or "",
            "extra": {
                "file_mtime": float(f.file_mtime or 0.0),
                "symbols_truncated": bool(f.symbols_truncated),
            },
        }
        for f in scan.files
    ]
    rows += [
        {
            "path": f"dep:{dep}",
            "role": "dependency",
            "name": dep,
            "content": "",
            "extra": {},
        }
        for dep in scan.dependencies
    ]
    rows += [
        {
            "path": symbol_structure_path(s.file_path, s.name, s.parent),
            "role": "symbol",
            "name": s.name,
            "content": s.docstring or "",
            "extra": {
                "symbol_kind": s.kind,
                "symbol_line": int(s.line_no or 0),
                "symbol_signature": s.signature or "",
                "symbol_parent": s.parent or "",
                "symbol_decorator": s.decorator or "",
                "symbol_file": s.file_path,
            },
        }
        for s in scan.symbols
    ]
    rows += [
        {
            "path": f"endpoint:{e.file_path}:{e.name}",
            "role": "endpoint",
            "name": e.name,
            "content": "",
            "extra": {"endpoint_kind": getattr(e, "kind", "") or "", "endpoint_file": e.file_path},
        }
        for e in scan.endpoints
    ]

    for row in rows:
        row[VIEW_ROOT_PROPERTY] = root_id
        row["project_id"] = project_id
    return rows


def _edge_rows(scan: Any) -> list[tuple[str, str, str]]:
    """(relationship, source path, target path) triples, keyed on structure paths within the root.

    Symbol edges use `symbol_structure_path` rather than a locally-built string. `DEFINES` links a
    file to the symbols it declares; `CALLS` links symbol to symbol, which is why `call_edges`
    already carries fully qualified paths and needs no reconstruction here.
    """
    edges: list[tuple[str, str, str]] = []
    edges += [("IMPORTS", i.source_path, i.target_path) for i in scan.imports]
    edges += [("TESTS", t.test_path, t.source_path) for t in scan.test_edges]
    edges += [
        ("DEFINES", s.file_path, symbol_structure_path(s.file_path, s.name, s.parent))
        for s in scan.symbols
    ]
    edges += [("CALLS", c.caller_path, c.callee_path) for c in scan.call_edges]
    edges += [
        ("EXPOSES", e.file_path, f"endpoint:{e.file_path}:{e.name}") for e in scan.endpoints
    ]
    return edges


def write_snapshot_structure(
    neo4j: Any,
    scan: Any,
    *,
    root_id: str,
    project_id: str,
    batch_size: int = DEFAULT_WRITE_BATCH,
    renew: Callable[[], None] | None = None,
) -> WriteReport:
    """Fill `root_id` with this scan's structure. Returns counts.

    `CREATE`, not `MERGE`: a root is new and empty, so there is nothing to merge with, and CREATE
    cannot accidentally reach a node belonging to another root or to the local scan. Promotion
    never hands the same BUILDING root to two writers -- `find_publishable_root` only ever offers a
    COMPLETE one -- so there is no second writer for CREATE to collide with.

    `renew` is called between batches. A lease checked once before a write of this size is a lease
    that expires in the middle of it.
    """
    nodes = _node_rows(scan, root_id, project_id)
    written_nodes = 0
    for batch in _chunks(nodes, batch_size):
        neo4j.execute(
            f"UNWIND $rows AS row "
            f"CREATE (n:{_NODE} {{{VIEW_ROOT_PROPERTY}: row.{VIEW_ROOT_PROPERTY}, "
            f"  project_id: row.project_id, structure_path: row.path, "
            f"  structure_role: row.role, name: row.name, content: row.content}}) "
            f"SET n += row.extra",
            {"rows": batch},
        )
        written_nodes += len(batch)
        if renew is not None:
            renew()

    edge_rows = [
        {"rel": rel, "source": source, "target": target}
        for rel, source, target in _edge_rows(scan)
    ]
    written_edges = 0
    for relationship in sorted({row["rel"] for row in edge_rows}):
        typed = [row for row in edge_rows if row["rel"] == relationship]
        for batch in _chunks(typed, batch_size):
            # BOTH endpoints are matched inside this root. An edge that reached a node in another
            # root would be a cross-snapshot link that no purge could reason about, and would make
            # a reclaimed root corrupt the one that outlived it.
            rows = neo4j.execute(
                f"UNWIND $rows AS row "
                f"MATCH (s:{_NODE} {{{VIEW_ROOT_PROPERTY}: $root, structure_path: row.source}}) "
                f"MATCH (t:{_NODE} {{{VIEW_ROOT_PROPERTY}: $root, structure_path: row.target}}) "
                f"CREATE (s)-[:{relationship}]->(t) "
                f"RETURN count(*) AS made",
                {"rows": batch, "root": root_id},
            )
            written_edges += int(rows[0].get("made") or 0) if rows else 0
            if renew is not None:
                renew()

    return WriteReport(nodes=written_nodes, edges=written_edges)


def snapshot_structure_writer(
    neo4j: Any,
    scan: Any,
    *,
    project_id: str,
    batch_size: int = DEFAULT_WRITE_BATCH,
    report: dict | None = None,
) -> Callable[[str, Callable[[], None]], None]:
    """Adapt :func:`write_snapshot_structure` to the callable `promote_snapshot` expects.

    Pass `report` to receive the counts, since `promote_snapshot` returns a promotion outcome
    rather than a write report and should not grow a channel for one caller's statistics.
    """

    def _write(root_id: str, renew: Callable[[], None]) -> None:
        counts = write_snapshot_structure(
            neo4j,
            scan,
            root_id=root_id,
            project_id=project_id,
            batch_size=batch_size,
            renew=renew,
        )
        if report is not None:
            report.update(counts)

    return _write


def iter_root_paths(neo4j: Any, *, root_id: str, role: str = "") -> Iterable[str]:
    """Structure paths held by a root. Reading a root directly, for verification and tests."""
    clause = " AND n.structure_role = $role" if role else ""
    rows = neo4j.execute(
        f"MATCH (n:{_NODE}) WHERE n.{VIEW_ROOT_PROPERTY} = $root{clause} "
        f"RETURN n.structure_path AS path ORDER BY path",
        {"root": root_id, "role": role} if role else {"root": root_id},
    )
    return [row["path"] for row in rows]
