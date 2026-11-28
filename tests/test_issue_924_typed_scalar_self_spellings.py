"""#924: the third-party guard must apply to every spelling the binder treats as the self."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.services.typed_scalar_rules import parse_scalar_row


def _parse(subject: str):
    source = "I visited my aunt who is married."
    episode = SimpleNamespace(uuid="ep", content=source, reference_time="2026-07-01T10:00:00+00:00")
    row = {
        "subject": subject, "attribute": "marital_status", "scope": "", "value_kind": "status",
        "unit": "", "display": "", "when": "", "operation": "absolute", "value": "married",
        "stated_span": "is married", "episode": 0,
    }
    drops: list[str] = []
    return parse_scalar_row(row, [episode], drops.append), drops


@pytest.mark.unit
@pytest.mark.parametrize("subject", ["user", "I", "me", "the user", "myself", " I "])
def test_third_party_fact_is_dropped_for_every_self_spelling(subject: str) -> None:
    proposal, drops = _parse(subject)
    assert proposal is None
    assert drops == ["value_not_in_span"]
