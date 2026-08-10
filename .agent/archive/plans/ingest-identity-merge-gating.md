# Plan: judge-gated merges + identity receipts

> **ARCHIVED 2026-07-11 (ctharvey-approved).** "Pending review" resolved — verified landed and
> live on the enrichment path. The auto-merge defect is closed: `correlation_service._route`
> routes `>= merge_threshold` to `"merge_proposed"` (never auto-merges); Part 1 deterministic
> vetoes (`check_ineligible_node_veto`/`check_co_mention_veto`/`check_anchor_project_veto`),
> Part 2 judge-gated merge (`_handle_merge_proposal`, k=3 unanimous-yes, fail-safe conflict),
> Part 4 identity receipts (`record_mcp_event(operation="identity_decision", ...)`). Wired via
> `enrichment_steps.py:823` and `lifecycle_service.py:349,402`. Archived per owner rule (a).

**Status: IMPLEMENTED 2026-07-04 (pending review).**
The identity-resolution slice of the 2026-07-03 ingest gap review — the highest-urgency plan of
the review set, because the defect is **live and irreversible**: every day of ingest risks
unrecoverable entity merges. Design authority: `.agent/memory-ingest-under-uncertainty.md` §2
(reversibility monotone in corroboration), §5 (the escalation ladder);
`memory-aggregation-under-uncertainty.md` §6b (determinism proposes, the model judges, confidence
gates) and §7 (scalar confidence is anti-correlated with correctness at the tail).

## The defect
`correlation_service.py` auto-merges entities at cosine > 0.95 (`CORRELATION_MERGE_THRESHOLD`,
`_action_for`, `merge_entity`) with no judge, no veto, and no verified undo trail — while the
*less* confident 0.85–0.95 band gets LLM review. The irreversible action has the weakest gate,
and high cosine on short names is exactly the tail where distinct entities look identical
("UserService" / "UserServiceV2"). Multiplier: an unnamed node's correlation query falls back to
the **whole episode body** (`enrichment_steps.py:812`), so a spurious >0.95 match on episode text
can trigger a wrong merge of nodes that share no name at all.

## Part 1 — deterministic merge vetoes (run first, zero model calls)
Cheap exogenous corroborators that catch what the embedding cannot; each is **abstain-only**
(vetoes a merge down to the conflict path; never rescues one):
1. **Co-mention veto.** If both entities are MENTIONED by the same episode, the extractor saw
   them side-by-side and emitted them as distinct — they are almost certainly two things. Veto,
   downgrade to conflict-flag. This is the exact guard for the near-name-collision tail.
2. **Anchor-project veto.** Both entities structurally anchored, to different single projects →
   veto (a memory about project A is not a memory about project B, whatever the names embed as).
3. **Namespace scoping check (verify, likely fine).** Confirm the correlation candidate search is
   namespace-scoped; if it can propose cross-namespace pairs, that is a second live bug — fix by
   scoping the search, not by a veto.

## Part 2 — the judge gates the merge rung
Convert `"merged"` from an action into a **proposal**:
1. `check_correlation` yields `merge_proposed` for the >0.95 band; the merge executes only after
   Part 1 vetoes pass AND an LLM judge confirms. Judge prompt shows both nodes' names, summaries,
   and one provenance quote each, and asks the neutral question ("do these denote the same
   real-world entity?" — leak no prior); k=3, merge only on unanimous yes.
2. Non-unanimous or judge-unavailable → the conflict path (the 0.85–0.95 treatment): flagged for
   review, `RELATES_TO` in the meantime. **Fail-safe direction is flag, never merge** — an
   unavailable LLM must not restore auto-merge.
3. Confident-no → `RELATES_TO` + receipt (they are still near-duplicates lexically; the edge
   preserves the relatedness the score found without the destruction the score proposed).
4. Cost note: >0.95 proposals are rare; k=3 on a rare event is noise in the budget. The
   per-episode budget caps still apply.

## Part 3 — unmerge-sufficient audit trail
Before any confirmed merge executes, snapshot the absorbed node — uuid, properties, edge list
(with weights/kinds), MENTIONS set — as an audit artifact attached to the survivor
(`merge_audit` props or a sidecar record). Verify what `merge_entity` currently preserves; today
it is presumed nothing. This is the §2 floor: if the judge is ever wrong, the wrong is repairable.

## Part 4 — identity receipts
Record every correlation decision — band, similarity, action taken, vetoes fired, judge votes —
via `record_mcp_event(operation="identity_decision")` (telemetry sink; the ingest twin of
perception's abstention receipts and retrieval's reachability receipts). Two consumers:
1. A bench-side band-distribution report: how often each band fires, merge-proposal confirm/deny
   rates. The 0.70/0.85/0.95 thresholds are unvalidated magic numbers; receipts create the data
   that would justify (or correct) them. **No threshold changes in this plan** — §8 of the
   aggregation doc applies: the instrument comes first.
2. Merge-rate monitoring: a sudden spike in proposals or confirms is the early signal of an
   embedding regression.

## Part 5 — kill the episode-body fallback
An unnamed, contentless node **skips correlation** (`enrichment_steps.py:812`): no name means no
identity claim to resolve, and correlating on episode text produces exactly the spurious
high-similarity pairs Part 2 exists to stop. One conditional.

## Explicitly NOT in scope (decided, not forgotten)
- Retuning the band thresholds (receipts first; §8).
- Backfilling/unmerging historical merges — impossible without trails; Part 3 stops the bleeding
  from now on. A one-off count of past merge events (if logs allow) is a nice-to-have diagnostic.
- The conflict-review pipeline itself (exists; unchanged — this plan feeds it more, better cases).

## Verification
1. Unit (invented domains): co-mention veto blocks a >0.95 same-episode pair; anchor-project veto
   blocks cross-project; unanimous judge merges; split judge flags; judge-unavailable flags
   (never merges); unnamed node skips correlation; receipts carry band/vetoes/votes.
2. Merge audit: a confirmed merge leaves an artifact from which the absorbed node's uuid, props,
   and edges are fully recoverable (assert round-trip in the test).
3. One live ingest pass on a scratch namespace: identity receipts visible; zero unjudged merges.
