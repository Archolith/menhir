from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_pipeline_788", ROOT / "deploy" / "release_pipeline.py"
)
assert SPEC is not None and SPEC.loader is not None
pipeline = importlib.util.module_from_spec(SPEC)
sys.modules["release_pipeline_788"] = pipeline
SPEC.loader.exec_module(pipeline)

DIGEST = "sha256:" + "a" * 64
IMAGE_ARGS = ["--image", "ghcr.io/archolith/menhir", "--tag", "0.3.3", "--digest", DIGEST]


def _reply(body: bytes, status: int = 200):
    def http(method, url, headers, data):
        return status, {}, body
    return http


def _run(capsys, argv, http=None) -> tuple[int, str]:
    code = pipeline.main(argv, http=http or _reply(b"{}"))
    return code, capsys.readouterr().err


def _assert_annotated(code: int, err: str) -> None:
    assert code == 1
    assert "::error title=Release pipeline::" in err
    assert "Traceback" not in err


def _failing_urlopen(monkeypatch, error: Exception) -> list:
    seen: list = []

    def boom(request, timeout=None):
        seen.append(request)
        raise error

    monkeypatch.setattr(pipeline.urllib.request, "urlopen", boom)
    return seen


def test_urlerror_becomes_a_pipeline_error(monkeypatch) -> None:
    _failing_urlopen(monkeypatch, urllib.error.URLError("no route"))
    with pytest.raises(pipeline.ReleasePipelineError, match="no route"):
        pipeline.urllib_http("GET", "https://example.test/", {}, None)


def test_timeout_becomes_a_pipeline_error(monkeypatch) -> None:
    _failing_urlopen(monkeypatch, TimeoutError("timed out"))
    with pytest.raises(pipeline.ReleasePipelineError):
        pipeline.urllib_http("GET", "https://example.test/", {}, None)


def test_urllib_http_sends_an_explicit_user_agent(monkeypatch) -> None:
    seen = _failing_urlopen(monkeypatch, urllib.error.URLError("stop"))
    with pytest.raises(pipeline.ReleasePipelineError):
        pipeline.urllib_http("GET", "https://example.test/", {}, None)
    assert seen[0].get_header("User-agent") == pipeline.USER_AGENT


def test_transport_error_exits_with_annotation(capsys, monkeypatch, tmp_path) -> None:
    _failing_urlopen(monkeypatch, urllib.error.URLError("no route"))
    code, err = _run(
        capsys, ["pypi-state", "--version", "0.3.3", "--dist-dir", str(tmp_path)],
        pipeline.urllib_http,
    )
    _assert_annotated(code, err)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"'])
def test_malformed_or_non_object_pypi_json_is_annotated(capsys, tmp_path, body) -> None:
    (tmp_path / "a.whl").write_bytes(b"x")
    code, err = _run(
        capsys, ["pypi-state", "--version", "0.3.3", "--dist-dir", str(tmp_path)], _reply(body),
    )
    _assert_annotated(code, err)


@pytest.mark.parametrize("body", [b"not json", b"[]"])
def test_malformed_registry_token_reply_is_annotated(capsys, body) -> None:
    code, err = _run(capsys, ["tag-image", *IMAGE_ARGS], _reply(body))
    _assert_annotated(code, err)


@pytest.mark.parametrize("missing", ["filename", "url", "digests"])
def test_pypi_entry_missing_keys_is_annotated(capsys, tmp_path, missing) -> None:
    (tmp_path / "a.whl").write_bytes(b"x")
    entry = {
        "filename": "a.whl",
        "url": "https://files.pythonhosted.org/a.whl",
        "digests": {"sha256": "0" * 64},
    }
    del entry[missing]
    code, err = _run(
        capsys, ["pypi-state", "--version", "0.3.3", "--dist-dir", str(tmp_path)],
        _reply(json.dumps({"urls": [entry]}).encode()),
    )
    _assert_annotated(code, err)


def test_unreadable_python_base_file_is_annotated(capsys, tmp_path) -> None:
    code, err = _run(capsys, [
        "plan", "--tag", "v0.3.3", "--github-ref", "refs/tags/v0.3.3",
        "--image", "ghcr.io/archolith/menhir",
        "--python-base-file", str(tmp_path / "missing.txt"),
    ])
    _assert_annotated(code, err)


@pytest.mark.parametrize("actor,token", [(None, "t"), ("a", None), ("", "t"), ("a", "  ")])
def test_unset_or_blank_credentials_fail_before_any_http_call(
    capsys, monkeypatch, actor, token,
) -> None:
    for name, value in (("GITHUB_ACTOR", actor), ("GH_TOKEN", token)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    calls: list = []

    def http(*args):
        calls.append(args)
        return 200, {}, b"{}"

    code, err = _run(capsys, ["tag-image", *IMAGE_ARGS], http)
    _assert_annotated(code, err)
    assert calls == []


@pytest.mark.parametrize("value", ["a\nb=c", "a\rb"])
def test_write_outputs_rejects_line_breaks(value) -> None:
    with pytest.raises(pipeline.ReleasePipelineError):
        pipeline._write_outputs({"key": value})


# ---- #790: empty dist / empty PyPI file list --------------------------------


def test_empty_dist_and_empty_pypi_urls_is_refused(capsys, tmp_path) -> None:
    http = _reply(json.dumps({"urls": []}).encode())
    with pytest.raises(pipeline.ReleasePipelineError):
        pipeline.adopt_published_distributions(http, "0.3.3", tmp_path)
    code, err = _run(
        capsys, ["pypi-state", "--version", "0.3.3", "--dist-dir", str(tmp_path)], http,
    )
    _assert_annotated(code, err)


def test_empty_dist_with_published_files_is_refused(tmp_path) -> None:
    entry = {
        "filename": "a.whl",
        "url": "https://files.pythonhosted.org/a.whl",
        "digests": {"sha256": "0" * 64},
    }
    with pytest.raises(pipeline.ReleasePipelineError):
        pipeline.adopt_published_distributions(
            _reply(json.dumps({"urls": [entry]}).encode()), "0.3.3", tmp_path,
        )


def test_pypi_state_writes_no_output_when_refused(capsys, tmp_path) -> None:
    code, _ = _run(
        capsys, ["pypi-state", "--version", "0.3.3", "--dist-dir", str(tmp_path)],
        _reply(json.dumps({"urls": []}).encode()),
    )
    assert code == 1
    assert "already_published" not in capsys.readouterr().out
