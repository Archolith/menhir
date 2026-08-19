# HANDOFF: menhir enrichment silently loses ~9% of production memories

**Last verified:** 2026-08-18 — FIX CONFIRMED LIVE, citation stale. The note cites `aa7c758`, which is NOT an ancestor of `main` (rebased, same pattern as CF-20). The fix itself IS on main: `_strip_embeddings` in `infrastructure/graphiti_model_patches.py:66`, wired through `graphiti_patches.py:22`, with the ~31KB `content_embedding` overrun documented in-place at :72. Recovery is recorded UNBLOCKED (dedupe request 2,123,893 -> 36,493 tokens). This is a handoff, not a plan -- consider moving it to `.agent/handoffs/`.


> **STATUS 2026-07-27 (later session): ROOT CAUSE FOUND AND FIXED — commit `aa7c758`.**
> All four verification queries in section 2 reproduced exactly. The leading hypothesis in
> section 4 was **wrong in mechanism** and is retracted below. Live recovery is **blocked on
> one decision** — see "Recovery status" — so do not start a bulk retry without reading it.
>
> **Actual cause:** menhir's own `content_embedding` (1536 floats, ~31KB serialized) leaks into
> graphiti's dedupe prompt. `get_entity_node_return_query` returns `properties(n) AS attributes`;
> `get_entity_node_from_record` pops only graphiti's own keys and has never heard of
> `content_embedding`; `node_operations._resolve_with_llm` then splats `**candidate.attributes`
> into `existing_nodes_context`. Measured on the live graph: 15 candidates = 471,685 chars, of
> which the embeddings are **98.5%**; the episode text is 0.95% of the prompt.
>
> **Section 4 is retracted.** The candidate set is NOT unbounded — it is bounded at
> `NODE_DEDUP_CANDIDATE_LIMIT = 15` per extracted name. What is unbounded is the *per-candidate
> payload*. This also means section 9 decision #1 is **not** a recall-quality tradeoff: stripping
> raw floats costs zero dedup quality, because the model could never use them.
>
> **Fix:** strip embeddings in `_safe_to_prompt_json` (`graphiti_model_patches.py`), the
> serializer every graphiti prompt routes through. Deliberately NOT fixed at
> `get_entity_node_from_record` — the Neo4j save is `SET n = $entity_data` (full property
> replacement), so the attributes round-trip is what preserves menhir's own
> namespace/scope/source stamps; popping there would silently strip them on the next write.
> Section 9 decision #2 also landed: `GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS` now measures the
> *assembled* request. Decision #3 (add_memory reporting success) is still open.
>
> **Recovery status: verified working, UNBLOCKED.** Confirmed on 4 episodes — the dedupe
> request dropped from 2,123,893 to 36,493 tokens and the recovered memories now return at top
> relevance from `recall_memories`.
>
> An earlier revision of this note claimed re-enrichment leaves "duplicate nodes and
> READY-but-empty tombstones". **That was wrong and is retracted.** The two-node shape is menhir's
> designed twin architecture, documented at `infrastructure/episode_lifecycle.py:781`: menhir
> creates the PENDING node, graphiti creates a content-identical twin during extraction, entities
> attach to *graphiti's* node, and the pending node points at its twin via
> `resolved_episode_uuid`. Lookups deliberately anchor on both. The mistake was reading
> entity-count-on-self as the health signal without checking the twin pointer.
>
> Measured integrity: all 4 re-enriched originals carry a correct `resolved_episode_uuid`
> resolving to 21 / 16 / 8 / 2 entities; 117 healthy pre-existing twin pairs; and
> **`READY` episodes with no entities and no twin pointer = 0**. Re-enrichment produces the same
> shape as a normal first-time write. Nothing to fix before a bulk run.

**Written 2026-07-27. Assumes you know NOTHING about the session that found this.**
Everything needed to verify, diagnose, and fix is below. Verify before trusting any of it.

Severity: **production data integrity**. Not a benchmark artifact. Affects the live memory graph
this workspace's agents write to every session.

---

## 1. The problem in one paragraph

