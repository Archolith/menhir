"""Loopback HTTP contract test for Hermes -> Menhir REST transport and write approval.

This exercises real socket requests; it is not a substitute for a graph-backed Menhir deployment.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_hermes_memory_provider import plugin


class _MenhirStub(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, status: int, response: dict):
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authenticated(self) -> bool:
        if self.headers.get("Authorization") != "Bearer test-token":
            self._send_json(401, {"detail": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self._authenticated():
            return
        if self.path == "/api/ready":
            self._send_json(200, {"status": "ready", "startup_mode": "online", "capabilities": {}, "failures": []})
        else:
            self._send_json(404, {})

    def do_POST(self):
        if not self._authenticated():
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        self.server.requests.append((self.path, body))
        if self.path == "/api/context":
            self._send_json(200, {"context": "Known project convention: use tabs.", "memory_count": 1})
        elif self.path == "/api/recall":
            self._send_json(200, {"results": [{"uuid": "m1", "name": "Code style", "content": "Use tabs",
                                                      "provenance": {"said_by": "user"}}]})
        elif self.path == "/api/memory":
            self._send_json(200, {"episode_id": "episode-123", "status": "queued"})
        else:
            self._send_json(404, {})


class HermesMenhirSocketTests(unittest.TestCase):
    def test_full_request_sequence_with_separate_user_approval(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MenhirStub)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as profile:
            provider = plugin.MenhirMemoryProvider()
            provider.save_config({"base_url": f"http://127.0.0.1:{server.server_port}/api",
                                  "namespace": "project-a", "write_enabled": True}, profile)
            provider.initialize("conversation-1", hermes_home=profile, agent_context="primary")
            self.assertEqual(json.loads(provider.handle_tool_call("menhir_status", {}))["server_status"], "ready")
            self.assertIn("Known project convention", provider.prefetch("What coding style do I use?"))
            recall = json.loads(provider.handle_tool_call("menhir_recall", {"query": "coding style"}))
            self.assertEqual(recall["results"][0]["uuid"], "m1")

            proposal = json.loads(provider.handle_tool_call("menhir_remember", {"episode": "Use tabs for code"}))
            self.assertEqual(proposal["status"], "pending_user_approval")
            self.assertEqual([path for path, _ in server.requests], ["/api/context", "/api/recall"])

            # Simulate a new Hermes process in the SAME profile/session after restart.
            resumed = plugin.MenhirMemoryProvider()
            resumed.initialize("conversation-1", hermes_home=profile, agent_context="primary")
            decision = f"Approve Menhir memory {proposal['approval_id']}"
            resumed.on_turn_start(3, decision, author_is_bot=False)
            self.assertIn("episode-123", resumed.prefetch(decision))
            self.assertEqual(server.requests[-1][0], "/api/memory")
            self.assertEqual(server.requests[-1][1]["namespace"], "project-a")
            self.assertEqual(server.requests[-1][1]["role"], "agent")
            self.assertEqual(server.requests[-1][1]["episode"], "Use tabs for code")
            self.assertEqual(len([path for path, _ in server.requests if path == "/api/memory"]), 1)


if __name__ == "__main__":
    unittest.main()
