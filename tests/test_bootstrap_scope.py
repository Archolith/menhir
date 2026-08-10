"""Unit tests for explicit startup bootstrap selection."""

import pytest

from menhir.domain.bootstrap_scope import (
    bootstrap_selection,
    normalize_bootstrap_scope,
    normalize_workspace_key,
)


@pytest.mark.unit
def test_workspace_keys_are_nfkc_trimmed_and_casefolded() -> None:
    assert normalize_workspace_key("  ＭＥＮＨＩＲ  ") == "menhir"


@pytest.mark.unit
def test_scope_parser_distinguishes_retention_from_bootstrap() -> None:
    assert normalize_bootstrap_scope(None) is None
    assert normalize_bootstrap_scope("none") is None
    assert normalize_bootstrap_scope(" GENERAL ") == "general"
    assert normalize_bootstrap_scope(" workspace: Project Alpha ") == "workspace:project alpha"


@pytest.mark.unit
def test_scope_parser_rejects_unknown_or_empty_workspace_scope() -> None:
    with pytest.raises(ValueError):
        normalize_bootstrap_scope("global")
    with pytest.raises(ValueError):
        normalize_bootstrap_scope("workspace:   ")


@pytest.mark.unit
def test_selection_is_general_only_without_workspace() -> None:
    assert bootstrap_selection(None) == ("general", ["general"])


@pytest.mark.unit
def test_selection_combines_general_and_exact_workspace() -> None:
    assert bootstrap_selection(" Project Alpha ") == (
        "workspace:project alpha",
        ["general", "workspace:project alpha"],
    )