When an agent calls `add_memory`, menhir creates an `:Episodic` node immediately and returns
success (`state=PENDING, queued`). Enrichment then runs asynchronously. For ~9% of memories that
enrichment fails with an OpenAI **400 context-length** error, the episode is marked `FAILED`, and
because enrichment is what produces `:Entity` nodes, the memory ends up with **zero entities**.
menhir's recall searches `:Entity`, not `:Episodic`. So the memory text sits in the database,
returns success to the caller, and is **permanently invisible to recall**. Nobody is told.

---

## 2. Verify it yourself (do this first, ~2 minutes)

Connection is in `projects/archolith/menhir/.env` (`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`).
At time of writing `NEO4J_URI=bolt://192.168.86.56:7687`. **All queries below are READ ONLY.**

```python
from dotenv import dotenv_values
from neo4j import GraphDatabase
cfg = dotenv_values(".env")            # run from projects/archolith/menhir
d = GraphDatabase.driver(cfg["NEO4J_URI"], auth=(cfg["NEO4J_USER"], cfg["NEO4J_PASSWORD"]))
```

**(a) Scale of the failure**
```cypher
MATCH (e:Episodic) RETURN count(e) AS eps,
  sum(CASE WHEN e.processing_state='READY'  THEN 1 ELSE 0 END) AS ready,
  sum(CASE WHEN e.processing_state='FAILED' THEN 1 ELSE 0 END) AS failed
```
Observed 2026-07-27: `eps=2165  ready=1954  failed=211` (9.7% failed).

**(b) Cause breakdown**
```cypher
MATCH (e:Episodic) WHERE e.processing_state='FAILED'
RETURN sum(CASE WHEN e.processing_error CONTAINS 'maximum context length' THEN 1 ELSE 0 END) AS ctx,
       count(*) AS total
```
Observed: `196/211` (93% of failures; 9.1% of ALL episodes).

**(c) The killer: FAILED episodes are unreachable**
```cypher
MATCH (e:Episodic) WHERE e.processing_state='FAILED'
OPTIONAL MATCH (e)-[]-(n:Entity)
WITH e, count(DISTINCT n) AS ents
RETURN count(*) AS failed, sum(CASE WHEN ents=0 THEN 1 ELSE 0 END) AS zero_ents
```
Observed: `211/211` have zero entities.

**(d) The verbatim error**
```cypher
MATCH (e:Episodic) WHERE e.processing_state='FAILED'
  AND e.processing_error CONTAINS 'maximum context length'
RETURN substring(e.processing_error,0,320) AS err LIMIT 2
```
Observed:
> This model's maximum context length is 128000 tokens. However, your messages resulted in
> **1,097,955 tokens** (including 169 in the response_format schemas.)

A second sample showed **2,123,893 tokens**.

---

## 3. The decisive clue

| | FAILED | READY |
|---|---|---|
| n | 211 | 1954 |
| median content length | 1,792 chars | 590 chars |
| max content length | 4,582 chars | 4,286 chars |

**The episode content is tiny.** 1,792 chars is roughly 450 tokens. The request sent to the model
is **1-2 million tokens**. So >99.9% of the payload is NOT the memory being stored -- it is context
assembled around it.

Larger episodes fail more often (median 1,792 vs 590), which is consistent with episode size acting
as a multiplier on whatever context is being gathered, rather than being the payload itself.

---

## 4. Leading hypothesis -- **UNVERIFIED, prove or kill it first**

Graphiti's entity-resolution/dedup step passes *existing* entities into the extraction prompt so the
model can match new mentions against them. If that candidate set is unbounded, it grows with the
namespace. The `default` namespace holds the overwhelming majority of production memories
(205 of 211 failures are in `default`), which fits.

**This is a hypothesis built from the token math and the namespace distribution. It has NOT been
confirmed by inspecting an actual outgoing request.** Do not build a fix on it until you have.

**How to confirm:** log or capture the actual `messages` payload for one failing episode and
measure what fraction is entity context vs prompt boilerplate vs episode content. Entry point:
`menhir/src/menhir/infrastructure/graphiti_llm_patches.py`, `_generate_response` -- it already logs
request begin/response and builds `openai_messages`. Add a size breakdown there, reproduce against
a THROWAWAY stack (never production), and read the real numbers.

---

## 5. Why the existing guardrail does not catch this

There IS a guardrail: `GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS` (default 12000, see
`settings_model.py` and `.env.example`). Its docstring: *"Optional guardrail for obviously oversized
episode text before Graphiti extraction. Uses a rough chars/4 token estimate."*

