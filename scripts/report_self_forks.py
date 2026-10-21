"""Two-tier self Phase 4: per-fork review report for ordinary "user" entities. READ ONLY.

Lists every ordinary entity named "user" in a namespace (the same candidates
`EpisodeLifecycleRepository.detect_self_forks` returns: never the anchored self, the inferred
self, or a derived View) with its facts and source episodes, and writes an approval manifest
template in which every candidate starts `"approved": false`.

The fold itself is NOT implemented here and is blocked: there is no journaled consolidation
tool yet (`.agent/workflows/canonical-self-migration-runbook.md`), and a fold must keep every
fact edge. An operator reviews this report and flips `approved` per uuid; only approved uuids
may enter a future fold, into the INFERRED tier (never the anchored one). Rejected forks stay
ordinary nodes.

Usage:
    python -m scripts.report_self_forks --namespace default --out ./fork-review
    python -m scripts.report_self_forks --namespace proj-a --json

Point NEO4J_URI at the graph to review. This script issues only MATCH/RETURN queries.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from menhir.domain.self_identity import inferred_self_uuid_for_namespace, self_uuid_for_namespace

_EXCERPT = 240


def _excerpt(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= _EXCERPT else value[: _EXCERPT - 3] + "..."


def _iso(value: object) -> str | None:
    return None if value is None else str(value)


def fetch_fork_facts(repo: Any, fork_uuid: str, limit: int) -> list[dict[str, Any]]:
    rows = repo.execute(
        """
        MATCH (f:Entity {uuid: $uuid})-[r:RELATES_TO]-(m:Entity)
        WHERE r.fact IS NOT NULL AND trim(toString(r.fact)) <> ''
        RETURN r.uuid AS edge_uuid, r.fact AS fact, r.name AS relation,
               startNode(r) = f AS outgoing, m.uuid AS other_uuid, m.name AS other_name,
               r.created_at AS created_at, r.expired_at AS expired_at,
               r.valid_at AS valid_at, r.invalid_at AS invalid_at,
               coalesce(r.episodes, []) AS episodes
        ORDER BY r.created_at DESC
        LIMIT $limit
        """,
        params={"uuid": fork_uuid, "limit": max(1, limit)},
    )
    return [
        {
            "edge_uuid": row.get("edge_uuid"),
            "relation": row.get("relation"),
            "direction": "out" if row.get("outgoing") else "in",
            "other": {"uuid": row.get("other_uuid"), "name": row.get("other_name")},
            "fact": row.get("fact"),
            "current": row.get("expired_at") is None,
            "created_at": _iso(row.get("created_at")),
            "valid_at": _iso(row.get("valid_at")),
            "invalid_at": _iso(row.get("invalid_at")),
            "episodes": [str(e) for e in row.get("episodes") or []],
        }
        for row in rows
    ]


def fetch_fork_episodes(repo: Any, fork_uuid: str, limit: int) -> list[dict[str, Any]]:
    rows = repo.execute(
        """
        MATCH (e:Episodic)-[:MENTIONS]->(f:Entity {uuid: $uuid})
        OPTIONAL MATCH (q:Episodic) WHERE q.resolved_episode_uuid = e.uuid
        RETURN e.uuid AS uuid, q.uuid AS queue_uuid,
               coalesce(q.source, e.source) AS source,
               q.turn_role AS turn_role,
               coalesce(q.is_evidence_projection, false) AS is_evidence_projection,
               coalesce(e.created_at, q.created_at) AS created_at,
               coalesce(q.content, e.content) AS content
        ORDER BY created_at DESC
        LIMIT $limit
        """,
        params={"uuid": fork_uuid, "limit": max(1, limit)},
    )
    return [
        {
            "episode_uuid": row.get("uuid"),
            "queue_uuid": row.get("queue_uuid"),
            "source": row.get("source"),
            "turn_role": row.get("turn_role"),
            "is_evidence_projection": bool(row.get("is_evidence_projection")),
            "created_at": _iso(row.get("created_at")),
            "excerpt": _excerpt(row.get("content")),
        }
        for row in rows
    ]


def build_report(repo: Any, lifecycle: Any, namespace: str, limit: int) -> dict[str, Any]:
    anchored = self_uuid_for_namespace(namespace)
    forks = lifecycle.detect_self_forks(namespace=namespace, self_uuid=anchored)
    candidates = []
    for fork_uuid in sorted(forks):
        meta = repo.execute(
            "MATCH (f:Entity {uuid: $uuid}) RETURN f.name AS name, f.group_id AS group_id, "
            "f.summary AS summary, f.created_at AS created_at",
            params={"uuid": fork_uuid},
        )
        row = meta[0] if meta else {}
        candidates.append({
            "uuid": fork_uuid,
            "namespace": namespace,
            "name": row.get("name"),
            "group_id": row.get("group_id"),
            "created_at": _iso(row.get("created_at")),
            "summary": _excerpt(row.get("summary")),
            "facts": fetch_fork_facts(repo, fork_uuid, limit),
            "source_episodes": fetch_fork_episodes(repo, fork_uuid, limit),
        })
    return {
        "namespace": namespace,
        "anchored_self_uuid": anchored,
        "inferred_self_uuid": inferred_self_uuid_for_namespace(namespace),
        "fold_status": "blocked: no journaled consolidation tool; report and manifest only",
        "candidates": candidates,
    }


def manifest_template(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "namespace": report["namespace"],
        "target": "inferred",
        "target_uuid": report["inferred_self_uuid"],
        "instructions": (
            "Set approved=true only for forks whose facts are about the human user. "
            "Unapproved forks stay ordinary nodes. Never target the anchored self."
        ),
        "forks": [
            {"uuid": c["uuid"], "approved": False, "facts": len(c["facts"]),
             "source_episodes": len(c["source_episodes"]), "reviewer_note": ""}
            for c in report["candidates"]
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Self-fork review: namespace `{report['namespace']}`",
        "",
        f"- anchored self: `{report['anchored_self_uuid']}`",
        f"- inferred self: `{report['inferred_self_uuid']}`",
        f"- fold: {report['fold_status']}",
        f"- candidates: {len(report['candidates'])}",
        "",
    ]
    for c in report["candidates"]:
        lines += [f"## `{c['uuid']}` ({c['name']})", "",
                  f"group_id `{c['group_id']}`, created {c['created_at']}", ""]
        lines.append(f"### Facts ({len(c['facts'])})")
        for f in c["facts"]:
            state = "current" if f["current"] else "expired"
            lines.append(f"- [{state}, {f['direction']} {f['other']['name']}] {f['fact']}")
        lines += ["", f"### Source episodes ({len(c['source_episodes'])})"]
        for e in c["source_episodes"]:
            tag = "projection" if e["is_evidence_projection"] else (e["source"] or "?")
            lines.append(f"- `{e['episode_uuid']}` {tag} role={e['turn_role']} {e['created_at']}: "
                         f"{e['excerpt']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--limit", type=int, default=200, help="facts/episodes per fork")
    parser.add_argument("--out", type=Path, help="write report.md, report.json, manifest.json here")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    from menhir.config import MemorySettings
    from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
    from menhir.infrastructure.neo4j import Neo4jRepository

    settings = MemorySettings.from_env()
    repo = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    lifecycle = EpisodeLifecycleRepository()
    lifecycle.neo4j = repo
    try:
        report = build_report(repo, lifecycle, args.namespace, args.limit)
    finally:
        close = getattr(repo, "close", None)
        if close is not None:
            close()
    manifest = manifest_template(report)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (args.out / "report.md").write_text(render_markdown(report), encoding="utf-8")
        (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"{len(report['candidates'])} candidate(s); wrote {args.out}")
    elif args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
