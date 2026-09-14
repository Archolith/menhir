"""Which .env Menhir reads, and how a refused Neo4j password is told apart from a down server.

Both found by running `menhir up --check` against misconfigurations a first run actually hits.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import dotenv
import pytest

from menhir import env_file
from menhir.config import MemorySettings
from menhir.core import runtime_preflight as rp

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------- env file resolution

def test_resolve_env_file_prefers_ENV_FILE_then_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENV_FILE", raising=False)
    assert env_file.resolve_env_file() is None

    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    assert env_file.resolve_env_file() == tmp_path / ".env"

    monkeypatch.setenv("ENV_FILE", str(tmp_path / "other.env"))
    assert env_file.resolve_env_file() == tmp_path / "other.env"


def test_load_menhir_env_reads_cwd_not_the_package_ancestry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.delenv("MENHIR_TEST_ENV_MARKER", raising=False)
    (tmp_path / ".env").write_text("MENHIR_TEST_ENV_MARKER=from-cwd\n", encoding="utf-8")

    assert env_file.load_menhir_env() == tmp_path / ".env"
    import os

    assert os.environ["MENHIR_TEST_ENV_MARKER"] == "from-cwd"


# ---------------------------------------------------------------- dependency dotenv guard

def _call_load_dotenv_as(module_name: str):
    """Invoke the (guarded) dotenv.load_dotenv from a frame whose module is ``module_name``."""
    code = "def call():\n    return load_dotenv()\n"
    mod = types.ModuleType(module_name)
    mod.__dict__["load_dotenv"] = dotenv.load_dotenv
    exec(code, mod.__dict__)
    return mod.call()


def test_guard_is_installed_by_importing_menhir() -> None:
    import menhir  # noqa: F401

    assert dotenv.load_dotenv is env_file._guarded_load_dotenv
    assert dotenv.main.load_dotenv is env_file._guarded_load_dotenv
    env_file.install_dotenv_guard()  # idempotent
    assert dotenv.load_dotenv is env_file._guarded_load_dotenv


def test_bare_load_dotenv_from_graphiti_core_is_a_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MENHIR_TEST_LEAK", raising=False)
    (tmp_path / ".env").write_text("MENHIR_TEST_LEAK=leaked\n", encoding="utf-8")
    # __main__ without __file__ makes find_dotenv use cwd, so this .env WOULD load if unguarded
    monkeypatch.setattr(sys.modules["__main__"], "__file__", None, raising=False)

    assert _call_load_dotenv_as("graphiti_core.helpers") is False
    import os

    assert "MENHIR_TEST_LEAK" not in os.environ


def test_explicit_path_load_dotenv_still_works_for_anyone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MENHIR_TEST_EXPLICIT", raising=False)
    target = tmp_path / "x.env"
    target.write_text("MENHIR_TEST_EXPLICIT=yes\n", encoding="utf-8")

    assert dotenv.load_dotenv(str(target)) is True
    import os

    assert os.environ["MENHIR_TEST_EXPLICIT"] == "yes"


# ---------------------------------------------------------------- neo4j probe

class _AuthErr(Exception):
    code = "Neo.ClientError.Security.Unauthorized"


class _Down(Exception):
    code = ""


def _driver_raising(exc: Exception):
    class _Session:
        def __enter__(self):
            raise exc

        def __exit__(self, *a):
            return False

    class _Driver:
        def session(self):
            return _Session()

        def close(self):
            pass

    class _GraphDatabase:
        @staticmethod
        def driver(uri, auth):
            return _Driver()

    return _GraphDatabase


def test_probe_neo4j_distinguishes_unauthorized_from_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "_NEO4J_IMPORT_ERROR", None)
    monkeypatch.setattr(rp, "GraphDatabase", _driver_raising(_AuthErr("refused")))
    assert rp.probe_neo4j("bolt://x", "neo4j", "wrong") == "unauthorized"

    monkeypatch.setattr(rp, "GraphDatabase", _driver_raising(_Down("connection refused")))
    assert rp.probe_neo4j("bolt://x", "neo4j", "pw") == "unreachable"


def test_collect_names_the_credential_when_neo4j_refuses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "check_graphiti_dependency", lambda: True)
    def _refused(*a, **k):
        rp._last_neo4j_status = "unauthorized"  # what the real probe records before returning False
        return False

    monkeypatch.setattr(rp, "check_neo4j_connectivity", _refused)
    monkeypatch.setattr(rp, "expected_graphiti_embedding_dimension", lambda settings: None)
    monkeypatch.setattr(rp, "check_llama_connectivity", lambda **k: True)

    caps = rp.collect_runtime_capabilities(
        MemorySettings(graphiti_provider="local", local_llm_chat_model="m", local_llm_embed_model="e"),
        require_venv=False,
    )
    assert caps.neo4j_status == "unauthorized"
    assert any("rejected NEO4J_USER/NEO4J_PASSWORD" in f for f in caps.failures)

    from menhir.cli.up import render_report, tier_report

    text = render_report(tier_report(caps, MemorySettings(graphiti_provider="local")), caps.startup_mode)
    assert "[MISS] neo4j: reachable  <- NEO4J_USER / NEO4J_PASSWORD were rejected" in text
