"""Last-line credential scrub for recall output (#577).

Applied to the finished ``RecallResult`` so it holds for every return path and cannot be
switched off by a retrieval tuning flag. Defence in depth: it redacts what the detector
recognises, nothing more. Fails closed: if the scan itself raises, the text-bearing
sections are withheld rather than returned unchecked.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from menhir.domain.recall import RecallResult, ScoredMemory, SourceMemory, TemporalFact
from menhir.domain.secret_scrub import redact, resolve_policy

logger = logging.getLogger(__name__)

WITHHELD_NOTE = "Recall output withheld: the credential scan failed."


def _clean(text: str | None) -> str | None:
    if not text:
        return text
    return redact(text)[0]


def _scrub_fact(fact: TemporalFact) -> TemporalFact:
    cleaned = _clean(fact.fact)
    return fact if cleaned is fact.fact else replace(fact, fact=cleaned)


def _scrub_memory(memory: ScoredMemory) -> ScoredMemory:
    name, content = _clean(memory.name), _clean(memory.content)
    facts = tuple(_scrub_fact(f) for f in memory.temporal_facts)
    if name is memory.name and content is memory.content and all(
        a is b for a, b in zip(facts, memory.temporal_facts)
    ):
        return memory
    return replace(memory, name=name, content=content, temporal_facts=facts)


def _scrub_source(source: SourceMemory) -> SourceMemory:
    cleaned = _clean(source.content)
    return source if cleaned is source.content else replace(source, content=cleaned)


def scrub_recall_result(result: RecallResult) -> RecallResult:
    """Redact credentials in ranked results, their temporal facts and source memories."""
    try:
        if resolve_policy() == "off":
            return result
        results = [_scrub_memory(m) for m in result.results]
        sources = (
            tuple(_scrub_source(s) for s in result.source_memories)
            if result.source_memories is not None
            else None
        )
    except Exception:
        logger.exception("Recall credential scan failed; withholding text-bearing output")
        note = f"{result.note} | {WITHHELD_NOTE}" if result.note else WITHHELD_NOTE
        return replace(result, results=[], source_memories=None, note=note)
    return replace(result, results=results, source_memories=sources)
