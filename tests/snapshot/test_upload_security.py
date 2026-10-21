"""Operator-key transport checks for the explicit snapshot sync command."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from menhir.snapshot.upload_client import SnapshotUploadError, SnapshotUploader


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://example.invalid", "sync.transport.insecure_url"),
        ("ftp://example.invalid", "sync.transport.invalid_url"),
        ("https://user:secret@example.invalid", "sync.transport.invalid_url"),
        ("https://example.invalid/?token=secret", "sync.transport.invalid_url"),
        ("https://example.invalid?", "sync.transport.invalid_url"),
        (" https://example.invalid", "sync.transport.invalid_url"),
        ("http://[::1", "sync.transport.invalid_url"),
    ],
)
def test_unsafe_targets_are_refused_before_auth_header_exists(
    url: str, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", raising=False)
    with pytest.raises(SnapshotUploadError) as excinfo:
        SnapshotUploader(url, auth_key="operator-secret")
    assert excinfo.value.code == code
    assert "operator-secret" not in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


def test_https_and_loopback_http_targets_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", raising=False)
    assert SnapshotUploader(
        "https://example.invalid", auth_key="key"
    ).endpoint.endswith("/mcp-http")
    assert SnapshotUploader("http://127.0.0.1:7690", auth_key="key").endpoint == (
        "http://127.0.0.1:7690/mcp-http"
    )
    assert SnapshotUploader("http://[::1]:7690", auth_key="key").endpoint == (
        "http://[::1]:7690/mcp-http"
    )


def test_explicit_insecure_override_is_visible_without_logging_the_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", "1")
    uploader = SnapshotUploader("http://example.invalid", auth_key="operator-secret")
    assert uploader.endpoint == "http://example.invalid/mcp-http"
    assert "MENHIR_ALLOW_INSECURE_BACKEND_URL" in caplog.text
    assert "operator-secret" not in caplog.text


def test_redirect_cannot_forward_the_operator_key() -> None:
    sink_requests: list[str | None] = []
    source_requests: list[str | None] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            sink_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args: object) -> None:
            pass

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)

    class SourceHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            source_requests.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{sink.server_port}/mcp-http"
            )
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (source, sink)
    ]
    for thread in threads:
        thread.start()
    try:
        uploader = SnapshotUploader(
            f"http://127.0.0.1:{source.server_port}", auth_key="operator-secret"
        )
        with pytest.raises(SnapshotUploadError) as excinfo:
            uploader._call("begin_project_snapshot", {})
        assert excinfo.value.code == "sync.transport.redirect_refused"
        assert source_requests == ["Bearer operator-secret"]
        assert sink_requests == []
    finally:
        for server in (source, sink):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
