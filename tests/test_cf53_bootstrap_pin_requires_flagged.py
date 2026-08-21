"""CF-53 — "a bootstrap pin requires flagged" is a DOMAIN rule, not a service check.

The coupling between ``bootstrap_scope`` and ``flagged`` is a rule about what a valid
bootstrap pin IS, so it belongs in ``domain/bootstrap_scope.py``. Previously only
``ingest_intake`` enforced it inline; a second writer calling ``normalize_bootstrap_scope``
directly produced a pinned-but-unflagged memory with no error.

These tests pin the finding: the rule now lives in the domain (test 1/6), the message is
unchanged (test 2), the positive controls still hold (tests 3-4), and the service still
enforces it end-to-end (test 5).
"""

import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from menhir.domain import MemorySession
from menhir.domain.bootstrap_scope import normalize_bootstrap_scope_for_flag
from menhir.services.ingest_intake import IngestIntakeMixin

_MESSAGE = "bootstrap_scope requires flagged=true"


class _Intake(IngestIntakeMixin):
    """Minimal mixin host: intake only touches ``graph_adapter`` and the enrichment switch."""

    def __init__(self, adapter: MagicMock) -> None:
        self.graph_adapter = adapter
        self._enrichment_enabled = False


def _session() -> MemorySession:
    return MemorySession(
        session_id="session-1",
        user_id="user-1",
        started_at=datetime.now(timezone.utc),
    )


def _read_source() -> str:
    path = Path(__file__).resolve().parents[1] / "src/menhir/domain/bootstrap_scope.py"
    return path.read_text(encoding="utf-8")


# 1 + 2. The rule lives in the domain: calling the DOMAIN function directly with a pin and
# flagged=False raises, and the message is byte-identical.
@pytest.mark.unit
def test_domain_function_rejects_unflagged_pin() -> None:
    with pytest.raises(ValueError) as exc:
        normalize_bootstrap_scope_for_flag("general", flagged=False)
    assert str(exc.value) == _MESSAGE


@pytest.mark.unit
def test_domain_function_message_is_unchanged() -> None:
    with pytest.raises(ValueError) as exc:
        normalize_bootstrap_scope_for_flag("workspace:proj", flagged=False)
    assert str(exc.value) == "bootstrap_scope requires flagged=true"


# 3. POSITIVE CONTROL: pin + flagged=True is accepted and returns the normalized scope.
@pytest.mark.unit
def test_domain_function_accepts_flagged_pin() -> None:
    assert normalize_bootstrap_scope_for_flag(" GENERAL ", flagged=True) == "general"
    assert normalize_bootstrap_scope_for_flag(
        " workspace: Project Alpha ", flagged=True
    ) == "workspace:project alpha"


# 4. POSITIVE CONTROL: no scope + flagged=False is fine (the rule is conditional on a pin).
@pytest.mark.unit
def test_domain_function_allows_no_scope_unflagged() -> None:
    assert normalize_bootstrap_scope_for_flag(None, flagged=False) is None
    assert normalize_bootstrap_scope_for_flag("none", flagged=False) is None


# 5. The service still enforces it end-to-end — BOTH paths raise the same error.
@pytest.mark.unit
@pytest.mark.asyncio
async def test_service_still_enforces_end_to_end() -> None:
    intake = _Intake(MagicMock())
    with pytest.raises(ValueError) as exc:
        await intake.queue_episode_for_enrichment(
            episode="a pinned but unflagged memory",
            session=_session(),
            source="user",
            flagged=False,
            bootstrap_scope="general",
        )
    assert str(exc.value) == _MESSAGE


# 6. STRUCTURAL GUARD: domain/bootstrap_scope.py mentions ``flagged`` in code, not only in a
# docstring. The finding was literally "grep -c flagged returns 1, the docstring line."
@pytest.mark.unit
def test_domain_module_mentions_flagged_in_code_not_only_docstring() -> None:
    tree = ast.parse(_read_source())
    enforcing = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "normalize_bootstrap_scope_for_flag":
            enforcing = node
            break
    assert enforcing is not None, "normalize_bootstrap_scope_for_flag not defined in domain"
    arg_names = {a.arg for a in enforcing.args.args}
    assert "flagged" in arg_names, "'flagged' must be a live parameter of the enforcing function"
