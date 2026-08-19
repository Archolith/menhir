# Titled-List Memory Shape (design note, NOT approved for build)

Status: DESIGN NOTE. Raised 2026-07-27 by ctharvey: "maybe a special view or memory for just bare
item lists with title." Nothing here is approved for implementation.

Related: task #11 (edgeless-roster collapse causes permanent content loss), task #10 (perceiver
emits subject strings no entity carries).

## The observation that prompted it

On the 2026-07-27 LME multismoke build, this turn failed enrichment permanently:

```
user: agents names below:Admon\nMagdy\nEhab\nSara\nMostafa\nNemr\nAdam
```

Extraction was CORRECT: 7 entities, 0 relationships, because the turn states no relationships.
Graphiti then drops every node with no surviving edge (`orphan_nodes_dropped=7`), nothing persists,
and menhir raises `CombinedExtractionCollapsedError`. Retry re-runs the same deterministic
extraction and re-collapses. The seven names are lost.

The same namespace later produced a `count=7 subject='agents'` typed assertion that could not bind,
because no `agents` entity existed to bind to -- a direct downstream consequence.

## The reframing (the actual point of this note)

A titled list is NOT missing its relations. **The list structure IS the relation.**

`agents names: [Admon, Magdy, ...]` states exactly seven facts of the form
`(agents-roster) -[MEMBER_OF]- (Admon)`. That is an ordinary triple. It fits the existing graph
model with no new node kind, no new View kind, and no change to recall.

So the cheaper framing of ctharvey's idea is: **recognize the shape and emit the membership edges**,
rather than invent a parallel storage mechanism for lists.

Consequences if that is right:
- every node has an edge -> nothing is orphan-pruned -> no collapse
- `raw_edges > 0` -> the classification problem in task #11 does not arise for this shape
- an `agents` container entity exists -> task #10's unbindable `agents` subject binds

One mechanism, three defects.

## Why NOT a new View kind

Menhir has exactly two shipped View kinds, `counter` and `scalar_state`. Both are DERIVED
projections: computed by folding assertions over time, with supersession, currency, and an
as-of evaluation. A list is none of those things -- it is stated once, directly, and does not fold.

Modelling it as a View would mean a View that is not derived from anything, which breaks the
category that makes Views auditable (every View can name the assertions that produced it).
`.agent/memory-view-kinds-frontier-transfer.md` ranks nine candidate View kinds and every one of
them is a fold over events; a list is not in that family.

If a list DOES need a View later, the natural one is a derived "current membership" View over
MEMBER_OF edges with add/remove events -- but that is a second step, and only worth it once
membership actually changes over time.

## The real design tension

Emitting MEMBER_OF edges is an INFERENCE. Menhir's governance model is built on not asserting what
was not stated, and the collapse receipt already tracks `endpoints_synthesized` as a distinct
signal -- i.e. synthesizing endpoints is treated as suspicious, by design.

The defensible position is that list syntax is a STATEMENT of membership, not an inference about it:
the user wrote the items under a title, and reading that as membership is parsing, not guessing.
The indefensible version is inferring membership from proximity in prose. Any implementation has to
keep that line sharp, and the parse must be deterministic (pure code, not an LLM judgement) so the
same turn always yields the same edges.

## Open questions (must be answered before building)

1. **How common is this shape?** n=1 in a 274-turn benchmark. This note exists because that one case
   caused permanent loss and blocked a second defect, not because frequency is established. Measure
   before building: count turns whose content matches a list shape across the LME corpora and a real
   production namespace.
2. **What counts as a list?** Newline-delimited is the observed case. Comma-separated, numbered,
   and bulleted forms all exist. Over-broad matching would turn ordinary prose into fake membership
   edges -- the failure mode is silent and wrong, which is worse than the current loud failure.
3. **Does the title always exist?** "agents names below:" is a clean title. A bare list with no title
   has no container to attach to, and inventing one is exactly the inference to avoid.
4. **Where does it run?** A deterministic pre-extraction parse, or a post-extraction repair when
   entities>0 and edges==0? The latter is narrower and only fires on the failing path.
5. **Does it interact with task #11's classification fix?** If lists stop collapsing, the remaining
   `entities>0, edges==0` cases are a smaller and possibly different population. Sequence matters:
   fixing classification first may make this look less urgent than it is, or vice versa.

