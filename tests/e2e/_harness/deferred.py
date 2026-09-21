"""Raise a lane's deferred failures as one assertion.

Several lanes collect failures instead of raising at the point of discovery, so one
expensive run exercises every criterion rather than stopping at the first defect. Gate C
still treats each as release-blocking -- deferring changes when the lane fails, never
whether it fails.

The joining lives here because each lane had its own copy, and a multi-line f-string
written into a lane by a patch script is exactly the shape that keeps arriving with
literal newlines inside the quotes.
"""

from __future__ import annotations

__all__ = ["raise_deferred"]

_SEPARATOR = "\n\n"


def raise_deferred(lane: str, failures: list[str]) -> None:
    """Raise one AssertionError naming every deferred failure, or return if there are none."""

    if not failures:
        return
    body = _SEPARATOR.join(failures)
    raise AssertionError(
        f"{lane} reproduced {len(failures)} release-blocking failure(s):{_SEPARATOR}{body}"
    )
