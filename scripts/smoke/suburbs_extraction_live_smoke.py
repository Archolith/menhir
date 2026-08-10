"""Isolated production-path smoke for the Rachel/suburbs extraction fix."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

MENHIR_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(MENHIR_ROOT / "src"))
load_dotenv(MENHIR_ROOT / ".env")

from menhir.config import MemorySettings  # noqa: E402
from menhir.core import build_memory_services, prepare_memory_runtime  # noqa: E402
from menhir.domain import IngestStatus, new_session  # noqa: E402


def _configure_runtime() -> None:
    password = os.getenv("MENHIR_LME_NEO4J_PASSWORD")
    if not password:
        raise RuntimeError("MENHIR_LME_NEO4J_PASSWORD is required")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")
    os.environ["NEO4J_URI"] = os.getenv("MENHIR_LME_NEO4J_URI", "bolt://localhost:7689")
    os.environ["NEO4J_PASSWORD"] = password
    os.environ["GRAPHITI_LLM_PROVIDER"] = "openai"
    os.environ["MEMORY_GRAPHITI_PROVIDER"] = "openai"
    os.environ["LLM_CHAT_PROVIDER"] = "openai"
    os.environ["MEMORY_CHAT_PROVIDER"] = "openai"
    os.environ["OPENAI_CHAT_MODEL"] = os.getenv("MENHIR_EXTRACTION_GATE_MODEL", "gpt-4o-mini")
    os.environ.setdefault("OPENAI_EMBED_MODEL", "text-embedding-3-small")


async def main() -> None:
    _configure_runtime()
    namespace = f"suburbs-fix-smoke-{uuid4().hex[:12]}"
    session = new_session("suburbs-fix-smoke", session_id=namespace)
    output_path = MENHIR_ROOT / "results" / "suburbs_extraction_live_smoke.json"
    built = build_memory_services(MemorySettings.from_env())
    evidence: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "model": os.environ["OPENAI_CHAT_MODEL"],
    }
    succeeded = False
    runtime_ready = False

    try:
        await prepare_memory_runtime(built)
        runtime_ready = True
        # Use the REAL LongMemEval utterance, sentence-split to match the bench
        # ingester.  The original short sentence passed but the full multi-topic
        # message failed because node resolution collapsed "the suburbs" into
        # the existing Chicago entity when other geographies were present.
        episodes = ["user: My friend Rachel lives in Chicago."]
        episodes.extend(f"user: Thanks for the information. Turn {index}." for index in range(12))
        # Final LME message split into sentences (bench splits on `. [A-Z]`):
        episodes.append("user: Miami Beach sounds fun, but I've been there before.")
        episodes.append("user: I'm thinking of somewhere more relaxed.")
        episodes.append(
            "user: My friend Rachel actually just moved back to the suburbs again,"
            " so I was thinking of somewhere not too far from a major city."
        )
        episodes.append("user: Any suggestions?")

        ingest_rows: list[dict[str, object]] = []
        for index, episode in enumerate(episodes):
            result = await built.ingest_service.ingest_episode(
                episode=episode,
                session=session,
                source="suburbs-fix-smoke",
                namespace=namespace,
            )
            ingest_rows.append(
                {
                    "index": index,
                    "episode": episode,
                    "status": result.status.value,
                    "episode_id": result.episode_id,
                    "nodes_touched": result.nodes_touched,
                    "edges_touched": result.edges_touched,
                }
            )
            if result.status is not IngestStatus.INGESTED:
                raise AssertionError(f"Episode {index} did not finish ingestion: {result.status}")
            print(f"ingested {index + 1}/{len(episodes)}", flush=True)

        suburb_entities = built.neo4j.execute(
            """
            MATCH (n:Entity {group_id: $namespace})
            WHERE toLower(coalesce(n.name, '')) CONTAINS 'suburb'
            RETURN n.uuid AS uuid, n.name AS name
            """,
            params={"namespace": namespace},
        )
        suburb_edges = built.neo4j.execute(
            """
            MATCH (rachel:Entity {group_id: $namespace})-[edge:RELATES_TO]-(place:Entity)
            WHERE toLower(rachel.name) = 'rachel'
              AND toLower(place.name) CONTAINS 'suburb'
              AND toLower(coalesce(edge.fact, '')) CONTAINS 'suburb'
            RETURN rachel.name AS source, place.name AS target, edge.fact AS fact,
                   edge.valid_at AS valid_at, edge.invalid_at AS invalid_at,
                   edge.expired_at AS expired_at
            """,
            params={"namespace": namespace},
        )
        chicago_edges = built.neo4j.execute(
            """
            MATCH (rachel:Entity {group_id: $namespace})-[edge:RELATES_TO]-(chicago:Entity)
            WHERE toLower(rachel.name) = 'rachel' AND toLower(chicago.name) = 'chicago'
            RETURN edge.fact AS fact, edge.valid_at AS valid_at,
                   edge.invalid_at AS invalid_at, edge.expired_at AS expired_at
            """,
            params={"namespace": namespace},
        )
        recall = await built.recall_service.recall(
            "Where does Rachel currently live?",
            namespace=namespace,
            include_session=True,
            limit=10,
            update_access=False,
        )
        recall_rows = [asdict(item) for item in recall.results]
        recall_text = json.dumps(recall_rows, default=str).lower()

        evidence.update(
            {
                "ingests": ingest_rows,
                "suburb_entities": suburb_entities,
                "suburb_edges": suburb_edges,
                "chicago_edges": chicago_edges,
                "recall_results": recall_rows,
                "recall_search_error": recall.search_error,
            }
        )
        output_path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")

        assert suburb_entities, "No suburbs entity was created"
        assert suburb_edges, "No Rachel-to-suburbs relationship was created"
        assert chicago_edges, "Establishing Rachel-to-Chicago edge was not created"
        assert all(
            row.get("invalid_at") is not None or row.get("expired_at") is not None
            for row in chicago_edges
        ), "Rachel-to-Chicago remained a current belief"
        assert "suburb" in recall_text, "Recall did not surface the current suburbs value"
        assert recall.search_error is None, f"Recall degraded: {recall.search_error}"
        succeeded = True
        print(json.dumps(evidence, indent=2, default=str))
    finally:
        if runtime_ready:
            await built.ingest_service.shutdown()
            await built.recall_service.shutdown()
        if succeeded:
            if not namespace.startswith("suburbs-fix-smoke-"):
                raise AssertionError(f"Refusing cleanup for unexpected namespace: {namespace}")
            built.neo4j.execute(
                "MATCH (n) WHERE n.group_id = $namespace DETACH DELETE n",
                params={"namespace": namespace},
            )
            remaining = built.neo4j.execute(
                "MATCH (n {group_id: $namespace}) RETURN count(n) AS count",
                params={"namespace": namespace},
            )
            remaining_nodes = int(remaining[0]["count"])
            evidence["cleanup_remaining_nodes"] = remaining_nodes
            output_path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
            assert remaining_nodes == 0, f"Namespace cleanup left {remaining_nodes} nodes"
        await built.graphiti_client.close()
        built.neo4j.close()


if __name__ == "__main__":
    asyncio.run(main())
