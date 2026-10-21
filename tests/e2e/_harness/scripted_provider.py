"""A scriptable fake model: per-prompt rules, per-call replies, every call logged.

The deterministic provider answers Graphiti's structured schemas with one fixed fact. That
is enough to reach READY, but the scalar and counting pipelines send plain chat prompts
and parse the reply text themselves, so a lane that wants to prove "this episode was
counted, that one was left out as a repeat" needs the model to say exactly that, for
exactly that prompt, on exactly that call.

A :class:`Script` is an ordered list of :class:`Rule`. A chat call is matched against the
rules in order, on the text of all its messages; the first rule whose ``match`` is a
substring of that text answers. A rule answers with a list of replies (call 0 gets the
first, call 1 the second, and the last repeats) or with a function of the call. A call
no rule matches falls through to the deterministic provider, so Graphiti enrichment keeps
working, and is logged as unmatched so a lane can fail on a prompt it did not expect.

The server runs in the test process. The backend under test is a separate process that
reaches it over loopback, the same way it would reach a real local model.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Any

from tests.e2e._harness.providers import (
    DeterministicProviderHandler,
    local_provider_environment,
)

__all__ = ["Call", "Rule", "Script", "ScriptedModel", "scripted_model"]


@dataclass(frozen=True)
class Call:
    """What one rule invocation saw. ``index`` counts calls to that rule from 0."""

    rule: str
    index: int
    prompt: str
    payload: dict[str, Any]


Reply = str | Callable[[Call], str]


@dataclass
class Rule:
    """Answer chat calls whose prompt text contains ``match``.

    ``replies`` is either a list (call i gets ``replies[min(i, len - 1)]``) or one callable
    that builds the reply from the :class:`Call`. ``structured`` lets a rule also answer
    calls that carry a ``response_format`` json_schema; by default those are left to the
    deterministic provider, which is what Graphiti expects.
    """

    name: str
    match: str
    replies: list[Reply] | Callable[[Call], str]
    structured: bool = False
    calls: list[Call] = field(default_factory=list)

    def answer(self, prompt: str, payload: dict[str, Any]) -> str:
        call = Call(rule=self.name, index=len(self.calls), prompt=prompt, payload=payload)
        self.calls.append(call)
        if callable(self.replies):
            return self.replies(call)
        if not self.replies:
            raise ValueError(f"rule {self.name!r} has no replies")
        reply = self.replies[min(call.index, len(self.replies) - 1)]
        return reply(call) if callable(reply) else reply


@dataclass
class Script:
    """The rules a lane installs, plus a log of every chat call the backend made."""

    rules: list[Rule] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, rule: Rule) -> Rule:
        with self._lock:
            self.rules.append(rule)
        return rule

    def replace(self, *rules: Rule) -> None:
        """Swap the whole rule set, e.g. between the two halves of a re-version lane."""
        with self._lock:
            self.rules = list(rules)

    def calls(self, name: str) -> list[Call]:
        with self._lock:
            return [call for rule in self.rules if rule.name == name for call in rule.calls]

    def respond(self, prompt: str, payload: dict[str, Any], *, structured: bool) -> str | None:
        """The scripted reply for this call, or None to fall through to the default."""
        with self._lock:
            for rule in self.rules:
                if rule.match in prompt and (rule.structured or not structured):
                    reply = rule.answer(prompt, payload)
                    self.log.append({"rule": rule.name, "index": len(rule.calls) - 1,
                                     "prompt_head": prompt[:300], "reply": reply})
                    return reply
            if not structured:
                self.unmatched.append({"prompt_head": prompt[:600]})
            self.log.append({"rule": None, "structured": structured,
                             "prompt_head": prompt[:300]})
            return None

    def evidence(self) -> str:
        with self._lock:
            return json.dumps({"log": self.log, "unmatched": self.unmatched}, indent=2,
                              default=str)


def _handler_for(script: Script) -> type[DeterministicProviderHandler]:
    class ScriptedHandler(DeterministicProviderHandler):
        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/v1/chat/completions":
                super().do_POST()
                return
            payload = self._request_json()
            messages = payload.get("messages") or []
            prompt = "\n".join(_content_text(m.get("content")) for m in messages)
            response_format = payload.get("response_format") or {}
            structured = bool(response_format.get("json_schema")) or bool(payload.get("logprobs"))
            reply = script.respond(prompt, payload, structured=structured)
            if reply is None:
                self._default(payload, prompt)
                return
            self._json(200, self._chat_response(reply))

        def _default(self, payload: dict[str, Any], prompt: str) -> None:
            # Same answers as DeterministicProviderHandler.do_POST, without re-reading
            # the request body it has already consumed.
            if payload.get("logprobs"):
                self._json(200, self._chat_response("True", with_logprobs=True))
                return
            schema = (payload.get("response_format") or {}).get("json_schema") or {}
            structured = self._structured_response(
                str(schema.get("name") or ""), prompt, schema.get("schema") or {})
            self._json(200, self._chat_response(json.dumps(structured)))

    return ScriptedHandler


def _content_text(content: Any) -> str:
    if isinstance(content, list):  # OpenAI content parts
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


@dataclass
class ScriptedModel:
    """A running scripted provider: its script, base URL and child-process env."""

    script: Script
    base_url: str

    @property
    def env(self) -> dict[str, str]:
        return local_provider_environment(self.base_url)


@contextmanager
def scripted_model(script: Script | None = None) -> Iterator[ScriptedModel]:
    script = script or Script()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(script))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ScriptedModel(script=script, base_url=f"http://127.0.0.1:{server.server_port}/v1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
