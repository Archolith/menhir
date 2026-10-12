"""Live-graph test of scripts/backfill_admitted_on.py (review R1/R2 of #273).

An ADMITTED_ON edge plus content equal to the turn text is what `resolve_turn_role` accepts as proof
of the speaker (#271), so the backfill must link only user turns to user-or-undeclared episodes. It
must still link the legacy `user: `-prefixed bench corpus (provenance only).

Run with:  pytest --run-online -m online tests/test_backfill_admitted_on_live.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backfill_admitted_on import _APPLY, _SURVEY, _UNMATCHED_SAMPLE  # noqa: E402

pytestmark = [pytest.mark.online]

# case -> (turn role/declarant, turn source_kind, episode content template, episode turn_role, linked?)
CASES = {
    "raw_undeclared": ("user", "hook", "{text}", None, True),
    "raw_declared_user": ("user", "hook", "{text}", "user", True),
    "legacy_prefixed": ("user", None, "user: {text}", None, True),
    "declared_assistant": ("user", "hook", "{text}", "assistant", False),
    "assistant_turn": ("assistant", "hook", "{text}", None, False),
    "agent_quoted_turn": ("user", "agent_quoted", "{text}", None, False),
}


@pytest.fixture
def seeded(test_neo4j_repo):
    prefix = f"backfill-r12-{uuid.uuid4().hex[:8]}-"
    namespace = prefix + "ns"
    for case, (role, source_kind, template, turn_role, _linked) in CASES.items():
        text = f"Turn text for {case}."
        test_neo4j_repo.execute(
            """
            CREATE (:TurnEvidence {turn_id: $turn_id, namespace: $ns, role: $role,
                                   declarant: $role, source_kind: $source_kind, text: $text})
            CREATE (:Episodic {uuid: $episode_uuid, group_id: $ns, content: $content,
                               turn_role: $turn_role, created_at: datetime()})
            """,
            params={
                "turn_id": f"{prefix}turn-{case}", "ns": namespace, "role": role,
                "source_kind": source_kind, "text": text,
                "episode_uuid": f"{prefix}ep-{case}", "content": template.format(text=text),
                "turn_role": turn_role,
            },
        )
    yield test_neo4j_repo, prefix
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace STARTS WITH $p OR n.group_id STARTS WITH $p DETACH DELETE n",
        params={"p": prefix},
    )


def _linked_cases(repo, prefix: str) -> set[str]:
    rows = repo.execute(
        """
        MATCH (e:Episodic)-[:ADMITTED_ON]->(t:TurnEvidence)
        WHERE t.namespace STARTS WITH $p
        RETURN e.uuid AS episode, t.turn_id AS turn
        """,
        params={"p": prefix},
    )
    cases = set()
    for row in rows:
        case = row["turn"].removeprefix(f"{prefix}turn-")
        assert row["episode"] == f"{prefix}ep-{case}"
        cases.add(case)
    return cases


def test_backfill_links_only_user_turns_to_user_or_undeclared_episodes(seeded) -> None:
    repo, prefix = seeded
    repo.execute(_APPLY, params={"prefix": prefix})
    expected = {case for case, spec in CASES.items() if spec[-1]}
    assert _linked_cases(repo, prefix) == expected


def test_survey_counts_only_eligible_user_turns(seeded) -> None:
    repo, prefix = seeded
    before = repo.execute(_SURVEY, params={"prefix": prefix})[0]
    # Eligible turns: the four user, non-agent_quoted ones; declared_assistant has no eligible episode.
    assert before == {
        "turns": 4, "already_linked": 0, "linkable": 3, "ambiguous": 0, "unmatched": 1,
    }
    unmatched = repo.execute(_UNMATCHED_SAMPLE, params={"prefix": prefix})
    assert [row["turn_id"] for row in unmatched] == [f"{prefix}turn-declared_assistant"]
    repo.execute(_APPLY, params={"prefix": prefix})
    after = repo.execute(_SURVEY, params={"prefix": prefix})[0]
    assert after["already_linked"] == 3 and after["linkable"] == 0


def test_backfill_is_idempotent(seeded) -> None:
    repo, prefix = seeded
    repo.execute(_APPLY, params={"prefix": prefix})
    repo.execute(_APPLY, params={"prefix": prefix})
    count = repo.execute(
        "MATCH (:Episodic)-[r:ADMITTED_ON]->(t:TurnEvidence) WHERE t.namespace STARTS WITH $p "
        "RETURN count(r) AS c",
        params={"p": prefix},
    )[0]["c"]
    assert count == 3
