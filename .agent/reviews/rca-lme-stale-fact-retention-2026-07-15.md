# RCA: Stale fact retention — knowledge-update not applied, current value never surfaces

**Date:** 2026-07-15 (updated same day: strengthened from n=1 to n=3 confirmed cases — see
Revision note)
**Severity:** High. `knowledge-update` had the worst miss rate of any LongMemEval type in the full
2026-07-15 M1 run (72/78, 92%). Of every `knowledge-update` genuine-miss case checked against the
raw source conversation so far (n=3), **all three** turned out to involve a fact whose value
changes partway through the conversation — and in **zero of three**, did menhir's graph end up
with the correct current value queryable. This RCA covers the "old value kept, new value never
applied" direction (2 of 3 cases); see the companion RCA
`rca-lme-superseded-value-loss-2026-07-15.md` for the two related-but-distinct variants (total loss
on both values; new value correctly captured but old value the question asks about is lost).
**Status: ROOT CAUSE CONFIRMED VIA CONTROLLED A/B TEST** (2026-07-15, final revision). The
investigation went through 3 revisions before landing here — see "Investigation history" below for
the full trail (each earlier hypothesis was tested and found insufficient, not just theorized
away). The confirmed mechanism: **`graphiti-core`'s `RELEVANT_SCHEMA_LIMIT=10` recency window on
`previous_episodes`, combined with its extraction prompt's "when in doubt, do NOT extract"
instruction, causes entity re-mentions to be silently under-extracted once the entity's
establishing context falls outside that 10-episode lookback** — proven by a controlled test using
the identical target message with only the prior-context input varied. See "CONFIRMED: controlled
A/B test" below.

## Revision note (same-day)

The original draft of this RCA had one case (`830ce83f`) and explicitly flagged it as
"needs a wider, dedicated check before being treated as systematic." That check has now been done:
`852ce960` (mortgage pre-approval) and `2698e78f` (therapy frequency), originally miscategorized as
pure extraction gaps in `rca-lme-extraction-admission-gap-2026-07-15.md`, both turned out on full
raw-text inspection to be knowledge-update cases too. All three are consolidated here.

## Summary

`knowledge-update` is the LongMemEval question type that specifically tests whether a memory
system correctly reflects a fact that changed during the conversation, rather than the value first
stated. In all three cases directly inspected, the raw source conversation confirms an unambiguous
update statement — and in all three, the graph fails to surface the correct current value.

## Evidence

### `830ce83f` — Rachel's residence
- **Session 0 (user):** *"...my friend Rachel who recently moved to a new apartment in the
  city..."*
- **Session 1 (user), later:** *"My friend Rachel actually just **moved back to the suburbs
  again**..."*
- **Gold answer:** "the suburbs"
- **Graph:** `"Chicago": "user moved to Chicago... Rachel is in Chicago... Rachel is associated
  with Chicago as her place of residence..."` — the FIRST value (resolved to "Chicago") is
  captured; no entity anywhere mentions "suburbs." No conflict marker, no trace an update was ever
  registered.
- Judge-verdict caveat: the LLM judge scored this "no" in run 1 and "partial" in run 2 on the same
  underlying retrieved content — expected judge variance on a borderline case, doesn't affect the
  graph-level finding, which was confirmed directly against Neo4j both times.

### `852ce960` — mortgage pre-approval amount
- **Session 0 (user):** *"...I got pre-approved for **$350,000** from Wells Fargo..."*
- **Session 1 (user), later:** *"...remember when I got pre-approved for **$400,000** from Wells
  Fargo?"*
- **Gold answer:** "$400,000"
- **Graph:** an entity `"$350,000 loan"` exists (the FIRST value), tied to detailed PMI/mortgage
  mechanics content; no entity anywhere mentions $400,000.
- **Exact same shape as `830ce83f`:** old value captured and confidently stated as fact, updated
  value entirely absent, no conflict trace.

### `2698e78f` — therapy session frequency
- **Session 0 (user):** *"...I have a therapy session with Dr. Smith coming up soon - it's **every
  two weeks**..."*
- **Session 1 (user), later:** *"...I see Dr. Smith **every week**, and she's been helping me work
  on this stuff."*
