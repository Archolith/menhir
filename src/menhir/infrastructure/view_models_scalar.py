"""Typed-scalar value identity and display helpers, split from ``view_models.py``.

Owns ``_scalar_norm`` — the register-content/signature string that delegates to the domain's
``normalize_scalar`` (CF-56) — and the duration-aware retrieval display the scalar View kinds
render. Every symbol here is re-exported from ``view_models.py``, so the original import path
keeps working unchanged.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from menhir.domain.typed_assertion import normalize_scalar


def _scalar_norm(value: Any) -> str:
    """Stable normalized string for a typed scalar value — the register content AND the signature
    basis. Handles the heterogeneous typed ValueKinds: numbers, ranges ``[lo, hi]``, booleans, and
    string states (clock_time ``07:30``, weekday ``saturday``, status ``finished``).

    CF-56: this now DELEGATES to ``domain.typed_assertion.normalize_scalar`` instead of
    re-implementing it. Both sides feed the same value identity — the domain's output becomes the
    ``assertion_key``, this one becomes the View signature — and the old comment here said the two
    "MUST mirror ... exactly". They did not. The hand-written copy routed every number through
    ``_fmt(float(value))`` and diverged in two ways the domain's ``_num_norm`` docstring already
    names as the reasons for its design:

      * ``float()`` is lossy above 2**53, so ``9007199254740993`` normalized to
        ``'9007199254740992'`` here and to the exact digits in the domain — the assertion and the
        View disagreeing about the same value, which is precisely what the mirror comment forbade.
      * ``_fmt`` raises ``OverflowError`` on ``inf``/``nan``; the domain stringifies them
        defensively so the key builder never crashes.

    Delegation is safe on every value the two already agreed on, which is every finite number
    representable in a float — i.e. everything ``validate_value`` admits in practice.
    """
    return normalize_scalar(value)


def _duration_seconds_endpoint_display(value: Any) -> str | None:
    """Render one canonical duration-in-seconds value without changing its stored identity."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        total = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not total.is_finite():
        return None

    sign = "-" if total < 0 else ""
    magnitude = abs(total)
    whole_seconds = int(magnitude)
    fractional_seconds = magnitude - whole_seconds
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    seconds_value = Decimal(seconds) + fractional_seconds
    seconds_text = format(seconds_value, "f")
    if "." in seconds_text:
        seconds_text = seconds_text.rstrip("0").rstrip(".")
    if seconds_value < 10:
        seconds_text = f"0{seconds_text}"
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{seconds_text}"
    return f"{sign}{minutes}:{seconds_text}"


def _scalar_display(payload: dict[str, Any]) -> str:
    """Return the retrieval-facing display while preserving the canonical scalar value.

    Duration perception deliberately canonicalizes elapsed ``M:SS``/``H:MM:SS`` values to seconds
    so folds, votes, deltas, and signatures stay numeric. The View is the presentation boundary, so
    it restores an elapsed-time display unless a caller supplied a more specific display.
    """
    explicit = payload.get("display")
    if explicit is not None and str(explicit).strip():
        return str(explicit)

    value = payload["value"]
    value_kind = str(payload.get("value_kind") or "").strip().lower()
    unit = str(payload.get("unit") or "").strip().lower()
    if value_kind == "duration" and unit == "seconds":
        if isinstance(value, (list, tuple)):
            displays = [_duration_seconds_endpoint_display(endpoint) for endpoint in value]
            if len(displays) == 2 and all(display is not None for display in displays):
                return displays[0] if displays[0] == displays[1] else f"{displays[0]}–{displays[1]}"
        else:
            display = _duration_seconds_endpoint_display(value)
            if display is not None:
                return display
    return _scalar_norm(value)
