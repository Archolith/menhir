"""CF-158 criterion 1 -- a re-executed write cannot leave two nodes under one uuid.

`:Entity.uuid` and `:Episodic.uuid` carry NO uniqueness constraint, and criterion 4 established
that they cannot be given one: graphiti owns a plain index on `:Entity(uuid)` and recreates it on
every startup, so Neo4j refuses the constraint outright (`IndexAlreadyExists`) and dropping the
index would put the two in a loop. That closes the schema route permanently, and leaves the write
path as the only place idempotency can live.

So these writes MERGE on the uuid instead of CREATE, with `ON CREATE SET` rather than a bare `SET`:
a MERGE that MATCHES means the node already exists, and overwriting its properties would reset
`status`, `last_accessed`, and every later edit back to the values the original call was born with.
Idempotent means "a second execution changes nothing", not "a second execution rewrites it".

**What this does and does not defend against, stated precisely.** It makes RE-EXECUTION OF THE SAME
STATEMENT WITH THE SAME PARAMETERS a no-op. That is the `Neo4jRepository.execute` retry loop, the
driver's own auto-commit retry, and any future layer that replays a statement verbatim. It does
NOT defend against a caller-level retry: every one of these uuids is minted fresh per call
(`str(uuid4())`), so a re-entered operation mints a NEW uuid and MERGE has nothing to match. That
is a different problem and is not claimed fixed here.

Criterion 2 -- non-idempotent counter mutations (`SET n.hot_count = coalesce(n.hot_count, 0) + 1`)
-- is deliberately untouched. MERGE does nothing for a double-applied increment, and letting a
node-identity fix appear to close the counter half is exactly how the remainder would get lost.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch
from uuid import uuid4

import pytest

_SRC = pathlib.Path("src/menhir")

_CONVERTED_SITES = [
    "infrastructure/temporal_repository.py",
    "infrastructure/todo_repository.py",
    "infrastructure/episode_lifecycle.py",
]


# ---------------------------------------------------------------------------
# Structural ratchets (offline)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("label", ["Entity", "Episodic", "Todo"])
def test_no_uuid_bearing_create_survives_for_the_unconstrained_labels(label: str) -> None:
    """RATCHET. Neither label can be given a uniqueness constraint, so a bare CREATE carrying a
    client-minted uuid is unrecoverable if it is ever re-executed. The needle is assembled at
    runtime so this file does not match itself.
    """
    needle = "CREATE " + "(" + "n:" + label + " {"

    offenders = [
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if needle in path.read_text(encoding="utf-8")
    ]

    assert not offenders, (
        f"bare CREATE of :{label} with a property map at {offenders}; "
        f"MERGE on the uuid with ON CREATE SET instead (CF-158 criterion 1)"
    )


@pytest.mark.unit
@pytest.mark.parametrize("relpath", _CONVERTED_SITES)
def test_the_converted_sites_use_on_create_set_not_a_bare_set(relpath: str) -> None:
    """A MERGE followed by an unqualified SET is not idempotent in the way that matters: it
    matches the existing node and then overwrites it, resetting state a later call had changed.
    """
    text = (_SRC / relpath).read_text(encoding="utf-8")
    merges = sum(
        text.count(f"MERGE ({var}:{label} " + "{uuid:")
        for var in ("n", "r")
        for label in ("Entity", "Episodic", "Todo")
    )

    assert merges >= 1, f"{relpath} no longer contains a uuid MERGE this test can check"
    assert "ON CREATE SET" in text, f"{relpath} MERGEs on uuid without ON CREATE SET"


# ---------------------------------------------------------------------------
# Execution-level proof (online)
# ---------------------------------------------------------------------------


class _CommitThenDrop:
    """A session wrapper that RUNS the statement, then reports the connection as lost.

    This is the ambiguous-commit interleaving that CF-158 is about: the server applied the write
    and the acknowledgement never arrived. The caller cannot tell it from "never applied", so a
    retry re-sends a statement that already committed.
    """

    def __init__(self, inner, fail_times: int, error: type[BaseException]) -> None:
        self._inner = inner
        self._remaining = fail_times
        self._error = error
        self.runs = 0

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def run(self, *args, **kwargs):
        result = self._inner.run(*args, **kwargs)
        list(result)  # force the statement to execute server-side before we drop the connection
        self.runs += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error("connection lost after the server committed")
        return iter([])


def _driver_that_drops_after_committing(repo, fail_times: int):
    """Wrap the live driver so the first `fail_times` executions commit and then raise."""
    from neo4j.exceptions import ServiceUnavailable

    real_driver = repo._get_driver()
    wrappers: list[_CommitThenDrop] = []

    class _Driver:
        def session(self, **kwargs):
            wrapper = _CommitThenDrop(
                real_driver.session(**kwargs),
                fail_times if not wrappers else 0,
                ServiceUnavailable,
            )
            wrappers.append(wrapper)
            return wrapper

    return _Driver(), wrappers


@pytest.mark.online
def test_the_positive_control_shows_the_harness_really_duplicates(test_neo4j_repo) -> None:
    """CONTROL. The historical CREATE shape, run under the same interleaving, MUST leave two nodes.

    Without this the idempotency tests below could pass because the harness never re-executed
    anything -- the failure mode that made three earlier tests in this programme vacuous.
    """
    node_uuid = str(uuid4())
    legacy = "CREATE (c:Entity {uuid: $uuid, name: 'control', type: 'TEMPORAL'})"

    for _ in range(2):
        test_neo4j_repo.execute(legacy, {"uuid": node_uuid})

    rows = test_neo4j_repo.execute(
        "MATCH (c:Entity {uuid: $uuid}) RETURN count(c) AS n", {"uuid": node_uuid}
    )
    assert rows[0]["n"] == 2, "the control did not duplicate; the graph is not seeing both writes"


@pytest.mark.online
def test_a_retried_temporal_entity_write_leaves_exactly_one_node(test_neo4j_repo) -> None:
    """THE FINDING. Server commits, connection drops, retry re-sends -- one uuid, one node."""
    from menhir.infrastructure.temporal_repository import TemporalRepository

    fixed_uuid = str(uuid4())
    repo = TemporalRepository(test_neo4j_repo)

    with patch("menhir.infrastructure.temporal_repository.uuid4", return_value=fixed_uuid):
        repo.create_temporal(content="ship the thing", target_date="2026-12-01")
        # The retry the driver would have made had the statement been declared re-executable.
        repo.create_temporal(content="ship the thing", target_date="2026-12-01")

    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity {uuid: $uuid}) RETURN count(n) AS n", {"uuid": fixed_uuid}
    )
    assert rows[0]["n"] == 1


@pytest.mark.online
def test_a_retried_temporal_write_does_not_reset_state_changed_since(test_neo4j_repo) -> None:
    """ON CREATE SET, not SET. A re-execution must not resurrect a completed reminder."""
    from menhir.infrastructure.temporal_repository import TemporalRepository

    fixed_uuid = str(uuid4())
    repo = TemporalRepository(test_neo4j_repo)

    with patch("menhir.infrastructure.temporal_repository.uuid4", return_value=fixed_uuid):
        repo.create_temporal(content="ship the thing", target_date="2026-12-01")
        test_neo4j_repo.execute(
            "MATCH (n:Entity {uuid: $uuid}) SET n.status = 'completed'", {"uuid": fixed_uuid}
        )
        repo.create_temporal(content="ship the thing", target_date="2026-12-01")

    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity {uuid: $uuid}) RETURN n.status AS status", {"uuid": fixed_uuid}
    )
    assert rows[0]["status"] == "completed", "the re-execution overwrote state set after creation"


@pytest.mark.online
def test_an_ambiguous_commit_on_the_episode_write_leaves_one_episode(test_neo4j_repo) -> None:
    """The same proof driven through `execute`'s own retry loop rather than by calling twice.

    `safe_to_reexecute=True` is passed BY THE TEST. Production still leaves it False -- flipping
    that is a retry-policy decision, separate from making the statement safe to retry.
    """
    from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository

    episode_uuid = str(uuid4())

    class _Recording:
        def __init__(self, inner):
            self.inner = inner
            self.statements: list[tuple[str, dict]] = []

        def execute(self, query, params=None, **kwargs):
            self.statements.append((query, params or {}))
            return self.inner.execute(query, params, **kwargs)

    recorder = _Recording(test_neo4j_repo)
    lifecycle = EpisodeLifecycleRepository()
    lifecycle.neo4j = recorder
    lifecycle.create_pending_episode(
        episode_uuid=episode_uuid, name="ep", content="c",
        session_id="s", user_id="u", source="agent_inference", source_confidence=0.5,
    )

    query, params = recorder.statements[0]
    driver, wrappers = _driver_that_drops_after_committing(test_neo4j_repo, fail_times=1)
    original = test_neo4j_repo._driver
    test_neo4j_repo._driver = driver
    try:
        with patch("menhir.infrastructure.neo4j._TRANSIENT_BACKOFF_BASE", 0.0):
            test_neo4j_repo.execute(query, params, safe_to_reexecute=True)
    finally:
        test_neo4j_repo._driver = original

    assert sum(w.runs for w in wrappers) == 2, "the ambiguous failure did not trigger a re-send"

    rows = test_neo4j_repo.execute(
        "MATCH (e:Episodic {uuid: $uuid}) RETURN count(e) AS n", {"uuid": episode_uuid}
    )
    assert rows[0]["n"] == 1


@pytest.mark.online
def test_a_retried_todo_reminder_leaves_one_entity_and_one_edge(test_neo4j_repo) -> None:
    """The reminder's HAS_REMINDER edge MERGEs too -- a second edge is as wrong as a second node."""
    from menhir.infrastructure.todo_repository import TodoRepository

    todo_uuid = str(uuid4())
    reminder_uuid = str(uuid4())
    repo = TodoRepository(test_neo4j_repo)

    # Both uuids are minted from this module, todo first then reminder, so a re-execution of the
    # method reproduces the same pair -- which is what a verbatim statement replay would send.
    with patch(
        "menhir.infrastructure.todo_repository.uuid4",
        side_effect=[todo_uuid, reminder_uuid, todo_uuid, reminder_uuid],
    ):
        repo.create_todo(content="water the plants", due_date="2026-12-01")
        repo.create_todo(content="water the plants", due_date="2026-12-01")

    rows = test_neo4j_repo.execute(
        """
        MATCH (r:Entity {uuid: $r_uuid})
        OPTIONAL MATCH (t:Todo {uuid: $todo_uuid})-[e:HAS_REMINDER]->(r)
        RETURN count(DISTINCT r) AS nodes, count(DISTINCT t) AS todos, count(e) AS edges
        """,
        {"r_uuid": reminder_uuid, "todo_uuid": todo_uuid},
    )
    assert rows[0]["todos"] == 1, "the parent :Todo duplicated"
    assert rows[0]["nodes"] == 1, "the reminder :Entity duplicated"
    assert rows[0]["edges"] == 1, "a second HAS_REMINDER edge was created"


@pytest.mark.online
def test_the_counter_remainder_is_still_open(test_neo4j_repo) -> None:
    """CRITERION 2, PINNED AS STILL BROKEN -- not an aspiration, an assertion.

    MERGE fixes node identity and does nothing for a double-applied increment. This test exists so
    that nobody reads criterion 1's closure as closing the counter half: if someone later makes
    increments idempotent, this test fails and forces the register to be updated deliberately.
    """
    node_uuid = str(uuid4())
    test_neo4j_repo.execute(
        "CREATE (n:Entity {uuid: $uuid, hot_count: 0})", {"uuid": node_uuid}
    )
    increment = "MATCH (n:Entity {uuid: $uuid}) SET n.hot_count = coalesce(n.hot_count, 0) + 1"

    for _ in range(2):
        test_neo4j_repo.execute(increment, {"uuid": node_uuid})

    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity {uuid: $uuid}) RETURN n.hot_count AS c", {"uuid": node_uuid}
    )
    assert rows[0]["c"] == 2, (
        "the counter became idempotent; CF-158 criterion 2 may now be closable -- "
        "update the register rather than deleting this test"
    )