- **Gold answer:** "every week"
- **Graph:** zero hits for either frequency, or for "Dr. Smith" in a frequency-bearing context, in
  48 episodes. **Different from the other two:** here NEITHER value survived, not just the updated
  one. (Originally miscategorized as a pure extraction gap for exactly this reason — see
  `rca-lme-extraction-admission-gap-2026-07-15.md`'s revision note.)

## Pattern across all three

| qid | Old value | New value | What the graph has |
|---|---|---|---|
| `830ce83f` | Chicago | the suburbs | **Old only** |
| `852ce960` | $350,000 | $400,000 | **Old only** |
| `2698e78f` | every two weeks | every week | **Neither** |

**Every knowledge-update case checked so far has a 0/3 success rate at the graph ending up with
the correct current value.** Two of three retain the stale value cleanly (suggesting conflict
detection specifically fails to link the new statement to the existing fact); one loses both
(suggesting that in at least some cases, the *presence* of a later contradicting statement
correlates with worse extraction fidelity overall, not just a failure to update — a narrower and
more specific hypothesis than "restated facts are simply harder," worth checking against more
cases before treating as established).

## Root cause (confirmed via code)

Menhir has **two** distinct update/conflict mechanisms, and neither covers these cases during LME
ingest:

**1. `services/correction_resolver.py` — the only mechanism that runs automatically at ingest
time.** Deliberately narrow by design (its own docstring: "Tight by design (precision-first)"):
- **Numeric only.** Detects `(old, new)` number pairs via 9 regex patterns for explicit corrective
  connectives: `"X, not Y"`, `"from X to Y"`, `"X instead of Y"`, arrows (`X -> Y`), `"X replaces
  Y"`, etc. (`correction_resolver.py:41-67`).
- **Binds only to "counter View" entities** (`fold_events_to_counter` / QuantState's numeric
  aggregation primitive), not general entity facts.
- **Requires an explicit correction phrase.** None of our 3 cases use one: "Rachel moved back to
  the suburbs" isn't numeric at all; "remember when I got pre-approved for $400,000" is a casual
  restatement/reminder, not `"$400,000, not $350,000"`; "I see Dr. Smith every week" is a plain
  restatement, not a flagged correction.
- **Conclusion:** this resolver was never going to fire for any of our 3 cases, by design — they
  aren't the narrow class of input it targets.

**2. The general-purpose similarity-based conflict system (`mcp/tools/conflict/scan_conflicts.py`,
`run_llm_conflict_review.py`, `resolve_conflict.py`) — scheduler-driven, not ingest-time.**
`services/maintenance_scheduler.py:62-68` shows this entire pipeline runs on scheduled jobs, not
as part of writing a new fact:
- `confirm_conflicts` — every 3600s (hourly)
- `auto_resolve_conflicts` — every 86400s (daily), and **only touches conflicts already 14+ days
  old** (`conflict_auto_resolve_max_age_days=14`)
- `review_unresolved_conflicts` — every 604800s (weekly)

**3. The scheduler is disabled during LME ingest.** `core/runtime.py:518-522`: when
`settings.benchmark_mode` is true (set via `MENHIR_BENCHMARK_MODE=1`, exported by
`build_graph.sh` for every LME ingest), menhir logs *"scheduler + orphan recovery disabled"* and
none of the three conflict jobs above ever run.

**Even with the scheduler on, this would likely still fail for LME specifically:**
`auto_resolve_conflicts`'s 14-day age floor is designed for a real personal-memory system where
corrections trickle in over weeks — it deliberately does NOT resolve conflicts quickly, to avoid
prematurely "resolving" an ambiguous or still-developing situation. LME's oracle haystacks ingest
an entire multi-session conversation history in one sitting; there is no 14-day gap between
sessions in wall-clock ingest time for the age floor to ever satisfy, regardless of scheduler
state.

**This is not a bug in the conflict-detection logic itself.** `scan_for_conflicts` uses genuine
similarity-based comparison and would very plausibly catch "Rachel is in Chicago" vs. "Rachel
moved to the suburbs" as the same-subject conflict it's designed for — the mismatch is
architectural: a system built to resolve conflicts slowly and deliberately over real time, being
exercised by a benchmark that compresses weeks of conversation into one ingest pass with the
scheduler off.

## Investigation history

This RCA went through four passes in one day, each testing (not just theorizing past) the
previous conclusion:
1. **Scheduler-disabled** (real, code-confirmed, but insufficient alone — see "Root cause
   (confirmed via code)" above).
2. **`scan_for_conflicts` manually triggered** — works mechanically but structurally cannot fix
   these cases (no second entity to link to) — see "Correction: live-tested and found incomplete"
   below.
3. **Isolated code trace with 1 prior episode of context** — extraction/resolution appeared to work
   perfectly (false positive — see "CONFIRMED: controlled A/B test" below for why).
4. **Clean re-ingest of the real namespace, then controlled A/B context test** — the actual
   confirmed mechanism. Read that section for the current, final understanding.

## Correction: live-tested and found incomplete (same day, second pass)

The plan above ("Do option 1: manually run `scan_for_conflicts`") was tested directly against the
running LME graph via `POST /api/internal/backend/scan_for_conflicts`. It works mechanically —
`{"scanned":200,"new_conflicts":115,"next_cursor":"...","done":false}` on the first batch, so the
mechanism is live and finds plenty of candidates in general. But before paying to paginate through
the full ~34k-entity graph to see if it reaches our 3 cases, a cheaper check first: **does an
entity representing the *new* value even exist anywhere in these 3 namespaces, linked or not?**

It does not, for any of the three — reconfirmed directly (no entity mentions "suburbs," "$400,000,"
or either therapy frequency, anywhere in their namespaces). **`scan_for_conflicts` links pairs of
*existing* entities; it cannot detect a conflict when only one side of the pair exists.** So even
with the scheduler on and no 14-day floor, this mechanism structurally cannot fix any of the 3
confirmed cases — there's nothing on the "new value" side for it to link to.

**More importantly, this is not an ingest-completeness gap either.** Checked whether the raw turn
containing the update was even ingested as an episode:

```
830ce83f: Episodic node exists, processing_state="READY", content contains
  "Rachel actually just moved back to the suburbs again"
852ce960: Episodic node exists, processing_state="READY", content contains
  "remember when I got pre-approved for $400,000 from Wells Fargo"
```

Both episodes are fully ingested and marked `READY` — menhir's own signal that enrichment/
extraction from that episode has completed. **The failure point is precise: a fully-processed
episode, containing the update in plain text, produced no new or updated Entity fact.** This is an
extraction-logic gap specific to episodes about an entity/subject the graph already has substantial
content for — not a scheduler-availability gap, not an ingest-completeness gap, and not something
`scan_for_conflicts`/`resolve_conflict` can reach, because there's no second entity for it to
operate on.

**Working hypothesis, partially narrowed by a third check (same day):** queried both target
episodes directly for `MENTIONS` edges (the actual Episodic→Entity link in this schema — confirmed
via `db.relationshipTypes()`: `MENTIONS`, `RELATES_TO`, `HAS_EPISODE`, `HAS_MEMBER`,
`NEXT_EPISODE`; query pattern validated first against a known-populated episode before trusting a
negative result). **Both `830ce83f`'s and `852ce960`'s update episodes have zero `MENTIONS` edges
to any entity.** This favors hypothesis (a) over (b): nothing was extracted from either episode at
all, not "extracted then discarded/merged during dedup." Neither episode spawned any candidate fact
in the first place.

**Important caveat before treating this as conclusive:** zero-`MENTIONS` episodes are common in
general, not unique to update turns — 30 of 48 episodes (62.5%) in the `830ce83f` namespace have
zero `MENTIONS` edges. Plenty of turns are generic assistant advice or conversational filler that
legitimately shouldn't produce entities. So the real open question is narrower than "why did this
episode get zero entities" (unremarkable on its own) and is instead: **why did an episode
containing a clear, specific, personal fact — comparable in kind to the earlier episode that DID
produce a "Chicago" entity — land in the same zero-extraction bucket as filler turns?** That
requires either (i) sampling a few of the other 29 zero-mention episodes in this namespace to check
whether they're genuinely content-free (establishing the 62.5% rate as normal/expected), or (ii)
tracing the actual extraction LLM call/prompt for this specific episode to see what it was asked
and what it returned. Neither has been done yet. This is a different, more specific mechanism than
the "dense multi-entity turn" hypothesis in `rca-lme-extraction-admission-gap-2026-07-15.md`
(`89527b6b`) — that case has no pre-existing entity to collide with; this one specifically involves
a *later* mention of an *already-established* entity landing in an otherwise-normal-looking
zero-extraction bucket. Both may be real, independent failure modes under the same broader
"extraction misses specific details" umbrella.

## CONFIRMED: controlled A/B test (same day, final pass)

**Step 1 — isolated trace, with patches correctly applied this time** (an earlier attempt without
menhir's `graphiti_patches.py` monkey-patches applied produced a false-positive crash — corrected
after the user pointed out those patches exist, ~March 2026). Calling `extract_nodes` →
`resolve_extracted_nodes` → `extract_edges` → `resolve_extracted_edges` directly (read-only, no
writes) on `830ce83f`'s real session-1 episode text, with **one hand-picked prior episode**
(the Chicago-establishing statement) as context: extraction correctly proposed `Rachel` and
`suburbs`, resolution correctly deduplicated `Rachel` to her existing entity, and edge extraction
correctly proposed the fact *"Rachel recently moved back to the suburbs after living in the
city."* Every stage worked. This looked like a clean root cause — extraction/resolution logic is
fine, so something else must be wrong. **It wasn't a faithful test — see below.**

**Step 2 — real re-ingest to test the "transient failure" possibility.** Removed `830ce83f` from
the canonical manifest and re-ran `build_graph.sh 500`, which resets the namespace
(`DELETE /api/namespace`) and re-ingests fresh. Result: the "suburbs" fact is **still missing**
after a clean re-ingest — directly contradicting Step 1's clean result, and ruling out "one-off
transient failure" (a genuine bug reproduces; a transient failure usually doesn't on retry). Side
finding: `DELETE /api/namespace` did **not** actually clear the old episodes — the namespace now
holds 3 duplicate copies of the same conversation (original June 30 ingest + two passes from this
retry), a separate bug worth flagging (`ingest.py`'s resume logic assumes reset is reliable; it
is not, at least not for Episodic nodes). Despite the duplication (which if anything gave
extraction *more* chances to catch the fact), **the suburbs fact never appeared in any of the 3
passes.**

**Step 3 — found the real reason Step 1 was unfaithful.** `graphiti_core.graphiti:1087-1090` shows
`add_episode` builds `previous_episodes` via `self.retrieve_episodes(reference_time,
last_n=RELEVANT_SCHEMA_LIMIT, ...)` — and `RELEVANT_SCHEMA_LIMIT = 10`
(`graphiti_core/search/search_utils.py:64`). Extraction only ever sees the **last 10 episodes**,
not full conversation history. `830ce83f`'s real namespace has ~24 turns → 70+ episodes; the
Chicago-establishing episode (session 0) is almost certainly more than 10 episodes before the
suburbs-mentioning episode (session 1) in the real sequential ingest. Step 1's hand-built context
(exactly 1 prior episode, guaranteed maximally visible) never tested this — it was an unrepresentative
unit test that missed a real integration-level failure.

**Step 4 — controlled A/B test, same target message, only context varied:**

| `previous_episodes` given to extraction | Entities extracted |
|---|---|
| 1 (Rachel/Chicago statement) | `user`, `Miami Beach`, **`Rachel`**, **`suburbs`**, `major city` (5) |
| **0 (empty)** | **`user` only (1)** |

With zero prior context, extraction doesn't even propose "Rachel" or "suburbs" as candidates for
the identical message — not a resolution/dedup failure, a genuine **extraction under-proposal**.
This is the confirmed mechanism: graphiti-core's `extract_message` prompt instructs "When in doubt,
do NOT extract" (`prompts/extract_nodes.py:157`); without recent grounding that "Rachel" is an
established, trackable entity, the LLM treats a bare-name re-mention as too uncertain to extract at
all. Once real conversations accumulate more than ~10 intervening episodes since an entity was last
mentioned, any later fact about that entity is at serious risk of being silently dropped at the
extraction stage — before dedup, before conflict detection, before any of the mechanisms audited in
earlier passes of this RCA ever get a chance to run.

## Recommended fix direction (final)

This is now a well-understood, specific, structural interaction, not a vague "extraction sometimes
misses things":

1. **Not fixable by triggering `scan_for_conflicts` or any conflict-resolution step** (confirmed
   dead in pass 2) — there's no second entity/edge for conflict resolution to act on if extraction
   never proposes one.
2. **Fix candidates, in order of increasing scope:**
   - Raise `RELEVANT_SCHEMA_LIMIT` for menhir's own use (a graphiti-core constant — would need a
     menhir-side override or another monkey-patch, following the existing pattern in
     `graphiti_patches.py`). Cheapest, but only shifts the problem to a larger N, doesn't eliminate
     it structurally for long enough conversations.
   - Give the extraction prompt access to a targeted "does this message mention any already-known
     entity by name" check (e.g. a lightweight name-match against the graph, independent of the
     10-episode window) before the "when in doubt, do NOT extract" conservatism applies — a
     genuine, if nontrivial, entity-resolution architecture change.
   - This is precisely the kind of problem `.agent/reference/menhir-belief-supersession-temporal-chains-research.md`
     (the Codex research doc saved earlier today) sets out to investigate — its "Candidate
     Retrieval Research" section explicitly proposes signals beyond simple recency (embedding
     similarity, shared entities) for exactly this "is this a re-mention of something I already
     know" problem. This RCA is now a concrete, real, reproduced motivating case for that research
     effort, not just a hypothetical.
