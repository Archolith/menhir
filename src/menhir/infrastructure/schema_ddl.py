"""Concurrency-safe execution of lazily-run schema statements.

``CREATE INDEX|CONSTRAINT ... IF NOT EXISTS`` is only name-guarded in the planner: in Neo4j 5, two
transactions creating the same rule at the same moment can both pass the existence check, and the
loser fails with ``Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists``. The rule the loser
asked for now exists, so that outcome is success for the caller. This matters for DDL that is
created lazily on first use (scalar, event-history and counting activation) rather than at startup,
because several requests can hit the first use together.

Only the "an equivalent rule already exists" code is treated as success. A rule that conflicts
(same name, different schema; a constraint that cannot come online over violating data) still
raises, as does every other error.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_CREATE_RULE_NAME = re.compile(r"CREATE\s+(?:CONSTRAINT|INDEX)\s+(\w+)")

#: Neo4j status code meaning "the schema rule you asked for is already in place" -- what Neo4j 5
#: raises when two transactions create the identical rule at once. Deliberately NOT included:
#: ``IndexAlreadyExists`` / ``ConstraintAlreadyExists`` mean a rule with a DIFFERENT name already
#: covers that schema, so the named rule is still missing -- a real conflict, not the race.
EQUIVALENT_SCHEMA_RULE_EXISTS_CODES: frozenset[str] = frozenset(
    {"Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists"}
)

#: Upper bound for waiting on racing/populating indexes to come ONLINE after activation.
AWAIT_ONLINE_TIMEOUT_S = 60


def is_equivalent_schema_rule_exists(exc: BaseException) -> bool:
    """True when ``exc`` is a Neo4j error saying an equivalent index/constraint already exists."""
    return str(getattr(exc, "code", "") or "") in EQUIVALENT_SCHEMA_RULE_EXISTS_CODES


def execute_schema_statement(neo4j: Any, query: str) -> None:
    """Run one schema statement, treating "equivalent rule already exists" as success.

    Safe for concurrent first use from several threads or processes: the statements are idempotent
    and the only race outcome is that benign error. Anything else is re-raised unchanged.
    """
    try:
        neo4j.execute(query)
    except Exception as exc:  # noqa: BLE001 - filtered by code below, re-raised otherwise
        if not is_equivalent_schema_rule_exists(exc):
            raise


def schema_rule_name(query: str) -> str | None:
    """Name of the rule a ``CREATE INDEX|CONSTRAINT <name> ...`` statement creates, else None."""
    match = _CREATE_RULE_NAME.search(query)
    return match.group(1) if match else None


def await_schema_online(neo4j: Any, names: Iterable[str], timeout_s: int = AWAIT_ONLINE_TIMEOUT_S) -> None:
    """Block (bounded) until each named index (or constraint-backing index) is ONLINE.

    A caller that lost the creation race returns immediately while the winner's index may still be
    POPULATING; the ONLINE readiness check would then fail spuriously. Only the rules this
    activation created are awaited, so an unrelated populating or failed index cannot stall or
    break the lane. A timeout propagates.
    """
    for name in names:
        neo4j.execute("CALL db.awaitIndex($name, $timeout)", {"name": name, "timeout": int(timeout_s)})