## Recommended sequence (if pursued)

1. MEASURE question 1 on existing corpora. Cheap, read-only, no new stack.
2. Only if the shape is non-trivially common, decide question 4 (parse point).
3. Build the deterministic shape detector with the sharp line from "design tension" above, behind a
   default-off flag, and measure entities-persisted and false-membership rates on a fresh stack.
4. Revisit whether a derived membership View is warranted -- separately, later.

Do NOT start at step 3.

## MEASURED 2026-07-27 (step 1 of the recommended sequence)

Read-only scan of both available corpora for the shape (a lead-in ending in `:` followed by >=3
short newline-delimited items):

| Corpus | user turns | titled lists | untitled lists |
|---|---|---|---|
| LME multismoke (12 items) | 283 | 3 (1.06%) | 0 |
| LME scalar-ku-20260722 | 2697 | 0 (0.00%) | 0 |

The 3 hits are **the same turn three times** -- the agents roster, duplicated by extraction-collapse
retries. So the true count is **1 unique turn in ~2,980**, and zero in the larger corpus.

**Verdict on LME: this shape is rare enough that building for it is not justified by frequency.**
The recommended sequence says do not proceed past step 1 unless the shape is non-trivially common.
It is not, in this data.

### The caveat that matters more than the number

LongMemEval is synthetic conversational QA -- chatty prose about restaurants, degrees, and
insurance. It is close to a worst case for list density and is NOT a proxy for this workspace's
real traffic. Rosters, file lists, task lists, dependency lists, and command output are ordinary in
agent/coding memory, and menhir's production namespaces carry exactly that kind of content.

So the honest read is: **frequency is unmeasured where it counts.** Before this is closed as
"not worth it", run the same scan against a real production namespace. That is still cheap and
read-only, but it touches production and needs approval first.

### What is decided regardless of frequency

