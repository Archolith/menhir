"""CF-87: all three graphiti patch modules share one warn-only version guard.

Two of the three patch modules (model, llm) monkeypatched graphiti-core with no
version guard at all; only extraction had one. That guard now lives as a shared
helper in ``graphiti_helpers`` so the expected prefix is a single declaration
and every patch module warns at import when the installed graphiti-core drifts
outside the tested range.

The guard is warn-only by design: a dependency bump must be a re-audit, not an
outage, and escalating to a hard failure is a decision this finding does not make.

Each patch module binds the shared helper, so the per-module guard is exercised
by calling that bound function under a patched ``importlib.metadata.version`` --
no ``importlib.reload``, which would reset the modules' own state mid-suite.
"""

import logging
from unittest.mock import patch

import pytest

import menhir.infrastructure.graphiti_helpers as helpers
from menhir.infrastructure import (
    graphiti_extraction_patches,
    graphiti_llm_patches,
    graphiti_model_patches,
)
from menhir.infrastructure.graphiti_helpers import (
    _EXPECTED_GRAPHITI_PREFIX,
    check_graphiti_version,
)

GUARD_LOG = "menhir.infrastructure.graphiti_helpers"
PATCH_MODULES = (graphiti_extraction_patches, graphiti_model_patches, graphiti_llm_patches)
GUARD_MARKER = "patches were written for"

pytestmark = pytest.mark.unit


def _guard_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and GUARD_MARKER in str(r.getMessage())
    ]


def _call_each_guard() -> None:
    for mod in PATCH_MODULES:
        mod.check_graphiti_version()


class TestGuardWarnsOnUnexpectedVersion:
    def test_each_module_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger=GUARD_LOG):
            with patch("importlib.metadata.version", return_value="0.30.0"):
                _call_each_guard()
        assert len(_guard_warnings(caplog)) == 3, [
            r.getMessage() for r in _guard_warnings(caplog)
        ]


class TestNoWarnAtExpectedVersion:
    def test_expected_version_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger=GUARD_LOG):
            with patch("importlib.metadata.version", return_value="0.29.2"):
                _call_each_guard()
        assert _guard_warnings(caplog) == [], [
            r.getMessage() for r in _guard_warnings(caplog)
        ]


class TestSingleDeclaration:
    def test_shared_prefix_is_the_only_declaration(self):
        assert _EXPECTED_GRAPHITI_PREFIX == "0.29."
        for mod in PATCH_MODULES:
            # No module re-spells its own constant or its own guard; each uses the
            # shared helper so a re-spelled guard cannot hide behind the name check.
            assert not hasattr(mod, "_EXPECTED_GRAPHITI_PREFIX")
            assert mod.check_graphiti_version is check_graphiti_version

    def test_all_guards_follow_the_shared_constant(self, caplog, monkeypatch):
        # Reword the single shared declaration: every guard must follow it, which
        # proves no module re-spelled its own prefix.
        monkeypatch.setattr(helpers, "_EXPECTED_GRAPHITI_PREFIX", "9.9.")

        with caplog.at_level(logging.WARNING, logger=GUARD_LOG):
            with patch("importlib.metadata.version", return_value="9.9.1"):
                _call_each_guard()
        assert _guard_warnings(caplog) == [], [
            r.getMessage() for r in _guard_warnings(caplog)
        ]

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=GUARD_LOG):
            with patch("importlib.metadata.version", return_value="0.30.0"):
                _call_each_guard()
        assert len(_guard_warnings(caplog)) == 3, [
            r.getMessage() for r in _guard_warnings(caplog)
        ]
