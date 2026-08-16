from __future__ import annotations

import pytest

from menhir.domain.projection import (
    ProjectionAbstention,
    ProjectionDefinition,
    ProjectionMaterialization,
    ProjectionRegistry,
    ProjectionRetirement,
    ProjectionTarget,
    evaluate_projection,
)


def _id(row: object) -> str:
    assert isinstance(row, dict)
    return str(row["id"])


def _type(row: object) -> str:
    assert isinstance(row, dict)
    return str(row["type"])


def _ownership_target(row: object) -> ProjectionTarget:
    assert isinstance(row, dict)
    return ProjectionTarget(
        subject_id=str(row["parcel"]),
        namespace=str(row.get("namespace") or "investigation"),
        key=("current_owner",),
    )


def _ownership_fold(target, assertions, as_of):
    del as_of
    rows = [row for row in assertions if isinstance(row, dict)]
    owners = {str(row["owner"]) for row in rows}
    ids = tuple(str(row["id"]) for row in rows)
    if len(owners) != 1:
        return ProjectionAbstention(
            target=target,
            reason="conflicting_owner_claims",
            contributor_ids=ids,
        )
    return ProjectionMaterialization(
        target=target,
        view_kind="investigation.current_owner",
        payload={"owner": next(iter(owners))},
        contributor_ids=ids,
    )


def _ownership_definition(*, definition_id: str = "investigation.ownership"):
    return ProjectionDefinition(
        definition_id=definition_id,
        version=1,
        input_assertion_types=frozenset(
            {"investigation.deed_owner", "investigation.statement_owner"}
        ),
        output_view_kind="investigation.current_owner",
        assertion_id_resolver=_id,
        assertion_type_resolver=_type,
        target_resolver=_ownership_target,
        fold=_ownership_fold,
    )


@pytest.mark.unit
def test_non_coding_projection_registers_additively_without_central_switch():
    base = ProjectionRegistry()
    definition = _ownership_definition()
    extended = base.with_definition(definition)

    assert len(base) == 0
    assert len(extended) == 1
    assert extended.get("investigation.ownership") is definition
    assert extended.definitions_for_assertion_type("investigation.deed_owner") == (definition,)
    assert extended.definitions_for_assertion_type("unrelated") == ()


@pytest.mark.unit
def test_multiple_assertion_types_feed_one_projection_target_deterministically():
    definition = _ownership_definition()
    rows = [
        {
            "id": "statement-b",
            "type": "investigation.statement_owner",
            "parcel": "parcel-7",
            "owner": "Alice",
        },
        {
            "id": "ignored",
            "type": "investigation.note",
            "parcel": "parcel-7",
            "owner": "Mallory",
        },
        {
            "id": "deed-a",
            "type": "investigation.deed_owner",
            "parcel": "parcel-7",
            "owner": "Alice",
        },
    ]

    forward = evaluate_projection(definition, rows)
    reverse = evaluate_projection(definition, list(reversed(rows)))

    assert forward == reverse
    assert len(forward) == 1
    outcome = forward[0]
    assert isinstance(outcome, ProjectionMaterialization)
    assert outcome.view_kind == "investigation.current_owner"
    assert outcome.payload == {"owner": "Alice"}
    assert outcome.contributor_ids == ("deed-a", "statement-b")
    assert outcome.target == ProjectionTarget(
        subject_id="parcel-7",
        namespace="investigation",
        key=("current_owner",),
    )


@pytest.mark.unit
def test_projection_can_abstain_without_core_knowing_domain_vocabulary():
    outcome = evaluate_projection(
        _ownership_definition(),
        [
            {
                "id": "deed-a",
                "type": "investigation.deed_owner",
                "parcel": "parcel-7",
                "owner": "Alice",
            },
            {
                "id": "statement-b",
                "type": "investigation.statement_owner",
                "parcel": "parcel-7",
                "owner": "Bob",
            },
        ],
    )[0]

    assert isinstance(outcome, ProjectionAbstention)
    assert outcome.reason == "conflicting_owner_claims"
    assert outcome.contributor_ids == ("deed-a", "statement-b")


