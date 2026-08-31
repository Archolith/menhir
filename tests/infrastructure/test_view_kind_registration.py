"""Focused tests for instance-local ViewKind registration."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from menhir.infrastructure.view_models import ViewKind
from menhir.infrastructure.view_repository import ViewRepository


class ExternalTestKind(ViewKind):
    """Minimal non-built-in kind used only to prove the registration seam."""

    name = "external_test"
    read_fields = "n.uuid AS uuid, n.view_subject AS subject, n.view_value AS value"

    def key_discriminator(self, payload: dict[str, object]) -> str:
        return str(payload["slot"])

    def signature(self, payload: dict[str, object]) -> str:
        return str(payload["value"])

    def surface(self, subject: str, payload: dict[str, object]) -> tuple[str, str]:
        value = str(payload["value"])
        return f"{subject} external test", value

    def write_props(
        self, subject: str, key: str, payload: dict[str, object]
    ) -> dict[str, object]:
        return {"view_value": str(payload["value"])}

    def parse(self, row: dict[str, object]) -> dict[str, object]:
        return {
            "uuid": row.get("uuid"),
            "subject": row.get("subject"),
            "value": row.get("value"),
        }


def _registry_with(kind: ViewKind) -> dict[str, ViewKind]:
    return {**ViewRepository.KINDS, kind.name: kind}


def test_default_registry_preserves_builtin_universe_and_is_instance_local() -> None:
    repo = ViewRepository(object())

    assert set(repo.KINDS) == set(ViewRepository.KINDS)
    assert repo.KINDS is not ViewRepository.KINDS


def test_view_key_preserves_deployed_default_namespace_identity() -> None:
    assert ViewRepository._key(
        None, "Alice", "finding", subject_uuid="entity-1"
    ) == "::entity-1::finding"
    assert ViewRepository._key(
        "", "Alice", "finding", subject_uuid="entity-1"
    ) == "::entity-1::finding"
    assert ViewRepository._key(
        "default", "Alice", "finding", subject_uuid="entity-1"
    ) == "default::entity-1::finding"
    assert ViewRepository._key(
        " default ", "Alice", "finding", subject_uuid="entity-1"
    ) == "default::entity-1::finding"
    assert ViewRepository._key(
        "tenant-a", "Alice", "finding", subject_uuid="entity-1"
    ) == "tenant-a::entity-1::finding"


def test_external_kind_is_injected_without_mutating_class_or_other_instances() -> None:
    kind = ExternalTestKind()
    registry = _registry_with(kind)

    repo = ViewRepository(object(), kinds=registry)

    assert repo.KINDS[kind.name] is kind
    assert kind.name not in ViewRepository.KINDS
    assert kind.name not in ViewRepository(object()).KINDS

    registry.pop(kind.name)
    assert repo.KINDS[kind.name] is kind


def test_registry_key_must_match_kind_name() -> None:
    kind = ExternalTestKind()

    with pytest.raises(ValueError, match="does not match kind.name"):
        ViewRepository(object(), kinds={"wrong_name": kind})


def test_record_dispatches_through_injected_external_kind() -> None:
    kind = ExternalTestKind()
    repo = ViewRepository(object(), kinds=_registry_with(kind))
    writer = Mock(
        return_value={
            "uuid": "view-1",
            "view_key": "::alice::finding",
            "kind": kind.name,
            "created": True,
            "superseded": False,
        }
    )
    repo._write_version = writer

    result = repo.record(
        kind.name,
        subject="Alice",
        slot="finding",
        value="resolved",
    )

    call = writer.call_args.kwargs
    assert call["kind"] == kind.name
    assert call["key"] == "::alice::finding"
    assert call["name"] == "Alice external test"
    assert call["summary"] == "resolved"
    assert call["sig"] == "resolved"
    assert call["extra_props"] == {"view_value": "resolved"}
    assert result["view_value"] == "resolved"
