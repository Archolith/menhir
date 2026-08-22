"""CF-181 -- the split-and-retry loop's failures become local, because the ceiling now fits the model.

CF-181 files the dedupe batch-split as costing "up to 2N-1 LLM round trips". **The mechanism that
makes those splits free already existed and was mis-set.** `_enforce_request_size` rejects an
oversized payload BEFORE the API call -- its own message says *"Not sent"* -- so a split attempt
that trips it costs no round trip and no provider tokens. But its ceiling was a fixed 100,000 with
no relationship to whatever model is deployed.

MEASURED on this host, not inferred:

    menhir ceiling            100,000   (default; no .env override)
    LLAMA_CONTEXT_SIZE         32,768   (scheduler .env)

The ceiling sat at **3x the real window**, so on the local path it could never fire before the
provider rejected -- and every split attempt cost a real round trip to a model running at ~27
tok/s. That is precisely the cost CF-181 is about, arriving through configuration rather than
through the algorithm the entry describes.

OWNER RULING 2026-08-22: derive the ceiling from the endpoint at runtime.

**Two safety rules carry the design, and both are asserted below.**

* Derivation may only LOWER the configured ceiling, never raise it. The setting is also a cost
  bound -- a 100,000-token request to a metered provider is expensive whether or not it fits.
* A configured ceiling of 0 disables the check, and derivation must not re-enable it. That would
  be the mechanism overriding a deliberate operator opt-out.

**What is NOT claimed.** The live sidecar holds 276 context-length failures (2026-07-15 to
08-10, now stopped), but they report windows of 128,000 and 1,047,576 -- remote providers, not the
local model -- and it was not established that they came through the dedupe path. They are not
evidence for this entry and are not cited as such.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import menhir.infrastructure.graphiti_llm_patches as patches
from menhir.infrastructure.graphiti_llm_patches import (
    GraphitiRequestTooLargeError,
    _enforce_request_size,
    _is_loopback,
    _n_ctx_from_props,
    resolve_request_ceiling,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache():
    patches._derived_ceilings.clear()
    yield
    patches._derived_ceilings.clear()


@pytest.fixture
def props_server():
    """A stand-in for the scheduler's `/llama/props`, with a controllable body."""
    state = {"payload": {"default_generation_settings": {"n_ctx": 32768}}, "hits": 0, "status": 200}

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            state["hits"] += 1
            body = json.dumps(state["payload"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["endpoint"] = f"http://127.0.0.1:{server.server_address[1]}/v1/t/memory--graphiti-search"
    yield state
    server.shutdown()


# ---------------------------------------------------------------------------
# Reading the window off the endpoint
# ---------------------------------------------------------------------------


def test_the_ceiling_is_derived_from_the_models_actual_window(props_server) -> None:
    """THE FINDING. 32,768 window, 25% held back for the response -> 24,576, not the 100,000 that
    could never fire."""
    ceiling = asyncio.run(resolve_request_ceiling(props_server["endpoint"]))

    assert ceiling == 24576


def test_room_is_reserved_for_the_response(props_server) -> None:
    """The window holds request PLUS completion. A ceiling equal to the whole window guarantees a
    rejection on any request that comes close to it -- the derivation would then cause the failure
    it exists to prevent."""
    ceiling = asyncio.run(resolve_request_ceiling(props_server["endpoint"]))

    assert ceiling < 32768


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"default_generation_settings": {"n_ctx": 8192}}, 8192),
        ({"n_ctx": 4096}, 4096),
        ({"ctx_size": 2048}, 2048),
        ({"default_generation_settings": {"ctx_size": 1024}}, 1024),
        ({"default_generation_settings": {"n_ctx": 0}}, None),
        ({"n_ctx": "not a number"}, None),
        ({}, None),
        ("not a dict", None),
    ],
)
def test_every_spelling_llama_cpp_has_used_is_read(payload, expected) -> None:
    """The key moved between llama.cpp versions. Reading only the current spelling returns None
    against a build one version away -- which is indistinguishable from "no scheduler here", so the
    derivation would switch itself off without saying anything."""
    assert _n_ctx_from_props(payload) == expected


# ---------------------------------------------------------------------------
# The two safety rules
# ---------------------------------------------------------------------------


def test_derivation_can_lower_the_ceiling_but_never_raise_it(props_server, monkeypatch) -> None:
    """SAFETY RULE 1. The configured value is also a cost bound: a huge-context model must not be
    allowed to widen it into an expensive request against a metered provider."""
    props_server["payload"] = {"n_ctx": 1_000_000}
    monkeypatch.setattr(patches, "_MAX_REQUEST_ESTIMATED_TOKENS", 10_000)

    assert asyncio.run(resolve_request_ceiling(props_server["endpoint"])) == 10_000


def test_a_disabled_check_stays_disabled(props_server, monkeypatch) -> None:
    """SAFETY RULE 2. `0` is a deliberate opt-out. Re-enabling it because a probe happened to
    succeed would be the mechanism overriding the operator."""
    monkeypatch.setattr(patches, "_MAX_REQUEST_ESTIMATED_TOKENS", 0)

    assert asyncio.run(resolve_request_ceiling(props_server["endpoint"])) == 0
    assert props_server["hits"] == 0, "a disabled check should not even probe"


def test_a_failed_probe_falls_back_to_the_configured_ceiling(props_server) -> None:
    """The derivation is an improvement when it works and never a regression when it does not. A
    probe failure that produced no ceiling would remove the guard entirely -- turning a mis-set
    bound into no bound, which is strictly worse than the finding."""
    props_server["status"] = 500

    assert asyncio.run(resolve_request_ceiling(props_server["endpoint"])) == 100_000