**It measures the EPISODE TEXT, not the ASSEMBLED PROMPT.** A 1,792-char episode estimates to ~450
tokens, passes the 12,000 check comfortably, and then produces a 1,000,000-token request. The
guardrail is measuring the wrong thing, which is why this has run silently.

Any fix should bound the thing that actually gets sent.

---

## 6. What is NOT the cause (already ruled out -- do not re-investigate)

- **NOT truncation of the response.** A separate, working mechanism handles that: on a JSON decode
  failure at a high char offset, `graphiti_llm_patches.generate_response` doubles `max_tokens`
  (ceiling 16384) and retries. Verified working: 2 truncations in 1682 calls, both recovered.
- **NOT `LLM_MAX_TOKENS`.** That caps the RESPONSE, not the request. It was raised 1500 -> 3000 on
  2026-07-27 for unrelated efficiency reasons (see commit `c42ab1f`).
- **NOT list-shaped content.** Measured: list-shaped content is 2.4% of FAILED vs 5.6% of READY --
  lists fail LESS than average. A related design note
  (`menhir-titled-list-memory-shape.md`) explores list handling; it is a SEPARATE, lower-priority
  concern and is not this bug.
- **NOT `CombinedExtractionCollapsedError`.** A different failure mode, 0 of the 196.

---

## 7. Scope

```
By namespace:  default 205 | archolith 4 | canary-graphiti-029-2026-07-12 2
By source:     claude-code 200 | codex 4 | project-scan 3 | opencode-* 4
Attempts:      avg 1.0  (they are NOT being retried)
```

Every failure is agent-written memory. The `default` namespace is where this workspace's agents
store session learnings, so the lost content is disproportionately decisions, corrections, and
conventions -- exactly the material the memory system exists to retain.

---

## 8. Nothing is permanently destroyed (important)

The episode content is intact on the `FAILED` nodes. Recovery is a re-enrichment pass once the
root cause is fixed. Confirm the content is present before designing anything:

```cypher
MATCH (e:Episodic) WHERE e.processing_state='FAILED'
RETURN substring(e.content,0,120) AS c LIMIT 10
```

The re-enrichment path exists: `fetch_failed_episode_retry_candidates`
(`infrastructure/episode_lifecycle.py`) feeds `retry_failed_episodes`
(`services/scheduler_tasks.py`). Do NOT trigger a mass retry before the fix -- 196 episodes would
re-fail identically and the retry counters would be burned for nothing.

---

## 9. Decisions the fix requires (do not pick unilaterally -- ask ctharvey)

1. **How to bound the context.** Cap the candidate entity set, page it, or filter by relevance to
   the episode. This is a REAL tradeoff: fewer dedup candidates means more duplicate entities in
   the graph. It is a recall-quality decision, not a mechanical change.
2. **Guardrail placement.** Measure the assembled request instead of (or in addition to) the
   episode text, and decide whether exceeding it should fail loudly or degrade gracefully.
3. **Should `add_memory` stop reporting success?** Today it returns `queued` and the caller is
   never told the write became unrecallable. This is arguably the more important defect -- it is
   why nobody noticed for at least 8 days. Options: synchronous validation, a status the caller can
   poll, or surfacing failures in a health endpoint.

---

## 10. Related existing work

- **Open TODO, HIGH, created 2026-07-19 (8+ days before discovery):**
  *"[menhir/memory-health] Investigate oversized enrichment LLM payload: a background enrichment job
  sen..."* -- anchored to `src/menhir/services/enrichment_steps.py`. Same issue, previously
  unquantified. Find it via `mcp__memory__list_todos`.
- `.agent/scripts-index.md` (menhir) indexes durable diagnostic scripts across menhir and
  archolith-bench. **Read it before writing a probe script** -- and note the naming convention:
  `_name.py` is a throwaway, `name.py` earns an index row in the same commit.
- `.agent/workflows/troubleshoot_enrichment_stalls.md` and the `get_episode_trace` MCP tool give a
  per-episode enrichment bundle -- better than log-scraping.

---

## 11. Suggested first moves

1. Run the four queries in section 2. Confirm the numbers still hold. **If they do not, stop and
   re-derive -- do not trust this document over the database.**
2. Capture one real failing request payload on a throwaway stack and get the actual size breakdown
   (section 4). This either confirms or kills the hypothesis and is the highest-information step.