The permanent-content-loss defect (task #11) does NOT depend on this shape being common. One turn
lost the roster forever and blocked a downstream binding. Fixing the collapse CLASSIFICATION is
justified on its own; the titled-list mechanism is an optimization on top of it, not the fix.

Sequence accordingly: classification first (task #11), list shape only if production data shows the
shape is common.

## THE FREQUENCY MEASUREMENT ABOVE MEASURED THE WRONG POPULATION (2026-07-27, ctharvey)

ctharvey asked: "if someone on menhir adds a memory of just items to fix, would it be relevant?"
That question exposes an error in the section above. The LME scan counted CONVERSATIONAL TURNS. The
population actually at risk is EXPLICIT `add_memory` CALLS, and those are a different distribution.

### Why an explicit list memory is worse off than the benchmark case

Traced on the multismoke graph. The LME roster survived -- all 7 names DO exist as :Entity:

```
Admon ENTITY EXISTS   Magdy ENTITY EXISTS   Ehab ENTITY EXISTS   Sara ENTITY EXISTS
Mostafa ENTITY EXISTS Nemr ENTITY EXISTS    Adam ENTITY EXISTS
```

But NOT via the user's roster turn, which collapsed. They were rescued by the ASSISTANT turn that
echoed the names back ("Thank you for providing the agent names. Here..."), which enriched fine.
The rescue was luck: a second turn happened to restate the content.

**An explicit `add_memory("items to fix: A, B, C")` has no assistant echo.** Nothing restates it.
So the rescue path does not exist.

### What happens to a collapsed list memory

- The content SURVIVES on the FAILED `:Episodic` node -- it is not deleted.
- It is present in the `episode_content` fulltext index (verified: querying "agents names" returns
  the FAILED node as the top hit, score 5.72).
- **But menhir's recall does not search that.** Candidate generation is over `:Entity`:
  `search_content_embeddings` is `MATCH (n:Entity) WHERE n.content_embedding IS NOT NULL`, scoped by
  `n.group_id`, and the bm25 arm ranks the same entity candidates. `:Episodic` appears in
  memory_queries only in listing/lookup shapes (by scope, by uuid), never in semantic recall.
- A collapsed memory produces ZERO entities. So there is nothing for recall to match.
- The FAILED pending node also carries `group_id = NULL` (menhir sets `namespace`), so even a
  group-scoped scan over episodes would miss it.

**Net: the memory is stored, findable by a raw fulltext query nobody issues, and invisible to recall.**
From the user's point of view they saved a list and menhir forgot it.

### Corrected framing

- The LME number (1 unique turn in ~2,980) is a fact about synthetic chat prose. It says nothing
  about `add_memory` usage, which is the at-risk path.
- Lists are a NATURAL shape for deliberate memory writes: things to fix, files touched, blockers,
  dependencies, names on a team. This workspace's own agent instructions produce exactly that.
- Severity is higher than the benchmark implied: silent invisibility of an explicitly-saved memory,
  with no error surfaced to the caller (enrichment failure is asynchronous).

### What to measure instead (supersedes step 1 above)

Count list-shaped content among **`add_memory` calls in a real namespace**, not conversational
turns. That is the denominator that matters. Read-only, but it touches production and needs
approval.

Also worth checking: does `add_memory` return success to the caller even when enrichment later
collapses? If so, the API actively reports a write that becomes unrecallable, which is its own
defect independent of lists.

## BUILT AND VALIDATED END-TO-END (2026-07-27)

Status of this note changes from DESIGN NOTE to IMPLEMENTED. ctharvey approved building the
MEMBER_OF form over the alternative (retaining relationless entities), on the grounds that it fits
the normal graph style rather than adding a second storage path. That judgement held up: retention
would have recovered tokens, not meaning -- rescued entities carry `name_embedding` but zero
`content_embedding`, and `search_content_embeddings` filters on `content_embedding IS NOT NULL`, so
retained-but-edgeless names stay invisible to recall anyway.

### What shipped

| Commit | Change |
|---|---|
| `1291a43` | `parse_titled_list` + MEMBER_OF emission in `_sanitize_combined_payload`; `relationless_extraction` classification in `enrichment_steps` |
| `e6d8b0a` | synthetic edges routed through `_sanitize_combined_edge` so they carry `episode_indices` instead of leaning on graphiti's pydantic default |
| `ad010ed` | corpus-reading guard test after the hand-written fixture was found to have invented a newline the data does not contain |
| `12f5cf9` | `list_membership_edges_added` added to the sanitation log line AND its trigger condition (a list-only sanitation logged nothing) |

Parser is a whitelist: title <=8 words, >=3 items, each item <=6 words with no sentence
punctuation; one non-conforming item refuses the whole block. It fires only when the model returned
zero edges, and only for items the extractor independently recognized as entities -- the parse
decides LIST, the extractor decides ENTITY, and both must agree before an edge is minted. That is
the sharp line the "design tension" section demanded.

Classification is separate and lands regardless: `entities>0, edges==0` is now reported as
`relationless_extraction ... retryable=false`, because re-running a deterministic extraction that
correctly found no relations cannot produce a different answer.

### Treatment run (isolated container, `:7706`, fixture `roster-smoke-7161e7e2.json`)

```
[1/1] 7161e7e2 ns=lme-7161e7e2 turns=14 episodes=25 ready=14 failed=0 requeued=0 item=184s
```

| | Control (pre-fix, `:7705`) | Treatment (post-fix, `:7706`) |
|---|---|---|
| episodes | 14 | 14 |
| READY / FAILED | 13 / 1 | **14 / 0** |
| MEMBER_OF edges | 0 | **7** |
| roster episode | FAILED | **READY** |

All seven names now attach to an `agents names below` container via
`'<name> is listed under agents names below'`, from the USER turn -- no assistant echo required.
The rescue-by-luck path documented above is no longer load-bearing.

### Caveats on these numbers

Assertion counts differ between the two runs (control 5, treatment 4) and entity naming varies
(`'shifts'` vs `'4 shifts'`). That is LLM extraction variance, not an effect of the change. Only the
four rows in the table above are attributable; small single-run deltas on this corpus are noise.

### Still open

The frequency question is still unmeasured on the population that matters (`add_memory` calls in a
real namespace), and the `add_memory`-returns-success-on-later-collapse question is untouched.
Neither blocks this: the fix is justified by the permanent-loss defect alone. Both remain read-only
production scans requiring approval.
