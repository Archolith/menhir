"""Post-extraction quality gates and structural anchoring for the enrichment pipeline.

Extracted verbatim from ``enrichment_steps.py``; the facade module re-exports everything
here so existing ``menhir.services.enrichment_steps`` import sites keep working unchanged.
"""

from __future__ import annotations

import logging
import re

from menhir.services.enrichment_steps_context import EnrichmentContext

#: Same logger object/name as the facade module: every record this code emits must keep
#: the ``menhir.services.enrichment_steps`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.enrichment_steps")

#: An edge fact is a short declarative sentence about two entities. These are the bounds a real
#: one stays inside; anything outside them is not a fact this pipeline produced.
_MAX_REPAIRED_FACT_CHARS = 500
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _is_admissible_repaired_fact(candidate: object) -> bool:
    """Whether a model-repaired edge fact may be persisted (CF-78).

    Deliberately a SHAPE check, not a semantic one. The register's ideal -- verify the fact is
    supported by the source span -- is a grounding test, and CF-17 is the standing record of how
    badly a naive grounding test goes: token overlap admitted every single-word contradiction of
    its source, and the substring replacement admitted quotation, attribution and conditionals as
    assertions. Shipping a second one of those here, to guard a lower-severity path, would be
    repeating a mistake this codebase has already made twice and written down.

    So: bound what can be said, and leave what it means to the pipeline that already judges it.

    - a length bound, because an edge fact is a sentence and anything at 500+ characters is a
      payload wearing a sentence's clothes;
    - no control characters or line breaks, which are what let stored text stop being one field
      and start looking like structure when it is rendered into a later prompt or an agent's
      context (CF-39 is that delivery site, and it is confirmed);
    - non-empty after stripping, which the old truthiness check nearly covered and did not, since
      a whitespace-only string is truthy.

    What this does NOT do is check that the fact is true, or that the model was not steered into
    writing it. A short, clean, well-formed lie passes. That is the honest boundary of a shape
    check and the reason `fact_source` still marks these as `llm_repaired`.
    """
    if not isinstance(candidate, str):
        return False
    text = candidate.strip()
    if not text or len(text) > _MAX_REPAIRED_FACT_CHARS:
        return False
    if "\n" in candidate or "\r" in candidate:
        return False
    return not _CONTROL_CHARS_RE.search(candidate)


# ---------------------------------------------------------------------------
# Structural anchoring (best-effort, non-fatal)
# ---------------------------------------------------------------------------

def _anchor_to_structural_entities(
    ctx: EnrichmentContext,
    extracted_node_uuids: list[str],
    episode_body: str,
) -> dict[str, int]:
    """Best-effort structural anchoring with narrative/diff provenance.

    Creates ANCHORED_TO edges from extracted semantic entities to structural
    file entities referenced in the episode body. Narrative-mentioned files
    get weight 1.0; diff-only files get weight 0.3.
    """
    from menhir.infrastructure.structural_anchoring import (
        extract_file_paths,
        normalize_to_repo_relative,
        split_narrative_and_diff,
    )

    counts: dict[str, int] = {"narrative": 0, "diff": 0}

    if not extracted_node_uuids:
        return counts

    try:
        narrative, diff = split_narrative_and_diff(episode_body)

        narrative_paths = extract_file_paths(narrative)
        diff_paths = extract_file_paths(diff)
        # Remove paths already in narrative (avoid double-linking)
        narrative_set = set(narrative_paths)
        diff_only_paths = [p for p in diff_paths if p not in narrative_set]

        if narrative_paths:
            normalized = [normalize_to_repo_relative(p) for p in narrative_paths]
            counts["narrative"] = ctx.graph_adapter.anchor_semantic_to_structural(
                extracted_node_uuids, normalized,
                anchor_source="narrative_path", weight=1.0,
            )

        if diff_only_paths:
            normalized = [normalize_to_repo_relative(p) for p in diff_only_paths]
            counts["diff"] = ctx.graph_adapter.anchor_semantic_to_structural(
                extracted_node_uuids, normalized,
                anchor_source="diff_path", weight=0.3,
            )

        total = counts["narrative"] + counts["diff"]
        if total > 0:
            logger.info(
                "Structural anchoring created %d edges (narrative=%d, diff=%d) episode=%s",
                total, counts["narrative"], counts["diff"], ctx.episode_uuid,
            )
    except Exception:
        logger.debug("Structural anchoring failed (non-fatal) episode=%s", ctx.episode_uuid, exc_info=True)

    return counts
