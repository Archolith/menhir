"""Context builder — packs recall results into a token-budget-limited string."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from menhir.domain.recall_provenance import provenance_label
from menhir.domain.recall_visibility import memory_lifecycle_note

from menhir.domain.recall import (
    QueryPreset,
    RecallResult,
    ScoredMemory,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from menhir.domain.retrieval_tuning import RetrievalTuningConfig
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.services.recall_service import RecallService


def _read_linked_doc_previews(docs: list[dict]) -> list[str]:
    """Read the first 200 characters of each linked document. BLOCKING BY DESIGN.

    Runs inside ``asyncio.to_thread`` (CF-74): it does a stat and an open per document, and it
    sat on the event loop until then -- directly beside an adapter call that was already
    correctly threaded. Behaviour is otherwise unchanged: the same 200-character prefix, the
    same ``errors="replace"``, and the same per-document swallow so one unreadable file cannot
    lose the rest of the section.
    """
    lines: list[str] = []
    for doc in docs:
        root_path = doc.get("root_path", "")
        if root_path and os.path.isfile(root_path):
            try:
                with open(root_path, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read(200)
                doc_type = doc.get("doc_type", "generic")
                tag = f" [{doc_type}]" if doc_type != "generic" else ""
                lines.append(f"- [[{doc['name']}]]{tag}: {content}")
            except Exception:
                logger.debug("Failed to read wiki doc %s", root_path, exc_info=True)
    return lines

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

try:
    import tiktoken

    _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
    _tiktoken_available = True
except (ImportError, OSError):
    _tiktoken_enc = None
    _tiktoken_available = False

_DENSE_SYMBOLS = re.compile(r"[{}\[\]()<>:;=`]")
_STRUCTURAL_DENSITY_THRESHOLD = 0.15


def estimate_tokens(text: str) -> tuple[int, str]:
    """Return (token_count, mode) where mode is 'tokenizer' or 'heuristic'."""
    if _tiktoken_available and _tiktoken_enc is not None:
        return len(_tiktoken_enc.encode(text)), "tokenizer"
    return math.ceil(len(text) / 3), "heuristic"


# Cached at import time — tiktoken availability is process-wide constant.
_, _ESTIMATION_MODE = estimate_tokens("probe")


def _is_structurally_dense(text: str) -> bool:
    """True when symbol density exceeds the structural threshold."""
    if not text:
        return False
    symbol_count = len(_DENSE_SYMBOLS.findall(text))
    return symbol_count / len(text) > _STRUCTURAL_DENSITY_THRESHOLD


# ---------------------------------------------------------------------------
# Redundancy filter
# ---------------------------------------------------------------------------

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = _PUNCTUATION_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _deduplicate(memories: list[ScoredMemory]) -> list[ScoredMemory]:
    """Remove near-duplicate memories, keeping the highest scorer.

    Never collapses memories with different claim shapes (view_kind values) —
    a View may only dedup against another View with the same view_kind.
    """
    if not memories:
        return memories

    # Pre-compute normalized text and word sets
    norm_map: dict[str, str] = {}
    words_map: dict[str, set[str]] = {}
    for m in memories:
        raw = m.content or m.name
        norm = _normalize(raw)
        norm_map[m.uuid] = norm
        words_map[m.uuid] = set(norm.split())

    # Exact-match collapse (skip if different claim shapes)
    seen_norms: dict[tuple[str, str | None, str | None, str | None, str | None], ScoredMemory] = {}
    unique: list[ScoredMemory] = []
    for m in memories:
        norm = norm_map[m.uuid]
        # Preserve distinct lifecycle states even when the original instruction is identical.
        dedup_key = (norm, m.view_kind, m.status, m.artifact_status, m.superseded_by)
        existing = seen_norms.get(dedup_key)
        if existing is not None:
            if m.final_score > existing.final_score:
                unique = [u if u.uuid != existing.uuid else m for u in unique]
                seen_norms[dedup_key] = m
        else:
            seen_norms[dedup_key] = m
            unique.append(m)

    # Jaccard overlap collapse (skip if different claim shapes)
    kept: list[ScoredMemory] = []
    for m in unique:
        words_m = words_map[m.uuid]
        redundant = False
        for k in kept:
            # Only dedup if both have same view_kind (or both are None)
            same_state = (m.status, m.artifact_status, m.superseded_by) == (
                k.status, k.artifact_status, k.superseded_by,
            )
            if m.view_kind == k.view_kind and same_state and _jaccard(words_m, words_map[k.uuid]) > 0.8:
                redundant = True
                break
        if not redundant:
            kept.append(m)

    return kept


def _source_time_lines(memory: ScoredMemory) -> list[str]:
    """Render source/world time without guessing from Menhir's belief-time clock."""
    facts = memory.temporal_facts
    if not facts:
        return ["  Source time: unknown."]

    lines = ["  Source-time evidence:"]
    for temporal_fact in facts:
        valid_at = temporal_fact.valid_at
        invalid_at = temporal_fact.invalid_at
        if valid_at and invalid_at:
            happened = f"{valid_at} through {invalid_at}"
        elif valid_at:
            happened = valid_at
        else:
            happened = "unknown"
        fact = temporal_fact.fact or "(supporting fact text unavailable)"
        belief_role = temporal_fact.temporal_role.replace("_", " ")
        if temporal_fact.temporal_role == "ended":
            belief_role = f"current belief, ended {invalid_at}"
        said = f" | said {temporal_fact.said_at}" if getattr(temporal_fact, "said_at", None) else ""
        lines.append(f"  - {happened}{said} | {fact} | belief: {belief_role}")
    return lines


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


