# RCA: Graphiti node-deduplication candidate fan-out exceeds the model context window

**Date:** 2026-08-09

**Severity:** Medium. Structural project writes succeed, but semantic enrichment fails before entity
and edge persistence. The episode is left `FAILED` and does not recover without an explicit retry.

**Status:** Root cause confirmed; remediation implemented and verified offline. All three failed
episodes were re-enriched successfully and the failed queue is empty.

> Correction: the first version of this RCA attributed the oversized request to Graphiti's ten
> previous episodes. Follow-up payload reconstruction and trace inspection disproved that
> attribution. The previous-episode windows were small; the oversized requests were assembled in
> node deduplication after extraction.

## Summary

The affected project narratives are only 2–3 KB, and their initial extraction requests succeeded.
Those extractions produced many entities: 69 for `workspace-meta` and 98 for `yawn.frontend`.

Graphiti 0.29 searches for up to 15 existing semantic candidates for every extracted entity. For
all unresolved entities, it then unions those candidates and serializes every candidate record into
one LLM node-deduplication request. A 98-entity extraction can therefore fan out to as many as 1,470
candidate records before overlap. As the graph grows and searches return fuller candidate sets, the
same episode produces a larger dedupe prompt.

The three failures occurred in `resolve_extracted_nodes`, not initial extraction. Two requests
reached the provider at roughly 135K input tokens against a 128K context limit. One was stopped by
Menhir's local guard at 402,344 characters.

## Impact

| episode | project | observed failure | recovery result |
|---|---|---|---|
| `5abfc71d` | `workspace-meta` | provider rejected 135,492 input tokens | `READY`; 4 nodes / 5 edges touched |
| `cc08fde1` | `yawn.frontend` | local guard rejected 402,344 assembled characters | `READY`; 11 nodes / 20 edges touched |
| `ee3e8a4b` | `workspace-meta` | provider rejected 135,947 input tokens | `READY`; 4 nodes / 5 edges touched |

All three queue rows were `FAILED`, with `attempts=1`, when investigated. Their structural project
scan writes had completed, but no semantic entities or edges were committed from the failed
episodes until the recovery run.

## Evidence

### Failure stage

Trace spans show that the initial extraction calls completed successfully at approximately
5.7–5.9K estimated input tokens. The failing call is downstream in
`graphiti_core.utils.maintenance.node_operations.resolve_extracted_nodes`.

This distinction matters: limiting the episode body or extraction context does not bound the later
dedupe candidate union.

### Previous episodes were not the oversized payload

The exact prior Graphiti episodes relevant to the three failures were reconstructed from the graph.
Their content totaled 5,813 characters across the inspected windows, and none contained a raw diff.
That is too small to explain a 401,970-character user message.

Graphiti does include up to ten previous episodes in several prompts, so that window remains
count-bounded rather than size-bounded. It is a separate hardening opportunity, not the cause of
these incidents.

### Candidate fan-out

Graphiti 0.29.2 performs the following sequence in
`graphiti_core/utils/maintenance/node_operations.py`:

1. `_semantic_candidate_search` requests up to `NODE_DEDUP_CANDIDATE_LIMIT = 15` candidates for
   each extracted entity.
2. Deterministic similarity resolves obvious matches.
3. For every still-unresolved entity, `resolve_extracted_nodes` merges all candidate lists into one
   global candidate collection.
4. `_resolve_with_llm` serializes the unresolved entities and the full merged candidate collection
   into one `dedupe_nodes.nodes` request.

The candidate records include their remaining graph attributes. Embedding vectors are already
stripped by Menhir's prompt serializer (fixed in `aa7c758`), but ordinary metadata multiplied over
hundreds or thousands of candidates is enough to exceed the context window.

The installed version is `graphiti-core 0.29.2` under the declared `>=0.29.2,<0.30` constraint.
Review of 0.29.3 and current upstream code found the same global-union behavior, so a dependency
upgrade does not remove the defect.

### The local estimate was optimistic