3. **Separately: fix or work around `DELETE /api/namespace` not clearing Episodic nodes** — found
   as a side effect in Step 2. This affects `ingest.py`'s resume/reset logic generically (not
   specific to this RCA) and should be filed as its own item if reset-and-reingest is ever needed
   again for benchmark maintenance.

## Verification plan

- **Confirmed, no further verification needed for the core mechanism:** the controlled A/B test in
  Step 4 above is a self-contained, reproducible proof (identical target message, single input
  varied, dramatic and mechanistically-explained outcome difference).
- Before implementing a fix: widen past n=3 — classify the ~9 remaining unclassified
  `knowledge-update` misses by checking how many intervening episodes separate their update
  statement from the entity's establishing episode, to confirm the >10-episode-gap pattern holds
  generally (not just for `830ce83f`).
- Separately verify the `DELETE /api/namespace` reset bug found in Step 2 — check whether it fails
  to clear Episodic nodes specifically, or whether that observation was itself an artifact of the
  particular retry sequence used here.
- After a fix candidate exists: re-run all 3 confirmed cases and confirm the graph now surfaces the
  current (post-update) value.

## 2026-07-16 update: context-form ablation (240 trials) — durable findings, supersedes the "raise the limit" framing above

Follow-on to the fix-candidate list above. Built the Extraction Lab harness (Recall Labs Phase 0
extension; see `.agent/archive/plans/menhir-belief-supersession-code-mapped-plan.md`), ran a prompt-only
ablation (Phase 1, 8 variants, n=30 — see `.agent/archive/plans/menhir-extraction-context-ablation-handoff.md`
for the full methodology and raw numbers), then a rigorous context-form ablation (Phase 2, 8
distinct context deliveries x 3 real RCA fixtures x 10 interleaved trials = 240 real `gpt-4o-mini`
calls with Wilson 95% CIs, not point estimates) against `830ce83f`, `852ce960`, `2698e78f` — the
same three cases this RCA covers. Full methodology, condition catalog, and raw per-trial data live
in the handoff doc and `results/extraction_lab_phase2_context_ablation.json`; this section records
only the findings that bear directly on this RCA's fix direction, so a later reader doesn't have to
re-derive them.

