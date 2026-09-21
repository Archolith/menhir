"""Evidence capture for the Gate C / Gate F campaign.

Phase C requires: "Capture all MCP requests/responses plus server logs as test
artifacts", and Gate F requires rerunning the same lanes on the frozen RC and comparing.
That comparison is only possible if both runs wrote the same *shape* of evidence, so the
shape lives here rather than being improvised per lane.

One directory per lane per run::

    evidence/<run_id>/<lane>/
        manifest.json     stack identity: commit, wheel hash, versions, config, timings
        transcript.jsonl  every MCP request and response, in order
        backend.log       `menhir serve` stdout+stderr
        stdio.log         the stdio bridge's stderr (its stdout is the MCP channel)
        result.json       lane verdict and per-criterion outcomes

``result.json`` records each acceptance criterion by its plan identifier, because "the
lane passed" is not the evidence the gate asks for -- the gate asks which checklist
items were proven.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["LaneEvidence", "new_run_id", "repo_commit", "tree_is_clean"]


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def repo_commit(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def tree_is_clean(repo_root: Path) -> tuple[bool, list[str]]:
    """Return cleanliness and the dirty paths.

    Gate D requires "the tree is clean" on the candidate, and a wheel built from a dirty
    tree carries changes no commit records -- so the campaign's own evidence would name
    a commit that does not describe what was tested. Lanes may run dirty for
    development, but the manifest records it and the RC run must refuse it.
    """

    try:
        output = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return False, ["<git status unavailable>"]

    paths = [line[3:].strip() for line in output.splitlines() if line.strip()]
    return (not paths), paths


@dataclass
class LaneEvidence:
    """Accumulates one lane's artifacts and writes them on close."""

    lane: str
    directory: Path
    criteria: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    _started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest.setdefault(
            "environment",
            {
                "python": sys.version,
                "platform": platform.platform(),
                "started_utc": datetime.now(timezone.utc).isoformat(),
            },
        )

    @property
    def transcript_path(self) -> Path:
        return self.directory / "transcript.jsonl"

    @property
    def backend_log_path(self) -> Path:
        return self.directory / "backend.log"

    @property
    def stdio_log_path(self) -> Path:
        return self.directory / "stdio.log"

    def record_stack(self, **details: Any) -> None:
        self.manifest.setdefault("stack", {}).update(details)

    def record(self, criterion: str, *, passed: bool, detail: Any = None) -> None:
        """Record one plan checklist item by its identifier, e.g. ``"tools/list"``.

        A criterion recorded twice keeps the WORSE outcome. A lane that asserts
        something, then re-asserts it more loosely after a retry, must not be able to
        overwrite the failure it already saw.
        """

        existing = self.criteria.get(criterion)
        if existing is not None and not existing["passed"]:
            passed = False
        self.criteria[criterion] = {
            "passed": passed,
            "detail": detail,
            "at_utc": datetime.now(timezone.utc).isoformat(),
        }

    def attach(self, name: str, content: str | bytes) -> Path:
        """Write a supporting artifact (a generated manifest, an inspect snapshot...)."""

        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def close(self, *, status: str) -> None:
        """Write manifest.json and result.json. ``status`` is PASS / FAIL / SKIPPED."""

        self.manifest["duration_seconds"] = round(time.monotonic() - self._started, 3)
        self.manifest["status"] = status
        (self.directory / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2, default=str), encoding="utf-8"
        )

        unproven = sorted(k for k, v in self.criteria.items() if not v["passed"])
        result = {
            "lane": self.lane,
            "status": status,
            "criteria_total": len(self.criteria),
            "criteria_passed": sum(1 for v in self.criteria.values() if v["passed"]),
            "criteria_unproven": unproven,
            "criteria": self.criteria,
        }
        (self.directory / "result.json").write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
