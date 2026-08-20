"""Selective `:TurnEvidence` capture — the evidence side of ADR 0001 (Claude MVP).

A `:TurnEvidence` node is a user prompt the producer's DETERMINISTIC triage judged *might* contain
durable memory evidence (a number, a possession, a preference, a decision, a correction...). The hook
observes every prompt but stores only candidates — boring prompts ("rewrite this", "continue") are
dropped and never reach Menhir. No LLM runs during capture. This is NOT transcript logging.

`TurnEvidence != Memory`: raw evidence is kept separate from `:Episodic` curated memory and never
enters normal recall. Phase 3 reads `role='user'` evidence to extract stated measures / base events;
the declarant is captured at write time, never inferred from prose. The write is idempotent on
`turn_key` so a double-fired hook does not duplicate evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

#: role values evidence may carry. Only 'user' is captured in the Claude MVP; the rest are reserved so
#: a later assistant/tool producer needs no schema change.
EVIDENCE_ROLES = ("user", "assistant", "tool", "agent")


def _normalize_occurred_at(value: str | None) -> str | None:
    """Validate an optional ISO-8601 source time and return a timezone-aware string."""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"occurred_at must be ISO-8601, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def derive_turn_key(
    *, source_kind: str, session_id: str | None, text: str, cwd: str | None = None,
    prompt_id: str | None = None, namespace: str | None = None,
) -> str:
    """Deterministic idempotency key: the same turn hashes identically, so a double-fired hook merges
    onto one node instead of duplicating evidence.

    IDENTITY (G18): when the producer supplies a stable per-prompt id (`prompt_id` -- e.g. Claude Code's
    `prompt_id` UUID, which is UNIQUE per genuine turn yet STABLE across a double-fired retry of the same
    submission), the key is derived from IT. This is what lets two GENUINE repetitions of the same text
    ("I have 20 coins" said twice) stay DISTINCT evidence sources -- keying on the text alone collapses
    them into one, which breaks the per-observation / reinforcement model. When no `prompt_id` is
    available (older Claude Code < 2.1.196, or a producer that supplies none), the key falls back to the
    prompt TEXT (prior behavior), accepting that identical text collapses to a single source.

    The key is per-namespace because `turn_key` carries a GLOBAL uniqueness constraint, so two
    namespaces presenting the same turn must produce different keys or they collapse onto one node."""
    h = hashlib.sha256()
    identity = (prompt_id or "").strip() or (text or "")
    for part in (source_kind or "", session_id or "", cwd or "", identity, namespace or ""):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _namespace_scoped_key(base_key: str, namespace: str | None) -> str:
    """Bind a caller-supplied `turn_key` to the caller's namespace.

    `turn_key` arrives from the request body, so an unscoped one lets a caller in one
    namespace address a node in another. Derived keys already fold the namespace in;
    this covers the supplied path with the same guarantee.
    """
    h = hashlib.sha256()
    h.update((namespace or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update((base_key or "").encode("utf-8", "replace"))
    return h.hexdigest()


def derive_prompt_hash(text: str) -> str:
    """Deterministic content fingerprint of the prompt (sha256 of the raw text).

    Provenance only: identifies WHICH prompt was captured without storing extra copies of it, and is
    stable across clients/sessions (unlike ``turn_key``, which folds in source/session/cwd for
    idempotency). Computed server-side so every stored node carries it regardless of the client."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


