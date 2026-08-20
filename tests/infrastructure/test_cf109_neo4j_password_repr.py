"""CF-109: the Neo4j password must not render in `repr`, at any nesting depth.

`repr=False` was already applied to `_driver` and `_driver_lock` -- the two fields that would
merely be noisy -- and omitted from the one field that is a credential. It leaked through plain
`str()`/f-string interpolation (not only an explicit `repr()` call), survived arbitrary nesting,
and propagated through every dataclass holding the repository as a field.

Construction is safe without a database: the driver is created lazily in `_get_driver`.
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.infrastructure.neo4j import Neo4jRepository

SECRET = "SUPERSECRET-PW"
URI = "bolt://h:7687"


def _repo() -> Neo4jRepository:
    return Neo4jRepository(uri=URI, database="neo4j", user="neo4j", password=SECRET)


def test_password_is_absent_from_repr_str_and_nesting() -> None:
    repo = _repo()

    # POSITIVE CONTROL first: repr must still render the object meaningfully. Without this,
    # every absence assertion below would pass against a repr that returned "".
    assert URI in repr(repo)
    assert "Neo4jRepository" in repr(repo)

    assert SECRET not in repr(repo)
    assert SECRET not in f"{repo}"          # plain interpolation, not an explicit repr() call
    assert SECRET not in str({"db": [repo]})  # nested inside a dict inside a list


def test_password_does_not_leak_through_a_holding_dataclass() -> None:
    """The propagation limb: any dataclass holding the repository inherited the leak."""

    @dataclass
    class Facade:
        neo4j: Neo4jRepository

    facade = Facade(neo4j=_repo())

    # POSITIVE CONTROL: the facade's repr does reach into the nested repository.
    assert URI in repr(facade)

    assert SECRET not in repr(facade)
    assert SECRET not in str(facade)


def test_password_is_still_a_required_field() -> None:
    """`field(repr=False)` must not have given the credential a default."""
    import pytest

    with pytest.raises(TypeError):
        Neo4jRepository(uri=URI, database="neo4j", user="neo4j")  # type: ignore[call-arg]
