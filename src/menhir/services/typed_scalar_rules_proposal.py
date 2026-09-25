"""TypedScalarProposal: the unbound, well-typed unit exchanged by the typed-scalar rules.

Moved verbatim from ``typed_scalar_rules``; the facade module re-exports the class so every
existing import site keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.domain.typed_assertion import build_source_key, normalize_scalar


@dataclass(frozen=True)
class TypedScalarProposal:
    """One UNBOUND, well-typed, grounded typed-scalar observation from a single extraction pass.

    It carries everything a `TypedAssertion` needs EXCEPT the resolved `subject_uuid` (binding is
    post-finalization, C.4.3) and the gate verdict (C.4.2). `subject_text` is the raw extracted
    subject that binding will resolve against the episode's surviving entities. A proposal only
    exists when its `stated_span` was UNIQUELY located in the episode text, so offsets are always
    known (>=0) and `claim_ordinal` is always 0 — two proposals citing the same source span share a
    `source_key` and are competing interpretations of ONE claim, never order-numbered siblings.
    `when` is the parsed/normalized ISO world-time, or None when the model gave none (C.4.3 then
    assigns an episode_reference / learned_fallback basis)."""

    subject_text: str
    attribute: str
    scope: str
    value_kind: str
    unit: str
    operation: str
    value: Any
    stated_span: str
    episode_uuid: str
    span_start: int
    span_end: int
    when: str | None = None
    display: str = ""
    claim_ordinal: int = 0

    @property
    def slot_key(self) -> tuple[str, str, str, str]:
        """(attribute, scope, value_kind, unit) — the ScalarStateView slot this proposal targets,
        independent of subject (subject is resolved at binding time)."""
        return (
            self.attribute.strip().lower(),
            self.scope.strip().lower(),
            self.value_kind.strip().lower(),
            (self.unit or "").strip().lower(),
        )

    @property
    def source_key(self) -> str:
        """BINDING-STABLE claim locator, computed by the SAME domain helper the `TypedAssertion`
        persists (`build_source_key`), so the durable identity this proposal predicts cannot drift
        from the one the store writes. Independent of model output order (offsets are located, not
        counted) and of subject binding."""
        return build_source_key(
            self.episode_uuid, self.span_start, self.span_end, self.claim_ordinal)

    @property
    def normalized_value(self) -> str:
        """Stable normalized value string — the vote key the C.4.2 consistency gate concentrates on
        (mirrors `TypedAssertion` / `ScalarStateKind` normalization)."""
        return normalize_scalar(self.value)