def _occurrence_contributor_line(contributor: Any) -> str:
    """Occurrence line: the event date when grounded, else only when it was mentioned (never 'valid')."""
    line = f"  - {contributor.relation}: {contributor.stated_span}"
    if contributor.source_sentence:
        line += f' -- "{contributor.source_sentence}"'
    mentioned = (contributor.mentioned_at or "")[:10] or "unknown"
    if contributor.event_at:
        return f"{line} (event {contributor.event_at[:10]}; mentioned {mentioned})"
    return f"{line} (mentioned {mentioned})"


def _fmt_count(n: Any) -> str:
    """Format a count as an int when integral, otherwise as-is."""
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def _count_match_lines(verdict: Any) -> list[str]:
    """Unit 4R match audit lines (per-action sums, item names) for a lower-bound counted View.

    Malformed JSON or wrong types skip the corresponding line silently."""
    lines: list[str] = []
    try:
        actions = json.loads(verdict.count_actions) if verdict.count_actions else None
    except (TypeError, ValueError):
        actions = None
    if isinstance(actions, dict) and actions:
        lines.append("  - per action: " + ", ".join(f"{k} {_fmt_count(n)}" for k, n in actions.items()))
    try:
        items = json.loads(verdict.count_items) if verdict.count_items else None
    except (TypeError, ValueError):
        items = None
    if isinstance(items, list) and items:
        lines.append("  - items: " + ", ".join(str(x) for x in items))
    return lines


