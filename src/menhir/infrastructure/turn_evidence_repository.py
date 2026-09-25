"""Selective `:TurnEvidence` capture — the evidence side of ADR 0001 (Claude MVP).

A `:TurnEvidence` node is a user prompt the producer's DETERMINISTIC triage judged *might* contain
durable memory evidence (a number, a possession, a preference, a decision, a correction...). The hook
observes every prompt but stores only candidates — boring prompts ("rewrite this", "continue") are
dropped and never reach Menhir. No LLM runs during capture. This is NOT transcript logging.

`TurnEvidence != Memory`: raw evidence is kept separate from `:Episodic` curated memory and never
enters normal recall. Phase 3 reads `role='user'` evidence to extract stated measures / base events;
the declarant is captured at write time, never inferred from prose. The write is idempotent on
`turn_key` so a double-fired hook does not duplicate evidence.

Facade module: the write, consolidation, purge, and stats halves live in
``turn_evidence_repository_{keys,writes,consolidation,purge,stats}.py`` (file-size refactor) and are
composed back into ``TurnEvidenceRepository`` here; public names are re-exported so existing import
sites keep working unchanged.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.turn_evidence_repository_consolidation import (
    TurnEvidenceConsolidationMixin,
)
from menhir.infrastructure.turn_evidence_repository_keys import (
    EVIDENCE_ROLES,
    derive_prompt_hash,
    derive_turn_key,
)
from menhir.infrastructure.turn_evidence_repository_purge import TurnEvidencePurgeMixin
from menhir.infrastructure.turn_evidence_repository_stats import TurnEvidenceStatsMixin
from menhir.infrastructure.turn_evidence_repository_writes import TurnEvidenceWritesMixin


class TurnEvidenceRepository(
    TurnEvidenceWritesMixin,
    TurnEvidenceConsolidationMixin,
    TurnEvidencePurgeMixin,
    TurnEvidenceStatsMixin,
):
    """Write and read selectively-captured `:TurnEvidence`, plus the Phase 3 debug-report stats."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    # ---- read (Phase 3 consumption) ----------------------------------------------------------

    def evidence_exists(self) -> bool:
        """True if any user-authored evidence exists — the switch Phase 3 uses to prefer evidence over
        the legacy `user:`-prefix Episodic path."""
        rows = self._neo4j.execute(
            "MATCH (t:TurnEvidence {role: 'user'}) RETURN count(t) AS c LIMIT 1"
        )
        return bool(rows and int(rows[0].get("c") or 0) > 0)

    def list_dirty_evidence_namespaces(self, *, limit: int = 200) -> list[str]:
        """Namespaces whose newest role=user evidence is newer than their consolidation watermark (or
        never consolidated). Keys on evidence metadata, not a text prefix; assistant/tool excluded."""
        rows = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence)
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.namespace IS NOT NULL
                  AND t.text IS NOT NULL AND t.text <> ''
            WITH t.namespace AS ns, max(t.recorded_at) AS newest
            WHERE newest IS NOT NULL
            OPTIONAL MATCH (w:ConsolidationWatermark {group_id: ns})
            WITH ns, newest, w.last_run_at AS watermark
            WHERE watermark IS NULL OR newest > watermark
            RETURN ns AS namespace
            ORDER BY newest DESC
            LIMIT $limit
            """,
            params={"limit": int(limit)},
        )
        return [str(r["namespace"]) for r in rows]

    def fetch_by_uuid(self, turn_id: str) -> dict[str, Any] | None:
        """Fetch one :TurnEvidence node by its turn_id, including both source and receive times."""
        rows = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence {turn_id: $turn_id})
            RETURN t.turn_id AS turn_id, t.role AS role, t.declarant AS declarant,
                   t.text AS text, t.session_id AS session_id, t.namespace AS namespace,
                   toString(t.occurred_at) AS occurred_at, toString(t.recorded_at) AS recorded_at
            LIMIT 1
            """,
            params={"turn_id": turn_id},
        )
        return dict(rows[0]) if rows else None

    def load_preceding_context(
        self,
        turn_id: str,
        *,
        namespace: str,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Return the bounded user/assistant turns immediately preceding one captured turn.

        This is extraction repair context, not a memory read. It excludes the current turn
        itself and orders the final result oldest first so the transcript remains readable.
        Tool/agent records are deliberately excluded: they are not dialogue and may contain
        large or instruction-like payloads.

        ``namespace`` is REQUIRED and is the CALLER's namespace (CF-236). The
        ``prior.namespace = current.namespace`` term below looks like scoping and is not: it
        scopes the prior turns to the ANCHOR's namespace, and the anchor is whatever `turn_id`
        the caller named. Without an independent check the caller could name a foreign turn and
        receive that namespace's raw prompt text, which then reaches the extraction prompt.
        """

        safe_limit = max(1, min(int(limit), 4))
        rows = self._neo4j.execute(
            """
            MATCH (current:TurnEvidence {turn_id: $turn_id})
            WHERE current.session_id IS NOT NULL
                  AND current.recorded_at IS NOT NULL
                  AND CASE WHEN trim(coalesce(current.namespace, '')) = '' THEN 'default'
                           ELSE trim(current.namespace) END = $namespace
            MATCH (prior:TurnEvidence)
            WHERE CASE WHEN trim(coalesce(prior.namespace, '')) = '' THEN 'default'
                       ELSE trim(prior.namespace) END =
                  CASE WHEN trim(coalesce(current.namespace, '')) = '' THEN 'default'
                       ELSE trim(current.namespace) END
                  AND prior.session_id = current.session_id
                  AND prior.turn_id <> current.turn_id
                  AND prior.recorded_at < current.recorded_at
                  AND prior.role IN ['user', 'assistant']
                  AND prior.text IS NOT NULL AND trim(prior.text) <> ''
            WITH prior
            ORDER BY prior.recorded_at DESC, prior.turn_key DESC
            LIMIT $limit
            RETURN prior.role AS role, prior.text AS text,
                   toString(prior.recorded_at) AS recorded_at
            """,
            params={"turn_id": turn_id, "limit": safe_limit, "namespace": namespace},
        )
        return [dict(row) for row in reversed(rows)]

    def load_user_evidence(self, namespace: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """User-authored evidence for a namespace, oldest first (timeline order for the fold). Rows are
        shaped like the Episodic loader (uuid, valid_at, content) so the consolidation path is
        unchanged. `content` is the RAW prompt text (no `user:` prefix)."""
        rows = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence {namespace: $ns})
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.text IS NOT NULL AND t.text <> ''
            RETURN t.turn_id AS uuid,
                   toString(coalesce(t.occurred_at, t.recorded_at, datetime())) AS valid_at,
                   t.text AS content
            ORDER BY coalesce(t.occurred_at, t.recorded_at), t.recorded_at, t.turn_id
            LIMIT $limit
            """,
            params={"ns": namespace, "limit": int(limit)},
        )
        return [dict(r) for r in rows]
