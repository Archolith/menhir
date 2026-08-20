"""A usage callback's REFUSAL must survive every wrapper between it and the LLM caller.

CF-227 established the principle at one layer: `_emit_llm_usage_event` swallowed everything a
callback raised, which left CF-79's per-job budget counting calls it could not stop. The fix taught
that function to re-raise `LlmUsageControlSignal`.

**This file exists because the fix stopped there.** `complete_llm_usage_call` is the emitter's own
caller, and it wraps the emit in a blanket `except Exception` whose purpose is to survive a
malformed provider usage payload. `LlmUsageControlSignal` is an `Exception`, so that handler
re-caught precisely what CF-227 had just released -- and then re-emitted the event, invoking the
budget callback a SECOND time for one LLM call.

Not currently exploitable: `LlmBudgetExceeded` is raised only on `phase="started"`, and
`start_llm_usage_call` has no wrapper. The trap is that the reachability, not the correctness, is
what keeps it harmless -- moving the refusal to the completed phase, or adding any other control
signal, re-opens CF-79 with no test failing.

So these tests assert the PROPERTY ("a refusal reaches the caller from every emit site") rather
than the one path that happens to be wired today.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure import observability as obs

pytestmark = [pytest.mark.unit]


class _Refusal(obs.LlmUsageControlSignal):
    """Stands in for `LlmBudgetExceeded` without importing the ingest layer."""


@pytest.fixture
def refusing_callback(monkeypatch):
    """A callback that refuses on a chosen phase and counts every invocation.

    The count is the load-bearing half: a handler that catches the signal and RE-EMITS still lets
    it reach the caller, so propagation alone would pass while the budget was charged twice.
    """
    calls: list[str] = []

    def _install(refuse_on: str):
        def cb(event) -> None:
            calls.append(event.phase)
            if event.phase == refuse_on:
                raise _Refusal(f"refused on {event.phase}")

        token = obs.set_llm_usage_callback(cb)
        monkeypatch.setattr(obs, "_default_llm_usage_callback", None, raising=False)
        return token

    _install.calls = calls  # type: ignore[attr-defined]
    return _install


def _handle():
    return obs.LLMCallHandle(
        call_id="c1", kind="chat", model="m", endpoint="e", operation="op", started_at=0.0
    )


def test_a_refusal_on_started_reaches_the_caller(refusing_callback) -> None:
    """The path CF-79 actually uses today. Pinned so a future wrapper cannot quietly cover it."""
    refusing_callback("started")
    with pytest.raises(obs.LlmUsageControlSignal):
        obs.start_llm_usage_call(kind="chat", model="m", endpoint="e")


def test_a_refusal_on_completed_reaches_the_caller(refusing_callback) -> None:
    """The regression. Before the fix the blanket `except Exception` in `complete_llm_usage_call`
    caught this, logged it at DEBUG, and the refusal never reached the LLM caller."""
    refusing_callback("completed")
    with pytest.raises(obs.LlmUsageControlSignal):
        obs.complete_llm_usage_call(_handle(), usage={"prompt_tokens": 1, "completion_tokens": 2})


def test_a_refused_completion_charges_the_callback_exactly_once(refusing_callback) -> None:
    """The half propagation alone cannot see.

    The old handler's recovery path re-emitted the same `call_id` after catching the signal, so a
    budget callback was notified twice for one LLM call. A test that only asserted `pytest.raises`
    would have passed against that behaviour.
    """
    install = refusing_callback
    install("completed")
    with pytest.raises(obs.LlmUsageControlSignal):
        obs.complete_llm_usage_call(_handle(), usage={"prompt_tokens": 1})

    assert install.calls == ["completed"], (
        f"the budget callback was invoked {len(install.calls)}x for one LLM call: {install.calls}"
    )


def test_a_refusal_on_failed_reaches_the_caller(refusing_callback) -> None:
    """`fail_llm_usage_call` has no wrapper today. Asserted anyway so adding one is a test
    failure rather than a silent third instance of this bug."""
    refusing_callback("failed")
    with pytest.raises(obs.LlmUsageControlSignal):
        obs.fail_llm_usage_call(_handle(), RuntimeError("provider exploded"))


def test_an_ordinary_callback_fault_is_still_swallowed(refusing_callback, monkeypatch) -> None:
    """The counter-assertion, without which the fix above could be 'let everything through'.

    Instrumentation must not take down the caller it observes. Only a signal that DECLARES itself
    control flow propagates; anything raised by accident stays swallowed.
    """
    def cb(event) -> None:
        raise ValueError("instrumentation bug")

    obs.set_llm_usage_callback(cb)
    monkeypatch.setattr(obs, "_default_llm_usage_callback", None, raising=False)

    obs.start_llm_usage_call(kind="chat", model="m", endpoint="e")
    obs.complete_llm_usage_call(_handle(), usage={"prompt_tokens": 1})
    obs.fail_llm_usage_call(_handle(), RuntimeError("boom"))


def test_a_malformed_usage_payload_still_emits_a_completion(monkeypatch) -> None:
    """The behaviour the blanket handler was written for must survive the narrowing.

    A provider payload that cannot be normalized still has to produce a `completed` event, just
    without token counts -- that is the whole reason the recovery path exists.
    """
    seen: list[dict] = []
    obs.set_llm_usage_callback(lambda e: seen.append({"phase": e.phase, "total": e.total_tokens}))
    monkeypatch.setattr(obs, "_default_llm_usage_callback", None, raising=False)

    class _Exploding:
        def __getattr__(self, name):  # any attribute access blows up normalization
            raise TypeError("unreadable usage payload")

    obs.complete_llm_usage_call(_handle(), usage=_Exploding())

    assert seen == [{"phase": "completed", "total": None}], seen