3. Only then bring ctharvey the section 9 decisions, with real numbers attached.

**Do not** start by writing a fix, mass-retrying, or touching production data. The measurement in
step 2 is cheap and determines everything downstream.

---

## 12. Provenance and honesty note

Found 2026-07-27 while investigating an unrelated scalar-binding bug. The trigger was ctharvey
asking whether a list-shaped memory would "be relevant" -- checking that led to the recall path,
which led here.

The session that produced this document made **four** factual errors that were later caught and
retracted, all from queries whose filters silently excluded the rows that would have falsified the
claim (e.g. filtering `group_id` when the relevant nodes carry `namespace`; filtering
`processing_state='ready'` when the stored value is uppercase `READY`). **Re-verify the numbers
here rather than inheriting them.** Section 2 exists so you can.

The concrete demonstration that convinced ctharvey: eight `add_memory` calls made during that
session all returned success, and all eight were later found `FAILED` with zero entities --
including the memory recording the discovery of this very bug.

---

# ADDENDUM 2026-07-27 (added hours after the above): THIS IS A TOTAL OUTAGE, NOT A 9% DEGRADATION

The "9.7% of episodes" figure in section 2 is **badly misleading** and should not be used to judge
urgency. It averages a healthy multi-month history against a completely broken present.

## Daily failure rate, production

```
day        total  failed   rate
2026-07-05     19      0    0.0%
2026-07-11    148      0    0.0%
2026-07-15     16      1    6.2%
2026-07-16     11      3   27.3%
2026-07-17      4      1   25.0%
2026-07-18     14      3   21.4%
2026-07-19     16     16  100.0%   <-- onset
2026-07-20     37     37  100.0%
2026-07-21     24     24  100.0%
2026-07-22     50     50  100.0%
2026-07-26     22     22  100.0%
2026-07-27     44     44  100.0%
```

Monthly: 2026-05 = 1/327 (0.3%), 2026-06 = 4/479 (0.8%), 2026-07 = 206/697 (29.6%).

**Since 2026-07-19 every single memory written to production has failed enrichment.** That is 193
consecutive memories, none recallable. The memory system has been effectively write-only for the
last eight days of activity.

Reproduce:
```cypher
MATCH (e:Episodic) WHERE e.created_at IS NOT NULL
  AND e.created_at >= datetime('2026-06-25T00:00:00Z')
WITH substring(toString(e.created_at),0,10) AS day, count(*) AS tot,
     sum(CASE WHEN e.processing_state='FAILED' THEN 1 ELSE 0 END) AS f
RETURN day, tot, f ORDER BY day
```

## This strengthens the unbounded-context hypothesis

The shape is a **monotonic ramp, not a step**: 0% through 07-15, then 6% -> 27% -> 25% -> 21%, then
100% from 07-19 onward and never recovering. A code regression or a config change would produce a
step. A steadily growing quantity crossing a fixed 128K ceiling produces exactly this ramp, with the
spread during the transition explained by per-episode size variation deciding who crosses first.

The `default` namespace holds 205 of 211 failures and is where agents write session learnings, so it
grows every session. That is the quantity to measure. **Still not confirmed against a real payload
(section 4 stands) -- but the time series is strong corroboration.**

If this is right, the ceiling was crossed permanently around 07-19 and no memory written to
`default` can succeed again until the context is bounded. It will not self-heal.

## Correlation to check first

The pre-existing HIGH todo *"[menhir/memory-health] Investigate oversized enrichment LLM payload"*
is dated **2026-07-19** -- the exact onset date. Someone saw the symptom the day it began, filed it,
and it was never diagnosed. Read that todo before anything else; it may already name the trigger.

Also check what shipped around 07-15..07-19 (`git log --since=2026-07-14 --until=2026-07-20`) --
not because a code change is the likely cause given the ramp shape, but because something may have
raised the growth rate (e.g. a change in what gets written to `default`, or entity-dedup behaviour).

## Revised urgency

Section 11 says "do not start by writing a fix". That still holds -- the payload measurement is one
step and prevents fixing the wrong thing. But this is an ongoing total loss of new memory, not a
background defect, so that measurement should happen immediately rather than being scheduled.

Every agent session that runs before this is fixed writes memories that cannot be read back.
