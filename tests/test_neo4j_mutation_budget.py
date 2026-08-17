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
from menhir.infrastructure import operation_owner as oo


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
def test_the_ownership_lease_now_exceeds_the_mutation_window_by_construction():
    """The inequality the whole safety argument rests on, asserted rather than assumed.

    This test previously asserted the OPPOSITE -- that the hand-picked 120s lease was too short --
    and said it should fail and be rewritten deliberately if the lease ever became sufficient. That
    happened: the TTL is now derived from the window instead of being an independent constant, so
    the relation holds by construction. Rewritten deliberately, as instructed.
    """

    window = n4.mutation_window_seconds(n4.SAGA_MUTATION_TIMEOUT_S)
    assert oo.DEFAULT_LEASE_SECONDS > window, (
        "an expired claim may only be read as ABANDONED if the lease outlives the maximum time a "
        "mutation can still be running"
    )


@pytest.mark.unit
@pytest.mark.parametrize("kind", sorted(oo.SAGA_STATEMENT_COUNTS))
def test_every_saga_kind_gets_a_lease_that_outlives_its_own_window(kind):
    """Per-kind, not just the default: the metric path's two statements need a longer lease."""

    statements = oo.SAGA_STATEMENT_COUNTS[kind]
    window = n4.mutation_window_seconds(n4.SAGA_MUTATION_TIMEOUT_S, statements=statements)

    assert oo.lease_seconds_for_kind(kind) > window


@pytest.mark.unit
def test_the_metric_kind_gets_a_longer_lease_than_a_single_statement_kind():
    """Guards the specific mistake of applying a one-statement TTL to the two-statement path."""

    assert oo.lease_seconds_for_kind("METRIC_WRITE") > oo.lease_seconds_for_kind("ENTITY_MERGE")
    assert oo.SAGA_STATEMENT_COUNTS["METRIC_WRITE"] == 2


@pytest.mark.unit
def test_an_unknown_kind_gets_the_most_conservative_lease():
    """A lease too long only delays recovery; too short lets a live writer be replayed."""

    unknown = oo.lease_seconds_for_kind("SOME_FUTURE_KIND")
    assert unknown == max(oo.lease_seconds_for_kind(k) for k in oo.SAGA_STATEMENT_COUNTS)


@pytest.mark.unit
def test_the_lease_is_derived_not_hardcoded():
    """If the timeout changes, the lease must move with it, or the inequality silently breaks.

    This test previously encoded the derivation as ``W + margin`` and FAILED when that formula was
    corrected -- which is what it was written to do. The lease must outlive the heartbeat detection
    lag as well as the mutation window, and since the lag is ``TTL / RENEW_DIVISOR`` the requirement
    ``TTL > TTL/D + W + M`` solves to ``TTL > D/(D-1) * (W + M)``. Asserted against that formula
    rather than a literal, so a future change to the timeout, the divisor or the margin moves the
    lease with it instead of quietly invalidating the ABANDONED classification.
    """
    window = n4.mutation_window_seconds(n4.SAGA_MUTATION_TIMEOUT_S)
    naive = window + oo.LEASE_SAFETY_MARGIN_S
    expected = oo.RENEW_DIVISOR / (oo.RENEW_DIVISOR - 1) * naive

    assert oo.DEFAULT_LEASE_SECONDS == oo.lease_seconds_for(statements=1)
    assert oo.DEFAULT_LEASE_SECONDS == pytest.approx(expected, abs=1.0)
    assert oo.DEFAULT_LEASE_SECONDS > naive, (
        "the naive window-only bound is insufficient once detection lag is counted"
    )
