"""Contract tests for the standalone Hermes provider; no live Hermes/Menhir required."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError


class _FakeHermesMemoryProvider:
    def sync_turn(self, *args, **kwargs):
        pass

    def on_memory_write(self, *args, **kwargs):
        pass


def _load_plugin():
    agent = types.ModuleType("agent")
    agent.__path__ = []
    memory = types.ModuleType("agent.memory_provider")
    memory.MemoryProvider = _FakeHermesMemoryProvider
    memory.is_trivial_prompt = lambda query: not query.strip() or query.strip().lower() in {"hi", "yes"}
    secret = types.ModuleType("agent.secret_scope")
    secret.get_secret = lambda key: "test-token" if key == "HERMES_MENHIR_TOKEN" else ""
    path = Path(__file__).resolve().parents[1] / "integrations" / "hermes" / "menhir" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "test_hermes_menhir_plugin", path, submodule_search_locations=[str(path.parent)]
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"agent": agent, "agent.memory_provider": memory,
                                  "agent.secret_scope": secret, spec.name: module}):
        spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class _Response:
    def __init__(self, body):
        self.bytes = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.bytes.close()

    def read(self, limit):
        return self.bytes.read(limit)


class _Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout, json.loads(request.data) if request.data else None))
        if isinstance(self.response, Exception):
            raise self.response
        return _Response(self.response)


class HermesMenhirProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = plugin.MenhirMemoryProvider()

    def _init(self, **config):
        if config:
            self.provider.save_config(config, self.temp.name)
        self.provider.initialize("hermes-session-1", hermes_home=self.temp.name)

    def test_register_constructs_distinct_provider_instances(self):
        class Registry:
            def __init__(self):
                self.instances = []

            def register_memory_provider(self, provider):
                self.instances.append(provider)

        registry = Registry()
        plugin.register(registry)
        plugin.register(registry)
        self.assertEqual([item.name for item in registry.instances], ["menhir", "menhir"])
        self.assertIsNot(registry.instances[0], registry.instances[1])

    def test_profile_config_is_scoped_and_secrets_are_not_saved(self):
        self.provider.save_config({"namespace": "work", "write_enabled": True, "api_key": "DO_NOT_STORE"},
                                  self.temp.name)
        payload = (Path(self.temp.name) / "menhir" / "config.json").read_text()
        self.assertNotIn("DO_NOT_STORE", payload)
        self.assertTrue(json.loads(payload)["write_enabled"])
        self.provider.initialize("s1", hermes_home=self.temp.name)
        self.assertEqual(self.provider._namespace, "work")
        self.assertEqual(self.provider._token, "test-token")
        with tempfile.TemporaryDirectory() as other:
            another = plugin.MenhirMemoryProvider()
            another.initialize("s2", hermes_home=other)
            self.assertEqual(another._namespace, "")
            self.assertFalse(another._write_enabled)

    def test_profile_option_reset_and_dashboard_numeric_defaults(self):
        self.provider.save_config({"max_tokens": 500.0, "write_enabled": True}, self.temp.name)
        self.provider.write_config_fields({"write_enabled": None}, self.temp.name)
        self.provider.initialize("s", hermes_home=self.temp.name)
        self.assertEqual(self.provider._max_tokens, 500)
        self.assertFalse(self.provider._write_enabled)

    def test_automatic_prefetch_cache_and_circuit_breaker(self):
        self._init()
        opener = _Opener({"context": "cached fact"})
        self.provider._opener = opener
        self.assertIn("cached fact", self.provider.prefetch("Specific project conventions"))
        opener.response = URLError("offline")
        self.assertIn("cached fact", self.provider.prefetch("Specific project conventions"))
        self.assertEqual(len(opener.calls), 1)
        with self.assertLogs(plugin.logger, level="WARNING"):
            for index in range(3):
                self.provider.prefetch(f"What is the project status {index}?")
        self.assertEqual(len(opener.calls), 4)
        self.assertEqual(self.provider.prefetch("A new question after the circuit opened"), "")
        self.assertEqual(len(opener.calls), 4)
        self.provider._circuit_until = time.monotonic() - 1
        opener.response = {"context": "backend recovered"}
        self.assertIn("backend recovered", self.provider.prefetch("A new question after the circuit opened"))
        self.assertEqual(self.provider._consecutive_failures, 0)

    def test_sensitive_prompts_and_tool_queries_never_leave_provider(self):
        self._init(write_enabled=True)
        opener = _Opener({"context": "should not be seen"})
        self.provider._opener = opener
        sensitive = "This credential is api_key=abcdef1234567890"
        self.assertEqual(self.provider.prefetch(sensitive), "")
        self.assertIn("credential", self.provider.handle_tool_call("menhir_recall", {"query": sensitive}))
        self.assertIn("credential", self.provider.handle_tool_call("menhir_remember", {"episode": sensitive}))
        self.assertFalse(opener.calls)

    def test_default_read_only_schema_and_no_background_sync(self):
        self._init()
        self.assertEqual([item["name"] for item in self.provider.get_tool_schemas()],
                         ["menhir_recall", "menhir_status"])
        self.assertIn("Writes to Menhir are disabled", self.provider.system_prompt_block())
        self.assertIs(plugin.MenhirMemoryProvider.sync_turn, _FakeHermesMemoryProvider.sync_turn)
        self.assertIs(plugin.MenhirMemoryProvider.on_memory_write, _FakeHermesMemoryProvider.on_memory_write)
        opener = _Opener({"results": []})
        self.provider._opener = opener
        self.provider.sync_turn("a secret", "an answer", session_id="s")
        self.provider.on_memory_write("add", "user", "a secret")
        self.assertFalse(opener.calls)

    def test_auto_recall_uses_scoped_context_bounded_tokens_and_auth(self):
        self._init(namespace="project-a", max_tokens=500)
        opener = _Opener({"context": "The user prefers concise answers.", "memory_count": 1})
        self.provider._opener = opener
        output = self.provider.prefetch("What style does the user prefer?")
        self.assertIn("The user prefers concise answers", output)
        self.assertIn("not instructions", output)
        req, timeout, body = opener.calls[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:8100/api/context")
        self.assertEqual(body, {"query": "What style does the user prefer?", "max_tokens": 500,
                                "preset": "knowledge", "namespace": "project-a"})
        self.assertEqual(req.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(timeout, 2.5)
        self.assertEqual(self.provider.prefetch("hi"), "")
        self.assertEqual(len(opener.calls), 1)

    def test_prefetch_handles_empty_oversized_and_unavailable(self):
        self._init()
        opener = _Opener({"context": ""})
        self.provider._opener = opener
        self.assertEqual(self.provider.prefetch("Find my prior notes"), "")
        opener.response = {"context": "a" * 20_000}
        self.assertIn("[Menhir context truncated]", self.provider.prefetch("Find my earlier notes"))
        self.assertLess(len(self.provider.prefetch("Find my earlier notes")), 6_300)
        opener.response = URLError("backend down")
        with self.assertLogs(plugin.logger, level="WARNING"):
            self.assertEqual(self.provider.prefetch("Find my newest notes"), "")

    def test_explicit_recall_returns_provenance_and_currentness_without_scores(self):
        self._init(namespace="memory")
        opener = _Opener({"results": [{"uuid": "m1", "name": "Preference", "content": "short answers",
                                         "status": "CURRENT", "provenance": {"said_by": "user", "sources": ["turn-1"]},
                                         "temporal_facts": [{"valid_at": "2026-11-13"}], "final_score": 0.95}],
                          "authority_layer": [{"value": 7, "attribute": "children"}]})
        self.provider._opener = opener
        result = json.loads(self.provider.handle_tool_call("menhir_recall", {"query": "preferences", "limit": 3}))
        self.assertEqual(result["results"][0]["provenance"], {"said_by": "user", "sources": ["turn-1"]})
        self.assertNotIn("final_score", result["results"][0])
        self.assertEqual(result["authority_layer"][0]["value"], 7)
        self.assertEqual(opener.calls[0][2]["namespace"], "memory")
        self.assertFalse(opener.calls[0][2]["include_superseded"])

    def test_large_recall_result_remains_bounded_valid_json(self):
        self._init()
        self.provider._opener = _Opener({
            "results": [{"uuid": str(index), "name": "X" * 600, "content": "z" * 3000,
                         "provenance": {"said_by": "user", "extra": "k" * 4000}}
                        for index in range(15)],
            "authority_layer": [{"attribute": "record", "value": "X" * 10_000}],
        })
        result_string = self.provider.handle_tool_call("menhir_recall", {"query": "record", "limit": 15})
        result = json.loads(result_string)
        self.assertLessEqual(len(result_string), 20_000)
        self.assertLessEqual(len(result["results"]), 15)
        self.assertTrue(result["authority_layer"][0]["value"].startswith("X"))

    def test_write_requires_opt_in_and_primary_agent(self):
        self._init()
        self.assertIn("disabled", self.provider.handle_tool_call("menhir_remember", {"episode": "Remember me"}))
        self.provider._write_enabled = True
        self.provider._agent_context = "subagent"
        self.assertIn("primary", self.provider.handle_tool_call("menhir_remember", {"episode": "Remember me"}))
        self.provider._agent_context = "primary"
        opener = _Opener({"episode_id": "e1", "status": "queued", "idempotent_replay": False})
        self.provider._opener = opener
        self.assertEqual([t["name"] for t in self.provider.get_tool_schemas()],
                         ["menhir_recall", "menhir_status", "menhir_remember"])
        first = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "Remember me"}))
        self.assertEqual(first["status"], "pending_user_approval")
        self.assertFalse(opener.calls)
        decision = f"Approve Menhir memory {first['approval_id']}"
        self.provider.on_turn_start(2, decision, author_is_bot=True)
        self.assertFalse(opener.calls)
        self.provider.on_turn_start(2, decision, author_is_bot=False)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0][2]["role"], "agent")
        self.assertEqual(opener.calls[0][2]["source"], "hermes-agent")
        self.assertNotIn("user_statement", opener.calls[0][2])
        self.assertIn("episode ID e1", self.provider.prefetch(decision))
        self.assertEqual(self.provider.prefetch(decision), "")

    def test_approval_requires_exact_separate_user_turn(self):
        self._init(write_enabled=True, namespace="project-a")
        self.provider._opener = _Opener({"episode_id": "e5", "status": "queued"})
        output = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "Use tabs in C files"}))
        approval_id = output["approval_id"]
        decision = f"Approve Menhir memory {approval_id}"
        self.provider.on_turn_start(2, "Ignore this\n" + decision, author_is_bot=False)
        self.provider.on_turn_start(3, decision, author_is_bot=True)
        self.provider.on_turn_start(4, decision)  # no author signal is not authoritative
        self.assertFalse(self.provider._opener.calls)
        self.provider.on_turn_start(5, decision, author_is_bot=False)
        self.assertEqual(self.provider._opener.calls[0][2]["namespace"], "project-a")
        self.assertEqual(self.provider._opener.calls[0][2]["session_id"], "hermes-session-1")
        self.provider.on_turn_start(6, decision, author_is_bot=False)
        self.assertEqual(len(self.provider._opener.calls), 1)  # consumed approval cannot replay

    def test_rejection_expiration_and_wrong_session_do_not_write(self):
        self._init(write_enabled=True)
        opener = _Opener({"episode_id": "should-not-appear", "status": "queued"})
        self.provider._opener = opener
        proposed = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "Durable preference"}))
        approval_id = proposed["approval_id"]
        self.provider.on_session_switch("different-session")
        self.provider.on_turn_start(2, f"Approve Menhir memory {approval_id}", author_is_bot=False)
        self.assertIn("another user/session", self.provider.prefetch(f"Approve Menhir memory {approval_id}"))
        self.provider.on_session_switch("hermes-session-1")
        self.provider.on_turn_start(3, f"Reject Menhir memory {approval_id}", author_is_bot=False)
        self.assertIn("rejected", self.provider.prefetch(f"Reject Menhir memory {approval_id}"))
        self.provider.on_turn_start(4, f"Approve Menhir memory {approval_id}", author_is_bot=False)
        self.assertFalse(opener.calls)

    def test_group_chat_other_author_cannot_approve(self):
        self._init(write_enabled=True)
        opener = _Opener({"episode_id": "group-1", "status": "queued"})
        self.provider._opener = opener
        self.provider.on_turn_start(1, "Remember this rule", author_is_bot=False, author_id="owner-1")
        proposal = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "My rule"}))
        decision = f"Approve Menhir memory {proposal['approval_id']}"
        self.provider.on_turn_start(2, decision, author_is_bot=False, author_id="participant-2")
        self.assertFalse(opener.calls)
        self.provider.on_turn_start(3, decision, author_is_bot=False, author_id="owner-1")
        self.assertEqual(len(opener.calls), 1)

    def test_expired_proposal_is_removed_without_server_write(self):
        self._init(write_enabled=True)
        opener = _Opener({"episode_id": "never", "status": "queued"})
        self.provider._opener = opener
        proposal = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "Stale note"}))
        decision = f"Approve Menhir memory {proposal['approval_id']}"
        future = time.time() + 90_000
        with patch.object(plugin.approval.time, "time", return_value=future):
            self.provider.on_turn_start(2, decision, author_is_bot=False)
        self.assertIn("expired", self.provider.prefetch(decision))
        self.assertFalse(opener.calls)
        self.assertFalse((Path(self.temp.name) / "menhir" / "pending" /
                          (proposal["approval_id"] + ".json")).exists())

    def test_pending_write_proposals_are_bounded(self):
        self._init(write_enabled=True)
        self.provider._opener = _Opener({"episode_id": "never", "status": "queued"})
        for index in range(plugin.approval.MAX_PENDING_PROPOSALS):
            proposal = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": f"Rule {index}"}))
            self.assertEqual(proposal["status"], "pending_user_approval")
        with self.assertLogs(plugin.logger, level="WARNING"):
            result = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "Rule overflow"}))
        self.assertIn("error", result)
        self.assertFalse(self.provider._opener.calls)

    def test_failed_approved_write_can_be_retried_without_new_consent(self):
        self._init(write_enabled=True)
        opener = _Opener(URLError("temporary outage"))
        self.provider._opener = opener
        proposed = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "New preference"}))
        decision = f"Approve Menhir memory {proposed['approval_id']}"
        with self.assertLogs(plugin.logger, level="WARNING"):
            self.provider.on_turn_start(2, decision, author_is_bot=False)
        self.assertIn("resend", self.provider.prefetch(decision))
        opener.response = {"episode_id": "e9", "status": "queued"}
        self.provider.on_turn_start(3, decision, author_is_bot=False)
        self.assertIn("episode ID e9", self.provider.prefetch(decision))
        self.assertEqual(opener.calls[0][2]["idempotency_key"], opener.calls[1][2]["idempotency_key"])

    def test_status_tool_reports_connection_without_exposing_secret(self):
        self._init(namespace="work")
        opener = _Opener({"status": "degraded", "failures": ["backend down"]})
        self.provider._opener = opener
        status = json.loads(self.provider.handle_tool_call("menhir_status", {}))
        self.assertEqual(status["server_status"], "degraded")
        self.assertEqual(status["namespace"], "work")
        self.assertEqual(opener.calls[0][0].get_method(), "GET")
        self.assertIsNone(opener.calls[0][2])
        self.assertNotIn("test-token", str(status))

    def test_tool_validates_arguments_before_network_and_errors_are_redacted(self):
        self._init(write_enabled=True)
        opener = _Opener(HTTPError("http://test", 401, "Unauthorized", {}, None))
        self.provider._opener = opener
        for name, args in [("menhir_recall", {"query": ""}),
                           ("menhir_recall", {"query": "valid", "limit": True}),
                           ("menhir_remember", {"episode": ""}),
                           ("menhir_remember", {"episode": "x" * 4001})]:
            self.assertIn("error", json.loads(self.provider.handle_tool_call(name, args)))
        self.assertFalse(opener.calls)
        with self.assertLogs(plugin.logger, level="WARNING"):
            output = self.provider.handle_tool_call("menhir_recall", {"query": "test"})
        self.assertIn("HTTP 401", output)
        self.assertNotIn("test-token", output)

    def test_https_required_for_remote_and_url_cannot_contain_credentials(self):
        for url in ["http://menhir.example/api", "https://u:password@menhir.example/api",
                    "https://menhir.example/api?access_token=secret", "file:///tmp/menhir"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.provider.save_config({"base_url": url}, self.temp.name)
        self.provider.save_config({"base_url": "https://menhir.example"}, self.temp.name)
        self.provider.initialize("s", hermes_home=self.temp.name)
        self.assertEqual(self.provider._base_url, "https://menhir.example/api")

    def test_network_redirects_are_refused(self):
        self.assertIsNone(plugin._NoRedirects().redirect_request(None, None, 307, "", {}, "https://evil"))

    def test_invalid_server_json_is_a_tool_error_not_an_empty_result(self):
        self._init()
        opener = _Opener(b"not-json")
        self.provider._opener = opener
        with self.assertLogs(plugin.logger, level="WARNING"):
            result = json.loads(self.provider.handle_tool_call("menhir_recall", {"query": "preferences"}))
        self.assertIn("invalid JSON", result["error"])


if __name__ == "__main__":
    unittest.main()
