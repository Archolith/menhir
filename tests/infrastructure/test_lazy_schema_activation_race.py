"""Lazy schema activation must tolerate concurrent first use.

Production symptom: 30 concurrent ``POST /api/phase3/run`` on a fresh graph returned HTTP 500 with
``Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists`` from ``activate_scalar_state``. Neo4j 5 can
raise that for ``CREATE INDEX|CONSTRAINT ... IF NOT EXISTS`` when two transactions create the same
rule together; the rule exists afterwards, so it is success. The offline tests use a fake driver that
reproduces the race; the online test fires real concurrent first activations.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from menhir.infrastructure.schema import get_scalar_state_activation_queries
from menhir.infrastructure.schema_ddl import execute_schema_statement, schema_rule_name
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository

EQUIVALENT = "Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists"


class _FakeClientError(Exception):
    """Stands in for neo4j.exceptions.ClientError: what the helper reads is ``.code``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


_RULE_NAME = re.compile(r"CREATE\s+(?:CONSTRAINT|INDEX)\s+(\w+)")


class _RacingNeo4j:
    """Fake driver. Every CREATE passes its own existence check, but the second creator of a rule
    name loses the race and gets EquivalentSchemaRuleAlreadyExists (the Neo4j 5 behaviour)."""

    def __init__(self, loser_code: str = EQUIVALENT) -> None:
        self.created: set[str] = set()
        self.loser_code = loser_code
        self._lock = threading.Lock()

    def execute(self, query: str, params=None, **_):
        match = _RULE_NAME.search(query)
        if match is None:
            return []  # gate counts, DROP IF EXISTS, backfill MATCH ... SET: no schema race
        with self._lock:
            if match.group(1) in self.created:
                raise _FakeClientError(self.loser_code, f"An equivalent rule already exists: {match.group(1)}")
            self.created.add(match.group(1))
        return []


def test_helper_treats_equivalent_schema_rule_as_success() -> None:
    class _Neo:
        def execute(self, query, params=None, **_):
            raise _FakeClientError(EQUIVALENT)

    execute_schema_statement(_Neo(), "CREATE INDEX i IF NOT EXISTS FOR (n:X) ON (n.p)")


@pytest.mark.parametrize(
    "code",
    [
        "Neo.ClientError.Schema.IndexAlreadyExists",  # different-named rule covers the schema
        "Neo.ClientError.Schema.ConstraintAlreadyExists",
        "Neo.ClientError.Schema.IndexWithNameAlreadyExists",  # same name, different schema
        "Neo.ClientError.Schema.ConstraintWithNameAlreadyExists",
        "Neo.ClientError.Schema.ConstraintCreationFailed",  # data violates the new constraint
        "Neo.ClientError.Statement.SyntaxError",
        "",
    ],
)
def test_helper_reraises_other_client_errors(code: str) -> None:
    error = _FakeClientError(code)

    class _Neo:
        def execute(self, query, params=None, **_):
            raise error

    with pytest.raises(_FakeClientError) as caught:
        execute_schema_statement(_Neo(), "CREATE INDEX i IF NOT EXISTS FOR (n:X) ON (n.p)")
    assert caught.value is error


def test_helper_reraises_non_neo4j_errors() -> None:
    class _Neo:
        def execute(self, query, params=None, **_):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        execute_schema_statement(_Neo(), "CREATE INDEX i IF NOT EXISTS FOR (n:X) ON (n.p)")


def test_scalar_activation_conflicting_rule_still_raises() -> None:
    neo = _RacingNeo4j(loser_code="Neo.ClientError.Schema.IndexWithNameAlreadyExists")
    TypedAssertionRepository(neo).activate_scalar_state()  # first creator: fine
    with pytest.raises(_FakeClientError):
        TypedAssertionRepository(neo).activate_scalar_state()  # real conflict must surface


class _RecordingNeo4j:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list = []

    def execute(self, query: str, params=None, **_):
        self.queries.append(query)
        self.params.append(params)
        return []


def _assert_awaits_exactly_created_rules(queries: list[str], params: list) -> None:
    assert not any("awaitIndexes" in q for q in queries)
    created = [m.group(1) for q in queries if (m := _RULE_NAME.search(q))]
    awaits = [i for i, q in enumerate(queries) if "db.awaitIndex(" in q]
    assert created and len(awaits) == len(created)
    last_create = max(i for i, q in enumerate(queries) if _RULE_NAME.search(q))
    assert min(awaits) > last_create  # all DDL first, then the awaits
    assert [params[i]["name"] for i in awaits] == created
    assert all(params[i]["timeout"] == 60 for i in awaits)


def test_scalar_activation_awaits_only_created_rules_after_ddl() -> None:
    neo = _RecordingNeo4j()
    TypedAssertionRepository(neo).activate_scalar_state()
    _assert_awaits_exactly_created_rules(neo.queries, neo.params)


def test_await_timeout_propagates() -> None:
    class _Neo(_RecordingNeo4j):
        def execute(self, query, params=None, **_):
            if "awaitIndex(" in query:
                raise _FakeClientError("Neo.ClientError.Procedure.ProcedureCallFailed")
            return super().execute(query, params)

    with pytest.raises(_FakeClientError):
        TypedAssertionRepository(_Neo()).activate_scalar_state()


def _run_concurrently(callables) -> list[BaseException]:
    barrier = threading.Barrier(len(callables))
    errors: list[BaseException] = []

    def _go(fn):
        barrier.wait()
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=len(callables)) as pool:
        list(pool.map(_go, callables))
    return errors


def test_concurrent_first_scalar_activation_succeeds_for_every_caller() -> None:
    neo = _RacingNeo4j()
    repos = [TypedAssertionRepository(neo) for _ in range(8)]
    assert _run_concurrently([r.activate_scalar_state for r in repos]) == []
    assert "scalar_reconcile_op_idx" in neo.created
    assert "typed_assertion_operation_idx" in neo.created


# ---------------------------------------------------------------------------
# Online: real Neo4j, real concurrent first activation.
# ---------------------------------------------------------------------------


def _drop_lazy_rules(neo) -> None:
    names = {n for q in get_scalar_state_activation_queries() if (n := schema_rule_name(q))}
    for name in sorted(names):  # constraints first: DROP INDEX on a constraint-backed name errors
        neo.execute(f"DROP CONSTRAINT {name} IF EXISTS")
    for name in sorted(names):
        neo.execute(f"DROP INDEX {name} IF EXISTS")


@pytest.mark.online
def test_online_concurrent_first_activation_does_not_raise(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.services.typed_scalar_service import ensure_scalar_state_activated

    n = 16
    for _ in range(3):  # several fresh rounds make the race likely to actually occur
        test_neo4j_repo.execute("MATCH (n) DETACH DELETE n")
        _drop_lazy_rules(test_neo4j_repo)
        adapters = [MemoryGraphAdapter(test_neo4j_repo) for _ in range(n)]
        errors = _run_concurrently(
            [lambda a=a: ensure_scalar_state_activated(a) for a in adapters]
        )
        assert errors == [], [f"{type(e).__name__}: {e}" for e in errors]
