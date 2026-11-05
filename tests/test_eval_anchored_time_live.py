"""Offline tests of the live harness pieces in scripts/eval_anchored_time.py (CF-288); fakes only."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def module(monkeypatch):
    spec = importlib.util.spec_from_file_location("eval_anchored_time", ROOT / "scripts" / "eval_anchored_time.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


class _Inner:
    def __init__(self, rates, fail=False):
        self.rates = rates
        self.fail = fail
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **request):
        self.calls += 1
        await asyncio.sleep(0)
        if self.fail:
            raise ConnectionError("boom")
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100))


REQUEST = {"messages": [{"role": "user", "content": "x" * 4000}], "max_completion_tokens": 100}


def test_resolver_route_uses_anchored_time_endpoint_when_set(module) -> None:
    provider = SimpleNamespace(base_url="http://prov/v1", api_key="pk")
    settings = SimpleNamespace(anchored_time_base_url="http://local:8080/v1", anchored_time_api_key="k",
                               anchored_time_extra_body='{"a":1}')
    assert module._resolver_route(settings, provider) == ("http://local:8080/v1", "k", {"a": 1})
    unset = SimpleNamespace(anchored_time_base_url="", anchored_time_api_key="", anchored_time_extra_body="")
    assert module._resolver_route(unset, provider) == ("http://prov/v1", "pk", {})


def test_usage_tap_never_overshoots_with_concurrent_calls(module) -> None:
    rates = (1.0, 1.0)
    inner = _Inner(rates)
    est = (len(__import__("json").dumps(REQUEST["messages"])) / 4 * rates[0] + 100 * rates[1]) / 1e6
    tap = module._UsageTap(inner, est * 3.5, rates)

    async def run():
        return await asyncio.gather(*(tap.create(**REQUEST) for _ in range(8)), return_exceptions=True)

    results = asyncio.run(run())
    assert inner.calls <= 3
    assert sum(isinstance(r, RuntimeError) for r in results) == 8 - inner.calls
    assert tap.spent <= tap.max_usd
    assert tap.refused is True


def test_usage_tap_releases_reservation_on_error(module) -> None:
    tap = module._UsageTap(_Inner((1.0, 1.0), fail=True), 1.0, (1.0, 1.0))
    with pytest.raises(ConnectionError):
        asyncio.run(tap.create(**REQUEST))
    assert tap.reserved == 0
