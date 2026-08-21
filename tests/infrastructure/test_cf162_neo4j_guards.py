"""CF-162: Neo4j driver-import guard and transient backoff regression tests.

Covers two defects: (a) the missing-driver fallback bound the driver exception names to
``type(None)``, which is not a ``BaseException`` subclass and turned the intended friendly
``ModuleNotFoundError`` into a ``TypeError`` inside ``execute``'s retry ``except`` tuple; and
(b) the retry loop slept (and logged a retry announcement) on the final attempt, before the
exception it was always about to raise.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from menhir.infrastructure import neo4j as n4
from menhir.infrastructure.neo4j import Neo4jRepository

pytestmark = pytest.mark.unit


def _make_repo(**overrides):
    defaults = dict(uri="bolt://localhost:7687", database="test", user="neo4j", password="pw")
    defaults.update(overrides)
    return Neo4jRepository(**defaults)


def _session_raising(exc_type, message="down"):
    """A session whose run() always raises ``exc_type``."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.run.side_effect = exc_type(message)
    mock_driver = MagicMock()
    mock_driver.session.return_value = mock_session
    repo = _make_repo()
    repo._driver = mock_driver
    return repo, mock_session


# --------------------------------------------------------------------------- (a) driver guard

#: The fallback branch only runs when `neo4j` is genuinely unimportable, which it is not here. Every
#: other test in this section reaches it by PATCHING the three names -- which proves the retry loop
#: behaves correctly given a catchable binding, but says nothing about what the module actually
#: binds. That gap is not hypothetical: reverting line 30 to `type(None)` left all of them passing.
#:
#: So this loads a SECOND copy of the module from the same source file with `neo4j` blocked at the
#: import system, executing the real `except ModuleNotFoundError` branch. `sys.modules` is left
#: untouched under a private name, so the real module -- and every other test in the worker -- is
#: unaffected.
_NO_DRIVER_MODULE_NAME = "_menhir_neo4j_without_driver"


