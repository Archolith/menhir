"""Replay runner — drives pre-authored episode sequences through the service layer.

The runner dispatches each step to the corresponding service-layer method (not MCP
tools) and collects structured per-step outcomes.  Stubbed mode (default) uses the
in-memory stubs from conftest; live mode uses real Neo4j/Graphiti (opt-in).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from menhir.domain import IngestStatus
from menhir.domain.models import ProcessingState
from menhir.domain.recall import QueryPreset
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services import IngestService, LifecycleService, RecallService, ScoringService


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepResult:
    """Outcome of a single replay step."""

    index: int
    action: str
    status: str  # "pass", "fail", "skip", "error"
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class ReplayResult:
    """Aggregate outcome of a full scenario replay."""

    scenario: str
    steps: list[StepResult]

    @property
    def passed(self) -> bool:
        return all(s.status == "pass" for s in self.steps)

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {"pass": 0, "fail": 0, "skip": 0, "error": 0}
        for s in self.steps:
            counts[s.status] = counts.get(s.status, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Fixture loading and validation
# ---------------------------------------------------------------------------

_REQUIRED_TOP_KEYS = {"scenario", "steps"}
_VALID_ACTIONS = {"ingest", "consolidate_session", "decay", "recall", "recover_orphans", "wait"}


class FixtureError(Exception):
    """Raised when a fixture fails structural validation."""


def load_fixture(source: dict[str, Any] | str | Path) -> dict[str, Any]:
    """Load and validate a replay fixture from a dict or JSON path.

    Raises FixtureError on structural problems.
    """
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            raise FixtureError(f"Fixture file not found: {path}")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    elif isinstance(source, dict):
        data = source
    else:
        raise FixtureError(f"Expected dict or path, got {type(source).__name__}")

    missing = _REQUIRED_TOP_KEYS - set(data.keys())
    if missing:
        raise FixtureError(f"Missing required top-level keys: {sorted(missing)}")

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise FixtureError("'steps' must be a non-empty list")

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            raise FixtureError(f"Step {idx} must be a dict")
        action = step.get("action")
        if action not in _VALID_ACTIONS:
            raise FixtureError(
                f"Step {idx}: invalid action {action!r}; must be one of {sorted(_VALID_ACTIONS)}"
            )
        if action == "ingest" and not step.get("text"):
            raise FixtureError(f"Step {idx}: 'ingest' action requires 'text' field")
        if action == "recall" and not step.get("query"):
            raise FixtureError(f"Step {idx}: 'recall' action requires 'query' field")

    return data


# ---------------------------------------------------------------------------
# Replay runner
# ---------------------------------------------------------------------------


@dataclass
class ReplayRunner:
    """Drives a replay fixture through the service layer and collects results.

    In stubbed mode the caller provides pre-built services wired to test stubs.
    """

    ingest_service: IngestService
    lifecycle_service: LifecycleService
    recall_service: RecallService
    session_id: str = "replay-default"
    user_id: str = "replay-user"

    async def run(self, fixture: dict[str, Any] | str | Path) -> ReplayResult:
        """Execute all steps in a fixture and return structured results."""
        data = load_fixture(fixture)
        scenario = str(data.get("scenario", "unnamed"))
        session_id = str(data.get("session_id", self.session_id))
        user_id = str(data.get("user_id", self.user_id))

        results: list[StepResult] = []
        for idx, step in enumerate(data["steps"]):
            action = step["action"]
            try:
                detail = await self._dispatch(step, session_id, user_id)
                failures = self._check_expectations(step.get("expect", {}), detail)
                if failures:
                    results.append(StepResult(
                        index=idx,
                        action=action,
                        status="fail",
                        detail=detail,
                        error="; ".join(failures),
                    ))
                else:
                    results.append(StepResult(
                        index=idx, action=action, status="pass", detail=detail
                    ))
            except Exception as exc:
                results.append(StepResult(
                    index=idx,
                    action=action,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                ))

        return ReplayResult(scenario=scenario, steps=results)

    # -----------------------------------------------------------------------
    # Action dispatchers
    # -----------------------------------------------------------------------

    async def _dispatch(
        self, step: dict[str, Any], session_id: str, user_id: str
    ) -> dict[str, Any]:
        action = step["action"]
        if action == "ingest":
            return await self._do_ingest(step, session_id, user_id)
        if action == "consolidate_session":
            return await self._do_consolidate(step, session_id)
        if action == "decay":
            return await self._do_decay(step)
        if action == "recall":
            return await self._do_recall(step)
        if action == "recover_orphans":
            return await self._do_recover_orphans(step)
        if action == "wait":
            return self._do_wait(step)
        raise FixtureError(f"Unknown action: {action!r}")

    async def _do_ingest(
        self, step: dict[str, Any], session_id: str, user_id: str
    ) -> dict[str, Any]:
        # Ingest is now queue-backed: direct ingest_episode records + enqueues, then
        # waits up to 60s. The replay simulates *full* ingestion synchronously by
        # recording the episode and driving the enrichment worker to a terminal state,
        # then maps that state to an ingest status for the fixture contract.
        source = step.get("source", "replay-test")
        episode_uuid = str(uuid4())
        self.ingest_service.graph_adapter.create_pending_episode(
            episode_uuid=episode_uuid,
            name=f"episode-{session_id}-{episode_uuid}",
            content=step["text"],
            session_id=session_id,
            user_id=user_id,
            source=source,
            source_confidence=0.5,
        )
        await self.ingest_service._process_pending_episode(episode_uuid)
        episode_row = self.ingest_service.graph_adapter.fetch_episode_processing(episode_uuid)
        raw_state = episode_row.get("processing_state") if episode_row else None
        # processing_state may be a ProcessingState enum; use its value, not str(enum).
        state = str(getattr(raw_state, "value", raw_state) or "").upper()
        status = {"READY": "INGESTED", "FAILED": "FAILED"}.get(state, "QUEUED")
        nodes = int(episode_row.get("enriched_nodes_touched", 0)) if episode_row else 0
        edges = int(episode_row.get("enriched_edges_touched", 0)) if episode_row else 0
        return {
            "episode_id": episode_uuid,
            "status": status,
            "state": state,
            "nodes_touched": nodes,
            "edges_touched": edges,
        }

    async def _do_consolidate(
        self, step: dict[str, Any], session_id: str
    ) -> dict[str, Any]:
        result = await self.lifecycle_service.consolidate_session(session_id)
        return {
            "promoted": result.promoted,
            "deleted": result.deleted,
            "conflicts_detected": result.conflicts_detected,
            "skipped_pending": result.skipped_pending,
            "orphan_episodes_cleaned": result.orphan_episodes_cleaned,
        }

    async def _do_decay(self, step: dict[str, Any]) -> dict[str, Any]:
        result = await self.lifecycle_service.apply_decay()
        return {
            "edge_counts_synced": result.edge_counts_synced,
            "sharpness_recalculated": result.sharpness_recalculated,
            "compressed": result.compressed,
            "deleted": result.deleted,
            "edges_bridged": result.edges_bridged,
            "orphan_subgraphs_cleaned": result.orphan_subgraphs_cleaned,
        }

    async def _do_recall(self, step: dict[str, Any]) -> dict[str, Any]:
        preset_name = step.get("preset", "knowledge")
        try:
            preset = QueryPreset(preset_name)
        except ValueError:
            preset = QueryPreset.KNOWLEDGE

        result = await self.recall_service.recall(
            step["query"],
            preset=preset,
            limit=step.get("limit", 10),
        )
        top_contents = [
            str(r.content or r.name or "") for r in result.results[:5]
        ]
        return {
            "results_count": len(result.results),
            "top_contents": top_contents,
        }

    async def _do_recover_orphans(self, step: dict[str, Any]) -> dict[str, Any]:
        max_age = step.get("max_age_hours", 4.0)
        result = await self.lifecycle_service.recover_orphans(max_age)
        return {
            "promoted": result.promoted,
            "deleted": result.deleted,
        }

    def _do_wait(self, step: dict[str, Any]) -> dict[str, Any]:
        # In stubbed mode, wait is a no-op. Callers can modify stub timestamps
        # between steps if needed.
        return {"waited": True}

    # -----------------------------------------------------------------------
    # Expectation checking
    # -----------------------------------------------------------------------

    @staticmethod
    def _check_expectations(
        expect: dict[str, Any], detail: dict[str, Any]
    ) -> list[str]:
        """Compare step result detail against expectations. Returns failure messages."""
        failures: list[str] = []
        for key, expected in expect.items():
            if key.endswith("_min"):
                field = key[:-4]
                actual = detail.get(field, 0)
                if actual < expected:
                    failures.append(f"{field}={actual} < min {expected}")
            elif key.endswith("_max"):
                field = key[:-4]
                actual = detail.get(field, 0)
                if actual > expected:
                    failures.append(f"{field}={actual} > max {expected}")
            elif key == "top_result_contains":
                top_contents = detail.get("top_contents", [])
                combined = " ".join(top_contents).lower()
                for keyword in expected:
                    if keyword.lower() not in combined:
                        failures.append(f"keyword {keyword!r} not found in top results")
            else:
                actual = detail.get(key)
                if actual != expected:
                    failures.append(f"{key}: expected {expected!r}, got {actual!r}")
        return failures