@pytest.mark.unit
def test_projection_output_order_is_target_stable():
    definition = _ownership_definition()
    rows = [
        {
            "id": "z1",
            "type": "investigation.deed_owner",
            "parcel": "z-parcel",
            "owner": "Zed",
        },
        {
            "id": "a1",
            "type": "investigation.deed_owner",
            "parcel": "a-parcel",
            "owner": "Ada",
        },
    ]

    outcomes = evaluate_projection(definition, rows)
    assert tuple(outcome.target.subject_id for outcome in outcomes) == (
        "a-parcel",
        "z-parcel",
    )


@pytest.mark.unit
def test_duplicate_definition_and_assertion_ids_fail_closed():
    definition = _ownership_definition()
    with pytest.raises(ValueError, match="duplicate projection definition_id"):
        ProjectionRegistry((definition, definition))
    with pytest.raises(ValueError, match="already registered"):
        ProjectionRegistry((definition,)).with_definition(definition)

    duplicate_rows = [
        {
            "id": "same",
            "type": "investigation.deed_owner",
            "parcel": "parcel-1",
            "owner": "Alice",
        },
        {
            "id": "same",
            "type": "investigation.statement_owner",
            "parcel": "parcel-1",
            "owner": "Alice",
        },
    ]
    with pytest.raises(ValueError, match="duplicate projection assertion id"):
        evaluate_projection(definition, duplicate_rows)


@pytest.mark.unit
def test_fold_cannot_forge_output_kind_target_or_lineage():
    row = {
        "id": "a1",
        "type": "investigation.deed_owner",
        "parcel": "parcel-1",
        "owner": "Alice",
    }

    def wrong_kind(target, assertions, as_of):
        del assertions, as_of
        return ProjectionMaterialization(
            target=target,
            view_kind="forged.kind",
            payload={},
            contributor_ids=("a1",),
        )

    def wrong_target(target, assertions, as_of):
        del target, assertions, as_of
        return ProjectionRetirement(
            target=ProjectionTarget("other", ("current_owner",)),
            reason="wrong",
        )

    def wrong_lineage(target, assertions, as_of):
        del assertions, as_of
        return ProjectionMaterialization(
            target=target,
            view_kind="investigation.current_owner",
            payload={},
            contributor_ids=("not-an-input",),
        )

    base = _ownership_definition()
    for fold, message in (
        (wrong_kind, "registered output"),
        (wrong_target, "different target"),
        (wrong_lineage, "outside its evaluated target"),
    ):
        definition = ProjectionDefinition(
            definition_id=f"test.{fold.__name__}",
            version=1,
            input_assertion_types=base.input_assertion_types,
            output_view_kind=base.output_view_kind,
            assertion_id_resolver=base.assertion_id_resolver,
            assertion_type_resolver=base.assertion_type_resolver,
            target_resolver=base.target_resolver,
            fold=fold,
        )
        with pytest.raises(ValueError, match=message):
            evaluate_projection(definition, [row])


@pytest.mark.unit
def test_definition_metadata_is_validated_without_interpreting_names():
    with pytest.raises(ValueError, match="version"):
        ProjectionDefinition(
            definition_id="x",
            version=0,
            input_assertion_types=frozenset({"a"}),
            output_view_kind="v",
            assertion_id_resolver=_id,
            assertion_type_resolver=_type,
            target_resolver=_ownership_target,
            fold=_ownership_fold,
        )
    with pytest.raises(ValueError, match="non-empty frozenset"):
        ProjectionDefinition(
            definition_id="x",
            version=1,
            input_assertion_types=frozenset(),
            output_view_kind="v",
            assertion_id_resolver=_id,
            assertion_type_resolver=_type,
            target_resolver=_ownership_target,
            fold=_ownership_fold,
        )
