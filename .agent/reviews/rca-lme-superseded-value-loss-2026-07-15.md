# RCA: Superseded-value loss — updated fact captured correctly, prior value not retained

**Date:** 2026-07-15
**Severity:** Medium-High for `knowledge-update` questions that specifically ask about a *prior*
or *initial* state (as opposed to the current one). This is the mirror image of
`rca-lme-stale-fact-retention-2026-07-15.md`: that RCA is "old value kept, new value never
applied"; this one is "new value correctly applied, old value discarded" — and the LME question
happens to ask about the old value.
**Status:** One case confirmed via direct graph inspection. Same caveat as the companion RCA: n=1
confirmed, needs widening before being treated as systematic.

**Update (same day):** the companion RCA's investigation surfaced a third variant —
`2698e78f` (therapy frequency), where **neither** the old nor the new value survived (total loss on
a value-changing fact, not a clean "kept one, lost the other"). That case is documented in
`rca-lme-stale-fact-retention-2026-07-15.md` since it's closer in shape to that RCA's cases, but
it's really a third point on the same spectrum as this RCA and that one:
old-only (n=2) / new-only (n=1, this RCA) / neither (n=1). All three should be checked against the
same conflict-detection root cause once that code is traced — they may turn out to be one bug with
three visible failure shapes, not three separate bugs.

## Summary

menhir appears to correctly update a fact when new information arrives — but the LME question in
this case specifically asks "where did you *initially* keep X," i.e. it wants the **superseded**
value, not the current one. If superseded values aren't retained (or aren't retained in a form
`/api/recall`'s default query surfaces), the correct-and-current graph state is *useless* for this
question shape, independent of how good ranking or extraction is.

## Evidence

### `07741c44`
- **Question:** "Where do I **initially** keep my old sneakers?"
- **Gold answer:** "under my bed"
- **Direct graph search** (`MATCH (n:Entity) WHERE n.group_id='lme-07741c44' AND ... 'sneaker'`)
  found extensive, clearly current-state content:
  > `"shoe storage box": "...sneakers are stored in a shoe rack in the closet... The user plans to
  > store old sneakers in a shoe rack during closet organization... The user is planning to store
  > sneakers in a shoe rack."`

  This is the **updated** location (shoe rack), captured richly and correctly. No entity in the
  namespace mentions "under my bed" — the prior location the question actually asks about.

## Why this is a distinct failure mode from stale retention

In `rca-lme-stale-fact-retention-2026-07-15.md`'s case (`830ce83f`), the system kept the *wrong*
(old) answer and never captured the update — a conflict-detection / supersession-application
failure. Here, the system did the "right" thing by normal memory-system standards (track current
state, don't clutter with stale facts) — but the LME question is explicitly asking for history,
not current state. This means the fix is NOT "detect updates better" (already working here); it's
"decide whether/how superseded values remain queryable at all."

## Relevant existing mechanism: `include_superseded`

`RecallLabRequest` already has an `include_superseded: bool = False` field (see
`src/menhir/explorer/recall_lab.py`), meaning menhir's recall path is at least aware of a
superseded/current distinction and has a flag to include superseded content. This RCA did **not**
test whether re-running `07741c44` with `include_superseded=True` surfaces the "under my bed" fact
— that is the single highest-value next check, since if the data is retained but excluded by
default, the fix may be as narrow as detecting "initially"/"originally"/"before"-shaped questions
and setting that flag, rather than any change to extraction or supersession logic at all.

## Recommended fix direction (not implemented)

1. **Immediate, cheap verification:** re-run `07741c44` (and the wider `knowledge-update` "no"
   sample from the widened investigation) through `/api/recall` or Recall Lab with
   `include_superseded=true`. Three possible outcomes, each pointing at a different fix:
   - The "under my bed" fact appears → this is a **query-shaping** problem only (default recall
     excludes superseded content; the harness/production caller needs a way to detect
     history-asking questions and request it). No graph/extraction change needed.
   - Nothing appears even with superseded content included → the prior value was genuinely
     **never retained** at all when it was superseded (deleted or overwritten, not archived) —
     this is a real data-retention gap requiring a change to how updates are applied.
   - Some superseded content appears but not this specific fact → a **partial** retention gap,
     narrower than full deletion but still needs investigation into what determines which
     superseded facts survive.
2. Only after (1) is known: decide whether the fix belongs in `recall_service`
   (query-time: auto-detect "initially/originally/before" phrasing and include superseded state)
   or in the write/consolidation path (retention: keep superseded facts queryable by default,
   change what "supersede" means operationally).

## Verification plan

- Direct test: call `/api/recall` (or Recall Lab) for `07741c44` with `include_superseded=true`,
  `include_invalidated=true`, and check whether "under my bed" appears anywhere in the expanded
  result set. This single test resolves most of the ambiguity in this RCA and should be done before
  any other work on this pattern.
- If retained: audit how many `knowledge-update` misses in the full run are actually
  history-asking questions (look for "initially," "originally," "before," "used to," "previously"
  in the question text) vs. current-state questions — these need different fixes and are currently
  conflated inside one question type.

## Related

- `.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md` — the opposite-direction failure in
  the same question type.
- `.agent/reviews/rca-lme-extraction-admission-gap-2026-07-15.md` — if the `include_superseded`
  test comes back empty, this case converges with that RCA (fact never retained anywhere,
  regardless of query flags).
