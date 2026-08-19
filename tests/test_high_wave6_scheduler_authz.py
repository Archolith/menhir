"""Counterexample tests for HIGH wave 6 (CF-9, CF-10, CF-31, CF-99, CF-100, CF-115).

Each test reproduces the scenario the register recorded, not the shape of the fix.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import time
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"


# ---------------------------------------------------------------------------
# CF-100 -- a lease lost mid-job did not stop the job
# ---------------------------------------------------------------------------


def _scheduler(**overrides: Any):
    from menhir.services.maintenance_scheduler import MaintenanceScheduler

    class _Ingest:
        def get_queue_depth(self) -> int:
            return 0

    class _Graph:
        pass

    kwargs: dict[str, Any] = {"ingest_service": _Ingest(), "graph_adapter": _Graph()}
    kwargs.update(overrides)
    return MaintenanceScheduler(**kwargs)


@pytest.mark.asyncio
async def test_cf100_job_is_abandoned_when_the_lease_is_force_taken_mid_run() -> None:
    """The filed defect. A displaced owner used to finish the job it was inside -- the check
    was between jobs, and `await coro` had nothing watching it -- so two schedulers mutated the
    same graph concurrently for the whole remaining duration of that job."""
    from menhir.services.maintenance_scheduler import _LeaseLostDuringJob

    sched = _scheduler()
    sched._stamp_lease_deadline(time.monotonic())

    mutations: list[str] = []

    async def long_job() -> dict[str, object]:
        mutations.append("step-1")
        await asyncio.sleep(0.05)
        mutations.append("step-2")  # must never be reached
        return {}

    task = asyncio.ensure_future(sched._await_job_under_lease(long_job(), "op"))
    await asyncio.sleep(0.01)
    sched._mark_lease_lost()

    with pytest.raises(_LeaseLostDuringJob):
        await task
    assert mutations == ["step-1"]


@pytest.mark.asyncio
async def test_cf100_silent_expiry_stops_the_job_with_nothing_reporting_a_loss() -> None:
    """The case the original code could not detect at all.

    Its only loss signal was a renewal that returned False. When the loop is starved (CF-99),
    or the heartbeat task dies, no renewal is attempted -- so none can fail. The lease simply
    expires under a live holder while every in-memory flag still says `owner`. Only the clock
    catches that, which is why `_lease_is_provable` is a deadline and not a flag.
    """
    from menhir.services.maintenance_scheduler import _LeaseLostDuringJob

    sched = _scheduler(lease_duration_s=0.05)
    sched._stamp_lease_deadline(time.monotonic())
    assert sched._lease_is_provable()

    reached_second_step = False

    async def long_job() -> dict[str, object]:
        nonlocal reached_second_step
        await asyncio.sleep(0.5)
        reached_second_step = True
        return {}

    with pytest.raises(_LeaseLostDuringJob):
        await sched._await_job_under_lease(long_job(), "op")

    assert reached_second_step is False
    assert sched._lease_lost is False  # nothing ever told it; the deadline did the work


@pytest.mark.asyncio
async def test_cf100_a_renewal_extends_the_deadline_and_the_job_survives() -> None:
    """The guard must not be a timer that kills long jobs. While the heartbeat keeps renewing,
    a job longer than one lease window runs to completion."""
    sched = _scheduler(lease_duration_s=0.05)
    sched._stamp_lease_deadline(time.monotonic())

    async def renewer() -> None:
        for _ in range(20):
            await asyncio.sleep(0.01)
            sched._stamp_lease_deadline(time.monotonic())

    keep_alive = asyncio.ensure_future(renewer())

    async def long_job() -> dict[str, object]:
        await asyncio.sleep(0.15)
        return {"done": True}

    result = await sched._await_job_under_lease(long_job(), "op")
    keep_alive.cancel()
    assert result == {"done": True}


@pytest.mark.asyncio
async def test_cf100_an_orderly_stop_still_lets_the_running_job_finish() -> None:
    """`_mark_lease_lost` sets `_stop_event`, so the interrupt must key on lease loss alone.
    Shutdown semantics are unchanged: the current job completes, the next one does not start."""
    sched = _scheduler()
    sched._stamp_lease_deadline(time.monotonic())
    sched._stop_event.set()

    async def job() -> dict[str, object]:
        await asyncio.sleep(0.02)
        return {"done": True}

    assert await sched._await_job_under_lease(job(), "op") == {"done": True}


@pytest.mark.asyncio
async def test_cf100_a_scheduler_that_never_took_the_lease_is_not_supervised() -> None:
    """A scheduler with no acquire behind it is not a displaced owner -- there is no second
    owner to race. Guarding it would refuse the direct-invocation shape while protecting
    nothing. Both production callers run only after a successful acquire; see
    `_lease_supervision_active`."""
    sched = _scheduler()
    assert sched._lease_supervision_active() is False

    async def job() -> dict[str, object]:
        await asyncio.sleep(0.02)
        return {"ran": True}

    assert await sched._await_job_under_lease(job(), "op") == {"ran": True}


@pytest.mark.asyncio
async def test_cf100_a_lost_lease_still_counts_as_supervised() -> None:
    """`_mark_lease_lost` zeroes the deadline, so a "never held it" test on the deadline alone
    would read a just-displaced owner as an unsupervised one and wave its jobs through."""
    sched = _scheduler()
    sched._stamp_lease_deadline(time.monotonic())
    sched._mark_lease_lost()
    assert sched._lease_valid_until == 0.0
    assert sched._lease_supervision_active() is True


def test_cf100_the_deadline_is_dated_from_before_the_store_call() -> None:
    """The store's expiry starts running during the call. Dating the window from the return
    would claim validity the store did not grant; dating it from before can only under-claim."""
    sched = _scheduler(lease_duration_s=10.0)
    before = time.monotonic()
    sched._stamp_lease_deadline(before)
    assert sched._lease_valid_until == pytest.approx(before + 10.0)


def test_cf100_every_renewal_goes_through_the_one_deadline_stamping_seam() -> None:
    """`lease_store.renew` must not be reachable except through `_renew_lease`, or a path could
    refresh the lease without refreshing the fact that authorizes mutation."""
    source = (_SRC / "services/maintenance_scheduler.py").read_text(encoding="utf-8")
    assert source.count("self.lease_store.renew(") == 1


@pytest.mark.asyncio
async def test_cf100_heartbeat_survives_a_store_error_instead_of_dying_silently() -> None:
    """A renewal that RAISES is not evidence the lease was taken, it is evidence that ownership
    is unknown. The original code let the exception kill the heartbeat task: nothing set
    `_stop_event`, nothing set `_lease_lost`, and the job loop carried on with no renewals at
    all -- the worst of both, since the lease then expired unnoticed."""
    sched = _scheduler(lease_duration_s=0.4, lease_heartbeat_s=0.01)

    calls = {"n": 0}

    def exploding_renew() -> bool:
        calls["n"] += 1
        raise RuntimeError("database is locked")

    sched._renew_lease = exploding_renew  # type: ignore[method-assign]
    task = asyncio.ensure_future(sched._heartbeat_loop())
    await asyncio.sleep(0.08)
    still_running = not task.done()
    sched._stop_event.set()
    await asyncio.sleep(0.03)
    task.cancel()

    assert still_running, "heartbeat died on a store error"
    assert calls["n"] > 1, "heartbeat stopped retrying after the first error"
    assert sched._lease_lost is False, "an unknown answer was treated as a definite loss"


# ---------------------------------------------------------------------------
# CF-99 -- blocking graph calls on the scheduler's event loop
# ---------------------------------------------------------------------------


def test_cf99_no_blocking_graph_call_remains_on_the_event_loop() -> None:
    """The invariant, not the six instances. A walker that answers "is this call on the event
    loop" has to model SCOPE: a plain `def` nested inside an `async def` runs on whatever
    thread dispatched it, and an earlier count of eight was wrong for exactly that reason.
    """
    tree = ast.parse((_SRC / "services/scheduler_tasks.py").read_text(encoding="utf-8"))

    offenders: list[str] = []

    def walk(node: ast.AST, on_loop: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
                continue
            if isinstance(child, (ast.FunctionDef, ast.Lambda)):
                # A sync def resets the context: its body runs wherever it is dispatched.
                walk(child, False)
                continue
            if (
                on_loop
                and isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "graph_adapter"
            ):
                offenders.append(f"{child.func.attr} (line {child.lineno})")
            walk(child, on_loop)

    walk(tree, False)
    assert offenders == []


def test_cf99_to_thread_receives_the_callable_not_its_result() -> None:
    """`to_thread(f(x))` type-checks, reads correctly, and still runs `f` on the event loop --
    it hands the thread the already-computed result. Every conversion must pass a reference."""
    tree = ast.parse((_SRC / "services/scheduler_tasks.py").read_text(encoding="utf-8"))
    bad: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Call)
        ):
            bad.append(node.lineno)
    assert bad == []


# ---------------------------------------------------------------------------
# CF-9 -- destroying a namespace needed less authority than deleting one node
# ---------------------------------------------------------------------------


def test_cf9_namespace_reset_requires_operator() -> None:
    """`POST /api/phase3/reset` calls `backend.delete_namespace`. The MCP tool for the same
    operation is operator-only, and `DELETE /memory/{uuid}` -- a strictly smaller blast
    radius -- is operator-only too. The transport was deciding the authority."""
    tree = ast.parse((_SRC / "api/routes_handlers.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "phase3_reset_impl"
    )
    tiers = [
        c.args[0].value
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
        and getattr(c.func, "id", None) == "require_tier"
        and c.args
        and isinstance(c.args[0], ast.Constant)
    ]
    assert tiers == ["operator"]


def test_cf9_matches_the_mcp_tool_for_the_same_operation() -> None:
    from menhir.mcp.tools.ops.delete_namespace import DeleteNamespaceTool

    assert DeleteNamespaceTool.required_tier == "operator"


# ---------------------------------------------------------------------------
# CF-31 -- two tier keys with the same value resolve to the higher tier
# ---------------------------------------------------------------------------


def _middleware(**keys: str):
    from menhir.api.auth import BearerAuthMiddleware

    async def _app(scope, receive, send):  # pragma: no cover - never invoked
        return None

    return BearerAuthMiddleware(_app, **keys)


@pytest.mark.parametrize(
    "keys",
    [
        {"operator_key": "same", "readonly_key": "same"},
        {"operator_key": "same", "agent_key": "same"},
        {"agent_key": "same", "readonly_key": "same"},
    ],
)
def test_cf31_duplicate_tier_keys_refuse_to_start(keys: dict[str, str]) -> None:
    """`_resolve_tier` returns on first match and tries operator first, so a shared value
    silently grants the highest tier it appears in. There was no runtime symptom: the
    privilege is simply handed over."""
    with pytest.raises(ValueError, match="must be distinct"):
        _middleware(**keys)


def test_cf31_the_legacy_api_key_alias_is_checked_too() -> None:
    """`api_key` is folded into the operator key for backwards compatibility. Validating the
    settings fields alone would miss a legacy `api_key` colliding with a configured agent key,
    which is why the check sits after that fallback rather than in the settings model."""
    with pytest.raises(ValueError, match="must be distinct"):
        _middleware(api_key="legacy", agent_key="legacy")


def test_cf31_the_ordinary_single_key_deployment_still_starts() -> None:
    """Two of the three are blank in almost every real deployment. An empty key is not a
    configured key, so blanks must be excluded rather than compared."""
    mw = _middleware(operator_key="op")
    assert mw._resolve_tier("op") == "operator"
    assert mw._resolve_tier("nope") is None


def test_cf31_distinct_keys_are_untouched() -> None:
    mw = _middleware(operator_key="op", agent_key="ag", readonly_key="ro")
    assert mw._resolve_tier("op") == "operator"
    assert mw._resolve_tier("ag") == "agent"
    assert mw._resolve_tier("ro") == "readonly"


# ---------------------------------------------------------------------------
# CF-115 -- 54 hand-written tool descriptions never reached the client
# ---------------------------------------------------------------------------


def _tool_classes() -> dict[str, type]:
    import importlib
    import pkgutil

    from menhir.mcp.contracts import BaseTool
    import menhir.mcp.tools as tools_pkg

    for module in pkgutil.walk_packages(tools_pkg.__path__, tools_pkg.__name__ + "."):
        importlib.import_module(module.name)

    found: dict[str, type] = {}

    def descend(cls: type) -> None:
        for sub in cls.__subclasses__():
            if getattr(sub, "name", None) and not sub.__name__.startswith("Base"):
                found[sub.name] = sub
            descend(sub)

    descend(BaseTool)
    return found


def test_cf115_every_registered_tool_has_a_non_empty_description() -> None:
    """`mcp.tool()` was called with no `description=`, and `@wraps` had no docstring to copy on
    36 of the 54 endpoints -- so those tools reached the agent as a bare name. This asserts the
    property the register cares about: what the client is handed, not that one argument is
    present."""
    empty = [
        name
        for name, cls in _tool_classes().items()
        if not cls().registered_description().strip()
    ]
    assert empty == []


def test_cf115_the_curated_description_survives_a_docstring() -> None:
    """18 endpoints have since grown docstrings carrying their `Args:` block. Overwriting one
    with the one-line class attribute would trade one loss for another, so both are kept and
    the curated line leads."""
    from menhir.mcp.contracts import BaseTool

    class _Tool(BaseTool):
        name = "t"
        description = "Curated one-liner."

        async def endpoint(self) -> str:  # type: ignore[override]
            """Detailed.

            Args:
                x: something.
            """
            return ""

    rendered = _Tool().registered_description()
    assert rendered.startswith("Curated one-liner.")
    assert "Args:" in rendered


def test_cf115_no_duplication_when_the_two_strings_agree() -> None:
    from menhir.mcp.contracts import BaseTool

    class _Tool(BaseTool):
        name = "t"
        description = "Same text."

        async def endpoint(self) -> str:  # type: ignore[override]
            """Same text."""
            return ""

    assert _Tool().registered_description() == "Same text."


def test_cf115_registration_passes_the_description_through() -> None:
    tree = ast.parse((_SRC / "mcp/contracts.py").read_text(encoding="utf-8"))
    cls = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "BaseTool"
    )
    register = next(
        n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "register"
    )
    assert "description=" in ast.unparse(register)


# ---------------------------------------------------------------------------
# CF-10 -- the failure page handed back a freshly signed consent token
# ---------------------------------------------------------------------------


def test_cf10_the_retry_page_cannot_sign_a_token() -> None:
    """Structural, not behavioural, and deliberately so: a boolean flag on `_render_consent`
    would have been one bad default away from leaking a token again. A function with no call to
    `_sign_consent` in its body cannot regress that way."""
    tree = ast.parse((_SRC / "api/oauth_authorize.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_render_consent_retry"
    )
    body = ast.unparse(fn)
    assert "_sign_consent" not in body
    assert "consent_token" not in body
    assert "<input" not in body


def test_cf10_both_failure_paths_use_the_tokenless_page() -> None:
    """The 401 and the 429 both re-rendered the signed form, so a guess loop never needed
    another GET. A reviewer made 14 admin-secret attempts from one unauthenticated GET."""
    tree = ast.parse((_SRC / "api/oauth_authorize.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "authorize_post"
    )
    renderers = [
        c.func.id
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and getattr(c.func, "id", "").startswith("_render_consent")
    ]
    assert renderers, "no consent page rendered from the POST handler"
    assert set(renderers) == {"_render_consent_retry"}


def test_cf10_the_get_path_still_issues_a_token() -> None:
    """The fix must not break the consent flow it protects: the page a human is actually shown
    still carries a single-use token."""
    tree = ast.parse((_SRC / "api/oauth_authorize.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "authorize_get"
    )
    assert "_render_consent(" in ast.unparse(fn)
