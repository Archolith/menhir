"""Measurement vocabulary for the P2A transport probe.

Extracted verbatim from ``p2a_transport_probe.py``: the probed sizes, the outcome classes, and
the per-trial / per-size result records. Nothing here performs I/O.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

KIB = 1024
MIB = 1024 * 1024

#: Doubling past the protocol's 2 MiB ceiling on purpose. The interesting reading is not where the
#: server stops accepting -- that is a constant we chose -- but where the *path* stops delivering,
#: which may be above or below it.
DEFAULT_SIZES = [
    64 * KIB,
    128 * KIB,
    256 * KIB,
    512 * KIB,
    1 * MIB,
    2 * MIB,
    4 * MIB,
    8 * MIB,
    16 * MIB,
]

#: Three passes, not one. P2A needs a size that works *reliably*; a single success at a size that
#: fails one time in three is worse than a lower ceiling, and one pass cannot tell them apart.
DEFAULT_TRIALS = 3

#: An honest product agent. See `ProbeClient.__init__` for why the default urllib one is unusable
#: through a real Cloudflare zone.
DEFAULT_USER_AGENT = "menhir-p2a-probe/0.1 (+snapshot transport measurement)"

# Outcome classes. The first two mean the ingress delivered the body.
DELIVERED_OK = "delivered_ok"
DELIVERED_REFUSED = "delivered_server_refused"
BLOCKED_HTTP = "blocked_http_status"
BLOCKED_TRANSPORT = "blocked_transport"
PROBE_ERROR = "probe_error"

DELIVERED = frozenset({DELIVERED_OK, DELIVERED_REFUSED})


@dataclass
class Trial:
    size_bytes: int
    wire_bytes: int
    outcome: str
    detail: str
    elapsed_s: float
    http_status: int | None = None
    server_code: str | None = None
    #: False when the server refused a chunk plan this large and the body was sent against the
    #: default plan instead. Such a trial measures the path only, never server acceptance.
    plan_negotiated: bool = True


@dataclass
class SizeResult:
    size_bytes: int
    wire_bytes: int
    trials: list[Trial] = field(default_factory=list)

    @property
    def delivered(self) -> int:
        return sum(1 for t in self.trials if t.outcome in DELIVERED)

    @property
    def reliable(self) -> bool:
        """Every trial delivered. Deliberately not a majority: a ceiling is the largest size that
        never failed, because a 'mostly works' chunk size produces intermittent upload failures in
        the field that are near-impossible to attribute."""
        return bool(self.trials) and self.delivered == len(self.trials)

    def median_elapsed(self) -> float:
        times = [t.elapsed_s for t in self.trials if t.outcome in DELIVERED]
        return statistics.median(times) if times else float("nan")

    def throughput_mib_s(self) -> float:
        median = self.median_elapsed()
        if math.isnan(median) or not median:
            return float("nan")
        return (self.wire_bytes / MIB) / median