def test_an_unreachable_endpoint_falls_back_rather_than_hanging() -> None:
    """The live case: the scheduler is up but the llama server is not, so `/llama/props` proxies to
    something dead. Verified against the real scheduler during development."""
    ceiling = asyncio.run(resolve_request_ceiling("http://127.0.0.1:8082/v1/t/probe-target"))

    assert ceiling == 100_000


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:8082/v1", True),
        ("http://localhost:8081/v1", True),
        ("https://api.deepseek.com/v1", False),
        ("https://api.openai.com/v1", False),
        (None, False),
        ("", False),
    ],
)
def test_only_loopback_endpoints_are_probed(endpoint, expected) -> None:
    """A remote OpenAI-compatible provider has no equivalent of `/props`, so probing one means an
    unsolicited GET against a third-party API on every cache miss, to learn nothing."""
    assert _is_loopback(endpoint) is expected


def test_a_remote_provider_is_never_probed(monkeypatch) -> None:
    """The rule above, through the resolver rather than the predicate.

    Asserts the PROBE was not called, not that a stub server saw no traffic. An earlier version
    checked the stub's hit count -- but a resolver that probed `api.deepseek.com` would send a real
    request to the internet and leave the stub untouched, so the test passed while the defect it
    names was present. Caught by mutation.
    """
    probed: list[str] = []

    async def _record(endpoint: str):
        probed.append(endpoint)
        return 32768

    monkeypatch.setattr(patches, "_probe_endpoint_context_window", _record)

    assert asyncio.run(resolve_request_ceiling("https://api.deepseek.com/v1")) == 100_000
    assert probed == [], f"a third-party endpoint was probed: {probed}"


# ---------------------------------------------------------------------------
# Caching -- and the staleness the cache deliberately bounds
# ---------------------------------------------------------------------------


def test_the_window_is_not_probed_on_every_request(props_server) -> None:
    """This resolves once per assembled request. Probing each time would add a round trip to
    remove one."""
    for _ in range(5):
        asyncio.run(resolve_request_ceiling(props_server["endpoint"]))

    assert props_server["hits"] == 1


def test_the_cache_expires_so_a_model_swap_is_picked_up(props_server, monkeypatch) -> None:
    """THE STALENESS HAZARD. The ceiling is a fact about whichever model the scheduler currently
    has loaded, and the scheduler swaps models. A value derived once at startup is a fact that
    outlives its subject -- so the TTL is what binds how long a stale ceiling can persist."""
    # The TTL is negative from the FIRST call, so the entry is already expired when the second one
    # reads it. An earlier version cleared the cache between calls instead -- which makes the
    # re-probe happen whether or not the expiry check exists, so removing the check entirely still
    # passed. Caught by mutation: only expiry may cause the second probe here.
    monkeypatch.setattr(patches, "_DERIVED_CEILING_TTL_S", -1.0)

    assert asyncio.run(resolve_request_ceiling(props_server["endpoint"])) == 24576
    props_server["payload"] = {"n_ctx": 8192}

    assert asyncio.run(resolve_request_ceiling(props_server["endpoint"])) == 6144
    assert props_server["hits"] == 2, "the expired entry was not re-probed"


def test_a_failed_probe_is_cached_too(props_server) -> None:
    """Otherwise a down scheduler means a probe on every single request -- the failure mode costs
    more than the thing being optimised."""
    props_server["status"] = 500

    for _ in range(4):
        asyncio.run(resolve_request_ceiling(props_server["endpoint"]))

    assert props_server["hits"] == 1


def test_ceilings_are_keyed_per_endpoint(props_server) -> None:
    """The wake sequence rotates base URLs per task, so one process legitimately talks to several
    endpoints. A single cached value would attribute one model's window to another."""
    asyncio.run(resolve_request_ceiling(props_server["endpoint"]))
    props_server["payload"] = {"n_ctx": 8192}
    other = props_server["endpoint"].replace("graphiti-search", "graphiti-add-episode")

    assert asyncio.run(resolve_request_ceiling(other)) == 6144
    assert props_server["hits"] == 2


# ---------------------------------------------------------------------------
# The enforcement point actually uses it
# ---------------------------------------------------------------------------


def test_the_derived_ceiling_is_what_gets_enforced() -> None:
    """TRAP T17. A resolver that returns the right number proves nothing about the guard using it,
    and the guard read a module global before this change."""
    messages = [{"role": "user", "content": "x" * 30_000}]  # ~10,000 estimated tokens

    assert _enforce_request_size(messages, "m", None, 20_000) == 10_000

    with pytest.raises(GraphitiRequestTooLargeError) as excinfo:
        _enforce_request_size(messages, "m", None, 5_000)
    assert "5,000 ceiling" in str(excinfo.value), "the rejection must quote the ceiling it applied"


def test_omitting_the_ceiling_still_uses_the_configured_one(monkeypatch) -> None:
    """POSITIVE CONTROL. The parameter is optional so existing callers keep working; a default that
    silently meant "no ceiling" would disable the guard for every one of them."""
    monkeypatch.setattr(patches, "_MAX_REQUEST_ESTIMATED_TOKENS", 1_000)

    with pytest.raises(GraphitiRequestTooLargeError):
        _enforce_request_size([{"role": "user", "content": "x" * 30_000}], "m", None)


def test_the_generate_path_resolves_before_enforcing() -> None:
    """The call site, asserted at source: the resolver has to be awaited and its result passed, or
    the derivation is dead code."""
    import inspect

    source = inspect.getsource(patches)
    assert "_ceiling = await resolve_request_ceiling(endpoint)" in source
    assert "openai_messages, model, endpoint, _ceiling" in source