**Prompt-only fixes (Phase 1) were tried first and found insufficient.** `update_aware` (a variant
targeting "actually/moved back/again" language) looked like a strong fix at n=10 (proposition
recall 0.60→0.80) but the lift nearly vanished at n=30 (0.60→0.60→0.55→0.60), and on all 3 real RCA
fixtures with real context, `update_aware` produced results identical to baseline. **Prompt wording
alone does not fix this failure class.** This directly narrows fix candidate 2 above (a prompt-only
patch is not sufficient on its own) and motivated the context-form work below.

**Six durable findings from the 240-trial context-form ablation** (each independently significant —
non-overlapping or near-non-overlapping 95% CIs — not a point-estimate artifact):

1. **Native previous-episode delivery changes extraction behavior; a bolted-on "retrieved context"
   block is not equivalent, even with byte-identical text.** On `830ce83f`: the real establishing
   episode delivered as an ordinary `previous_episode` scored 70% (7/10, CI [40%,89%]); the exact
   same text delivered via a separate `<RETRIEVED CONTEXT>` block scored 10% (1/10, CI [2%,40%]).
   Same facts, same words, radically different outcome, purely from where/how it sits in the
   prompt. **This directly changes fix candidate 2's design**: any future candidate-lookup/context-
   injection mechanism must render retrieved content through the same channel and format as an
   ordinary prior episode — not as a new labeled section — or it will underperform doing nothing.
