"""Post-LLM dedup identity gate vetoing merges without positive identity evidence."""

from __future__ import annotations

import logging
from typing import Any

from menhir.infrastructure.graphiti_extraction_patches import _combined_extraction_cache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Post-LLM identity gate for dedup decisions
# ---------------------------------------------------------------------------

# After the LLM returns its dedup decisions, this gate validates each merge
# by checking for positive identity evidence between the extracted entity name
# and the target entity name.  If no positive evidence exists, the merge is
# overridden to "new entity" (duplicate_candidate_id = -1).
#
# Positive evidence (any one is sufficient):
#   - Exact name match (case-insensitive)
#   - One name is a substring of the other (≥3 chars)
#   - Acronym match (e.g. IBM ↔ International Business Machines)
#   - Shared token overlap ≥50% (Jaccard on lowered word tokens)
#
# This is the final safety net: if temperature=0 and the prompt patch both
# fail to prevent an incorrect merge, this gate catches it.  Every override
# is logged for analysis.
#
# See: Trial 10 FAIL_B_DEDUP_MERGED — "the suburbs" merged into "Chicago"
# has zero positive identity evidence under all four criteria.

_identity_gate_logger = logging.getLogger("menhir.dedup_identity_gate")


def _has_positive_identity_evidence(extracted_name: str, candidate_name: str) -> bool:
    """Return True if there is positive evidence that two names refer to the same entity.

    Conservative: returns True on any plausible match signal so legitimate merges
    (Bob→Robert, NYC→New York City, IBM→International Business Machines) are not blocked.
    Returns False only when there is genuinely no lexical relationship.
    """
    a = extracted_name.strip().lower()
    b = candidate_name.strip().lower()

    if not a or not b:
        return False

    # 1. Exact match
    if a == b:
        return True

    # 2. Substring (either direction, min 3 chars to avoid trivial matches like "a")
    if len(a) >= 3 and a in b:
        return True
    if len(b) >= 3 and b in a:
        return True

    # 3. Acronym: check if one name's initials match the other
    a_tokens = a.split()
    b_tokens = b.split()
    if len(a_tokens) == 1 and len(b_tokens) > 1:
        # a might be an acronym of b
        acronym = "".join(t[0] for t in b_tokens if t)
        if a.replace(".", "") == acronym:
            return True
    if len(b_tokens) == 1 and len(a_tokens) > 1:
        # b might be an acronym of a
        acronym = "".join(t[0] for t in a_tokens if t)
        if b.replace(".", "") == acronym:
            return True

    # 4. Token overlap (Jaccard ≥ 0.5)
    a_set = set(a_tokens) - {"the", "a", "an", "of", "in", "at", "on", "for", "to"}
    b_set = set(b_tokens) - {"the", "a", "an", "of", "in", "at", "on", "for", "to"}
    if a_set and b_set:
        intersection = a_set & b_set
        union = a_set | b_set
        if len(intersection) / len(union) >= 0.5:
            return True

    return False


def _edge_facts_mention(entity_name: str, cached_edges: list[Any] | None) -> set[str]:
    """Return the set of fact texts from cached edges that mention ``entity_name``."""
    if not cached_edges or not entity_name:
        return set()
    name_lower = entity_name.strip().lower()
    if len(name_lower) < 3:
        return set()
    facts: set[str] = set()
    for edge in cached_edges:
        fact = ""
        if hasattr(edge, "fact"):
            fact = edge.fact or ""
        elif isinstance(edge, dict):
            fact = edge.get("fact", "")
        if name_lower in fact.lower():
            facts.add(fact)
    return facts


