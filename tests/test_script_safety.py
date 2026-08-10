"""Fail-closed target guards for mutating operator scripts."""

from __future__ import annotations

import pytest

from menhir.infrastructure.script_safety import (
    is_loopback_target,
    target_host,
    write_refusal_reason,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    ["bolt://localhost:7687", "bolt://127.0.0.1:7687", "neo4j://[::1]:7687"],
)
def test_loopback_targets_are_recognized(uri: str) -> None:
    assert is_loopback_target(uri) is True


@pytest.mark.unit
def test_remote_write_requires_explicit_acknowledgement() -> None:
    reason = write_refusal_reason(
        "bolt://graph.example:7687",
        production_markers=(),
        allow_remote=False,
        allow_production=False,
    )

    assert reason is not None
    assert "non-loopback" in reason


@pytest.mark.unit
def test_remote_acknowledgement_allows_nonproduction_target() -> None:
    assert write_refusal_reason(
        "bolt://graph.example:7687",
        production_markers=("prod",),
        allow_remote=True,
        allow_production=False,
    ) is None


@pytest.mark.unit
def test_production_marker_requires_stronger_acknowledgement_case_insensitively() -> None:
    reason = write_refusal_reason(
        "bolt://MENHIR-PROD.example:7687",
        production_markers=("menhir-prod",),
        allow_remote=True,
        allow_production=False,
    )

    assert reason is not None
    assert "production marker" in reason
    assert write_refusal_reason(
        "bolt://MENHIR-PROD.example:7687",
        production_markers=("menhir-prod",),
        allow_remote=False,
        allow_production=True,
    ) is None


@pytest.mark.unit
@pytest.mark.parametrize("uri", ["not-a-uri", "https://graph.example:7687"])
def test_invalid_target_uri_fails_closed(uri: str) -> None:
    with pytest.raises(ValueError, match="invalid Neo4j URI"):
        target_host(uri)