Menhir estimated assembled requests using four characters per token and floor division. Code-heavy
JSON, paths, identifiers, and punctuation tokenize more densely than ordinary prose. The provider
counts prove the estimate can undercount materially, but the earlier RCA's precise “26%” claim was
derived by combining measurements from different requests and should be treated only as a lower
bound, not an exact ratio.

## Root cause

**Primary:** Graphiti builds one node-deduplication prompt from the union of semantic candidates for
every unresolved extracted entity. Request size therefore grows with both extracted-entity count
and graph candidate density, with no batching or assembled-size budget at that layer.

**Secondary:** Menhir's four-characters-per-token guard was too optimistic for this payload shape.
It allowed two oversized requests to reach the provider.

**Contributing:** Provider context-limit failures entered the generic retry loop even though the
payload was unchanged. Retrying could not succeed and consumed additional calls before the episode
was marked failed.

## Remediation

The implemented fallback preserves Graphiti's normal behavior until a request is proven too large:

1. Search for semantic candidates once, keeping Graphiti's 15-candidate limit and original order.
2. Attempt the normal single node-dedupe request.
3. If either the local guard or provider reports a context-length failure, split the unresolved
   entity indices in half.
4. Rebuild each half's candidate union using only candidates retrieved for entities in that half,
   then resolve the halves sequentially.
5. Recurse until each request fits. If a single entity remains oversized, surface the explicit
   error instead of looping.

This approach does not lower per-entity retrieval depth or discard candidate attributes. The cost
is extra LLM calls only when the normal request exceeds the configured budget.

Additional changes:

- provider `context_length_exceeded` errors are normalized immediately to
  `GraphitiRequestTooLargeError`, bypassing the unchanged generic retries;
- the fallback estimate now uses ceiling division at three characters per token;
- split events log entity count, candidate count, and recursion depth.

## Verification

Local regression coverage includes:

- a 69-entity / 1,035-candidate case that fits and remains one request;
- a 98-entity / 1,470-candidate case that exceeds a synthetic ceiling, recursively splits, resolves
  every entity in original order, and keeps exactly 15 candidates per entity;
- a 98-entity case using Graphiti's real dedupe prompt builder and Menhir's real request-size guard,
  proving the assembled prompt is recursively split until every child request is accepted;
- conservative request-size estimates and local guard behavior;
- provider context-limit classification with no unchanged retry.

Offline verification on 2026-08-09 produced 5,729 passes and 180 skips. One unrelated,
timing-sensitive scheduler-heartbeat test failed in the final full-suite run and passed immediately
when rerun alone. All Graphiti-focused tests, including the real-prompt integration case, passed.
Live recovery then completed in three controlled steps:

1. `cc08fde1` was re-enriched as the canary and reached `READY` in about 56 seconds;
2. `5abfc71d` and `ee3e8a4b` were requeued one at a time and both reached `READY`;
3. a final unfiltered queue check found zero `FAILED` episodes.

No adaptive-split warning appeared in the live logs. The retries returned smaller extraction sets
than the original failed attempts, so their dedupe prompts fit without fallback (24,188 estimated
tokens for the largest observed retry prompt). The live run therefore verifies end-to-end recovery
and normal-path compatibility; the oversized fallback itself is verified deterministically by the
real-prompt-builder regression above. The next naturally oversized episode should be checked for
split telemetry as an operational follow-up, not treated as a blocker to this incident recovery.

## Rejected explanations and fixes

- **Previous-episode context as the incident cause:** disproved by reconstructing the relevant
  windows (5,813 characters total across the inspected windows).
- **Raw diff amplification:** none of the relevant previous episodes contained a diff. The separate
  unbounded stored-diff issue remains tracked elsewhere.
- **Narrative segmentation as the general fix:** it can reduce extracted-entity count for project
  ingest and may be useful operationally, but it does not bound candidate fan-out for other episode
  types. It is containment, not the root fix.
- **Raising or disabling the local ceiling:** this would move more deterministic failures to the
  provider without reducing request size.
- **Reducing the 15-candidate retrieval limit:** that would trade dedupe quality for capacity even
  on healthy requests. Adaptive splitting contains the request without reducing per-entity recall.