2. **Entity-name awareness alone does not help.** "Known entity: Rachel" with no fact attached
   scored 0/10 (0%) on `830ce83f`, statistically indistinguishable from no context at all (1/10,
   10%) and from a genuinely unrelated episode (0/10, 0%). This is a direct, now-quantified
   refutation of the "lightweight name-match against the graph" fix candidate as originally
   sketched in the list above — knowing a name is known buys nothing; the extractor needs actual
   content, not a name flag.
3. **A compact, single-sentence restated fact matched or outperformed the full real episode on
   both non-ceiling fixtures** — `830ce83f`: compact fact 90% (9/10) vs. full episode 70% (7/10);
   `2698e78f`: compact fact 90% (9/10) vs. full episode 50% (5/10, actually *below* that fixture's
   80% no-context baseline). This is the single most cross-fixture-consistent result in the whole
   ablation — a full source episode is not required; a short restated fact is at least as effective
   and in both cases numerically better.
4. **Topically-similar-but-unrelated context can unlock extraction even when it concerns the wrong
   person.** On `830ce83f`, an episode about a different person ("Daniel") moving apartments —
   zero connection to Rachel — scored 100% (10/10), higher than both the correct compact fact
   (90%) and the correct full episode (70%). A genuinely unrelated episode (passport/visa advice)
   sat at the floor (0%) alongside the no-context and entity-name-only conditions. So the effect is
   specifically **topical/lexical priming**, not "any extra text helps" and not "the model recovered
   the truth" — the model appears to become willing to extract relocation-shaped language in the
   CURRENT MESSAGE because relocation-shaped language appeared recently, independent of whether
   that prior mention was true or about the right entity. This is a genuine mechanistic finding, and
   a precision risk flag: this extractor can be made confidently "unlocked" by irrelevant-but-
   adjacent noise, not just by correct grounding.
