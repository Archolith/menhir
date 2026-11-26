"""Offline contracts for the scalar proposer/reviewer probe's safety and arm accounting."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_probe():
    path = Path(__file__).parents[1] / "scripts" / "probe_scalar_proposer_reviewer.py"
    spec = importlib.util.spec_from_file_location("probe_scalar_proposer_reviewer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def data(self):
        return self.rows


class _Session:
    def __init__(self, namespaces):
        self.namespaces = namespaces
        self.suffix_calls = []
        self.episode_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, query, params):
        if "ENDS WITH" in query:
            self.suffix_calls.append(params["s"])
            return _Result(self.namespaces.get(params["s"], []))
        self.episode_calls += 1
        return _Result([{"episode": "fake"}])


class _Driver:
    def __init__(self, session):
        self._session = session
        self.closed = False

    def session(self):
        return self._session

    def close(self):
        self.closed = True


def test_probe_main_loads_dotenv_and_reports_shared_no_cards(monkeypatch, capsys):
    probe = _load_probe()
    session = _Session({"suffix": [{"ns": "one-suffix"}]})
    driver = _Driver(session)
    graph_calls = []
    openai_calls = []

    class FakeGraphDatabase:
        @staticmethod
        def driver(uri, auth):
            graph_calls.append((uri, auth))
            return driver

    class FakeOpenAI:
        def __init__(self, **kwargs):
            openai_calls.append(kwargs)

    def fake_load_dotenv():
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        monkeypatch.setenv("MENHIR_BENCH_NEO4J_PASSWORD", "loaded-secret")

    monkeypatch.setattr(probe, "GraphDatabase", FakeGraphDatabase)
    monkeypatch.setattr(probe, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(probe, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(probe, "build_episodes", lambda rows: ["fake-episode"])
    monkeypatch.setattr(probe, "propose_claim_cards", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--ns", "suffix", "--arms", "B", "D", "--password", ""],
    )

    assert probe.main() == 0
    assert graph_calls == [("bolt://127.0.0.1:7701", ("neo4j", ""))]
    assert openai_calls == [{"api_key": "fake-key"}]
    assert session.episode_calls == 1
    output = capsys.readouterr().out
    for arm in ("B", "D"):
        assert f"=== ARM {arm} ===\n  commits: 0\n  no-card proposals: 1" in output


def test_probe_main_refuses_ambiguity_before_paid_work_or_capture(monkeypatch, capsys, tmp_path):
    probe = _load_probe()
    session = _Session({
        "good": [{"ns": "good-suffix"}],
        "ambiguous": [{"ns": "one-ambiguous"}, {"ns": "two-ambiguous"}],
    })
    driver = _Driver(session)
    openai_calls = []
    graph_calls = []

    class FakeGraphDatabase:
        @staticmethod
        def driver(uri, auth):
            graph_calls.append((uri, auth))
            return driver

    class FakeOpenAI:
        def __init__(self, **kwargs):
            openai_calls.append(kwargs)

    def fake_load_dotenv():
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        monkeypatch.setenv("MENHIR_BENCH_NEO4J_PASSWORD", "loaded-secret")

    capture = tmp_path / "partial.jsonl"
    monkeypatch.setattr(probe, "GraphDatabase", FakeGraphDatabase)
    monkeypatch.setattr(probe, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(probe, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--ns", "good", "ambiguous", "--jsonl", str(capture)],
    )

    assert probe.main() == 2
    assert session.suffix_calls == ["good", "ambiguous"]
    assert session.episode_calls == 0
    assert openai_calls == []
    assert not capture.exists()
    assert graph_calls == [("bolt://127.0.0.1:7701", ("neo4j", "loaded-secret"))]
    assert driver.closed
    assert "ambiguous namespace suffix ambiguous" in capsys.readouterr().err
