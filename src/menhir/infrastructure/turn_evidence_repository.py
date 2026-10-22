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
from uuid import uuid4

from menhir.domain.namespace import (
    TURN_EVIDENCE_NULL_NAMESPACE_KEY,
    normalize_namespace,
    stamped_namespace,
    tenant_scope_cypher,
    tenant_scope_params,
    turn_evidence_cursor_keys,
)

# Discovery over the stored spellings of a silo (#170). A row's silo: null and '' are the default
# silo (namespace_spellings). A row's cursor key: its stored spelling (null -> $null_key), so each
# spelling keeps its own watermark. __IN_KEYS__ selects a silo's rows (null coalesces to '').
_TE_SILO = "CASE WHEN coalesce(t.namespace, '') IN ['', 'default'] THEN 'default' ELSE t.namespace END"
_TE_CURSOR_KEY = "coalesce(t.namespace, $null_key)"
_TE_IN_KEYS = tenant_scope_cypher("t")


def _silo_query(query: str) -> str:
    return (query.replace("__SILO__", _TE_SILO).replace("__CKEY__", _TE_CURSOR_KEY)
            .replace("__IN_KEYS__", _TE_IN_KEYS))


def _silo_params(namespace: str | None = None) -> dict[str, Any]:
    # Stamped first: tenant_scope_params(None) means "no filter", never "the default silo".
    return {"null_key": TURN_EVIDENCE_NULL_NAMESPACE_KEY,
            "keys": turn_evidence_cursor_keys(namespace),
            **tenant_scope_params(stamped_namespace(namespace))}


# Advance every cursor key of a silo to the processed position, never backward (#170). A batch is
# the (recorded_at, turn_id)-ordered prefix of the silo's unprocessed rows across all spellings, so
# every unprocessed row of any spelling at or before the position was in it. A key stamped with
# another perceiver_version was read as reset, so it moves to the position unconditionally.
def advance_keys_cypher(carry: str = "") -> str:
    """SET clause moving watermark `w` to ($cursor_at, $cu) under $pv; `carry` keeps extra vars."""
    keep = f"{carry}, " if carry else ""
    return f"""
    WITH {keep}w, datetime($cursor_at) AS pos
    WITH {keep}w, pos, (w.cursor_at IS NULL OR w.perceiver_version IS NULL
                  OR w.perceiver_version <> $pv OR pos > w.cursor_at
                  OR (pos = w.cursor_at AND $cu > coalesce(w.cursor_uuid, ''))) AS forward
    SET w.cursor_at = CASE WHEN forward THEN pos ELSE w.cursor_at END,
        w.cursor_uuid = CASE WHEN forward THEN $cu ELSE w.cursor_uuid END,
        w.perceiver_version = $pv, w.last_run_at = datetime($at)
"""

# Only rows the scalar lane has finished, judged against the scalar cursor of the row's own spelling.
_SCALAR_GATE = """
    MATCH (s:ScalarConsolidationWatermark {group_id: ck})
    WHERE s.perceiver_version = $spv AND s.cursor_at IS NOT NULL
          AND (t.recorded_at < s.cursor_at
               OR (t.recorded_at = s.cursor_at AND t.turn_id <= s.cursor_uuid))
    WITH __CARRY__
"""

# A silo is dirty when any of its user rows lies beyond the cursor of that row's own spelling.
_DIRTY_TEMPLATE = """
    MATCH (t:TurnEvidence)
    WHERE t.role = 'user' AND t.declarant = 'user'
          AND t.text IS NOT NULL AND t.text <> ''
    WITH t, __SILO__ AS ns, __CKEY__ AS ck
    __GATE__
    OPTIONAL MATCH (w:__WM__ {group_id: ck})
    WITH ns, w, t.recorded_at AS ckey, t.turn_id AS tuuid
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
"""

# The next page of a silo's user rows past each row's own spelling cursor, in (recorded_at, turn_id)
# order. A spelling cursor stamped by another perceiver_version is ignored (reset).
_BATCH_TEMPLATE = """
    MATCH (t:TurnEvidence)
    WHERE __IN_KEYS__
          AND t.role = 'user' AND t.declarant = 'user' AND t.text IS NOT NULL AND t.text <> ''
    WITH t, __CKEY__ AS ck
    __GATE__
    OPTIONAL MATCH (w:__WM__ {group_id: ck})
    WITH t, w, (w IS NULL OR w.perceiver_version IS NULL OR w.perceiver_version <> $pv) AS reset
    WITH t, t.recorded_at AS ckey,
         CASE WHEN reset THEN null ELSE w.cursor_at END AS cca,
         CASE WHEN reset THEN null ELSE w.cursor_uuid END AS cu
    WHERE cca IS NULL OR ckey > cca OR (ckey = cca AND t.turn_id > cu)
    RETURN t.turn_id AS uuid,
           toString(coalesce(t.occurred_at, t.recorded_at)) AS valid_at,
           toString(ckey) AS cursor_at,
           t.text AS content__EXTRA__
    ORDER BY ckey, t.turn_id
    LIMIT $limit
"""


def _cursor_dirty_query(watermark_label: str, *, scalar_gated: bool = False) -> str:
    gate = _SCALAR_GATE.replace("__CARRY__", "t, ns, ck") if scalar_gated else ""
    return _silo_query(_DIRTY_TEMPLATE.replace("__GATE__", gate)
                       .replace("__WM__", watermark_label))


def _cursor_batch_query(watermark_label: str, *, extra_return: str = "",
                        scalar_gated: bool = False) -> str:
    gate = _SCALAR_GATE.replace("__CARRY__", "t, ck") if scalar_gated else ""
    return _silo_query(_BATCH_TEMPLATE.replace("__GATE__", gate)
                       .replace("__WM__", watermark_label)
                       .replace("__EXTRA__", extra_return))


