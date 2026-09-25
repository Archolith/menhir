"""Shared constants, types, and hashing helper for the LME scalar source-time repair.

Extracted verbatim from ``repair_lme_scalar_source_times.py``. The facade re-imports
every name, so the original module path stays importable unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"
REPAIR_SOURCE = "scalar-source-time-repair"


class RepairRefusal(RuntimeError):
    """The graph/fixture combination is not safe enough to mutate."""


class QueryExecutor(Protocol):
    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SourceTimeIndex:
    fixture_sha256: str
    session_times: dict[tuple[str, str], datetime]
    namespaces: frozenset[str]


@dataclass(frozen=True)
class RepairPlan:
    evidence_updates: tuple[dict[str, str | None], ...]
    assertion_updates: tuple[dict[str, str | None], ...]
    rebuild_targets: tuple[tuple[str, str], ...]
    evidence_total: int
    assertion_total: int
    evidence_already_correct: int
    assertions_already_correct: int

    def summary(self) -> dict[str, int]:
        return {
            "evidence_total": self.evidence_total,
            "evidence_updates": len(self.evidence_updates),
            "evidence_already_correct": self.evidence_already_correct,
            "assertion_total": self.assertion_total,
            "assertion_updates": len(self.assertion_updates),
            "assertions_already_correct": self.assertions_already_correct,
            "rebuild_targets": len(self.rebuild_targets),
        }


def _fixture_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