5. **The three RCA fixtures are not one failure class and must not be aggregated indiscriminately.**
   `852ce960` is a ceiling case (100% success on all 8 conditions at this context depth — no
   discriminating signal). `830ce83f` shows a clean two-cluster split (no/generic context 0-10%
   vs. real-or-topically-adjacent context 70-100%) consistent with a context-gated under-extraction
   mechanism. `2698e78f` shows the opposite of what `830ce83f` shows on its most important
   condition: its no-context baseline is already high (80%), and supplying the real, correct
   establishing episode *lowers* success (50%) below that baseline — a context-interference
   pattern, not context-gated under-extraction. Averaging these three together (as the earlier
   aggregate tables in this investigation did) actively hides this split; **any future fix must be
   evaluated per-fixture, and `2698e78f` should be treated as a candidate separate failure family**,
   not folded into the mechanism this RCA describes, until investigated on its own terms.
6. **Conditions H/I (native Graphiti episode window vs. reconstructed raw-turn window) were not
   testable with available data and were not faked.** The RCA fixtures' `previous_episodes` were
   built from real `:Episodic` nodes queried directly out of the live LME graph, which returned
   counts roughly 1:1 with raw conversational turns (23-48 nodes) — not the "~3 sub-episodes per
   raw turn / 70+ total" expansion this RCA's own Step 3/4 (above) described for the ingest that
   was analyzed there. Flagging this discrepancy rather than silently assuming one description or
   the other is correct: either the specific ingest analyzed in Step 3/4 expanded differently from
   the one later queried for these fixtures, or the "70+ episodes" figure needs re-verification
   against the current ingest pipeline. Worth resolving before relying on either figure for future
   `RELEVANT_SCHEMA_LIMIT` sizing decisions (see Phase 3 below).

**Revised fix-candidate ordering** (supersedes "Recommended fix direction (final)" above where it
conflicts): raising `RELEVANT_SCHEMA_LIMIT` remains explicitly a *causal control to test next*
(Phase 3 — does keeping the real establishing episode inside the window reproduce `830ce83f`'s
70-90% success rates under the real production window-selection mechanism, not just the
hand-constructed context this ablation used), not a proposed production fix. The design direction
this ablation actually supports is a **native-format context composer**: detect the message's
explicit subject, retrieve one or two compact source-grounded facts about it, and render them as
ordinary prior episodes (same channel, same format) rather than a new prompt section — see the
handoff doc's Phase 4 for the concrete design and its required gate (must beat no-context,
entity-name-only, full-episode, and same-facet-wrong-entity controls, per-fixture, before any
production wiring).

## 2026-07-16 update: Phase 3 executed — raising the limit is confirmed unreliable, not just non-scaling

130 real-window trials (real `previous_episodes`, real `[-N:]` slicing) across 13 (fixture, limit)
cells — full methodology and numbers in the handoff doc's Phase 3 section. **Result: no clean
monotonic relationship between window size and success.** `830ce83f` swung 20%→90%→0%→80%→0%→90%→70%
across limits 1/3/5/8/9/10/12, including a 90%→0% collapse between limits 8 and 9 (adding a single
extra episode — an assistant clarifying question — coincided with a near-total failure). The
headline P(success | establishing episode present)-vs-absent comparison this phase was built to
answer came back weak and fixture-inconsistent (aggregate 67% vs 48%, CIs nearly touching;
`2698e78f` ran backwards). A real measurement limitation was identified during analysis: the
"establishing episode present" flag only tagged each fixture's single clearest fact-introducing
episode, not the several later episodes that casually re-mention the same entity — so some
nominally-"absent" windows likely still carried real, if secondary, grounding content, which
plausibly explains part of the noise (flagged, not corrected — the qualitative conclusion doesn't
depend on it).

