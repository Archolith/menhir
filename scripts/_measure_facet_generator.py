"""Gate (a) measurement: FACET as GENERATOR over the scoped corpus (dummy 7687).

Reranker-vs-generator decision for R2 Phase 4. Phase 3 already showed FACET-as-
reranker (over the vector recall pool) yields ~0 candidates (sparse overlap). This
measures the GENERATOR role: hand contribute() the full project-scoped corpus and
see whether it surfaces in-scope, facet-coherent memories a vector recall would miss.

Faithful within its limits: no embedder. We proxy "would vector have found it?" with
lexical token-Jaccard between candidate content and the query (LOW jaccard + strong
facet overlap = the net-new a similarity ranker misses). The definitive vector-miss
answer needs the embedder-backed recall pool (gate c). Read-only on the dummy.

Reusable for gate (b): repoint PROJECT + the connection at a hardened/clean fixture to
re-check RELEVANCE (this dummy has synthetic-noise anchors, so it proves mechanism only).
Run: ``.venv/Scripts/python.exe scripts/_measure_facet_generator.py``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from menhir.domain.facet_candidate_source import FacetCandidateSource  # noqa: E402
from menhir.domain.facet_derivation import derive_facets  # noqa: E402
from menhir.domain.facets import FacetedQuery  # noqa: E402
from menhir.infrastructure.facet_graph_reader import Neo4jFacetGraphReader  # noqa: E402
from menhir.infrastructure.neo4j import Neo4jRepository  # noqa: E402

PROJECT = "cth.mcp.memory"
_TOKEN = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main() -> None:
    repo = Neo4jRepository(
        uri="bolt://localhost:7687", database="neo4j",
        user="neo4j", password="menhirdummy123",
    )
    try:
        # BROAD cross-scope pool: all anchored memories. The scope facet must do the
        # filtering here (pre-scoping the pool to project=P double-applies scope and
        # makes the generator degenerate -> returns the whole scope). This is the
        # faithful generator test: scope filters, symbol/operation converge.
        broad = repo.execute(
            """
            MATCH (n:Entity)-[:ANCHORED_TO]->(f) WHERE f.structure_path IS NOT NULL
            OPTIONAL MATCH (n)-[:ANCHORED_TO]->(f2)-[:DEFINES]->(s)
            WHERE s.symbol_kind IS NOT NULL
            WITH n, collect(DISTINCT f.structure_project) AS projs,
                 collect(DISTINCT s.name)[0..6] AS syms
            RETURN n.uuid AS uuid,
                   substring(coalesce(n.content,n.summary,n.name,''),0,140) AS content,
                   CASE WHEN size(projs)=1 THEN projs[0] ELSE null END AS proj,
                   syms
            """,
        )
        pool_uuids = [r["uuid"] for r in broad]
        content_by_uuid = {r["uuid"]: r["content"] or "" for r in broad}
        proj_by_uuid = {r["uuid"]: r["proj"] for r in broad}
        in_scope = sum(1 for r in broad if r["proj"] == PROJECT)
        print(f"broad anchored pool: {len(pool_uuids)} memories; {in_scope} in project={PROJECT!r}")

        # Seed queries from single-project=PROJECT memories that carry symbols.
        seeds = [r for r in broad if r["proj"] == PROJECT and r["syms"]][:5]
        queries: list[tuple[str, str]] = []
        for r in seeds:
            sym = r["syms"][0]
            queries.append((f"how does {sym} work and where is it implemented", sym))
        # Add two operation/topic-scoped queries (no symbol) to exercise scope+operation.
        queries.append(("what changed in the enrichment ingest pipeline", "<topic:ingest>"))
        queries.append(("how is conflict resolution and superseding handled", "<topic:conflict>"))

        source = FacetCandidateSource(Neo4jFacetGraphReader(repo))

        for qtext, tag in queries:
            qfacets = derive_facets(content=qtext, project=PROJECT)
            qpairs = sorted(f"{f}={v}" for f, v in qfacets.discrete_pairs())
            qtok = _tokens(qtext)
            exps = source.contribute(FacetedQuery(facets=qfacets), pool_uuids)

            # Convergence buckets over the FULL candidate set. Structural = symbol|test|file.
            # test/file are convergence-only (not required), so fold in convergence keys too.
            _STRUCT = {"symbol", "test", "file"}
            fam = lambda e: {rf.split("=", 1)[0] for rf in e.matched_required} | set(
                (e.convergence or {}).keys()
            )
            both = [e for e in exps if "project" in fam(e) and fam(e) & _STRUCT]
            symbol_only = [e for e in exps if fam(e) & _STRUCT and "project" not in fam(e)]
            scope_only = [e for e in exps if fam(e) == {"project"}]

            print("\n" + "=" * 78)
            print(f"QUERY[{tag}]: {qtext}")
            print(f"  query facets: {qpairs}")
            print(
                f"  candidates: {len(exps)} of {len(pool_uuids)} pool  "
                f"(flood ratio {len(exps)/max(1,len(pool_uuids)):.0%})"
            )
            print(
                f"  convergence: project+struct={len(both)}  struct_only={len(symbol_only)}  "
                f"scope_only={len(scope_only)}"
            )
            # Are the top-K the convergent ones, and are they truly in-scope?
            top = exps[:8]
            top_conv = sum(1 for e in top if "project" in fam(e) and fam(e) & _STRUCT)
            top_inscope = sum(1 for e in top if proj_by_uuid.get(e.memory_id) == PROJECT)
            print(f"  top-8: {top_conv}/8 convergent, {top_inscope}/8 actually in project={PROJECT!r}")
            for e in top:
                c = content_by_uuid.get(e.memory_id, "")
                jac = _jaccard(qtok, _tokens(c))
                conv = {k: v for k, v in (e.convergence or {}).items() if v}
                print(
                    f"    #{e.rank} score={e.score:.2f} jac={jac:.3f} "
                    f"proj={proj_by_uuid.get(e.memory_id)} req={e.matched_required} conv={conv}"
                )
                print(f"        content: {c[:90]!r}")
    finally:
        repo.close()


if __name__ == "__main__":
    main()