class CountingFenceLost(RuntimeError):
    """The run no longer owns the namespace's counting fence (wiped, reclaimed, or lease taken)."""


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
        namespace_key = normalize_namespace(namespace)
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
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET f.lock_nonce = $operation_id, f.locked_at = datetime()
            WITH f
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
                t.evidence_finalized = true,
                t.evidence_generation = f.generation,
                t._created = true
            ON MATCH SET
                t._created = false,
                t.occurred_at = coalesce(
                    t.occurred_at,
                    CASE WHEN $occurred_at IS NULL THEN null ELSE datetime($occurred_at) END
                ),
                t.evidence_finalized = true,
                t.evidence_generation = f.generation
            RETURN t.turn_id AS turn_id, coalesce(t._created, false) AS created,
                   toString(t.recorded_at) AS recorded_at,
                   toString(t.occurred_at) AS occurred_at
            """,
            params={
                "turn_key": key,
                "operation_id": f"turn-evidence:{key}",
                "namespace_key": namespace_key,
                "role": role,
                "declarant": declarant,
                "text": text[:8000],
                "session_id": session_id,
                "occurred_at": normalized_occurred_at,
                # Stamped so an unspecified namespace lands in 'default' (#170). turn_key stays
                # derived from the raw value so a retried pre-stamp turn still merges onto its node.
                "namespace": stamped_namespace(namespace),
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
        """True if any user-authored evidence exists. Only the scalar visibility gate reads it; no
        path falls back to Episodic content any more (#271)."""
        rows = self._neo4j.execute(
            "MATCH (t:TurnEvidence {role: 'user'}) RETURN count(t) AS c LIMIT 1"
        )
        return bool(rows and int(rows[0].get("c") or 0) > 0)

    def list_dirty_evidence_namespaces(self, *, limit: int = 200) -> list[str]:
        """Namespaces whose newest role=user evidence is newer than their consolidation watermark (or
        never consolidated). Keys on evidence metadata, not a text prefix; assistant/tool excluded.
        agent_quoted is included (#170): it is the only user input a hookless install produces."""
        rows = self._neo4j.execute(
            _silo_query("""
            MATCH (t:TurnEvidence)
            WHERE t.role = 'user' AND t.declarant = 'user'
                  AND t.text IS NOT NULL AND t.text <> ''
            WITH __SILO__ AS ns, max(t.recorded_at) AS newest
            WHERE newest IS NOT NULL
            OPTIONAL MATCH (w:ConsolidationWatermark {group_id: ns})
            WITH ns, newest, w.last_run_at AS watermark
            WHERE watermark IS NULL OR newest > watermark
            RETURN ns AS namespace
            ORDER BY newest DESC
            LIMIT $limit
            """),
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
                   t.source_kind AS source_kind,
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
        unchanged. `content` is the RAW prompt text (no `user:` prefix).

        Every row also carries `newest_recorded_at`: the newest recorded_at among ALL the silo's user
        rows at load time, past the `limit` window too. The counter lane stamps its watermark with it
        (DB clock, #170), so a turn recorded after this load stays dirty. Turns past the window are
        never folded -- the counter lane's known limit; the counting lane is the one that pages."""
        rows = self._neo4j.execute(
            _silo_query("""
            MATCH (t:TurnEvidence)
            WHERE __IN_KEYS__
                  AND t.role = 'user' AND t.declarant = 'user' AND t.text IS NOT NULL AND t.text <> ''
            WITH collect(t) AS ts, max(t.recorded_at) AS newest
            UNWIND ts AS t
            RETURN t.turn_id AS uuid,
                   toString(coalesce(t.occurred_at, t.recorded_at, datetime())) AS valid_at,
                   t.text AS content,
                   toString(newest) AS newest_recorded_at
            ORDER BY coalesce(t.occurred_at, t.recorded_at), t.recorded_at, t.turn_id
            LIMIT $limit
            """),
            params={"limit": int(limit), **_silo_params(namespace)},
        )
        return [dict(r) for r in rows]

    # ---- scalar-consolidation discovery (G14 bridge) -----------------------------------------
    # The typed-scalar path (ScalarStateView C.4.3) discovered work via :Episodic 'user:' ONLY, so in
    # a Turn-capturing production box it saw NO user input and its declarant foundation was unreachable
    # (G14/F2). Scalar discovery now reads :TurnEvidence only (#95; counter discovery likewise since
    # #271), using the SAME per-namespace :ScalarConsolidationWatermark cursor (keyed by the namespace
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
            _cursor_dirty_query("ScalarConsolidationWatermark"),
            params={"pv": str(perceiver_version), "limit": int(limit), **_silo_params()},
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
            _cursor_batch_query("ScalarConsolidationWatermark"),
            params={"pv": str(perceiver_version), "limit": int(limit),
                    **_silo_params(namespace)},
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
        the stored `cursor_at`). Independent of scalar and counter consolidation. agent_quoted is
        included (#170); event authority demotes a lead founded on it to advisory."""
        rows = self._neo4j.execute(
            _cursor_dirty_query("EventConsolidationWatermark"),
            params={"pv": str(perceiver_version), "limit": int(limit), **_silo_params()},
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
            _cursor_batch_query(
                "EventConsolidationWatermark",
                extra_return=",\n           t.source_kind AS source_kind,"
                             "\n           t.session_id AS session_id"),
            params={"pv": str(perceiver_version), "limit": int(limit),
                    **_silo_params(namespace)},
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
        backfill resumes exactly where it stopped. Every spelling key of the silo moves (#170)."""
        self._neo4j.execute(
            """
            UNWIND $keys AS k
            MERGE (w:EventConsolidationWatermark {group_id: k})
            """ + advance_keys_cypher(),
            params={"cursor_at": cursor_at, "cu": cursor_uuid,
                    "pv": str(perceiver_version), "at": at, **_silo_params(namespace)},
        )

    # ---- counting-consolidation discovery (counting lane, builds on scalars) -------------------
    # The counting lane has its OWN cursor (:CountingConsolidationWatermark, keyed by the namespace
    # string in `group_id`) but may only read turns the scalar lane has FINISHED: a turn is eligible
    # only when it is at or before the namespace's :ScalarConsolidationWatermark cursor stamped with
    # the scalar perceiver_version in effect. No scalar watermark, or one stamped by another version
    # (a scalar reset in progress), makes every turn ineligible, so counting pauses until scalars
    # catch up. Same turn filter as the scalar loader (agent_quoted included).

    def list_counting_dirty_evidence_namespaces(
        self, *, perceiver_version: str, scalar_perceiver_version: str, limit: int = 200,
    ) -> list[str]:
        """Namespaces with at least one role=user :TurnEvidence that the scalar lane has finished
        (at or before its cursor for `scalar_perceiver_version`) and the counting lane has not
        (beyond the counting cursor for `perceiver_version`, or no/other-version counting cursor)."""
        rows = self._neo4j.execute(
            _cursor_dirty_query("CountingConsolidationWatermark", scalar_gated=True),
            params={"pv": str(perceiver_version), "spv": str(scalar_perceiver_version),
                    "limit": int(limit), **_silo_params()},
        )
        return [str(r["namespace"]) for r in rows]

    def load_next_counting_evidence_batch(
        self, namespace: str, *, perceiver_version: str, scalar_perceiver_version: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """The next page of role=user :TurnEvidence AFTER the counting cursor and AT OR BEFORE the
        scalar cursor for `scalar_perceiver_version`, oldest first by (recorded_at, turn_id). Row shape
        matches load_next_scalar_evidence_batch. Returns nothing when the scalar watermark is missing
        or stamped by another version (pause, never read ahead of scalars). A counting cursor stamped
        by another counting version is ignored (reset)."""
        rows = self._neo4j.execute(
            _cursor_batch_query("CountingConsolidationWatermark", scalar_gated=True),
            params={"pv": str(perceiver_version), "spv": str(scalar_perceiver_version),
                    "limit": int(limit), **_silo_params(namespace)},
        )
        return [dict(r) for r in rows]

    def claim_counting_namespace(
        self, namespace: str, *, run_id: str, lease_seconds: int, now: str,
    ) -> dict[str, Any] | None:
        """Claim the namespace's counting fence (owner + lease on the watermark node). Returns
        {"incarnation", "epoch", "rebuild_subjects"} or None when another live owner holds it.
        `rebuild_subjects` are subjects a re-version retire left for rebuild (an earlier owner
        stopped before rebuilding them). Never sets the cursor."""
        rows = self._neo4j.execute(
            """
            MERGE (w:CountingConsolidationWatermark {group_id: $ns})
            ON CREATE SET w.incarnation = randomUUID(), w.epoch = 0, w.created_at = datetime($now)
            SET w.last_claim_attempt_at = datetime($now)
            WITH w
            WHERE w.owner IS NULL OR w.lease_expires_at IS NULL
                  OR w.lease_expires_at < datetime($now) OR w.owner = $run_id
            SET w.incarnation = coalesce(w.incarnation, randomUUID()), w.owner = $run_id,
                w.epoch = coalesce(w.epoch, 0) + 1,
                w.lease_expires_at = datetime($now) + duration({seconds: $lease})
            RETURN w.incarnation AS incarnation, w.epoch AS epoch,
                   coalesce(w.rebuild_subjects, []) AS rebuild_subjects
            """,
            params={"ns": namespace, "run_id": str(run_id), "lease": int(lease_seconds),
                    "now": now},
        )
        if not rows:
            return None
        return {"incarnation": str(rows[0]["incarnation"]), "epoch": int(rows[0]["epoch"]),
                "rebuild_subjects": [str(s) for s in (rows[0].get("rebuild_subjects") or []) if s]}

    def renew_counting_claim(
        self, namespace: str, *, run_id: str, incarnation: str, epoch: int,
        lease_seconds: int, now: str,
    ) -> bool:
        """Extend the lease; True iff this run still owns the exact (incarnation, epoch) fence."""
        rows = self._neo4j.execute(
            """
            MATCH (w:CountingConsolidationWatermark {group_id: $ns})
            WHERE w.owner = $run_id AND w.incarnation = $inc AND w.epoch = $epoch
            SET w.lease_expires_at = datetime($now) + duration({seconds: $lease})
            RETURN count(w) AS c
            """,
            params={"ns": namespace, "run_id": str(run_id), "inc": str(incarnation),
                    "epoch": int(epoch), "lease": int(lease_seconds), "now": now},
        )
        return bool(rows) and int(rows[0]["c"]) > 0

    def release_counting_claim(
        self, namespace: str, *, run_id: str, incarnation: str,
        rebuilt_subjects: list[str] | None = None,
    ) -> None:
        """Drop ownership if this run still holds it; a no-op when the fence is gone or taken.
        `rebuilt_subjects` (rebuilt after this owner's last retire of them) leave the watermark's
        rebuild list; only the owner appends to it, so nothing was added behind this owner."""
        self._neo4j.execute(
            """
            MATCH (w:CountingConsolidationWatermark {group_id: $ns})
            WHERE w.owner = $run_id AND w.incarnation = $inc
            SET w.owner = null, w.lease_expires_at = null,
                w.rebuild_subjects = [s IN coalesce(w.rebuild_subjects, []) WHERE NOT s IN $done]
            """,
            params={"ns": namespace, "run_id": str(run_id), "inc": str(incarnation),
                    "done": [str(s) for s in (rebuilt_subjects or []) if s]},
        )

    def advance_counting_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str, run_id: str, incarnation: str, epoch: int,
    ) -> None:
        """Advance the namespace's counting cursor to the last TurnEvidence actually processed
        (monotonic recorded_at, never world time), stamping the counting perceiver_version.
        Fenced: raises CountingFenceLost unless this run still owns the watermark node. The fence is
        the `namespace` node; every spelling key of the silo moves, in the same statement (#170)."""
        rows = self._neo4j.execute(
            """
            MATCH (f:CountingConsolidationWatermark {group_id: $ns})
            WHERE f.owner = $run_id AND f.incarnation = $inc AND f.epoch = $epoch
            UNWIND $keys AS k
            MERGE (w:CountingConsolidationWatermark {group_id: k})
            """ + advance_keys_cypher("f") + """
            RETURN count(DISTINCT f) AS c
            """,
            params={"ns": namespace, "cursor_at": cursor_at, "cu": cursor_uuid,
                    "pv": str(perceiver_version), "at": at, "run_id": str(run_id),
                    "inc": str(incarnation), "epoch": int(epoch),
                    **_silo_params(namespace)},
        )
        if not rows or int(rows[0]["c"]) < 1:
            raise CountingFenceLost(
                f"counting fence lost for namespace {namespace!r} during advance_counting_cursor")

    def prior_version_occurrences(
        self, namespace: str, turn_ids: list[str], *, perceiver_version: str,
    ) -> list[dict[str, Any]]:
        """Current occurrence claims on these turns stamped by another counting version (the rows
        a re-read may replace), with their spans. Read-only."""
        ids = [str(i) for i in (turn_ids or []) if i]
        if not ids:
            return []
        rows = self._neo4j.execute(
            """
            MATCH (h:TypedAssertionHead)-[:CURRENT]->(a:TypedAssertion)
            WHERE """ + tenant_scope_cypher("a") + """
                  AND a.operation = 'occurrence' AND coalesce(a.claim_ordinal, 0) = 1
                  AND a.episode_uuid IN $turn_ids
                  AND coalesce(a.perceiver_version, '') <> $pv
            RETURN a.assertion_id AS assertion_id, h.source_key AS source_key,
                   a.episode_uuid AS episode_uuid, a.span_start AS span_start,
                   a.span_end AS span_end, a.subject_uuid AS subject_uuid
            ORDER BY a.episode_uuid, a.span_start, a.assertion_id
            """,
            params={"turn_ids": ids, "pv": str(perceiver_version),
                    **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def current_version_occurrence_spans(
        self, namespace: str, turn_ids: list[str], *, perceiver_version: str,
        include_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Spans of the bound occurrences of this counting version stored on these turns: the
        current ones (by any run, including a partial earlier one) and `include_ids` (this run's
        rows waiting for their head). What a re-read actually stored, for rule B. Read-only."""
        ids = [str(i) for i in (turn_ids or []) if i]
        if not ids:
            return []
        rows = self._neo4j.execute(
            """
            MATCH (a:TypedAssertion)
            WHERE """ + tenant_scope_cypher("a") + """
                  AND a.operation = 'occurrence' AND coalesce(a.claim_ordinal, 0) = 1
                  AND a.episode_uuid IN $turn_ids AND a.perceiver_version = $pv
                  AND NOT coalesce(a.binding_pending, false)
                  AND (EXISTS { MATCH (:TypedAssertionHead)-[:CURRENT]->(a) }
                       OR a.assertion_id IN $include)
            RETURN a.assertion_id AS assertion_id, a.episode_uuid AS episode_uuid,
                   a.span_start AS span_start, a.span_end AS span_end
            ORDER BY a.episode_uuid, a.span_start, a.assertion_id
            """,
            params={"turn_ids": ids, "pv": str(perceiver_version),
                    "include": [str(i) for i in (include_ids or []) if i],
                    **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def retire_prior_version_occurrences(
        self, namespace: str, turn_ids: list[str], *, perceiver_version: str,
        run_id: str, incarnation: str, epoch: int,
        keep_ids: list[str] | None = None, promote_ids: list[str] | None = None,
    ) -> list[str]:
        """Retire current occurrence claims on these turns stamped by another counting version,
        in one fenced query. `keep_ids` are left current. A retired row's head passes CURRENT to
        its new-version row among `promote_ids` in the same statement: the record stores a rank-0
        re-version non-current next to the old row, so the head is never left without its count.
        The retired subjects are added to the watermark's rebuild list in the same statement (the
        owner clears them on release once rebuilt). Returns those subject_uuids."""
        ids = [str(i) for i in (turn_ids or []) if i]
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (w:CountingConsolidationWatermark {group_id: $ns})
            WHERE w.owner = $run_id AND w.incarnation = $inc AND w.epoch = $epoch
            SET w.last_retire_at = datetime()
            WITH w, w IS NOT NULL AS fence_ok
            OPTIONAL MATCH (h:TypedAssertionHead)-[cr:CURRENT]->(a:TypedAssertion)
            WHERE fence_ok AND """ + tenant_scope_cypher("a") + """
                  AND a.operation = 'occurrence' AND coalesce(a.claim_ordinal, 0) = 1
                  AND a.episode_uuid IN $turn_ids
                  AND coalesce(a.perceiver_version, '') <> $pv
                  AND NOT a.assertion_id IN $keep
            SET a.superseded = true, a.expired_at = datetime(),
                a.retired_reason = 'counting_reversion'
            DELETE cr
            WITH w, fence_ok, h, a
            OPTIONAL MATCH (h)-[:HAS_VERSION]->(n:TypedAssertion)
            WHERE a IS NOT NULL AND n.assertion_id IN $promote AND n.perceiver_version = $pv
                  AND NOT coalesce(n.binding_pending, false)
            WITH w, fence_ok, h, a, n ORDER BY n.recorded_at ASC, n.assertion_id ASC
            WITH w, fence_ok, h, a, collect(n)[0] AS n
            FOREACH (_ IN CASE WHEN n IS NULL THEN [] ELSE [1] END |
                SET n.superseded = false, a.superseded_by = n.assertion_id
                MERGE (h)-[:CURRENT]->(n)
                MERGE (n)-[:SUPERSEDES]->(a))
            WITH w, fence_ok, collect(DISTINCT a.subject_uuid) AS subjects
            FOREACH (_ IN CASE WHEN fence_ok AND size(subjects) > 0 THEN [1] ELSE [] END |
                SET w.rebuild_subjects = reduce(
                    acc = [], s IN coalesce(w.rebuild_subjects, []) + subjects |
                    CASE WHEN s IN acc THEN acc ELSE acc + s END))
            RETURN fence_ok, subjects
            """,
            params={"ns": namespace, "run_id": str(run_id), "inc": str(incarnation),
                    "epoch": int(epoch), "turn_ids": ids, "pv": str(perceiver_version),
                    "keep": [str(k) for k in (keep_ids or []) if k],
                    "promote": [str(p) for p in (promote_ids or []) if p],
                    **tenant_scope_params(namespace)},
        )
        if not rows or not rows[0]["fence_ok"]:
            raise CountingFenceLost(
                f"counting fence lost for namespace {namespace!r} during "
                "retire_prior_version_occurrences")
        return [str(s) for s in (rows[0]["subjects"] or []) if s]

    def mark_counting_flag_state(
        self, namespace: str, *, enabled: bool, at: str, create_only: bool = False,
        match: bool = False, view_selection: bool = False,
    ) -> None:
        """Record the counting flag value (and the 4R match fold mode and the counted-View
        selection mode) last applied to this namespace, for the flag sweep. create_only: set it
        only if no marker exists; only a completed sweep may flip one. A marker with no `match`
        or `view_selection` reads as off."""
        if create_only:
            query = """
            MERGE (m:CountingFlagState {namespace_key: $ns})
            ON CREATE SET m.group_id = $ns, m.enabled = $enabled, m.match = $match,
                          m.view_selection = $view_selection, m.updated_at = datetime($at)
            """
        else:
            query = """
            MERGE (m:CountingFlagState {namespace_key: $ns})
            SET m.group_id = $ns, m.enabled = $enabled, m.match = $match,
                m.view_selection = $view_selection, m.updated_at = datetime($at)
            """
        self._neo4j.execute(
            query,
            params={"ns": namespace, "enabled": bool(enabled), "match": bool(match),
                    "view_selection": bool(view_selection), "at": at},
        )

    def counting_flag_sweep_targets(
        self, *, enabled: bool, namespaces: list[str] | None, limit: int, match: bool = False,
        view_selection: bool = False,
    ) -> list[str]:
        """Namespaces whose recorded flag state differs from `enabled` or whose match fold mode
        or counted-View selection mode differs from `match` / `view_selection` (need a rebuild
        sweep).
        First records a create-only enabled=true marker for any namespace that holds current
        occurrence assertions but no marker (counted before markers existed), so the off-sweep
        can find it. Uses the TypedAssertion(operation) index."""
        params = {"namespaces": None if namespaces is None else [str(n) for n in namespaces]}
        self._neo4j.execute(
            """
            MATCH (h:TypedAssertionHead)-[:CURRENT]->(a:TypedAssertion {operation: 'occurrence'})
            WHERE coalesce(a.claim_ordinal, 0) = 1
            WITH DISTINCT a.namespace AS ns
            WHERE ns IS NOT NULL AND ns <> ''
              AND ($namespaces IS NULL OR ns IN $namespaces)
              AND NOT EXISTS { MATCH (:CountingFlagState {namespace_key: ns}) }
            MERGE (m:CountingFlagState {namespace_key: ns})
            ON CREATE SET m.group_id = ns, m.enabled = true, m.updated_at = datetime()
            """,
            params=params,
        )
        rows = self._neo4j.execute(
            """
            MATCH (m:CountingFlagState)
            WHERE (m.enabled <> $enabled OR coalesce(m.match, false) <> $match
                   OR coalesce(m.view_selection, false) <> $view_selection)
                  AND ($namespaces IS NULL OR m.namespace_key IN $namespaces)
            RETURN m.namespace_key AS namespace
            ORDER BY m.namespace_key
            LIMIT $limit
            """,
            params={"enabled": bool(enabled), "match": bool(match),
                    "view_selection": bool(view_selection),
                    "namespaces": None if namespaces is None else [str(n) for n in namespaces],
                    "limit": int(limit)},
        )
        return [str(r["namespace"]) for r in rows]

    def counting_sweep_subjects(
        self, namespace: str, *, limit: int, after: str = "",
    ) -> list[str]:
        """Subjects with current counted occurrences or a current counted scalar_state View,
        ordered, strictly after `after` (keyset paging)."""
        rows = self._neo4j.execute(
            """
            CALL {
                MATCH (h:TypedAssertionHead)-[:CURRENT]->(a:TypedAssertion)
                WHERE """ + tenant_scope_cypher("a") + """
                      AND a.operation = 'occurrence' AND coalesce(a.claim_ordinal, 0) = 1
                      AND a.subject_uuid IS NOT NULL
                RETURN DISTINCT a.subject_uuid AS subject_uuid
                UNION
                MATCH (n:Entity {view_kind: 'scalar_state', group_id: $ns})
                WHERE coalesce(n.view_current, true) AND n.scalar_basis = 'counted'
                      AND n.view_subject_uuid IS NOT NULL
                RETURN DISTINCT n.view_subject_uuid AS subject_uuid
            }
            WITH DISTINCT subject_uuid
            WHERE subject_uuid > $after
            RETURN subject_uuid
            ORDER BY subject_uuid
            LIMIT $limit
            """,
            params={"ns": namespace, "limit": int(limit), "after": str(after or ""),
                    **tenant_scope_params(namespace)},
        )
        return [str(r["subject_uuid"]) for r in rows]

    def scalar_claims_for_turns(
        self, namespace: str, turn_ids: list[str], *, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Current scalar-lane claims (claim_ordinal 0, operation absolute|delta|expire) already
        stored for these turns, so the counting lane can be told what scalars captured. Bounded."""
        ids = [str(i) for i in (turn_ids or []) if i]
        if not ids:
            return []
        rows = self._neo4j.execute(
            """
            MATCH (h:TypedAssertionHead)-[:CURRENT]->(a:TypedAssertion)
            WHERE a.episode_uuid IN $ids AND """ + tenant_scope_cypher("a") + """
                  AND coalesce(a.claim_ordinal, 0) = 0
                  AND a.operation IN ['absolute', 'delta', 'expire']
            RETURN a.episode_uuid AS episode_uuid, a.subject_display AS subject,
                   a.attribute AS attribute, a.scope AS scope, a.unit AS unit,
                   a.operation AS operation, a.value_json AS value_json,
                   a.stated_span AS stated_span
            ORDER BY a.episode_uuid, a.span_start, a.attribute
            LIMIT $limit
            """,
            params={"ids": ids, "limit": int(limit), **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def count_namespace(self, namespace: str) -> int:
        """Count `:TurnEvidence` nodes captured for a namespace (throwaway-eval observability)."""
        rows = self._neo4j.execute(
            "MATCH (t:TurnEvidence {namespace: $ns}) RETURN count(t) AS c",
            params={"ns": namespace},
        )
        return int(rows[0]["c"]) if rows else 0

    def purge_namespace(self, namespace: str) -> int:
        """Delete a namespace's evidence and atomically invalidate dependent Views.

        `:TurnEvidence` is keyed by `t.namespace` (not `group_id`), so the graph-partition
        teardown (`delete_namespace`, which matches `group_id`) does NOT remove it. Throwaway
        Phase 3 eval resets must call this to leave zero residue and stay re-runnable. Contributor
        UUIDs on retained historical Views are scrubbed, current dependent Views are retired, and
        fold cursors are reset in the same transaction so no caller can leave a recallable ghost.
        """
        namespace_key = normalize_namespace(namespace)
        operation_id = uuid4().hex
        purge_query = """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET f.lock_nonce = $operation_id,
                f.locked_at = datetime(),
                f.generation = coalesce(f.generation, 0) + 1,
                f.last_reset_operation_id = $operation_id,
                f.last_reset_at = datetime()
            WITH f
            OPTIONAL MATCH (t:TurnEvidence)
            WHERE __TURN_SCOPE__
            WITH f, collect(DISTINCT t) AS doomed
            WITH f, doomed, [t IN doomed WHERE t.turn_id IS NOT NULL | t.turn_id] AS doomed_ids
            OPTIONAL MATCH (a:TypedAssertion)
            WHERE __ASSERTION_SCOPE__
               OR a.episode_uuid IN doomed_ids
               OR EXISTS { MATCH (source:TurnEvidence)-[:FOUNDS]->(a) WHERE source IN doomed }
            WITH f, doomed, doomed_ids, collect(DISTINCT a) AS scalar_assertions
            WITH f, doomed, doomed_ids, scalar_assertions,
                 [a IN scalar_assertions | a.assertion_id] AS scalar_assertion_ids
            OPTIONAL MATCH (event:TypedEventAssertion)
            WHERE __EVENT_SCOPE__
               OR event.episode_uuid IN doomed_ids
               OR event.turn_evidence_uuid IN doomed_ids
               OR EXISTS { MATCH (source:TurnEvidence)-[:FOUNDS]->(event) WHERE source IN doomed }
            WITH f, doomed, doomed_ids, scalar_assertions, scalar_assertion_ids,
                 collect(DISTINCT event) AS event_assertions
            OPTIONAL MATCH (scalar_head:TypedAssertionHead)-[:HAS_VERSION]->(head_assertion:TypedAssertion)
            WHERE head_assertion IN scalar_assertions
              AND NOT EXISTS {
                  MATCH (scalar_head)-[:HAS_VERSION]->(outside:TypedAssertion)
                  WHERE NOT outside IN scalar_assertions
              }
            WITH f, doomed, doomed_ids, scalar_assertions, scalar_assertion_ids,
                 event_assertions, collect(DISTINCT scalar_head) AS scalar_heads
            OPTIONAL MATCH (event_head:TypedEventAssertionHead)
                  -[:HAS_VERSION]->(head_event:TypedEventAssertion)
            WHERE head_event IN event_assertions
              AND NOT EXISTS {
                  MATCH (event_head)-[:HAS_VERSION]->(outside:TypedEventAssertion)
                  WHERE NOT outside IN event_assertions
              }
            WITH f, doomed, doomed_ids, scalar_assertions, scalar_assertion_ids,
                 event_assertions, scalar_heads,
                 collect(DISTINCT event_head) AS event_heads
            OPTIONAL MATCH (stale_repair)
            WHERE ((stale_repair:ScalarProjectionRepair
                    OR stale_repair:ViewProjectionRepair)
                   AND __REPAIR_SCOPE__)
               OR (stale_repair:AssertionRebind
                   AND stale_repair.assertion_id IN scalar_assertion_ids)
            WITH f, doomed, doomed_ids, scalar_assertions, event_assertions,
                 scalar_heads, event_heads, collect(DISTINCT stale_repair) AS stale_repairs
            OPTIONAL MATCH (v:Entity)
            WHERE (coalesce(v.is_view, false)
                   OR coalesce(v.is_quantstate, false)
                   OR v.view_kind IS NOT NULL)
              AND (
                  any(eid IN coalesce(v.episode_uuids, []) WHERE eid IN doomed_ids)
                  OR v.turn_evidence_uuid IN doomed_ids
                  OR EXISTS {
                      MATCH (source:TurnEvidence)-[:MENTIONS]->(v)
                      WHERE source IN doomed
                  }
              )
            WITH f, doomed, doomed_ids, scalar_assertions, event_assertions,
                 scalar_heads, event_heads, stale_repairs,
                 collect(DISTINCT v) AS dependent_views
            WITH f, doomed, doomed_ids, scalar_assertions, event_assertions,
                 scalar_heads, event_heads, stale_repairs, dependent_views,
                 [v IN dependent_views
                  WHERE coalesce(v.view_current, v.qs_current, true)
                    AND NOT coalesce(v.retired, false)] AS current_views,
                 [f.namespace_key] + [v IN dependent_views |
                     coalesce(v.namespace, v.group_id, 'default')] AS namespaces
            FOREACH (v IN dependent_views |
                SET v.episode_uuids = [eid IN coalesce(v.episode_uuids, [])
                                       WHERE NOT eid IN doomed_ids],
                    v.supporting_event_count = size([eid IN coalesce(v.episode_uuids, [])
                                                     WHERE NOT eid IN doomed_ids])
            )
            FOREACH (v IN [candidate IN dependent_views
                           WHERE candidate.turn_evidence_uuid IN doomed_ids] |
                REMOVE v.turn_evidence_uuid
            )
            FOREACH (v IN current_views |
                SET v.view_current = false,
                    v.qs_current = false,
                    v.retired = true,
                    v.retired_reason = 'contributing_evidence_erased',
                    v.expired_at = datetime(),
                    v.last_accessed = datetime()
                REMOVE v.ss_view_key_current
            )
            FOREACH (repair IN stale_repairs | DETACH DELETE repair)
            WITH f, doomed, doomed_ids, scalar_assertions, event_assertions, scalar_heads,
                 event_heads, stale_repairs, dependent_views, current_views, namespaces
            // #170 D1 read markers of the erased turns (reversion_repository).
            OPTIONAL MATCH (rm:TypedReadMarker)
            WHERE rm.marker_key IN [i IN doomed_ids | 'scalar:' + i]
                                   + [i IN doomed_ids | 'event:' + i]
            WITH f, doomed, scalar_assertions, event_assertions, scalar_heads, event_heads,
                 stale_repairs, dependent_views, current_views, namespaces,
                 collect(rm) AS read_markers
            FOREACH (m IN read_markers | DETACH DELETE m)
            WITH f, doomed, scalar_assertions, event_assertions, scalar_heads, event_heads,
                 stale_repairs, dependent_views, current_views, namespaces,
                 [v IN current_views | {
                     view: v,
                     source_family: CASE
                         WHEN v.view_kind IN ['scalar_state', 'scalar_history']
                         THEN 'typed_scalar_assertions'
                         WHEN v.view_kind = 'timeline' AND EXISTS {
                             MATCH (v)-[:EVENT_HISTORY_ENTRY]->(:TypedEventAssertion)
                         }
                         THEN 'typed_event_assertions'
                         ELSE 'none'
                     END,
                     reconstructible: CASE
                         WHEN v.view_kind IN ['scalar_state', 'scalar_history'] AND EXISTS {
                             MATCH (v)-[:CURRENT_ANCHOR|CONTRIBUTED_TO|HISTORY_ENTRY]
                                   ->(source:TypedAssertion)
                             WHERE NOT source IN scalar_assertions
                         }
                         THEN true
                         WHEN v.view_kind = 'timeline' AND EXISTS {
                             MATCH (v)-[:EVENT_HISTORY_ENTRY]->(source:TypedEventAssertion)
                             WHERE NOT source IN event_assertions
                         }
                         THEN true
                         ELSE false
                     END
                 }] AS view_repairs
            FOREACH (repair IN view_repairs |
                MERGE (rr:ViewProjectionRepair {
                    repair_key: $operation_id + '\u001f' + repair.view.uuid
                })
                ON CREATE SET rr.operation_id = $operation_id,
                              rr.operation_kind = 'TURN_EVIDENCE_NAMESPACE_PURGE',
                              rr.view_uuid = repair.view.uuid,
                              rr.view_key = coalesce(repair.view.view_key, repair.view.qs_key),
                              rr.view_kind = repair.view.view_kind,
                              rr.namespace = coalesce(
                                  repair.view.namespace, repair.view.group_id, 'default'),
                              rr.namespace_key = f.namespace_key,
                              rr.fence_generation = f.generation,
                              rr.view_subtype = repair.view.view_subtype,
                              rr.subject_uuid = repair.view.view_subject_uuid,
                              rr.predicate = repair.view.view_predicate,
                              rr.domain = coalesce(repair.view.view_domain, ''),
                              rr.source_family = repair.source_family,
                              rr.reconstructible = repair.reconstructible,
                              rr.remaining_evidence_count = size(
                                  coalesce(repair.view.episode_uuids, [])),
                              rr.status = CASE
                                  WHEN repair.reconstructible THEN 'pending'
                                  ELSE 'terminal_not_rebuildable'
                              END,
                              rr.terminal_reason = CASE
                                  WHEN NOT repair.reconstructible
                                  THEN 'not_rebuildable'
                                  ELSE null
                              END,
                              rr.started_at = datetime()
            )
            WITH f, doomed, scalar_assertions, event_assertions, scalar_heads, event_heads,
                 stale_repairs, dependent_views, current_views, namespaces, view_repairs
            CALL {
                WITH namespaces
                UNWIND namespaces AS ns
                OPTIONAL MATCH (w)
                WHERE (w:ConsolidationWatermark OR w:ScalarConsolidationWatermark
                       OR w:EventConsolidationWatermark OR w:CountingConsolidationWatermark)
                  AND (coalesce(w.group_id, w.namespace, '') = ns
                       OR (ns = 'default'
                           AND coalesce(w.group_id, w.namespace, '') IN ['', '\\u0000null']))
                WITH collect(DISTINCT w) AS watermarks
                FOREACH (w IN watermarks | DETACH DELETE w)
                RETURN size([w IN watermarks WHERE w IS NOT NULL]) AS watermarks_reset
            }
            FOREACH (a IN scalar_assertions | DETACH DELETE a)
            FOREACH (event IN event_assertions | DETACH DELETE event)
            FOREACH (head IN scalar_heads | DETACH DELETE head)
            FOREACH (head IN event_heads | DETACH DELETE head)
            FOREACH (t IN doomed | DETACH DELETE t)
            RETURN size(doomed) AS c, size(current_views) AS dependent_views_retired,
                   size(dependent_views) AS dependent_views_scrubbed,
                   size(current_views) AS view_repairs_created,
                   size(scalar_assertions) AS typed_assertions_deleted,
                   size(event_assertions) AS typed_event_assertions_deleted,
                   size(stale_repairs) AS stale_repairs_deleted,
                   watermarks_reset
            """
        purge_query = (
            purge_query
            .replace("__TURN_SCOPE__", tenant_scope_cypher("t"))
            .replace("__ASSERTION_SCOPE__", tenant_scope_cypher("a"))
            .replace("__EVENT_SCOPE__", tenant_scope_cypher("event"))
            .replace("__REPAIR_SCOPE__", tenant_scope_cypher("stale_repair"))
        )
        rows = self._neo4j.execute(
            purge_query,
            params={
                "namespace_key": namespace_key,
                "operation_id": operation_id,
                **tenant_scope_params(namespace),
            },
        )
        return int(rows[0]["c"]) if rows else 0

    # ---- stats (debug report) ----------------------------------------------------------------

    def evidence_stats(self) -> dict[str, Any]:
        """Capture stats for the Phase 3 debug report, including triage breakdowns.

        CF-114: was SIX sequential full-label scans of `:TurnEvidence`, one per metric, each its own
        round trip. Now THREE, and every aggregation stays server-side.

        The obvious collapse -- one pass that `collect()`s the grouped fields and counts them in
        Python -- was implemented, measured, and rejected. It trades scan count for unbounded
        transfer, which is the wrong trade for a label the entry itself says grows without bound
        (Neo4j 5.26.21, test instance):

            n=50,000   6 queries (before)   400,007 dbHits    20 rows over wire    105 ms
                       1 query  (collect)   300,001 dbHits   250,000 values        303 ms
                       3 queries (this)     400,003 dbHits    15 rows over wire    100 ms

        The `collect` version is 3x slower at 50k and degrades further with N. This shape keeps the
        payload bounded by DISTINCT-value cardinality rather than row count.

        `role` and `triage_version` are folded into one composite grouping, and the marginals plus
        `total` and `latest` are derived from it -- correct because summing a partition gives the
        whole, and max-of-maxes is the max. `source_kind` keeps its own query so its `LIMIT 10`
        stays server-side. `triage_reason` keeps its own because `UNWIND` on a list property
        multiplies cardinality and would corrupt every other aggregate sharing the pass.

        Ordering (count DESC) and the `source_kind` top-10 truncation are unchanged, so the
        returned dict is identical in shape and content to the six-query version.
        """
        composite = self._neo4j.execute(
            """
            MATCH (t:TurnEvidence)
            RETURN t.role AS role, t.triage_version AS version, count(*) AS c,
                   toString(max(t.recorded_at)) AS latest
            """
        )

        total = 0
        role_counts: dict[str, int] = {}
        version_counts: dict[str, int] = {}
        latest: str | None = None
        for row in composite:
            count = int(row["c"] or 0)
            total += count
            role_counts[str(row["role"])] = role_counts.get(str(row["role"]), 0) + count
            version_counts[str(row["version"])] = version_counts.get(str(row["version"]), 0) + count
            row_latest = row["latest"]
            if row_latest is not None and (latest is None or str(row_latest) > latest):
                latest = str(row_latest)

        def _desc(counts: dict[str, int]) -> dict[str, int]:
            return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

        by_role = _desc(role_counts)
        by_version = _desc(version_counts)

        by_source: dict[str, int] = {}
        for row in self._neo4j.execute(
            "MATCH (t:TurnEvidence) RETURN t.source_kind AS sk, count(t) AS c ORDER BY c DESC LIMIT 10"
        ):
            by_source[str(row["sk"])] = int(row["c"])

        by_reason: dict[str, int] = {}
        for row in self._neo4j.execute(
            "MATCH (t:TurnEvidence) UNWIND t.triage_reason AS reason "
            "RETURN reason AS reason, count(*) AS c ORDER BY c DESC"
        ):
            by_reason[str(row["reason"])] = int(row["c"])

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