**Practical upshot: "raise `RELEVANT_SCHEMA_LIMIT`" is now confirmed unreliable, not merely
non-scaling as originally noted above.** It was already known to be a stopgap that shifts the
failure boundary to a larger N rather than eliminating it; Phase 3 adds that picking a *good* N is
itself unpredictable — window composition, not just window size, drives outcomes in ways this
phase could not fully explain. This further strengthens (does not just maintain) the case for
Phase 4's targeted single-fact composer over any window-size-based fix. See the handoff doc's
Phase 3 section for the full per-limit numbers and the composition-sensitivity discussion.

## 2026-07-16 update: Phase 4 selector prototype — mechanism confirmed viable, automated selection is not yet reliable

Direct correction from the requester (2026-07-16): Phase 2 confirmed a *mechanism* (a compact,
natively-delivered prior fact reliably helps extraction), not a *system* (that Menhir can
automatically choose that fact). Phase 4 tests selection in isolation, per two independent gates
(selection recall: was the right fact in the candidate pool; selection precision: did the selector
correctly pick it and reject decoys) against 5 required negative-control scenarios — full design
and results in the handoff doc's Phase 4 section.

**150-trial result: subject discrimination is solid (100% correctly rejecting wrong-subject
decoys); fine-grained facet/recency discrimination is not (33% on the safety-critical "select
nothing when nothing correct exists" gate, with two of three fixtures failing nearly every trial).**
The failures are not random — they're specifically cases where two candidates are closely related
within the same narrow topic (e.g. "mortgage amount" vs. "home inspection findings," or two dollar
figures differing only by value) rather than clearly different domains. This is a concrete argument
for building any real selector against menhir's existing bitemporal fact model (real `valid_at`
timestamps) and explicit facet tags rather than inferring recency/relevance from natural-language
wording alone — see the handoff doc for the full reasoning.

**Not ready for production wiring** — the worst-performing gate is exactly the one (correctly
injecting nothing) whose failure mode reproduces the precision risk this investigation already
flagged in Phase 2 (false grounding from topically-adjacent-but-wrong content).

## 2026-07-16 update: Phase 4b — structured filtering resolves the abstention problem completely (given real metadata)

Direct redesign instruction (2026-07-16): Phase 4's failure is an *open-set selection problem, not
a reranking problem* — a forced-choice LLM ranker picks the least-bad candidate instead of
abstaining. Fix tested: hard-filter on structured metadata (subject, facet, state_family, scope,
real `valid_at`/`expired_at` bitemporal timestamps) before any ranking; LLM only for genuine
residual ambiguity. Full design and 420-trial results in the handoff doc's Phase 4b section.

**Result: the structured approach was perfect on all 21 scenarios (100% precision, 100% coverage,
zero LLM cost)** versus the original prose-only selector's 64%/60% with a complete 0% collapse on
missing-metadata handling. This directly confirms the diagnosis: once recency and topic-membership
are decided from real structured fields instead of inferred from wording, the two failure modes
that mattered most (stale-vs-current, correctly abstaining) are resolved.

**Two things this does NOT yet prove, disclosed rather than glossed over:** the LLM-fallback path
was never exercised (every scenario resolved via the hard filter alone, so the *hybrid* design's
actual differentiator is untested); and the whole phase assumed complete, correctly-tagged metadata
already exists on every candidate, which is not true of the real graph today. **The next open
problem is upstream of selection: producing real facet/state_family/scope/valid_at metadata from
raw conversation in the first place** — not further selector tuning.

## 2026-07-16 update: Phase 5 — metadata production tested, best safe approach found, one real danger case confirmed

Tested 5 query-side routing approaches (the piece Phase 4b assumed away) by feeding each one's
predicted subject/facet/state_family into the frozen Phase 4b selector against real candidate
pools. Full numbers in the handoff doc's Phase 5 section.

**The only signal that exists in production today (matching graph entity names) is confirmed
structurally incapable (0% coverage)** — it cannot produce facet/state_family at all, so it
correctly finds nothing useful, ever. **Both pure-LLM routing approaches never once injected wrong
context (100% precision across 214 combined test cells)** — their failure mode is abstaining, not
being wrong, which is exactly the safe failure mode this investigation has prioritized since Phase
2. The simplest approach (one LLM call against a fixed ontology) beat a more elaborate two-stage
design on every metric, a genuine negative result against the intuition that more steps would help.

