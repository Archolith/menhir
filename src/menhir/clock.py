"""The one UTC timestamp helper.

CF-93: `_utc_now_iso` was declared **sixteen** times across `infrastructure/` and `services/` --
the same one-line function, copied. All sixteen are behaviourally identical (the outlier in
`verifier_sync.py` differed only by doing its `datetime` import inside the function), so nothing was
broken; the finding is that nothing kept them identical.

That is not hypothetical for this codebase. CF-77's `Embed` alias was copied the same way and one
copy dropped `| None`. A timestamp helper is exactly the kind of thing someone "improves" in one
place -- switching to `time.time()`, dropping the timezone, truncating microseconds -- and every
stored `created_at`, lease expiry and saga receipt in the system is written through one of these.

Top level rather than under `infrastructure/` or `services/`: both layers use it and neither owns
it. `domain/` does not use it at all today, and putting it here keeps that option open without a
layering violation.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utc_now_iso"]


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with an explicit offset.

    Timezone-aware on purpose. A naive timestamp compares wrongly against the aware ones already
    stored throughout the graph and the sidecar, and `scalar_state_fold` raises `TypeError` outright
    when a naive `as_of` meets an aware `valid_at` (DOMB-COR-3).
    """
    return datetime.now(timezone.utc).isoformat()