class _BlockNeo4jImport:
    def find_spec(self, name, path=None, target=None):
        if name == "neo4j" or name.startswith("neo4j."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


def _load_neo4j_module_with_no_driver():
    spec = importlib.util.spec_from_file_location(_NO_DRIVER_MODULE_NAME, Path(n4.__file__))
    module = importlib.util.module_from_spec(spec)
    saved = {
        k: v for k, v in list(sys.modules.items()) if k == "neo4j" or k.startswith("neo4j.")
    }
    for key in saved:
        del sys.modules[key]
    blocker = _BlockNeo4jImport()
    sys.meta_path.insert(0, blocker)
    # dataclasses resolves `cls.__module__` through sys.modules while decorating, so the module
    # has to be registered for the duration of exec_module.
    sys.modules[_NO_DRIVER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.pop(_NO_DRIVER_MODULE_NAME, None)
        sys.modules.update(saved)
    return module


def test_the_fallback_branch_really_binds_an_exception_class():
    """THE test for (a). Executes the real `except ModuleNotFoundError` branch with `neo4j`
    unimportable, and asserts what the module BINDS -- not what a patch supplied."""
    module = _load_neo4j_module_with_no_driver()

    assert module._NEO4J_IMPORT_ERROR is not None
    for name in ("ServiceUnavailable", "SessionExpired", "TransientError"):
        bound = getattr(module, name)
        assert isinstance(bound, type) and issubclass(bound, BaseException), name
        assert bound is not type(None), name


def test_with_the_driver_absent_execute_raises_the_intended_message():
    """The end-to-end counterexample: `_get_driver` raises its friendly ModuleNotFoundError, the
    retry loop's `except` tuple has to be evaluated to let it through, and with `type(None)` bound
    that evaluation raised `TypeError: catching classes that do not inherit from BaseException`
    instead. No patching -- this is the real fallback module."""
    module = _load_neo4j_module_with_no_driver()
    repo = module.Neo4jRepository(uri="bolt://x", database="d", user="u", password="p")

    with pytest.raises(ModuleNotFoundError, match="neo4j is required"):
        repo.execute("RETURN 1")


def test_loading_the_fallback_module_does_not_disturb_the_real_one():
    """POSITIVE CONTROL for the loader itself. It manipulates `sys.meta_path` and `sys.modules`;
    if it leaked, it would corrupt every later test in the worker rather than fail here."""
    _load_neo4j_module_with_no_driver()

    assert n4._NEO4J_IMPORT_ERROR is None
    assert n4.ServiceUnavailable.__name__ == "ServiceUnavailable"
    assert _NO_DRIVER_MODULE_NAME not in sys.modules
    assert importlib.util.find_spec("neo4j") is not None


def test_sentinel_is_a_catchable_exception_class():
    """Fallback binding must be an exception subclass, not NoneType (CF-162)."""
    assert issubclass(n4._Neo4jDriverUnavailable, BaseException)
    assert n4._Neo4jDriverUnavailable is not type(None)


def test_missing_driver_raises_module_not_found_not_typeerror():
    """Driver-gone exception must surface as ModuleNotFoundError, not a TypeError (CF-162)."""
    repo = _make_repo()
    with patch.object(
        Neo4jRepository,
        "_get_driver",
        side_effect=ModuleNotFoundError(
            "neo4j is required to create a Neo4jRepository driver."
        ),
    ):
        with patch("menhir.infrastructure.neo4j.ServiceUnavailable", n4._Neo4jDriverUnavailable):
            with patch("menhir.infrastructure.neo4j.SessionExpired", n4._Neo4jDriverUnavailable):
                with patch("menhir.infrastructure.neo4j.TransientError", n4._Neo4jDriverUnavailable):
                    with pytest.raises(
                        ModuleNotFoundError,
                        match="neo4j is required to create a Neo4jRepository driver.",
                    ):
                        repo.execute("RETURN 1")


def test_none_type_binding_would_raise_typeerror():
    """Positive control: NoneType binding reproduces the original TypeError (CF-162)."""
    repo = _make_repo()
    with patch.object(
        Neo4jRepository,
        "_get_driver",
        side_effect=ModuleNotFoundError("neo4j is required to create a Neo4jRepository driver."),
    ):
        with patch("menhir.infrastructure.neo4j.ServiceUnavailable", type(None)):
            with patch("menhir.infrastructure.neo4j.SessionExpired", type(None)):
                with pytest.raises(TypeError, match="do not inherit from BaseException"):
                    repo.execute("RETURN 1")


# --------------------------------------------------------------------------- (b) backoff


def test_transient_exhaustion_skips_final_sleep():
    """No sleep (or retry log) on the final transient attempt (CF-162)."""
    class FakeTransientError(Exception):
        pass

    repo, _ = _session_raising(FakeTransientError)
    sleeps: list[float] = []
    with patch("menhir.infrastructure.neo4j.TransientError", FakeTransientError):
        with patch("menhir.infrastructure.neo4j.time.sleep", side_effect=sleeps.append):
            with pytest.raises(FakeTransientError, match="down"):
                repo.execute("RETURN 1")

    base = n4._TRANSIENT_BACKOFF_BASE
    assert sleeps == [base * 1, base * 2]
    assert len(sleeps) == n4._TRANSIENT_RETRIES - 1
    assert base * 4 not in sleeps


def test_ambiguous_exhaustion_skips_final_sleep():
    """No sleep on the final ambiguous (ServiceUnavailable) attempt with safe re-execute (CF-162)."""
    class FakeServiceUnavailable(Exception):
        pass

    repo, _ = _session_raising(FakeServiceUnavailable)
    sleeps: list[float] = []
    with patch("menhir.infrastructure.neo4j.ServiceUnavailable", FakeServiceUnavailable):
        with patch("menhir.infrastructure.neo4j.time.sleep", side_effect=sleeps.append):
            with pytest.raises(FakeServiceUnavailable, match="down"):
                repo.execute("RETURN 1", safe_to_reexecute=True)

    base = n4._TRANSIENT_BACKOFF_BASE
    assert sleeps == [base * 1, base * 2]
    assert base * 4 not in sleeps


def test_success_on_second_attempt_sleeps_exactly_once():
    """Positive control: a retry that succeeds must still sleep once (CF-162)."""
    class FakeTransientError(Exception):
        pass

    call_count = 0
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    def run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise FakeTransientError("down")
        mock_result = MagicMock()
        mock_record = MagicMock()
        mock_record.data.return_value = {"ok": True}
        mock_result.__iter__ = MagicMock(return_value=iter([mock_record]))
        return mock_result

    mock_session.run.side_effect = run
    mock_driver = MagicMock()
    mock_driver.session.return_value = mock_session
    repo = _make_repo()
    repo._driver = mock_driver

    sleeps: list[float] = []
    with patch("menhir.infrastructure.neo4j.TransientError", FakeTransientError):
        with patch("menhir.infrastructure.neo4j.time.sleep", side_effect=sleeps.append):
            rows = repo.execute("RETURN 1")

    assert sleeps == [n4._TRANSIENT_BACKOFF_BASE * 1]
    assert rows == [{"ok": True}]


def test_no_retry_log_on_exhausted_attempt(caplog):
    """The exhausted attempt must not log a misleading 'retrying in' line (CF-162)."""
    class FakeTransientError(Exception):
        pass

    repo, _ = _session_raising(FakeTransientError)
    with patch("menhir.infrastructure.neo4j.TransientError", FakeTransientError):
        with patch("menhir.infrastructure.neo4j.time.sleep", lambda s: None):
            with caplog.at_level(logging.WARNING, logger="menhir.infrastructure.neo4j"):
                with pytest.raises(FakeTransientError, match="down"):
                    repo.execute("RETURN 1")

    messages = [rec.message for rec in caplog.records]
    exhausted = [m for m in messages if "exhausted" in m]
    assert exhausted, "expected the exhausted-attempt warning"
    assert all("retrying in" not in m for m in exhausted)
    assert len(exhausted) == 1
    assert len(messages) == n4._TRANSIENT_RETRIES