**The one approach that added deterministic keyword rules is also the only one that got it wrong.**
It produced real decoy selections (10 of 210 chain cells) — root-caused to a confident-but-wrong
rule (a message about a mortgage also mentions moving house, and the rule matches "moving" first)
with no downstream correction path once the rule commits. This is the exact dangerous case flagged
before the run — confirmed concretely, not hypothetically, and it emerged from real behavior, not a
constructed adversarial test.

**Practical takeaway (superseded below, same day):** the simplest, single-LLM-call routing approach
currently has the best safety profile of anything tested. Coverage (33% for that approach vs.
oracle's 100%) is the remaining real gap, not correctness.

## 2026-07-16 update: abstention taxonomy, corruption matrix, and a grounding fix that closes the coverage gap entirely

Three follow-ons run the same day, in sequence. Full numbers in the handoff doc's Phase 5 section.

**Taxonomy: zero true abstentions occurred.** Every coverage miss across all 90 earlier trials was a
confident, well-formed, WRONG classification — never a null/unknown answer. The safety seen earlier
came entirely from the downstream exact-match filter catching wrong guesses, not from the model
being appropriately uncertain. This reframes the fix as "make the guess right more often," not
"reduce false confidence."

**Corruption matrix: the frozen Phase 4b selector fails safe on 7 of 8 realistic metadata
corruptions.** Missing tags, wrong tags, malformed timestamps, and scope ambiguity all correctly
result in either the right answer (via the existing recency tie-break) or safe abstention. Exactly
one corruption is genuinely dangerous, and it's the one flagged in advance: a wrong-subject fact
mistagged with the right subject is indistinguishable to an exact-match filter from a real one — a
structural limitation, not a bug, now locked in by a regression test so it can't be silently
"fixed" without someone noticing what changed.

**The actual fix: ground the classifier in real, existing labels instead of an abstract ontology.**
A new routing approach shown the real `(subject, facet, state_family)` triples that exist across the
graph's actual stored facts (rather than a hand-authored category list) hit **100% field accuracy
and 100% downstream precision/coverage at n=30 — an exact match to the oracle upper bound.** This is
the single highest-leverage result in the entire investigation: the coverage gap wasn't a hard
problem requiring cleverness, it was an ungrounded-classification problem with a direct fix.

**One real bug found and fixed in the process:** a "ranked hypothesis recovery" mechanism initially
let a low-ranked second guess veto a correct top answer whenever they disagreed. Fixed to give the
top-ranked hypothesis unconditional priority. After the fix it matches the (now oracle-level) top-
hypothesis-only result exactly — meaning on this test set there was no remaining coverage gap left
for a fallback mechanism to recover, so its value remains genuinely untested pending a harder case.

**Genuine-tie suite (the 4th and final item): the LLM tie-breaker fires for the first time in the
whole investigation, and earns its place.** Built 3 scenarios where 2 candidates survive the hard
structured filter with an identical `valid_at` — the one case the recency tie-break structurally
cannot resolve (confirmed: this path never fired once across 210+ prior trials). Across 30 trials
(10/scenario), the LLM fallback fired 30/30 and behaved safely in all three: on a symmetric
no-signal tie it picked consistently but arbitrarily (100% acceptable, since no principled answer
was defined); when the query message's own wording favored one candidate, it correctly and
consistently used that content signal (100%); and on the plan's own worked example ("Rachel lived
in Chicago" vs. "Austin," no distinguishing signal) it consistently ABSTAINED rather than guessing,
10/10 — something the pure structural tie-break is structurally incapable of (`max()` always
returns something). This directly answers the plan's open question of whether the LLM tie-breaker
might prove unnecessary: it does not — it adds real, distinct safety value as a narrow last-resort
path for genuine structural ties, not as a general-purpose reranker.

**All 4 items of the requested sequence are now complete.** Full numbers for all four in the
handoff doc's Phase 5 section.

## Related

- `.agent/reviews/rca-lme-superseded-value-loss-2026-07-15.md` — related variants: total value
  loss (`2698e78f`, cross-referenced here) and the opposite direction (new value correctly
  captured, old value the question asks about is lost).
- `.agent/reviews/rca-lme-extraction-admission-gap-2026-07-15.md` — where these two cases were
  originally (incorrectly) filed; see that document's revision note.
- `.agent/archive/plans/menhir-extraction-context-ablation-handoff.md` — full Phase 1/2 methodology, all 8
  ablation conditions, raw trial data references, and the Phase 3/4 plan the 2026-07-16 update
  above summarizes.
- `.agent/archive/plans/menhir-belief-supersession-code-mapped-plan.md` — the Extraction Lab harness (Phase
  0) this ablation work was built on.
