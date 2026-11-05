"""Episode value type shared by the scalar, event and counting consolidation lanes.

This module used to hold the counter perception boundary (`perceive_and_fold`); that consolidation
counter lane was removed 2026-10-28 (#168, duplicate of the counting lane). The pre-removal code is
on branch `archive/consolidation-counter-2026-10-28`. Only `Episode` remains.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Episode:
    uuid: str
    content: str
