"""The survivor delta a merge owns -- and how to recognize it (plan Phase 5, invariant 9).

An exact unmerge must reverse what the merge did to the SURVIVOR, not just recreate the absorbed
node. But it must never clobber state that someone changed AFTER the merge: "newer or conflicting
state must be preserved and routed to NEEDS_REVIEW."

The merge's property mutation is a PURE FUNCTION of the two pre-merge nodes (see the SET clause in
``CorrelationRepository.merge_entity``). Because the lossless snapshot captures both nodes' pre-merge
state, we can REPLAY that function to compute exactly what the merge must have written. Comparing
that to the survivor's current values answers the only question that matters:

    current == replayed(merge)  -> nothing changed since the merge; safe to restore pre-merge values
    current != replayed(merge)  -> someone changed the survivor after the merge; NEEDS_REVIEW

This module owns that replay. It is pure and mirrors the Cypher exactly; if the merge's SET clause
ever changes, this must change with it (the round-trip tests will catch a divergence).
"""

from __future__ import annotations

from typing import Any

from menhir.domain.utils import (
    authority_for,
    corroboration_for,
    effective_authority,
    node_contributors,
    primary_source_for,
)

#: The survivor properties the merge writes. These -- and only these -- are what an unmerge restores
#: on the survivor. Lineage (`merged_from`, `merge_audit`) is handled SUBTRACTIVELY instead of by
#: restore, so that absorptions performed by OTHER merges after this one are not erased.
MERGE_OWNED_SURVIVOR_PROPERTIES = (
    "summary", "content", "source", "source_confidence", "sources", "corroboration",
)

#: NO FORMULA VERSIONING. Merges recorded before this module changed were produced by the previous
#: rule (`source` comma-appended, `source_confidence += 0.1`). Replaying them with the rule below
#: yields a different value than the graph holds, so Guard 2 in `unmerge_coordinator` reports
#: SURVIVOR_CHANGED_SINCE_MERGE and REFUSES to unmerge them. That is a deliberate, accepted loss --
#: unmerge stops working for merges recorded 2026-07-12..2026-07-28 (measured: ~77% of 649 entries
#: would diverge). Nothing is corrupted; the capability is simply unavailable for that window.
#: Do NOT "fix" a surprising SURVIVOR_CHANGED_SINCE_MERGE on an old merge by loosening the guard.


def _s(value: Any) -> str:
    """Cypher's size(coalesce(x, '')) semantics: null behaves as the empty string."""
    return "" if value is None else str(value)


def replay_survivor_merge(
    survivor: dict[str, Any], absorbed: dict[str, Any]
) -> dict[str, Any]:
    """Recompute the survivor property values a merge of ``absorbed`` into ``survivor`` produces.

    Mirrors merge_entity's SET clause exactly:

        richer = absorbed WHEN size(coalesce(absorbed.summary, absorbed.content,'')) >
                               size(coalesce(survivor.summary, survivor.content,'')) * 1.2
                 ELSE survivor
        summary           = coalesce(richer.summary, richer.content, survivor.summary)
        content           = absorbed.content WHEN size(coalesce(absorbed.content,'')) >
                                                 size(coalesce(survivor.content,'')) * 1.2
                            ELSE survivor.content
        sources           = ordered union of both contributor lists, deduped
        source            = lowest-tier contributor          (primary_source_for)
        source_confidence = min of both nodes' EFFECTIVE authority (derive_merged_provenance)
        corroboration     = number of distinct writer FAMILIES (corroboration_for)

    Provenance is DERIVED here, never accumulated. The previous rule comma-appended `source` and
    added 0.1 to `source_confidence` per merge, which made trust a function of how many duplicates a
    node absorbed: four absorptions walked a `claude-code` entity (0.7) to 1.0, the tier reserved for
    the user's own statements. `min` over the contributors cannot do that at any merge count.
    """
    s_summary, s_content = survivor.get("summary"), survivor.get("content")
    a_summary, a_content = absorbed.get("summary"), absorbed.get("content")

    absorbed_text = _s(a_summary if a_summary is not None else a_content)
    survivor_text = _s(s_summary if s_summary is not None else s_content)
    richer = absorbed if len(absorbed_text) > len(survivor_text) * 1.2 else survivor

    r_summary, r_content = richer.get("summary"), richer.get("content")
    summary = r_summary if r_summary is not None else (
        r_content if r_content is not None else s_summary
    )

    content = a_content if len(_s(a_content)) > len(_s(s_content)) * 1.2 else s_content

    return {
        "summary": summary,
        "content": content,
        **derive_merged_provenance(survivor, absorbed),
    }


