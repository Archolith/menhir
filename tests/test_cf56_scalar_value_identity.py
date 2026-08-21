"""CF-56 — scalar value-identity is owned twice across the domain/infrastructure boundary.

``normalize_scalar`` (domain) and ``_scalar_norm`` (infrastructure) both produce the persisted
value string AND feed ``assertion_key``/View signatures, but neither imports the other and each
names the other as its authority in a comment. A future divergence would silently split or merge
assertions.

This file carries:
  * the DIFFERENTIAL test (evidence the normalizers agree over a wide input set — and the
    regression guard);
  * VALUE_KINDS equality + structural single-source guards;
  * positive controls that ``normalize_scalar`` behaviour is unchanged.
"""

import ast
from pathlib import Path

import pytest

from menhir.domain.typed_assertion import VALUE_KINDS, normalize_scalar
from menhir.infrastructure.view_models import ScalarStateKind, _scalar_norm

#: The differential input set. Wide by design: ints beyond 2**53, floats needing repr
#: round-tripping, negative zero, huge/tiny exponents, Decimal-ish strings, booleans, None,
#: empty string, and degenerate/genuine ranges (mixed-typed endpoints included).
DIFFERENTIAL_INPUTS = [
    0,
    1,
    -1,
    2,
    2.0,
    -2.0,
    1.5,
    0.1,
    0.30000000000000004,
    1e21,
    1e-7,
    1e308,
    1e-320,
    5e-324,
    1e19,
    -0.0,
    2**53,
    2**53 + 1,
    2**64,
    10**19,
    10**19 + 1,
    10**20,
    -10**19 - 1,
    True,
    False,
    None,
    "",
    "1.5",
    "0012",
    " 45 ",
    "1e3",
    "12.0",
    "0.5",
    "saturday",
    "07:30",
    "finished",
    "workspace:proj",
    [1.5, 2.5],
    [1300, 1300],
    [5, 5],
    [1, "1"],
    [1300, "1300"],
    ["a", "b"],
    [10**19 + 1, 10**19 + 1],
    [2**53 + 1, 2**53 + 1],
    [1e21, 1e21],
    [-3, -3],
]


@pytest.mark.unit
def test_scalar_norm_agrees_with_normalize_scalar() -> None:
    """DIFFERENTIAL: both functions must produce the same string for every input.

    Sixteen probes is not a proof of equivalence; this is the wide set. If ANY input diverges
    here the stored ``assertion_key`` values are already at stake and the reviewer decides —
    we do NOT merge or 'fix' either side on a divergence.
    """
    mismatches = []
    for value in DIFFERENTIAL_INPUTS:
        domain = normalize_scalar(value)
        infra = _scalar_norm(value)
        if domain != infra:
            mismatches.append((value, domain, infra))
    assert not mismatches, (
        "normalize_scalar and _scalar_norm DIVERGE (do not merge):\n"
        + "\n".join(
            f"  input={value!r}\n    normalize_scalar={domain!r}\n    _scalar_norm  ={infra!r}"
            for value, domain, infra in mismatches
        )
    )


@pytest.mark.unit
def test_value_kinds_sets_are_equal() -> None:
    """The two allowlists are the same set; a re-spelling that drifts fails here."""
    assert set(VALUE_KINDS) == set(ScalarStateKind.VALUE_KINDS)


def _view_models_tree() -> ast.AST:
    path = Path(__file__).resolve().parents[1] / "src/menhir/infrastructure/view_models.py"
    return ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_scalar_state_value_kinds_derives_from_domain() -> None:
    """STRUCTURAL: ScalarStateKind.VALUE_KINDS must be ASSIGNED from the domain name, not
    re-spelled. (Do NOT use `is` on interned string literals — it is meaningless.)"""
    tree = _view_models_tree()
    scalar_state = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ScalarStateKind"
    )
    assignment = next(
        n
        for n in scalar_state.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "VALUE_KINDS" for t in n.targets)
    )
    rhs = assignment.value
    assert isinstance(rhs, ast.Name), "VALUE_KINDS must be assigned from a single imported name"
    assert rhs.id == "DOMAIN_VALUE_KINDS", (
        f"VALUE_KINDS must derive from the domain name, got {rhs.id!r}"
    )


@pytest.mark.unit
def test_normalize_scalar_positive_controls() -> None:
    """POSITIVE CONTROL: normalize_scalar still returns exactly what it returned before."""
    assert normalize_scalar(1) == "1"
    assert normalize_scalar(2.0) == "2"
    assert normalize_scalar(True) == "true"
    assert normalize_scalar(False) == "false"
    assert normalize_scalar("  saturday ") == "saturday"
    assert normalize_scalar([1300, 1300]) == "1300"
    assert normalize_scalar([1300, "1300"]) == "1300"
    assert normalize_scalar([1.5, 2.5]) == "1.5-2.5"
    assert normalize_scalar(None) == "None"


@pytest.mark.unit
def test_unknown_value_kind_still_rejected() -> None:
    """POSITIVE CONTROL: an unknown value_kind is still rejected at the same rejection points as
    before — the domain TypedAssertion constructor and the infra ScalarStateKind._slot."""
    from menhir.domain.typed_assertion import TypedAssertion

    with pytest.raises(ValueError):
        TypedAssertion(
            subject_uuid="s", subject_display="S", attribute="a", scope="",
            value_kind="not_a_kind", unit="", operation="absolute", value=5,
            stated_span="q", episode_uuid="e", valid_at="2026-01-01", learned_at="2026-01-01",
        )
    with pytest.raises(ValueError):
        ScalarStateKind._slot({
            "attribute": "a", "scope": "", "value_kind": "not_a_kind", "unit": "",
        })
