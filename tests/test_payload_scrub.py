"""#577: scrub_payload redacts string leaves of structured output."""

from __future__ import annotations

from collections import namedtuple

import pytest

from menhir.domain import payload_scrub
from menhir.domain.payload_scrub import PayloadScrubFailed, scrub_payload

pytestmark = pytest.mark.unit

AWS = "AKIAIOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_nested_structures_are_scrubbed():
    out = scrub_payload({"a": [f"k {AWS}", {"b": f"t {GITHUB}"}], "n": 3, "none": None})
    flat = repr(out)
    assert AWS not in flat and GITHUB not in flat
    assert "[REDACTED:" in flat
    assert out["n"] == 3 and out["none"] is None


def test_tuple_stays_tuple():
    out = scrub_payload((f"x {AWS}", 1))
    assert isinstance(out, tuple) and AWS not in out[0] and out[1] == 1


def test_namedtuple_type_preserved():
    Pair = namedtuple("Pair", "left right")
    out = scrub_payload(Pair(f"x {GITHUB}", 2))
    assert isinstance(out, Pair)
    assert GITHUB not in out.left and out.right == 2


def test_set_scrubbed():
    out = scrub_payload({f"x {AWS}"})
    assert isinstance(out, set)
    assert all(AWS not in item for item in out)


def test_off_policy_returns_input(monkeypatch):
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "off")
    value = {"a": AWS}
    assert scrub_payload(value) is value


def test_detector_failure_raises(monkeypatch):
    def boom(_text):
        raise RuntimeError("detector down")

    monkeypatch.setattr(payload_scrub, "redact", boom)
    with pytest.raises(PayloadScrubFailed):
        scrub_payload({"a": "text"})