def contributors_of(node: dict[str, Any]) -> list[str]:
    """One node's contributor labels. See ``utils.node_contributors`` for the read rules."""
    return node_contributors(node.get("sources"), node.get("source"))


def merged_contributors(
    survivor: dict[str, Any], absorbed: dict[str, Any]
) -> list[str]:
    """The ordered, de-duplicated contributor list a merge produces.

    Reads `sources` when present and falls back to the legacy comma-joined `source`, so a node
    written before the list existed merges correctly without being migrated first. Survivor
    contributors keep their positions; the absorbed node's new ones append.
    """
    survivor_sources = contributors_of(survivor)
    absorbed_sources = contributors_of(absorbed)
    seen = set(survivor_sources)
    return survivor_sources + [s for s in absorbed_sources if s not in seen]


def derive_merged_provenance(
    survivor: dict[str, Any], absorbed: dict[str, Any]
) -> dict[str, Any]:
    """The four provenance values a merge writes onto the survivor.

    THE single derivation. ``merge_entity`` passes the result straight into its Phase 2 parameters
    and this module replays it for the unmerge guard, so the write and its inverse cannot drift --
    a second copy of this arithmetic anywhere would break exact unmerge the moment either changed.

    Authority is the minimum of what each input node ACTUALLY had (`effective_authority`), not the
    minimum of what their labels ALLOW. Deriving it from labels alone re-raises a node that was
    deliberately stamped below its tier: two `project-scan` nodes explicitly held at 0.5 would merge
    to the label's 0.9 ceiling, promoting an inference to scanned ground truth. Taking the minimum of
    the observed values -- each already capped by its own ceiling -- makes the merged node no more
    trusted than either input and no more trusted than any contributing label permits.
    """
    sources = merged_contributors(survivor, absorbed)
    authority = min(
        effective_authority(contributors_of(survivor), survivor.get("source_confidence")),
        effective_authority(contributors_of(absorbed), absorbed.get("source_confidence")),
        # Redundant with the two above (each is already capped by its own contributors, and the
        # union's ceiling is the lower of the two), but stated so the union bound is enforced here
        # rather than inferred from a proof that a later edit could invalidate.
        authority_for(sources),
    )
    return {
        "sources": sources,
        "source": primary_source_for(sources),
        "source_confidence": authority,
        "corroboration": corroboration_for(sources),
    }


def survivor_matches_merge_output(
    current: dict[str, Any], survivor_pre: dict[str, Any], absorbed_pre: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Does the survivor still hold exactly what the merge wrote?

    Returns ``(matches, differences)``. ``differences`` maps each divergent property to
    ``{"expected": <what the merge wrote>, "actual": <what is there now>}`` so a NEEDS_REVIEW
    diagnostic can say precisely WHAT changed after the merge rather than just "conflict".
    """
    expected = replay_survivor_merge(survivor_pre, absorbed_pre)
    differences: dict[str, Any] = {}
    for key in MERGE_OWNED_SURVIVOR_PROPERTIES:
        want, got = expected.get(key), current.get(key)
        if key == "source_confidence":
            # Float compare with a tolerance: the graph round-trips through a double.
            if want is None and got is None:
                continue
            if want is None or got is None or abs(float(want) - float(got)) > 1e-9:
                differences[key] = {"expected": want, "actual": got}
            continue
        if want != got:
            differences[key] = {"expected": want, "actual": got}
    return (not differences), differences


def restorable_survivor_properties(survivor_pre: dict[str, Any]) -> dict[str, Any]:
    """The survivor's pre-merge values for the properties the merge owns -- what an unmerge writes
    back. Deliberately excludes lineage, which is reversed subtractively."""
    return {k: survivor_pre.get(k) for k in MERGE_OWNED_SURVIVOR_PROPERTIES}
