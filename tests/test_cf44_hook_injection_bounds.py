"""CF-44 -- the hook block is bounded end to end, and the turn gate fails closed.

Three independent defects on the surface CF-39 established, all confirmed at source before fixing:

1. `list_temporal_in_window` was called with **no limit**, unlike its two siblings (flagged at 10,
   TODOs at 5). Every open TEMPORAL node in a +/-30-day window became a line.
2. `--max-tokens` reached only `build_context`, so it capped the Context section alone. Reminders,
   TODOs and Pinned were assembled outside any budget.
3. `should_run_this_turn` reset `data = {}` on an unreadable counter and swallowed write failures.
   The gate is `count % frequency == 0`, so a reset counter gives `0 % N == 0` -> True: a corrupt
   or unwritable file made recall fire on **every** turn instead of every Nth. The gate degraded in
   the expensive direction, which is the half of this finding that costs real money.

OWNER RULING 2026-08-22 on the budget's scope: `--max-tokens` becomes the budget for the whole
injected block, with a **reserved floor for Context** so the section the hook exists for cannot be
starved by the lists above it. That mirrors `context_builder`, which already reserves for its own
TODO section rather than letting one part consume the total.

**Two lines are deliberately outside the budget, and saying so is the point.** `temporal_line` and
`write_nudge` are generated locally from the clock and the current prompt. They carry no stored
text, so no amount of graph content can inflate them. The bound this offers is *everything
graph-derived is budgeted* -- which is the property that matters, since the block is injected into
an agent's turn and the graph is writable by anything with write access.

**The missing-file case is the trap in defect 3.** Failing closed on an unreadable counter is
right; failing closed on an *absent* one would mean a fresh install never recalls at all. Those two
look identical if you only test "the file did not parse".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from menhir.cli.output import (
    CONTEXT_RESERVE_FRACTION,
    DEFAULT_HOOK_TOKEN_BUDGET,
    REMINDER_LIMIT,
    format_hook_output,
    should_run_this_turn,
)

pytestmark = pytest.mark.unit


def _reminders(count: int, *, size: int = 70) -> list[dict]:
    return [
        {"target_date": f"2026-09-{(i % 28) + 1:02d}", "content": f"reminder {i} " + "x" * size}
        for i in range(count)
    ]


def _todos(count: int) -> list[dict]:
    return [{"priority": "high", "content": f"todo {i} " + "y" * 60} for i in range(count)]


def _pinned(count: int) -> list[dict]:
    return [{"name": f"pin{i}", "content": f"pinned {i} " + "z" * 60} for i in range(count)]


# ---------------------------------------------------------------------------
# Defect 1 -- the reminder query carries a bound
# ---------------------------------------------------------------------------


class _RecordingNeo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query: str, params: dict) -> list[dict]:
        self.calls.append((query, params))
        return []


def _temporal_repo() -> tuple[object, _RecordingNeo4j]:
    from menhir.infrastructure.temporal_repository import TemporalRepository

    neo4j = _RecordingNeo4j()
    return TemporalRepository(neo4j), neo4j


def test_a_limit_reaches_the_query_rather_than_being_trimmed_later() -> None:
    """THE FINDING. Trimming in the formatter would still transport and parse every row; the bound
    belongs in Cypher."""
    repo, neo4j = _temporal_repo()

    repo.list_in_window(window_days=30, limit=10)

    query, params = neo4j.calls[0]
    assert "LIMIT $limit" in query
    assert params["limit"] == 10


def test_omitting_the_limit_still_returns_the_whole_window() -> None:
    """POSITIVE CONTROL. This narrows the HOOK, not everything that lists reminders -- the
    reminder tooling legitimately wants the full window, and a mandatory bound would have silently
    truncated it."""
    repo, neo4j = _temporal_repo()

    repo.list_in_window(window_days=30)

    query, params = neo4j.calls[0]
    assert "LIMIT" not in query
    assert params["limit"] is None


def test_an_absurd_limit_is_clamped_like_the_window_already_was() -> None:
    """`window_days` was already clamped to 365. A limit that is not clamped is the same hazard
    with a different name."""
    repo, neo4j = _temporal_repo()

    repo.list_in_window(window_days=30, limit=10_000_000)

    assert neo4j.calls[0][1]["limit"] == 500


def test_the_hook_asks_for_a_bounded_number_of_reminders() -> None:
    """TRAP T17. The repository accepting a limit proves nothing about the caller passing one --
    and the caller not passing one is the entire finding."""
    import inspect

    from menhir.cli import hook

    source = inspect.getsource(hook)
    assert source.count("limit=REMINDER_LIMIT") == 2, (
        "both the prompt and postcompact hook paths must bound the reminder query"
    )
    assert REMINDER_LIMIT > 0


# ---------------------------------------------------------------------------
# Defect 2 -- the budget covers the whole block
# ---------------------------------------------------------------------------


def test_a_flood_of_reminders_cannot_produce_an_unbounded_block() -> None:
    """THE FINDING. 400 open reminders used to become 400 lines, injected into a turn."""
    unbudgeted = format_hook_output([], None, None, temporal_memories=_reminders(400),
                                    max_tokens=10_000_000)
    budgeted = format_hook_output([], None, None, temporal_memories=_reminders(400))

    assert len(budgeted) < len(unbudgeted) / 4, "the budget did not bite on 400 reminders"
    assert "more omitted for budget" in budgeted


def test_the_omission_is_announced_rather_than_silent() -> None:
    """A block that silently drops 380 reminders is worse than a long one: the reader cannot tell
    the difference between "nothing due" and "too much to show"."""
    output = format_hook_output([], None, None, temporal_memories=_reminders(400))

    assert "### Reminders (400)" in output, "the true total must survive the trim"
    assert "more omitted for budget" in output


def test_reminders_cannot_starve_the_pinned_section() -> None:
    """THE RESERVATION, and the reason it is per-section rather than one running total. Pinned is
    user-flagged -- the operator explicitly asked for it -- so it is the least droppable thing in
    the block. First-come-first-served over a single pool would let a long reminder list consume
    the lot before Pinned is even reached."""
    output = format_hook_output(
        _pinned(10), None, None,
        temporal_memories=_reminders(400),
        todos=_todos(5),
    )

    assert "### Pinned (10)" in output
    assert "pinned 0" in output


def test_context_keeps_its_reserved_floor_under_a_flood() -> None:
    """The owner's ruling as an assertion. Context is what the hook exists for; a budget that let
    the list sections consume all of it would be bounded and useless."""
    context = "\n".join(f"- [0.9] recalled memory line {i} " + "q" * 60 for i in range(400))

    # ALL THREE list sections are saturated on purpose. The floor only binds when the lists
    # can actually spend their whole allowance -- flooding Reminders alone leaves TODOs' and
    # Pinned's reserved shares unspent, so context receives them as surplus and the test
    # passes whether or not a floor exists. Established by mutation: the single-flood version
    # could not tell the difference even with BOTH floor guarantees removed.
    output = format_hook_output(
        _pinned(400), context, "q",
        temporal_memories=_reminders(400),
        todos=_todos(400),
    )

    assert "### Context" in output
    assert "recalled memory line 0" in output, "context was starved by the list sections"


def test_context_takes_the_surplus_when_the_lists_are_short() -> None:
    """The floor is a floor, not a cap. With nothing above it, Context should get materially more
    than its reserved share -- otherwise the reservation has quietly become a ceiling."""
    context = "\n".join(f"- [0.9] line {i} " + "q" * 60 for i in range(400))

    def _context_body(block: str) -> str:
        # Compare the CONTEXT section, not the whole block -- the crowded block is longer overall
        # precisely because it carries the list sections, so a length comparison on the block
        # measures the opposite of what this test is about.
        return block.split("```text", 1)[1]

    alone = _context_body(format_hook_output([], context, "q"))
    crowded = _context_body(format_hook_output(
        _pinned(10), context, "q", temporal_memories=_reminders(400), todos=_todos(5)
    ))

    assert len(alone) > len(crowded) * 1.3, (
        f"context got {len(alone)} chars alone vs {len(crowded)} crowded; "
        "the reserved floor has become a ceiling"
    )


def test_a_budget_of_zero_still_produces_a_well_formed_block() -> None:
    """Degenerate input reaches this through a CLI flag. Dividing the allowance three ways, or
    trimming to a zero budget, must not raise."""
    output = format_hook_output(
        _pinned(3), "some context", "q", temporal_memories=_reminders(3), max_tokens=0
    )

    assert isinstance(output, str)


def test_the_cli_default_and_the_budget_default_are_one_constant() -> None:
    """The flag feeds the budget. Two literals would drift, which is the CF-76 / CF-150 shape."""
    import inspect

    from menhir.cli import hook

    assert "] = DEFAULT_HOOK_TOKEN_BUDGET," in inspect.getsource(hook.run)
    assert 0 < CONTEXT_RESERVE_FRACTION < 1
    assert DEFAULT_HOOK_TOKEN_BUDGET > 0


def test_the_hook_passes_its_budget_to_the_formatter() -> None:
    """TRAP T17 again. `format_hook_output` accepting `max_tokens` proves nothing about the two
    hook paths passing it -- and not passing it is indistinguishable from the defect."""
    import inspect

    from menhir.cli import hook

    assert inspect.getsource(hook).count("max_tokens=max_tokens,\n    )") == 2


# ---------------------------------------------------------------------------
# Defect 3 -- the turn gate fails closed
# ---------------------------------------------------------------------------


def test_an_unreadable_counter_skips_the_turn(tmp_path: Path) -> None:
    """THE FINDING. `data = {}` gives count 0, and `0 % 5 == 0` is True -- so a corrupt file ran
    recall EVERY turn. The gate failed toward the expensive direction."""
    counter = tmp_path / "counter.json"
    counter.write_text("not valid json!!!")

    assert should_run_this_turn("sess1", 5, counter) is False


def test_a_counter_holding_valid_json_that_is_not_an_object_also_skips(tmp_path: Path) -> None:
    """`json.loads("null")` succeeds, and `None.get(...)` then raises inside the hook. Parsing is
    not validating."""
    counter = tmp_path / "counter.json"
    counter.write_text("null")

    assert should_run_this_turn("sess1", 5, counter) is False


def test_a_missing_counter_still_runs(tmp_path: Path) -> None:
    """THE TRAP. An absent file is the ordinary first run, not a fault. Failing closed on it would
    mean a fresh install never recalls at all -- and both cases end with an empty dict, so a fix
    that only asks "did it parse" gets this wrong."""
    counter = tmp_path / "nested" / "counter.json"

    assert should_run_this_turn("sess1", 5, counter) is True
    assert counter.exists()


def test_an_unwritable_counter_skips_the_turn(tmp_path: Path, monkeypatch) -> None:
    """The same defect wearing a different hat. If the increment never persists, the next turn
    reads the same count -- so an unwritable counter pins the gate open forever, not just once."""
    counter = tmp_path / "counter.json"
    # count=5 with frequency=5 is a turn the gate WOULD fire on. An earlier version used count=3,
    # which returns False on its own -- so the test passed without the fail-closed return doing
    # anything. Caught by mutation, not by review.
    counter.write_text(json.dumps({"sess1": {"count": 5, "ts": 0}}))

    def _boom(*_a, **_kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", _boom)

    assert should_run_this_turn("sess1", 5, counter) is False


def test_a_skipped_turn_says_so_on_stderr(tmp_path: Path, capsys) -> None:
    """What makes failing closed safe. Silently skipping every turn would disable recall with no
    signal at all -- the operator would see memory quietly stop working. stderr rather than the
    logger for the reason CF-198 established: logging is not configured this early."""
    counter = tmp_path / "counter.json"
    counter.write_text("{{{")

    should_run_this_turn("sess1", 5, counter)

    assert "turn counter" in capsys.readouterr().err


def test_a_healthy_counter_still_gates_every_nth_turn(tmp_path: Path) -> None:
    """POSITIVE CONTROL. A gate that returned False whenever anything looked unusual would pass
    every test above while disabling the feature."""
    counter = tmp_path / "counter.json"
    results = [should_run_this_turn("sess1", 5, counter) for _ in range(10)]

    assert results == [True, False, False, False, False, True, False, False, False, False]
