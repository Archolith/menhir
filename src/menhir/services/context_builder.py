"""Context builder — packs recall results into a token-budget-limited string."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from menhir.domain.recall import (
    EventAuthorityVerdict,
    QueryPreset,
    RecallResult,
    ScoredMemory,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.services.recall_service import RecallService

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
    seen_norms: dict[tuple[str, str | None], ScoredMemory] = {}
    unique: list[ScoredMemory] = []
    for m in memories:
        norm = norm_map[m.uuid]
        # Use (normalized_text, view_kind) as dedup key to prevent collapsing across shapes
        dedup_key = (norm, m.view_kind)
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
            if m.view_kind == k.view_kind and _jaccard(words_m, words_map[k.uuid]) > 0.8:
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
        lines.append(f"  - {happened} | {fact} | belief: {belief_role}")
    return lines


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


#: Gates that represent a failed event selection (no resolved object). Only these gates fail closed;
#: any other advisory gate is not inferred as unresolved.
_SELECTION_FAIL_CLOSED_GATES = frozenset(
    {"anchor", "ambiguity", "time", "scope", "no_candidate"}
)


def _event_selection_failed(verdict: EventAuthorityVerdict) -> bool:
    """True when an event advisory's selection itself failed to resolve an object.

    Fail-closed applies only to an ``advisory`` whose gate is a selection-failure gate
    (``anchor``, ``ambiguity``, ``time``, ``scope``, ``no_candidate``). Route/foundation/evidence
    advisories carry a resolved selection and stay advisory; a future advisory gate that is not a
    selection failure must not be inferred as unresolved from ``object_key`` alone.
    """
    return (
        verdict.status == "advisory"
        and verdict.gate in _SELECTION_FAIL_CLOSED_GATES
    )


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
    # Frontier: cluster recalled memories into temporal/currency-aware evidence bundles
    # (domain/brief_builder) instead of a flat "[Memory i] name: content" list. Off = today's
    # behavior byte-for-byte. Wired from settings.frontier_brief_builder at bootstrap.
    brief_builder_enabled: bool = False

    async def build_context(
        self,
        query: str,
        *,
        max_tokens: int = 2000,
        preset: QueryPreset = QueryPreset.KNOWLEDGE,
        session_id: str | None = None,
        include_scores: bool = False,
        namespace: str | None = None,
    ) -> ContextResult:
        """Recall memories and pack them into a token-budget-limited string."""

        # Rich context always keeps historical source-time evidence. This does not add
        # invalidated candidates; it only prevents a recalled older fact from losing the
        # happened-at stamp needed to compare it with a later fact.
        recall_result: RecallResult = await self.recall_service.recall(
            query, preset=preset, namespace=namespace,
            include_invalidated=True,
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
                pass

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

        # Fail-closed determination, computed FIRST so it gates scalar authority too. Any event
        # advisory whose selection itself failed to resolve an object (explicit selection-failure
        # gates anchor/ambiguity/time/scope/no_candidate) means "did I" cannot be answered from
        # grounded evidence. We render only the unresolved instruction: current-state scalar
        # authority, ranked memories, and the supplementary timeline are ALL suppressed as answer
        # evidence — scalar authority answers the present, not a missing historical anchor, and
        # would mislead a temporal-before query.
        fail_closed = any(
            _event_selection_failed(v)
            for v in (recall_result.event_authority_layer or ())
        )

        # Phase 4c/7.J: authority is a distinct verdict layer, packed BEFORE ranked memories. The
        # bounded contributors carry relation labels + user wording so the consuming LLM can narrate
        # the transition without re-adjudicating which value is current. Disabled/no-verdict path
        # adds no lines and preserves the historical context byte-for-byte. Suppressed entirely
        # when an event verdict failed closed (current-state scalar must not answer a temporal query).
        if not fail_closed:
            for verdict in recall_result.authority_layer or ():
                heading = (
                    f"[Scalar authority: {verdict.status.upper()}] {verdict.attribute} = "
                    f"{verdict.value} ({verdict.kind}"
                    + (f", valid {verdict.valid_at}" if verdict.valid_at else "")
                    + ")"
                )
                authority_lines = [heading]
                for contributor in verdict.contributors:
                    authority_lines.append(
                        f"  - {contributor.relation}: {contributor.stated_span} "
                        f"(valid {contributor.valid_at or 'unknown'})"
                    )
                if verdict.contributors_truncated:
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

        # First-person event authority, packed after the scalar authority blocks and before
        # ranked memories. A fully-grounded verdict is rendered as explicitly authoritative/
        # preferred while preserving its grounding fields (predicate, selected object display/key,
        # valid_at, time_basis, domain, exact stated_span quote, episode/TurnEvidence identities,
        # gate). An advisory whose selection itself failed to resolve an object (selection-failure
        # gates: anchor/ambiguity/time/scope/no_candidate, i.e. resolution failed) fails closed:
        # it renders an explicit unresolved instruction and packs NEITHER ranked memories NOR the
        # supplementary timeline as answer evidence. Route/foundation/evidence advisories carry a
        # resolved selection, stay advisory, and block only when resolution itself failed.
        # Disabled/no-verdict path adds no lines and preserves the historical context byte-for-byte.
        for verdict in recall_result.event_authority_layer or ():
            if verdict.status == "leads":
                heading = (
                    f"[Event authority: LEADS (authoritative)] {verdict.predicate} = "
                    f"{verdict.object_display} (key: {verdict.object_key}, kind {verdict.kind}"
                    + (f", valid {verdict.valid_at}" if verdict.valid_at else "")
                    + ")"
                )
                event_lines = [heading]
                if verdict.time_basis:
                    event_lines.append(f"  - time basis: {verdict.time_basis}")
                if verdict.domain:
                    event_lines.append(f"  - domain: {verdict.domain}")
                if verdict.stated_span:
                    event_lines.append(f'  - source quote: "{verdict.stated_span}"')
                event_lines.append(
                    "  - identities: episode "
                    f"{verdict.episode_uuid or 'unknown'}, turn evidence "
                    f"{verdict.turn_evidence_uuid or 'unknown'}"
                )
                event_lines.append(f"  - gate: {verdict.gate}")
                event_lines.append(
                    "  - instruction: prefer the selected object over any conflicting "
                    "related memory"
                )
            elif _event_selection_failed(verdict):
                heading = (
                    f"[Event authority: UNRESOLVED] {verdict.predicate} "
                    f"(kind {verdict.kind})"
                )
                event_lines = [heading]
                event_lines.append(f"  - gate: {verdict.gate}")
                event_lines.append(f"  - reason: {verdict.reason}")
                event_lines.append(
                    "  - instruction: do not infer an answer from ranked memories or "
                    "timeline; report unresolved"
                )
            else:
                heading = (
                    f"[Event authority: ADVISORY] {verdict.predicate} "
                    f"(kind {verdict.kind})"
                )
                event_lines = [heading]
                event_lines.append(f"  - gate: {verdict.gate}")
                event_lines.append(f"  - reason: {verdict.reason}")
            event_block = "\n".join(event_lines)
            event_tokens = _tokens_for(event_block)
            if running_tokens + event_tokens > effective_budget:
                truncated = True
                break
            lines.append(event_block)
            running_tokens += event_tokens

        # Fail-closed already determined above (before scalar authority). When it is set, the
        # event unresolved instruction was rendered and ranked memories are NOT packed as answer
        # evidence (tempting, but unverified against this selection).

        # Relevance-ranked list first — recall order is the load-bearing signal (an A/B
        # showed a Timeline-first brief buries the answer, which is usually the top-relevance,
        # often-undated memory). Identical to the off path. Skipped entirely when an event
        # verdict failed closed so tempting memories are not packed as answer evidence.
        if not fail_closed:
            for idx, mem in enumerate(memories, start=1):
                name = mem.name
                content = mem.content or ""

                # Build verdict markers: append inline after name when features that produce
                # them are active. Markers only activate when their gates are on (warden_label
                # needs enable_warden_gate; is_superseded_view needs include_superseded).
                markers = []
                if getattr(mem, "is_scalar_authority", False):
                    markers.append("current authority")
                if mem.is_superseded_view:
                    markers.append("superseded view")
                if mem.warden_label is not None:
                    markers.append(f"flagged: {mem.warden_label.value}")
                if mem.breakdown.conflict_bonus == 1.0:
                    markers.append("unresolved conflict")

                if markers:
                    name_with_markers = f"{name} ({', '.join(markers)})"
                else:
                    name_with_markers = name

                if include_scores:
                    line = f"[Memory {idx}] (score: {mem.final_score:.2f}) {name_with_markers}: {content}"
                else:
                    line = f"[Memory {idx}] {name_with_markers}: {content}"
                line = "\n".join((line, *_source_time_lines(mem)))

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
                    break

                lines.append(line)
                memory_ids.append(mem.uuid)
                running_tokens += tokens
                if is_stale:
                    lines.append(advisory_line)
                    running_tokens += advisory_tokens

        if not fail_closed and self.brief_builder_enabled and not truncated:
            # Frontier: APPEND a supplementary Timeline view below the relevance list, so
            # temporal questions get an ordered, currency-marked chain without displacing
            # the answer. Only if budget remains; recall used include_invalidated so the
            # Timeline can show superseded->current progression.
            from menhir.domain.brief_builder import build_timeline_bundle, render_bundles

            timeline = build_timeline_bundle(memories)
            if timeline is not None:
                block = render_bundles([timeline], include_provenance=include_scores)
                tokens = _tokens_for(block)
                if running_tokens + tokens <= effective_budget:
                    lines.append(block)
                    running_tokens += tokens

        # Track actual memory count — advisory lines are not memories.
        # memory_ids was populated only for actual memory entries, so its
        # length is the correct count regardless of advisory lines.
        actual_memory_count = len(memory_ids)

        # Abstention honesty: render explicit message when recall found nothing
        if not lines:
            lines.append("Memory: nothing relevant found for this query.")
            if recall_result.note:
                lines.append(f"Memory note: {recall_result.note}")
            running_tokens = _tokens_for("\n\n".join(lines))
        elif recall_result.note:
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
                    # Build wiki context: read first 200 chars of each linked doc
                    wiki_lines = []
                    for doc in linked_docs[:5]:  # cap at 5 docs
                        root_path = doc.get("root_path", "")
                        if root_path and os.path.isfile(root_path):
                            try:
                                content = open(
                                    root_path, "r", encoding="utf-8", errors="replace"
                                ).read(200)
                                doc_type = doc.get("doc_type", "generic")
                                tag = f" [{doc_type}]" if doc_type != "generic" else ""
                                wiki_lines.append(
                                    f"- [[{doc['name']}]]{tag}: {content}"
                                )
                            except Exception:
                                logger.debug("Failed to read wiki doc %s", root_path, exc_info=True)
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
