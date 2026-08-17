"""CF-211: bounding saga mutation execution, and the budget an ownership TTL must exceed.

Two things are under test.

The **timeout plumbing**, which is easy to get subtly wrong: `Session.run(query, parameters=None,
**kwargs)` treats keywords as Cypher PARAMETERS, so passing `timeout=` there sends a query
parameter named `timeout` and bounds nothing at all. The transaction timeout must ride on
`neo4j.Query(text, timeout=...)`. A test asserting only "no exception" would pass against the
broken version, so these assert on the object actually handed to the driver.

The **budget**, which the ownership TTL has to exceed. Not one attempt: the whole window, across
retries, backoff and every statement the mutation issues.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure import neo4j as n4


class _FakeResult(list):
    def __iter__(self):
        return super().__iter__()


class _FakeSession:
    """Records exactly what was handed to run(), so plumbing can be asserted, not assumed."""

    def __init__(self, recorder):
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **kwargs):
        self._recorder.append((query, kwargs))
        return []


class _FakeDriver:
    def __init__(self, recorder):
        self._recorder = recorder

    def session(self, **_kw):
        return _FakeSession(self._recorder)


@pytest.fixture()
def repo_and_calls(monkeypatch):
    calls: list[tuple[object, dict]] = []
    repo = n4.Neo4jRepository(uri="bolt://x", database="neo4j", user="u", password="p")
    monkeypatch.setattr(repo, "_get_driver", lambda: _FakeDriver(calls))
    return repo, calls


# --------------------------------------------------------------------------- plumbing


@pytest.mark.unit
def test_no_timeout_passes_a_plain_string(repo_and_calls):
    repo, calls = repo_and_calls
    repo.execute("MATCH (n) RETURN n", {"a": 1})

    query, kwargs = calls[0]
    assert isinstance(query, str), "unbounded calls must keep passing a plain string"
    assert kwargs == {"a": 1}


@pytest.mark.unit
def test_timeout_is_carried_on_a_query_object_not_as_a_parameter(repo_and_calls):
    """The whole point of CF-211's correction.

    `session.run(q, timeout=30)` would send a Cypher PARAMETER named 'timeout' and bound nothing --
    a fix that looks applied and does nothing. It must be a neo4j.Query carrying the timeout.
    """
    repo, calls = repo_and_calls
    repo.execute("MATCH (n) RETURN n", {"a": 1}, timeout_s=30.0)

    query, kwargs = calls[0]
    assert isinstance(query, n4.Query), f"expected neo4j.Query, got {type(query).__name__}"
    assert query.timeout == 30.0
    assert "timeout" not in kwargs, "timeout must NOT leak into the Cypher parameters"
    assert kwargs == {"a": 1}, "parameters must be unaffected by bounding"


@pytest.mark.unit
def test_the_query_text_is_preserved_when_bounded(repo_and_calls):
    repo, calls = repo_and_calls
    repo.execute("MATCH (x) RETURN x", timeout_s=5.0)

    query, _ = calls[0]
    assert str(query) == "MATCH (x) RETURN x"


@pytest.mark.unit
def test_a_params_key_named_timeout_is_still_a_parameter(repo_and_calls):
    """Guards the collision the broken form would have caused.

    A query legitimately using $timeout must keep working, and must not be confused with the
    transaction bound.
    """
    repo, calls = repo_and_calls
    repo.execute("RETURN $timeout", {"timeout": 99}, timeout_s=30.0)

    query, kwargs = calls[0]
    assert isinstance(query, n4.Query) and query.timeout == 30.0
    assert kwargs == {"timeout": 99}, "the caller's own $timeout parameter must survive intact"


# --------------------------------------------------------------------------- driver config


@pytest.mark.unit
def test_driver_is_built_with_auto_commit_retries_disabled(monkeypatch):
    """Enforces the assumption mutation_window_seconds() is built on.

    Session.run retries once on its own (MAX_AUTO_COMMIT_RETRIES = 1) unless this is off. That
    retry sits OUTSIDE Menhir's loop, so with it enabled a "3 attempt" mutation could execute six
    times and any TTL derived from the budget would be too short.
    """
    captured: dict = {}

    def _fake_driver(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(n4, "GraphDatabase", type("G", (), {"driver": staticmethod(_fake_driver)}))
    repo = n4.Neo4jRepository(uri="bolt://x", database="neo4j", user="u", password="p")
    repo._get_driver()

    assert captured.get("disable_auto_commit_retries") is True


# --------------------------------------------------------------------------- the budget


@pytest.mark.unit
def test_window_exceeds_a_single_attempt_by_the_retry_factor():
    """The TTL must cover every attempt, not one. This is the error the budget exists to prevent."""
    window = n4.mutation_window_seconds(30.0)
    single_attempt = n4._CONNECTION_ACQUISITION_TIMEOUT_S + 30.0

    assert window > single_attempt * n4._TRANSIENT_RETRIES, "backoff must be included too"
    assert window == pytest.approx(single_attempt * 3 + (0.5 + 1.0 + 2.0))


@pytest.mark.unit
def test_window_includes_connection_acquisition_not_just_the_timeout():
    """A call can wait for a connection before its transaction timeout even starts counting."""
    assert n4.mutation_window_seconds(0.0) > 0.0
    assert n4.mutation_window_seconds(30.0) - n4.mutation_window_seconds(0.0) == pytest.approx(90.0)


@pytest.mark.unit
def test_a_multi_statement_mutation_has_a_proportionally_larger_window():
    """The METRIC_WRITE path issues two separately-bounded, separately-retried statements."""
    one = n4.mutation_window_seconds(30.0, statements=1)
    two = n4.mutation_window_seconds(30.0, statements=2)

    assert two == pytest.approx(one * 2)


@pytest.mark.unit
def test_statements_below_one_is_treated_as_one():
    assert n4.mutation_window_seconds(30.0, statements=0) == n4.mutation_window_seconds(30.0)


@pytest.mark.unit
def test_the_default_ownership_lease_is_currently_too_short_for_the_budget():
    """Documents the live consequence rather than asserting a number that reads as fine.

    The CF-20b default lease is 120s. The bounded single-statement window already exceeds it, so
    the lease CANNOT yet be justified by this budget -- which is exactly why CF-20c live replay
    stays blocked. If a future change makes the lease sufficient, this test should fail and be
    rewritten deliberately.
    """
    from menhir.infrastructure import operation_owner as oo

    window = n4.mutation_window_seconds(n4.SAGA_MUTATION_TIMEOUT_S)
    assert window > oo.DEFAULT_LEASE_SECONDS, (
        "if this fails, the lease now covers the mutation window -- re-derive the safety argument "
        "before treating an expired claim as ABANDONED"
    )
