"""Integration smoke-test: create_pending_episode persists reference_time to Neo4j.

Run with:
  PYTHONPATH=src python scripts/_integ_reference_time.py

Requires menhir-neo4j-dummy on bolt://localhost:7687  (neo4j / menhirdummy123).
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

FRONTIER_SRC = str(Path(__file__).resolve().parents[1] / "src")
if FRONTIER_SRC not in sys.path:
    sys.path.insert(0, FRONTIER_SRC)

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "menhirdummy123"
TEST_NS = "integ-reference-time-test"
BACKDATED = datetime(2023, 7, 14, 8, 30, tzinfo=timezone.utc)


def main() -> int:
    from menhir.infrastructure.neo4j import Neo4jRepository
    from menhir.infrastructure.episode_repository import EpisodeRepository

    neo4j = Neo4jRepository(
        uri=NEO4J_URI,
        database="neo4j",
        user=NEO4J_USER,
        password=NEO4J_PASSWORD,
    )
    repo = EpisodeRepository(neo4j)

    # Clean up any prior run
    neo4j.execute(
        "MATCH (e:Episodic {namespace: $ns}) DETACH DELETE e",
        params={"ns": TEST_NS},
    )

    test_uuid = uuid.uuid4().hex

    # ---- exercise the changed code path ----
    repo.create_pending_episode(
        episode_uuid=test_uuid,
        name="test-episode",
        content="Alice moved to Seattle in July 2023.",
        session_id="integ-session-1",
        user_id="integ-user",
        source="test",
        source_confidence=1.0,
        namespace=TEST_NS,
        reference_time=BACKDATED,
    )

    # ---- verify Neo4j node ----
    rows = neo4j.execute(
        "MATCH (e:Episodic {uuid: $uuid}) RETURN e.reference_time AS rt, e.queued_at AS qa",
        params={"uuid": test_uuid},
    )
    neo4j.close()

    if not rows:
        print("FAIL — no Episodic node found", flush=True)
        return 1

    rt = rows[0]["rt"]
    qa = rows[0]["qa"]
    print(f"  reference_time : {rt!r}", flush=True)
    print(f"  queued_at      : {qa!r}", flush=True)

    if rt is None:
        print("FAIL — reference_time is None; not persisted", flush=True)
        return 1

    year = rt.year if hasattr(rt, "year") else int(str(rt)[:4])
    if year != 2023:
        print(f"FAIL — reference_time year={year}, expected 2023", flush=True)
        return 1

    # queued_at must NOT be 2023 — separation check
    if qa is not None and hasattr(qa, "year") and qa.year == 2023:
        print("FAIL — queued_at was also backdated; reference_time/queued_at separation broken", flush=True)
        return 1

    qa_year = qa.year if (qa is not None and hasattr(qa, "year")) else "?"
    print(
        f"PASS — reference_time.year={year} (2023), queued_at.year={qa_year} (separation preserved)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