@dataclass(frozen=True)
class ContextResult:
    query: str
    context: str
    token_estimate: int
    estimation_mode: str
    memory_count: int
    memory_ids: list[str]
    truncated: bool
    preset: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class ContextBuilderService:
    recall_service: RecallService
    graph_adapter: MemoryGraphAdapter | None = None
    # Frontier: per-call size of the additive source-memory section inside build_context's
    # recall. None = no explicit per-call limit; retrieval tuning controls section enablement.
    # Bootstrap supplies the configured limit when the deployment enables the section.
    source_memory_limit: int | None = None
    # Use the same configured admission/ranking profile as ordinary backend recall.
    retrieval_tuning: RetrievalTuningConfig | None = None

    async def build_context(
        self,
        query: str,
        *,
        max_tokens: int = 2000,
        preset: QueryPreset = QueryPreset.KNOWLEDGE,
        session_id: str | None = None,
        include_scores: bool = False,
        include_warden_status: bool = False,
        namespace: str | None = None,
    ) -> ContextResult:
        """Recall memories and pack them into a token-budget-limited string."""

        # Rich context always keeps historical source-time evidence. This does not add
        # invalidated candidates; it only prevents a recalled older fact from losing the
        # happened-at stamp needed to compare it with a later fact.
        recall_kwargs: dict[str, Any] = {}
        if include_warden_status:
            recall_kwargs["include_warden_status"] = True
        if self.retrieval_tuning is not None:
            recall_kwargs["tuning"] = self.retrieval_tuning
        if self.source_memory_limit is not None:
            recall_kwargs["source_memory_limit"] = self.source_memory_limit
        recall_result: RecallResult = await self.recall_service.recall(
            # build_context is an ordinary agent recall surface.  Fresh tracked writes
            # initially produce SESSION-scoped nodes, so excluding that scope makes a
            # write visible to recall_memories but immediately disappear from context.
            query, preset=preset, namespace=namespace, include_session=True,
            session_id=session_id,
            include_invalidated=True,
            **recall_kwargs,
        )
        memories = list(recall_result.results)

        # Redundancy filter
        memories = _deduplicate(memories)

        # Fetch matching TODOs early so their cost is reflected in the budget.
        todo_section: str = ""
        todo_tokens: int = 0
        if self.graph_adapter is not None and query.strip():
            try:
                matching_todos = await asyncio.to_thread(
                    self.graph_adapter.list_todos_matching_query,
                    query,
                    limit=3,
                    namespace=namespace,
                )
                if matching_todos:
                    todo_lines = []
                    for t in matching_todos:
                        tag = (t.get("priority") or "normal").upper()
                        ref = f" {t['code_ref']} —" if t.get("code_ref") else " —"
                        snippet = t.get("content") or ""
                        if len(snippet) > 80:
                            snippet = snippet[:80] + "..."
                        todo_lines.append(f"- [{tag}]{ref} {snippet}")
                    todo_section = "\n\n**Related open TODOs:**\n" + "\n".join(
                        todo_lines
                    )
                    todo_tokens, _ = estimate_tokens(todo_section)
            except Exception:
                logger.warning(
                    "build_context: TODO search failed; continuing without TODOs",
                    exc_info=True,
                )

        # Determine effective budget using the process-wide estimation mode constant.
        # Reserve tokens for the TODO section so the total stays within max_tokens.
        mode = _ESTIMATION_MODE
        effective_budget = (
            max_tokens if mode == "tokenizer" else math.floor(max_tokens * 0.5)
        )
        effective_budget = max(0, effective_budget - todo_tokens)

        # Pack in score order (already sorted by recall)
        lines: list[str] = []
        memory_ids: list[str] = []
        running_tokens = 0
        truncated = False

        def _tokens_for(text: str) -> int:
            candidate_text = text + "\n\n"
            tokens, _ = estimate_tokens(candidate_text)
            if mode == "heuristic" and _is_structurally_dense(candidate_text):
                tokens = math.ceil(tokens * 1.3)
            return tokens

        # Reserve a brief coverage notice before ranked memories. If it cannot fit, suppress
        # that lane rather than return unchecked memories with their notice dropped.
        ranked_notice_fits = True
        if recall_result.warden_notice:
            notice_line = "Memory checks: " + recall_result.warden_notice
            cost = _tokens_for(notice_line)
            if cost <= effective_budget:
                lines.append(notice_line)
                running_tokens += cost
            else:
                ranked_notice_fits = False
                truncated = True
        if include_warden_status and recall_result.warden_status is not None:
            diagnostic = "Warden status: " + json.dumps(asdict(recall_result.warden_status), separators=(",", ":"))
            cost = _tokens_for(diagnostic)
            if running_tokens + cost <= effective_budget:
                lines.append(diagnostic)
                running_tokens += cost
            else:
                truncated = True
        if not ranked_notice_fits:
            memories = []

        # Phase 4c/7.J: authority is a distinct verdict layer, packed BEFORE ranked memories. The
        # bounded contributors carry relation labels + user wording so the consuming LLM can narrate
        # the transition without re-adjudicating which value is current. Disabled/no-verdict path
        # adds no lines and preserves the historical context byte-for-byte.
        for verdict in recall_result.authority_layer or ():
            shown_value = (
                f"at least {verdict.value} you mentioned" if verdict.lower_bound
                else verdict.value
            )
            if verdict.lower_bound:
                _uncertain = verdict.count_uncertain
                if (
                    isinstance(_uncertain, (int, float))
                    and not isinstance(_uncertain, bool)
                    and _uncertain > 0
                ):
                    shown_value = (
                        f"at least {verdict.value} you mentioned, plus "
                        f"{_fmt_count(_uncertain)} more that may be repeats"
                    )
            if verdict.kind == "history":
                _history_n = (
                    verdict.contributors_total if verdict.contributors_total > 0
                    else len(verdict.contributors)
                )
                heading = (
                    f"[Scalar authority: {verdict.status.upper()}] {verdict.attribute} "
                    f"history, {_history_n} entries (history"
                    + (f", valid {verdict.valid_at}" if verdict.valid_at else "")
                    + ")"
                )
            else:
                heading = (
                    f"[Scalar authority: {verdict.status.upper()}] {verdict.attribute} = "
                    f"{shown_value} ({verdict.kind}"
                    + (f", valid {verdict.valid_at}" if verdict.valid_at else "")
                    + ")"
                )
            authority_lines = [heading]
            if verdict.lower_bound:
                authority_lines.extend(_count_match_lines(verdict))
            for contributor in verdict.contributors:
                if contributor.operation == "occurrence":
                    authority_lines.append(_occurrence_contributor_line(contributor))
                    continue
                authority_lines.append(
                    f"  - {contributor.relation}: {contributor.stated_span} "
                    f"(valid {contributor.valid_at or 'unknown'})"
                )
            if (
                verdict.contributors_truncated
                and not verdict.contributors
                and verdict.contributors_total > 0
                and verdict.view_uuid
            ):
                authority_lines.append(
                    f"  - built from {verdict.contributors_total} items; "
                    f"drill down: get_scalar_view_items(view_uuid={verdict.view_uuid})"
                )
            elif verdict.contributors_truncated:
                authority_lines.append(
                    f"  - provenance truncated: {verdict.contributors_total} total; "
                    f"continue at offset {verdict.next_offset} for View {verdict.view_uuid or 'synthetic'}"
                )
            authority_block = "\n".join(authority_lines)
            authority_tokens = _tokens_for(authority_block)
            if running_tokens + authority_tokens > effective_budget:
                truncated = True
                break
            lines.append(authority_block)
            running_tokens += authority_tokens

        # Query hint goes after the authority blocks and before the ranked memories: the shape
        # the #168 replay A/B tested.
        hint_line = None
        hint_at = len(lines)
        if recall_result.query_hint:
            candidate = f"Query note: {recall_result.query_hint}"
            hint_tokens = _tokens_for(candidate)
            if running_tokens + hint_tokens <= effective_budget:
                hint_line = candidate
                running_tokens += hint_tokens
            else:
                truncated = True

        # Relevance-ranked list first — recall order is the load-bearing signal (an A/B
        # showed a Timeline-first brief buries the answer, which is usually the top-relevance,
        # often-undated memory). Identical to the off path.
        for idx, mem in enumerate(memories, start=1):
            name = mem.name
            content = mem.content or ""

            # Build verdict markers: append inline after name when features that produce
            # them are active. Markers only activate when their gates are on (warden_label
            # needs enable_warden_gate; is_superseded_view needs include_superseded).
            markers = []
            lifecycle_note = memory_lifecycle_note(
                status=mem.status, artifact_status=mem.artifact_status, superseded_by=mem.superseded_by,
            )
            if lifecycle_note:
                markers.append(lifecycle_note)
            if getattr(mem, "is_scalar_authority", False):
                markers.append("current authority")
            if mem.is_superseded_view:
                markers.append("superseded view")
            if mem.warden_label is not None:
                markers.append(f"flagged: {mem.warden_label.value}")
            if mem.breakdown.conflict_bonus == 1.0:
                markers.append("unresolved conflict")
            provenance_marker = provenance_label(getattr(mem, "provenance", None))
            if provenance_marker:
                markers.append(provenance_marker)

            if markers:
                name_with_markers = f"{name} ({', '.join(markers)})"
            else:
                name_with_markers = name

            if include_scores:
                line = f"[Memory {idx}] (score: {mem.final_score:.2f}) {name_with_markers}: {content}"
            else:
                line = f"[Memory {idx}] {name_with_markers}: {content}"
            line = "\n".join((line, *_source_time_lines(mem)))
            if mem.needs_review:
                # Pack the warning atomically with its memory under the token budget.
                line += (
                    "\n  Review required: " + (mem.review_reason or "source state changed")
                    + ". Verify the source before asserting this memory as current truth."
                )

            tokens = _tokens_for(line)
            if mem.verifier_evidence:
                line += "\n" + "\n".join(e.context_line() for e in mem.verifier_evidence)
                tokens = _tokens_for(line)

            # Stale-anchor advisory: atomic with the memory line.
            # If the memory is stale, check memory + advisory combined budget.
            # Never include a stale memory without its advisory.
            stale_info = mem.stale_anchor_info
            is_stale = stale_info is not None and stale_info.get("stale_anchor")
            if is_stale:
                stale_path = stale_info.get("path") or "unknown"
                verification = stale_info.get("stale_verification")
                if verification and isinstance(verification, dict):
                    v_outcome = str(verification.get("outcome") or "")
                    if v_outcome == "still_valid":
                        advisory_line = (
                            f"  ⚠️ Stale file anchor: {stale_path} changed after "
                            f"this memory, but it was later verified against the "
                            f"current file. Use with normal caution."
                        )
                    elif v_outcome == "outdated":
                        advisory_line = (
                            f"  ⚠️ Stale file anchor: {stale_path} — this memory "
                            f"was verified against the current file and appears "
                            f"outdated. Do not rely on it. Update or supersede it."
                        )
                    else:
                        advisory_line = (
                            f"  ⚠️ Stale file anchor: {stale_path} changed after "
                            f"this memory was anchored. Before relying, inspect "
                            f"the current file. If outdated, update or supersede it."
                        )
                else:
                    advisory_line = (
                        f"  ⚠️ Stale file anchor: {stale_path} changed after this "
                        f"memory was anchored. Before relying on this memory, inspect "
                        f"the current file. If outdated, update or supersede it."
                    )
                advisory_tokens = _tokens_for(advisory_line)
                total_tokens = tokens + advisory_tokens
            else:
                total_tokens = tokens
                advisory_line = None
                advisory_tokens = 0

            if running_tokens + total_tokens > effective_budget:
                truncated = True
                continue

            lines.append(line)
            memory_ids.append(mem.uuid)
            running_tokens += tokens
            if is_stale:
                lines.append(advisory_line)
                running_tokens += advisory_tokens

        # Source-memory lane (additive):
        # the session's most similar raw episode memories (up to the limit) with their own time, packed AFTER the
        # ranked list so it supplements rather than displaces relevance order. A
        # per-request knob-off (or lane failure) adds no lines and preserves the context byte-for-byte.
        if recall_result.source_memories:
            source_lines = ["Source memories (raw episodes, oldest first):"]
            for sm in recall_result.source_memories:
                source_lines.append(
                    f"  - {sm.reference_time or 'unknown time'} | {sm.content}"
                )
            source_block = "\n".join(source_lines)
            source_tokens = _tokens_for(source_block)
            if running_tokens + source_tokens <= effective_budget:
                lines.append(source_block)
                running_tokens += source_tokens
            else:
                truncated = True

        # Track actual memory count — advisory lines are not memories.
        # memory_ids was populated only for actual memory entries, so its
        # length is the correct count regardless of advisory lines.
        actual_memory_count = len(memory_ids)

        # Abstention honesty: render explicit message when recall found nothing
        if not lines:
            lines.append("Memory: nothing relevant found for this query.")
            if recall_result.note:
                lines.append(f"Memory note: {recall_result.note}")
            if hint_line is not None:
                lines.append(hint_line)
            running_tokens = _tokens_for("\n\n".join(lines))
        else:
            if hint_line is not None:
                lines.insert(hint_at, hint_line)
            if recall_result.note:
                # Append note if results exist and note is present
                note_line = f"Memory note: {recall_result.note}"
                note_tokens = _tokens_for(note_line)
                if running_tokens + note_tokens <= effective_budget:
                    lines.append(note_line)
                    running_tokens += note_tokens

        context = "\n\n".join(lines)
        if todo_section:
            context += todo_section
            running_tokens += todo_tokens

        # Fetch linked wiki/reference documents and include their context (best-effort)
        wiki_section = ""
        wiki_tokens = 0
        if self.graph_adapter is not None and memory_ids:
            try:
                linked_docs = await asyncio.to_thread(
                    self.graph_adapter.get_linked_documents, memory_ids[:10]
                )
                if linked_docs:
                    # CF-74: the stat+read below is blocking filesystem I/O on the recall request
                    # path. The graph call directly above is already threaded; this ran on the
                    # event loop in the same block, up to five times, and a linked doc on a slow
                    # or network mount stalled every other request in the process.
                    wiki_lines = await asyncio.to_thread(
                        _read_linked_doc_previews, linked_docs[:5]  # cap at 5 docs
                    )
                    if wiki_lines:
                        wiki_section = "\n\n=== Wiki Context ===\n" + "\n".join(
                            wiki_lines
                        )
                        wiki_tokens, _ = estimate_tokens(wiki_section)
                        # 30% max for wiki, but only if we have budget
                        wiki_budget = math.floor(effective_budget * 0.3)
                        if wiki_tokens > wiki_budget:
                            # truncate
                            wiki_section = (
                                wiki_lines[0]
                                + f"\n...(truncated, {len(wiki_lines)} docs)"
                            )
                            wiki_tokens = estimate_tokens(wiki_section)[0]
                        context += wiki_section
                        running_tokens += wiki_tokens
            except Exception:
                logger.debug("wiki_section fetch failed", exc_info=True)

        return ContextResult(
            query=query,
            context=context,
            token_estimate=running_tokens,
            estimation_mode=mode,
            memory_count=actual_memory_count,
            memory_ids=memory_ids,
            truncated=truncated,
            preset=preset.value,
        )
