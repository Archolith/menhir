"""CF-79 -- the budget hazard the entry documented but nothing enforced.

CF-79 is FULLY CLOSED: the per-job reservation and the session ledger both stop calls rather than
counting them, each proven by counting calls whose bodies actually EXECUTE. What it left behind is
a hazard recorded in prose only:

    `asyncio.to_thread` COPIES the context, so the usage callback -- and therefore BOTH budgets --
    applies. A bare `threading.Thread` starts with a FRESH context, finds no callback, and every
    call runs UNMETERED.

A prose note cannot fail. This file makes the mechanism executable and pins the raw-thread census,
so an LLM call added later on a raw thread is a test failure rather than a silent budget escape
that "nothing reports".

**One correction to the entry while pinning it:** it says *"the only raw thread in the codebase"*.
There are **two** -- `saga_reconcile_gate.GateHeartbeat` and `saga_writer_heartbeat`. Both are
heartbeat loops with zero LLM references, so the conclusion holds; the count did not.
"""

from __future__ import annotations

import contextvars
import pathlib
import re
import threading

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"

#: Every raw-thread construction site, with why it is safe. A THIRD entry here is not automatically
#: a bug -- it is a review trigger: prove the thread makes no model call, or route it through
#: `asyncio.to_thread` so the usage callback survives.
_KNOWN_RAW_THREADS = {
    "services/saga_reconcile_gate.py": "GateHeartbeat._loop -- gate heartbeat, no model calls",
    "services/saga_writer_heartbeat.py": "saga writer heartbeat -- no model calls",
}


def test_the_context_copy_is_what_carries_the_budget() -> None:
    """THE MECHANISM, executed. This is the difference the hazard rests on, and it is a property of
    contextvars rather than of Menhir -- so it holds no matter how the budgets are implemented."""
    callback: contextvars.ContextVar[str] = contextvars.ContextVar("usage_callback", default="")
    callback.set("installed")

    seen_in_raw_thread: list[str] = []

    def _read() -> None:
        seen_in_raw_thread.append(callback.get())

    raw = threading.Thread(target=_read)
    raw.start()
    raw.join()

    seen_in_copied_context: list[str] = []
    contextvars.copy_context().run(lambda: seen_in_copied_context.append(callback.get()))

    assert seen_in_raw_thread == [""], "a bare Thread should NOT see the callback"
    assert seen_in_copied_context == ["installed"], "a copied context should"


def test_every_raw_thread_site_is_accounted_for() -> None:
    """THE RATCHET. Not "no raw threads" -- two are legitimate. The invariant is that a NEW one
    cannot appear without someone stating why it makes no model call."""
    found: dict[str, int] = {}
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = len(re.findall(r"threading\.Thread\(", text))
        if hits:
            found[path.relative_to(_SRC).as_posix()] = hits

    assert set(found) == set(_KNOWN_RAW_THREADS), (
        "the raw-thread census changed. A thread that makes an LLM call escapes BOTH budgets and "
        f"nothing reports it -- justify it in _KNOWN_RAW_THREADS or use asyncio.to_thread. {found}"
    )


def test_no_executor_dispatch_reintroduces_the_same_escape() -> None:
    """`run_in_executor` and a hand-rolled `ThreadPoolExecutor` lose the context exactly as a bare
    Thread does, so the ratchet above would miss them entirely if either appeared."""
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in ("run_in_executor", "ThreadPoolExecutor("):
            if pattern in text:
                offenders.append(f"{path.relative_to(_SRC).as_posix()}:{pattern}")

    assert offenders == [], (
        "executor dispatch does not copy the context, so an LLM call routed through it runs "
        f"unmetered: {offenders}"
    )


@pytest.mark.parametrize("module", sorted(_KNOWN_RAW_THREADS))
def test_the_known_raw_threads_still_make_no_model_calls(module: str) -> None:
    """The justification, re-checked rather than trusted. These are safe because of what they DO,
    and that can change without the thread count changing -- which the census test alone would not
    notice."""
    text = (_SRC / module).read_text(encoding="utf-8", errors="replace")

    for marker in ("chat_completion", "create_chat_completion", "llm_client", "LLMAdapter"):
        assert marker not in text, (
            f"{module} now references {marker!r}; a model call on this raw thread would escape "
            "both the per-job reservation and the session ledger"
        )