class TurnEvidenceRepository:
    """Write and read selectively-captured `:TurnEvidence`, plus the Phase 3 debug-report stats."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    # ---- write -------------------------------------------------------------------------------

    def record_turn_evidence(
        self,
        *,
        text: str,
        role: str = "user",
        declarant: str | None = None,
        session_id: str | None = None,
        occurred_at: str | None = None,
        namespace: str | None = None,
        source_kind: str = "unknown",
        source_id: str | None = None,
        source_client: str | None = None,
        hook_version: str | None = None,
        cwd: str | None = None,
        transcript_path: str | None = None,
        triage_reason: list[str] | None = None,
        triage_version: str | None = None,
        metadata: dict[str, Any] | None = None,
        turn_key: str | None = None,
        prompt_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one candidate evidence record (idempotent on turn_key). Returns
        {turn_id, created, recorded_at, occurred_at}. `role` and non-empty `text` are required.

        `source_client`/`hook_version` are additive PROVENANCE labels (which producer captured this,
        at what version) and are optional — older callers that omit them store nulls. `prompt_hash`
        is derived server-side from `text` so it is always present and client-independent. Free-form
        `metadata` (project_root/git_branch/git_commit/...) is stored verbatim as JSON.

        `recorded_at` is always the server receive time and remains the monotonic processing cursor.
        `occurred_at` is optional world time supplied by replay/import producers; live host hooks omit
        it and therefore retain receive-time semantics."""
        text = (text or "").strip()
        if not text:
            raise ValueError("record_turn_evidence requires non-empty text")
        if role not in EVIDENCE_ROLES:
            raise ValueError(f"role must be one of {EVIDENCE_ROLES}, got {role!r}")
        declarant = declarant or role
        normalized_occurred_at = _normalize_occurred_at(occurred_at)
        key = (
            _namespace_scoped_key(turn_key, namespace)
            if turn_key
            else derive_turn_key(
                source_kind=source_kind, session_id=session_id, text=text, cwd=cwd,
                prompt_id=prompt_id, namespace=namespace,
            )
        )
        rows = self._neo4j.execute(
            """
            MERGE (t:TurnEvidence {turn_key: $turn_key})
            ON CREATE SET
                t.turn_id = randomUuid(),
                t.recorded_at = datetime(),
                t.occurred_at = CASE
                    WHEN $occurred_at IS NULL THEN null
                    ELSE datetime($occurred_at)
                END,
                t.role = $role,
                t.declarant = $declarant,
                t.text = $text,
                t.session_id = $session_id,
                t.namespace = $namespace,
                t.source_kind = $source_kind,
                t.source_id = $source_id,
                t.source_client = $source_client,
                t.hook_version = $hook_version,
                t.cwd = $cwd,
                t.transcript_path = $transcript_path,
                t.triage_reason = $triage_reason,
                t.triage_version = $triage_version,
                t.prompt_length = $prompt_length,
                t.prompt_hash = $prompt_hash,
                t.prompt_id = $prompt_id,
                t.metadata = $metadata_json,
                t._created = true
            ON MATCH SET
                t._created = false,
                t.occurred_at = coalesce(
                    t.occurred_at,
                    CASE WHEN $occurred_at IS NULL THEN null ELSE datetime($occurred_at) END
                )
            RETURN t.turn_id AS turn_id, coalesce(t._created, false) AS created,
                   toString(t.recorded_at) AS recorded_at,
                   toString(t.occurred_at) AS occurred_at
            """,
            params={
                "turn_key": key,
                "role": role,
                "declarant": declarant,
                "text": text[:8000],
                "session_id": session_id,
                "occurred_at": normalized_occurred_at,
                "namespace": namespace,
                "source_kind": source_kind,
                "source_id": source_id,
                "source_client": source_client,
                "hook_version": hook_version,
                "cwd": cwd,
                "transcript_path": transcript_path,
                "triage_reason": list(triage_reason or []),
                "triage_version": triage_version,
                "prompt_length": len(text),
                "prompt_hash": derive_prompt_hash(text),
                "prompt_id": (prompt_id or None),
                "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
            },
        )
        row = rows[0] if rows else {}
        return {
            "turn_id": str(row.get("turn_id") or ""),
            "created": bool(row.get("created")),
            "recorded_at": str(row.get("recorded_at") or ""),
            "occurred_at": str(row.get("occurred_at")) if row.get("occurred_at") else None,
        }

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
            WHERE current.namespace IS NOT NULL AND current.session_id IS NOT NULL
                  AND current.recorded_at IS NOT NULL
                  AND coalesce(current.namespace, 'default') = $namespace
            MATCH (prior:TurnEvidence)
            WHERE prior.namespace = current.namespace
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

    # ---- scalar-consolidation discovery (G14 bridge) -----------------------------------------
    # The typed-scalar path (ScalarStateView C.4.3) discovered work via :Episodic 'user:' ONLY, so in
    # a Turn-capturing production box it saw NO user input and its declarant foundation was unreachable
    # (G14/F2). These mirror the counter-path switch: when :TurnEvidence exists, scalar discovery reads
    # it, using the SAME per-namespace :ScalarConsolidationWatermark cursor (keyed by the namespace
    # string in `group_id`) as the Episodic path -- so a box transitions with no cursor reset. The
    # work-discovery key is `recorded_at` (monotonic capture time, always present). Imported/replayed
    # evidence may also carry `occurred_at` world time; it drives assertion validity while recorded_at
    # continues to drive cursor advancement. The loaded row's `uuid` is the `turn_id`, which the G14
    # grounding Cypher resolves as a :TurnEvidence anchor and FOUNDS the assertion.

    def list_scalar_dirty_evidence_namespaces(
        self, *, perceiver_version: str, limit: int = 200,
    ) -> list[str]:
        """Namespaces with at least one role=user :TurnEvidence BEYOND the scalar cursor for
        `perceiver_version` (never scalar-consolidated, a different perceiver_version, or evidence past
        the stored `cursor_at`). The :TurnEvidence analogue of PersonalMemoryRepository.
        list_scalar_dirty_namespaces; independent of counter consolidation."""
        rows = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence)
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.namespace IS NOT NULL
                  AND t.text IS NOT NULL AND t.text <> ''
            OPTIONAL MATCH (w:ScalarConsolidationWatermark {group_id: t.namespace})
            WITH t.namespace AS ns, w, t.recorded_at AS ckey, t.turn_id AS tuuid
            WITH ns,
                 max(CASE
                     WHEN w IS NULL OR w.perceiver_version IS NULL
                          OR w.perceiver_version <> $pv OR w.cursor_at IS NULL
                          OR ckey > w.cursor_at
                          OR (ckey = w.cursor_at AND tuuid > w.cursor_uuid)
                     THEN 1 ELSE 0 END) AS unprocessed
            WHERE unprocessed = 1
            RETURN ns AS namespace
            ORDER BY ns
            LIMIT $limit
            """,
            params={"pv": str(perceiver_version), "limit": int(limit)},
        )
        return [str(r["namespace"]) for r in rows]

    def load_next_scalar_evidence_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """The next page of role=user :TurnEvidence for a namespace AFTER its scalar cursor (for
        `perceiver_version`), oldest first by `(recorded_at, turn_id)`. A cursor stamped by a different
        perceiver_version is IGNORED (reset). Rows are shaped like PersonalMemoryRepository.
        load_next_scalar_batch: `uuid`=turn_id (the G14 grounding anchor), `cursor_at`=recorded_at (the
        monotonic key the caller advances the cursor with), `valid_at`=occurred_at when supplied and
        recorded_at otherwise (the assertion's world time), `content`=raw prompt text. Bounded by
        `limit`.

        valid_at MUST be non-null: a null `valid_at` forces the assertion to
        `learned_fallback` (the perception batch's `learned_at`), which is captured AFTER LLM extraction
        and therefore AFTER the rebuild's `as_of` (captured before extraction) -- so the fold's
        `valid_at <= as_of` filter EXCLUDES the just-learned assertion as "future" and NO View
        materializes on the first (and, absent a re-dirtying event, only) consolidation pass. Both the
        source time and receive-time fallback precede consolidation, so either folds normally."""
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (w:ScalarConsolidationWatermark {group_id: $ns})
            WITH w, (w IS NULL OR w.perceiver_version IS NULL OR w.perceiver_version <> $pv) AS reset
            WITH CASE WHEN reset THEN null ELSE w.cursor_at END AS cca,
                 CASE WHEN reset THEN null ELSE w.cursor_uuid END AS cu
            MATCH (t:TurnEvidence {namespace: $ns})
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.text IS NOT NULL AND t.text <> ''
            WITH t, cca, cu, t.recorded_at AS ckey
            WHERE cca IS NULL OR ckey > cca OR (ckey = cca AND t.turn_id > cu)
            RETURN t.turn_id AS uuid,
                   toString(coalesce(t.occurred_at, t.recorded_at)) AS valid_at,
                   toString(ckey) AS cursor_at,
                   t.text AS content
            ORDER BY ckey, t.turn_id
            LIMIT $limit
            """,
            params={"ns": namespace, "pv": str(perceiver_version), "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    # ---- event-consolidation discovery (event-history cursor) ----------------------------------
    # The typed-event path needs an INDEPENDENT, truncation-safe cursor over canonical user
    # TurnEvidence. It is a SEPARATE label (:EventConsolidationWatermark) keyed by the namespace string
    # in `group_id` — deliberately NOT :ScalarConsolidationWatermark or :ConsolidationWatermark — so
    # event consolidation advances without disturbing the scalar/counter cursors. Same truncation-safe
    # ordering as the scalar path: (recorded_at, turn_id). Only role=user, declarant=user, nonempty
    # text participates. `valid_at` = occurred_at or recorded_at; the caller advances the cursor with
    # the monotonic `cursor_at` (recorded_at), never world-time.

    def list_event_dirty_evidence_namespaces(
        self, *, perceiver_version: str, limit: int = 200,
    ) -> list[str]:
        """Namespaces with at least one role=user :TurnEvidence BEYOND the event cursor for
        `perceiver_version` (never event-consolidated, a different perceiver_version, or evidence past
        the stored `cursor_at`). Independent of scalar and counter consolidation."""
        rows = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence)
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.namespace IS NOT NULL
                  AND t.text IS NOT NULL AND t.text <> ''
            OPTIONAL MATCH (w:EventConsolidationWatermark {group_id: t.namespace})
            WITH t.namespace AS ns, w, t.recorded_at AS ckey, t.turn_id AS tuuid
            WITH ns,
                 max(CASE
                     WHEN w IS NULL OR w.perceiver_version IS NULL
                          OR w.perceiver_version <> $pv OR w.cursor_at IS NULL
                          OR ckey > w.cursor_at
                          OR (ckey = w.cursor_at AND tuuid > w.cursor_uuid)
                     THEN 1 ELSE 0 END) AS unprocessed
            WHERE unprocessed = 1
            RETURN ns AS namespace
            ORDER BY ns
            LIMIT $limit
            """,
            params={"pv": str(perceiver_version), "limit": int(limit)},
        )
        return [str(r["namespace"]) for r in rows]

    def load_next_event_evidence_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """The next page of role=user :TurnEvidence for a namespace AFTER its event cursor (for
        `perceiver_version`), oldest first by (recorded_at, turn_id). A cursor stamped by a different
        perceiver_version is IGNORED (reset). Rows are shaped like the scalar loader: `uuid`=turn_id,
        `cursor_at`=recorded_at (the monotonic key the caller advances the cursor with),
        `valid_at`=occurred_at when supplied and recorded_at otherwise, `content`=raw prompt text,
        plus generic source fields (source_kind, session_id) genuinely useful to future Episode
        construction. Bounded by `limit` — a full page means more may remain."""
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (w:EventConsolidationWatermark {group_id: $ns})
            WITH w, (w IS NULL OR w.perceiver_version IS NULL OR w.perceiver_version <> $pv) AS reset
            WITH CASE WHEN reset THEN null ELSE w.cursor_at END AS cca,
                 CASE WHEN reset THEN null ELSE w.cursor_uuid END AS cu
            MATCH (t:TurnEvidence {namespace: $ns})
            WHERE t.role = 'user' AND t.declarant = 'user' AND t.text IS NOT NULL AND t.text <> ''
            WITH t, cca, cu, t.recorded_at AS ckey
            WHERE cca IS NULL OR ckey > cca OR (ckey = cca AND t.turn_id > cu)
            RETURN t.turn_id AS uuid,
                   toString(coalesce(t.occurred_at, t.recorded_at)) AS valid_at,
                   toString(ckey) AS cursor_at,
                   t.text AS content,
                   t.source_kind AS source_kind,
                   t.session_id AS session_id
            ORDER BY ckey, t.turn_id
            LIMIT $limit
            """,
            params={"ns": namespace, "pv": str(perceiver_version), "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    def advance_event_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str,
    ) -> None:
        """Advance the namespace's event cursor to the last TurnEvidence ACTUALLY processed, keyed on
        its monotonic `cursor_at` (recorded_at, NOT world-time), stamping the perceiver_version. The
        watermark is :EventConsolidationWatermark, keyed by the namespace string in `group_id` — fully
        independent of the scalar/counter cursors. Called after each processed batch so a partial
        backfill resumes exactly where it stopped."""
        self._neo4j.execute(
            """
            MERGE (w:EventConsolidationWatermark {group_id: $ns})
            SET w.cursor_at = datetime($cursor_at), w.cursor_uuid = $cu,
                w.perceiver_version = $pv, w.last_run_at = datetime($at)
            """,
            params={"ns": namespace, "cursor_at": cursor_at, "cu": cursor_uuid,
                    "pv": str(perceiver_version), "at": at},
        )

    def count_namespace(self, namespace: str) -> int:
        """Count `:TurnEvidence` nodes captured for a namespace (throwaway-eval observability)."""
        rows = self._neo4j.execute(
            "MATCH (t:TurnEvidence {namespace: $ns}) RETURN count(t) AS c",
            params={"ns": namespace},
        )
        return int(rows[0]["c"]) if rows else 0

    def purge_namespace(self, namespace: str) -> int:
        """Delete every `:TurnEvidence` node for a namespace; returns the count.

        `:TurnEvidence` is keyed by `t.namespace` (not `group_id`), so the graph-partition
        teardown (`delete_namespace`, which matches `group_id`) does NOT remove it. Throwaway
        Phase 3 eval resets must call this to leave zero residue and stay re-runnable.
        """
        rows = self._neo4j.execute(
            "MATCH (t:TurnEvidence {namespace: $ns}) DETACH DELETE t RETURN count(t) AS c",
            params={"ns": namespace},
        )
        return int(rows[0]["c"]) if rows else 0

    # ---- stats (debug report) ----------------------------------------------------------------

    def evidence_stats(self) -> dict[str, Any]:
        """Capture stats for the Phase 3 debug report, including triage breakdowns."""
        total_rows = self._neo4j.execute("MATCH (t:TurnEvidence) RETURN count(t) AS c")
        total = int(total_rows[0]["c"]) if total_rows else 0
        by_role: dict[str, int] = {}
        for r in self._neo4j.execute(
            "MATCH (t:TurnEvidence) RETURN t.role AS role, count(t) AS c ORDER BY c DESC"
        ):
            by_role[str(r["role"])] = int(r["c"])
        by_source: dict[str, int] = {}
        for r in self._neo4j.execute(
            "MATCH (t:TurnEvidence) RETURN t.source_kind AS sk, count(t) AS c ORDER BY c DESC LIMIT 10"
        ):
            by_source[str(r["sk"])] = int(r["c"])
        by_version: dict[str, int] = {}
        for r in self._neo4j.execute(
            "MATCH (t:TurnEvidence) RETURN t.triage_version AS v, count(t) AS c ORDER BY c DESC"
        ):
            by_version[str(r["v"])] = int(r["c"])
        by_reason: dict[str, int] = {}
        for r in self._neo4j.execute(
            "MATCH (t:TurnEvidence) UNWIND t.triage_reason AS reason "
            "RETURN reason AS reason, count(*) AS c ORDER BY c DESC"
        ):
            by_reason[str(r["reason"])] = int(r["c"])
        latest_rows = self._neo4j.execute(
            "MATCH (t:TurnEvidence) RETURN toString(max(t.recorded_at)) AS latest"
        )
        latest = latest_rows[0]["latest"] if latest_rows else None
        return {
            "turn_evidence_table_exists": total > 0,
            "total_turn_evidence": total,
            "by_role": by_role,
            "by_source_kind": by_source,
            "claude_code_hook_evidence": int(by_source.get("claude_code_hook", 0)),
            "triage_version_counts": by_version,
            "triage_reason_counts": by_reason,
            "user_evidence": int(by_role.get("user", 0)),
            "latest_recorded_at": latest,
        }
