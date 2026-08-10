"""Live smoke for slice 4: frontmatter declarations and their resolution."""

from __future__ import annotations

from dotenv import load_dotenv

from menhir.config.settings_model import MemorySettings
from menhir.domain.work_artifact import ArtifactMedium, ArtifactSourceSpec, ArtifactType
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository


def _source(path: str) -> ArtifactSourceSpec:
    return ArtifactSourceSpec(
        medium=ArtifactMedium.MARKDOWN,
        locator={"repository": "menhir", "path": path},
    )


def main() -> int:
    load_dotenv()
    s = MemorySettings.from_env()
    n = Neo4jRepository(
        uri=s.neo4j_uri, user=s.neo4j_user, password=s.neo4j_password, database=s.neo4j_database
    )
    repo = WorkArtifactRepository(n)

    old = repo.create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="smoke oauth v1",
        source=_source(".agent/plans/smoke-oauth-v1.md"),
        namespace="smoke",
    )
    new = repo.create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="smoke oauth v2",
        source=_source(".agent/plans/smoke-oauth-v2.md"),
        namespace="smoke",
    )
    report = repo.create_artifact(
        artifact_type=ArtifactType.IMPLEMENTATION_REPORT,
        title="smoke oauth report",
        source=_source(".agent/for-review/smoke-oauth-report.md"),
        namespace="smoke",
    )

    out = repo.declare_from_frontmatter(
        new["artifact_uuid"],
        {
            "supersedes": ["smoke-oauth-v1"],   # by filename stem
            "about": ["OAuth"],                 # existing semantic entity
            "reviews": ["smoke oauth report"],  # illegal: a plan cannot review
            "author": "ctharvey",               # not a declaration key
        },
    )

    # An implementation report implementing a plan that does not exist yet.
    late_out = repo.declare_from_frontmatter(
        report["artifact_uuid"], {"implements": ["never-written"]}
    )
    out["results"].extend(late_out["results"])

    print("ignored keys  :", out["ignored"])
    for r in out["results"]:
        print(f"  {r['key']:<12} {r['raw_target']:<20} {r['status']:<11} {r.get('reason') or ''}")

    old_status = n.execute(
        "MATCH (a:WorkArtifact {artifact_uuid: $u}) RETURN a.status AS s",
        {"u": old["artifact_uuid"]},
    )
    print("v1 status     :", old_status[0]["s"], "<- moved by the declaration")

    # Re-declaring identical frontmatter must not duplicate or re-litigate.
    repo.declare_from_frontmatter(new["artifact_uuid"], {"supersedes": ["smoke-oauth-v1"]})
    stored = repo.declarations(new["artifact_uuid"])
    print("declarations  :", len(stored), "records after a second identical pass")
    print("retained raw  :", [d["raw_target"] for d in stored if d["resolution_status"] != "resolved"])

    # The unresolvable one resolves once its target exists -- no document edit.
    late = repo.create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="never-written",
        source=_source(".agent/plans/never-written.md"),
        namespace="smoke",
    )
    retry = repo.resolve_declarations(report["artifact_uuid"])
    print("retry         :", [(r["raw_target"], r["status"], r.get("reason")) for r in retry])

    for uuid in (
        old["artifact_uuid"], new["artifact_uuid"],
        report["artifact_uuid"], late["artifact_uuid"],
    ):
        repo.delete_artifact(uuid)
    orphans = n.execute(
        "MATCH (d:ArtifactDeclaration) WHERE NOT (:WorkArtifact)-[:DECLARES]->(d) "
        "RETURN count(d) AS c",
        {},
    )
    print("cleanup       : orphan declarations:", orphans[0]["c"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
