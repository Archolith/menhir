"""Contract tests for the standalone Hermes provider; no live Hermes/Menhir required."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
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
    spec = importlib.util.spec_from_file_location("test_hermes_menhir_plugin", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"agent": agent, "agent.memory_provider": memory, "agent.secret_scope": secret}):
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
        self.calls.append((request, timeout, json.loads(request.data)))
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

    def test_default_read_only_schema_and_no_background_sync(self):
        self._init()
        self.assertEqual([item["name"] for item in self.provider.get_tool_schemas()], ["menhir_recall"])
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
        self.assertIn("[Menhir context truncated]", self.provider.prefetch("Find my prior notes"))
        self.assertLess(len(self.provider.prefetch("Find my prior notes")), 6_300)
        opener.response = URLError("backend down")
        with self.assertLogs(plugin.logger, level="WARNING"):
            self.assertEqual(self.provider.prefetch("Find my prior notes"), "")

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
                         ["menhir_recall", "menhir_remember"])
        first = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "Remember me"}))
        second = json.loads(self.provider.handle_tool_call("menhir_remember", {"episode": "Remember me"}))
        self.assertEqual(first["status"], "queued")
        self.assertEqual(opener.calls[0][2]["idempotency_key"], opener.calls[1][2]["idempotency_key"])
        self.assertEqual(opener.calls[0][2]["role"], "agent")
        self.assertEqual(opener.calls[0][2]["source"], "hermes-agent")
        self.assertNotIn("user_statement", opener.calls[0][2])
        self.assertEqual(second["episode_id"], "e1")

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