def _patch_graphiti_dedup_identity_gate() -> None:
    """Wrap the LLM dedup resolver to veto merges lacking positive identity evidence.

    Patches ``_resolve_with_llm`` in ``graphiti_core.utils.maintenance.node_operations``
    to intercept the ``NodeResolutions`` returned by the LLM.  For each merge decision
    (``duplicate_candidate_id >= 0``), the gate applies two checks:

    1. **Name-level identity evidence** — exact match, substring, acronym, or ≥50%
       token Jaccard between extracted and candidate entity names.  If none exists,
       the merge is vetoed.

    2. **Edge-consistency invariant** — if the cached edges' fact text mentions the
       extracted entity name but *not* the candidate entity name, the fact contradicts
       the merge (e.g. fact "Rachel moved to the suburbs" mentions "suburbs" but not
       "Chicago").  This veto fires even when name-level evidence exists, as it
       signals the LLM is merging entities that the extraction itself distinguished.

    Every override is logged for analysis.

    This is defense-in-depth behind temperature=0 and the prompt patch.  It catches
    the residual failure mode where the LLM decides two names are the same entity
    despite zero lexical relationship (e.g. "the suburbs" → "Chicago").
    """
    try:
        import graphiti_core.utils.maintenance.node_operations as _no_module
        from graphiti_core.prompts.dedupe_nodes import NodeResolutions

        if getattr(_no_module, "_menhir_identity_gate_patched", False):
            return

        _original_resolve_with_llm = _no_module._resolve_with_llm

        async def _gated_resolve_with_llm(
            llm_client,
            extracted_nodes,
            indexes,
            state,
            episode=None,
            previous_episodes=None,
            entity_types=None,
        ):
            # Capture the original generate_response to intercept the LLM output
            _orig_gen = llm_client.generate_response

            # Read the combined-extraction edge cache (populated by the Menhir
            # combined-extraction patch before node resolution runs).  This is
            # read-only — the cache is consumed later by _extract_edges_from_combined_cache.
            cached_edges: list[Any] | None = None
            cached = _combined_extraction_cache.get()
            if cached is not None:
                cached_edges = cached[1]  # tuple is (episode_key, edges)

            async def _intercepted_gen(messages, response_model=None, **gen_kwargs):
                resp = await _orig_gen(messages, response_model=response_model, **gen_kwargs)
                prompt_name = gen_kwargs.get("prompt_name", "")

                if "dedupe_nodes" not in prompt_name:
                    return resp

                # resp is the raw dict the LLM returned -- it has NOT been through
                # PatchedNodeResolutions yet, so nothing has validated its shape. A model
                # returning `entity_resolutions: ["Alice"]` (bare strings, not objects)
                # reaches this gate intact and used to raise AttributeError straight up
                # through add_episode, failing the whole episode: its content lands in the
                # graph with no entities, add_memory still reports success, and recall can
                # never see it again. The type guards below mirror the fail-safe already in
                # PatchedNodeResolutions._drop_degenerate: skip what cannot be read, treat
                # an uncoercible duplicate_candidate_id as -1 ("no duplicate").
                if not isinstance(resp, dict):
                    return resp
                resolutions = resp.get("entity_resolutions") or []
                if not isinstance(resolutions, list):
                    return resp

                # Build lookup: extracted node id → name
                llm_extracted_nodes = [
                    extracted_nodes[i] for i in state.unresolved_indices
                ]
                extracted_by_id = {
                    i: node.name for i, node in enumerate(llm_extracted_nodes)
                }

                # Build lookup: candidate_id → name
                candidate_by_id = {
                    i: node.name for i, node in enumerate(indexes.existing_nodes)
                }

                overrides = []
                for resolution in resolutions:
                    if not isinstance(resolution, dict):
                        continue  # unusable entry; leave the LLM's output untouched
                    try:
                        dup_id = int(resolution.get("duplicate_candidate_id", -1))
                    except (TypeError, ValueError):
                        continue  # fail-safe: "no duplicate", nothing for the gate to veto
                    if dup_id < 0:
                        continue  # already "new entity"

                    try:
                        ext_id = int(resolution.get("id"))
                    except (TypeError, ValueError):
                        ext_id = None  # unusable index; fall back to the reported name
                    ext_name = extracted_by_id.get(ext_id, str(resolution.get("name") or ""))
                    cand_name = candidate_by_id.get(dup_id, "")

                    veto_reason = ""

                    # Check 1: name-level identity evidence
                    if not _has_positive_identity_evidence(ext_name, cand_name):
                        veto_reason = "no positive identity evidence"

                    # Check 2: edge-consistency invariant
                    # If cached edges mention the extracted name but NOT the
                    # candidate name, the fact text contradicts the merge.
                    if not veto_reason and cached_edges:
                        ext_facts = _edge_facts_mention(ext_name, cached_edges)
                        if ext_facts:
                            cand_facts = _edge_facts_mention(cand_name, cached_edges)
                            if not cand_facts:
                                veto_reason = (
                                    f"edge-consistency: facts mention {ext_name!r} "
                                    f"but not {cand_name!r}"
                                )

                    if veto_reason:
                        overrides.append({
                            "extracted_name": ext_name,
                            "candidate_name": cand_name,
                            "original_dup_id": dup_id,
                            "reason": veto_reason,
                        })
                        resolution["duplicate_candidate_id"] = -1

                if overrides:
                    ep_content = episode.content[:120] if episode else ""
                    for ov in overrides:
                        _identity_gate_logger.warning(
                            "Identity gate VETO: %r merged into %r by LLM — %s. "
                            "Overriding to new entity. Episode: %s",
                            ov["extracted_name"],
                            ov["candidate_name"],
                            ov["reason"],
                            ep_content,
                        )

                return resp

            llm_client.generate_response = _intercepted_gen
            try:
                await _original_resolve_with_llm(
                    llm_client, extracted_nodes, indexes, state,
                    episode, previous_episodes, entity_types,
                )
            finally:
                llm_client.generate_response = _orig_gen

        _no_module._resolve_with_llm = _gated_resolve_with_llm  # type: ignore[assignment]
        _no_module._menhir_identity_gate_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti dedup identity gate patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti dedup identity gate: %s", exc)
