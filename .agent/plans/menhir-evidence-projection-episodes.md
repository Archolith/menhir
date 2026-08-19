# Evidence-Projection Episodes: entities from the user's own words

Status: PLAN, approved in direction 2026-07-27 by ctharvey ("i think second"). Not built.
Supersedes the context-injection design sketched earlier in the same session (see "Designs that
died" below -- three were tried, and the reasons they failed are the load-bearing part of this doc).

Related: task #13, `.agent/adr/0001-conversation-turn-capture-surface.md`, task #10 (subject
normalization, which this makes largely unnecessary), commit fd1f13f (part 1, already landed).

## The problem in one line

A typed scalar assertion is extracted from the USER's raw words, but the entities it must bind to are
extracted from the AGENT's memory text -- a paraphrase. The two vocabularies need not agree, so the
bind fails.

## Why the obvious fixes do not work

Menhir already captures the user's words as `:TurnEvidence` (ADR 0001). But `:TurnEvidence` is
deliberately never enriched, so **no entities are ever extracted from it**. The scalar binder needs
entities; entities exist only on enriched `:Episodic` nodes. Hence the current production path:

1. `load_next_scalar_batch` returns `:TurnEvidence.turn_id` whenever any Turn evidence exists
   (`memory_graph_adapter.py:1039`) -- always true in production, the hook captures independently of
   `add_memory`.
2. `fetch_linked_entities_for_episode` resolves that turn_id to an episode through `ADMITTED_ON`.
3. That episode's entities are the bind candidates.

Step 3 is the flaw even when the edge exists: those entities come from the agent's paraphrase. And
`link_episode_admission`'s own docstring notes that **most evidence has no memory at all**, so for the
majority of turns step 2 bridges to nothing.

## The decision

When a memory is added in the context of a captured turn, ALSO enrich the turn's text -- as an
**evidence-projection episode**: an `:Episodic` that exists solely so entities from the user's own
words exist, and which never enters recall or ordinary decay.

Why this shape and not the alternatives:

- The curation gate stays with the agent. A projection is minted only when someone decided the moment
  was worth remembering, so volume is bounded by an already-curated rate -- not by hook volume.
- The assertion and the entities then come from the SAME string, so subject binding matches by
  construction. Task #10's determiner-stripping and snake_case folding stop being load-bearing.
- It honors ADR 0001's separation. The ADR's evidence ladder is
  `Turn -> ExtractedBaseEvent -> StatedMeasure -> FoldDerivedView` and never routes through Memory;
  a non-recallable projection is a step on that ladder, not a promotion to Memory.

**This is cheaper than it looks.** Recall candidates are `:Entity`, not `:Episodic`
(`search_content_embeddings` = `MATCH (n:Entity) WHERE n.content_embedding IS NOT NULL`), so a
projection episode is invisible to semantic recall with no work at all. The exclusion effort is
confined to episode-listing and decay paths.

## Why this is worth building even if scalar binding were abandoned

Raised by ctharvey 2026-07-27: "we will need this for provenance/governance pass anyways." Checked
against `menhir/.agent/artifacts-provenance-governance-status.md` (ledger, last verified 2026-07-11
against production) -- it holds, and more strongly than the scalar case:

- **The evidence layer has no emitter.** `:Evidence` nodes and `SUPPORTED_BY` edges are 0 in
  production; the L4 slice is "tested-but-unwired," and the ledger's own next step is "wire the
  existing L4 slice into runtime + give it one emitter." A captured turn is the one source in the
  system that is not agent-authored, which makes a projection the natural first emitter.
- **Provenance is caller-declared and unverified.** The ledger is blunt that the trust policy is
  "convention-sound, not adversarially-sound" -- nothing checks that a claimed anchor resolves or that
  `source=HUMAN` is true. An `ADMITTED_ON` edge to a hook-captured turn is at least drawn by the
  SYSTEM at capture time rather than asserted in an argument, and the projection's text is copied from
  `t.text` inside the query, so a caller cannot choose the words that land in a node stamped
  `source='user'`.

  > **CORRECTION 2026-07-27 — an earlier version of this line, and commits `2ce2b12` / `08e8b44`,
  > claimed this was "the first provenance link a caller cannot forge by asserting it". THAT IS
  > FALSE.** `TurnEvidenceRequest` (`api/routes_support.py:256`) takes `role` and `declarant` FROM THE
  > CALLER, and its own docstring says the server never infers them. Any client holding an agent key
  > can POST a `:TurnEvidence` with `role='user', declarant='user'` and arbitrary text, then cite it.
  > The chain is producer-trust end to end: it is only as good as the assumption that the thing
  > holding the agent key is an honest hook. This work IMPROVES provenance (the words are no longer
  > caller-chosen, and the link is machine-drawn) but it does NOT make it adversarially sound, and
  > the governance ledger's complaint stands unaddressed.
  >
  > Making it adversarially sound is a separate, tractable piece of work, recorded here because it is
  > the natural follow-on: give producers their own credential tier distinct from `agent`, and refuse
  > `declarant='user'` from an agent-tier caller. Until then, treat every declarant claim as
  > "asserted by whoever held the key", not as evidence a human spoke.
- **Anchor coverage is sparse.** `ANCHORED_TO` sits at ~3.7% (1,869 of 50,228 nodes), one of the four
  live numbers the ledger says drives reprioritization. Projections raise it on exactly the nodes that
  matter -- entities derived from user statements.
- **Governance enforcement is blocked on coverage, not on code.** Roadmap item 3: the wardens exist
  and are off by policy, and foundation-typed admission "has little to verify until evidence/anchor
  coverage rises."

So the ordering is: scalar binding is the FIRST CONSUMER of this, not its justification.

**Scope discipline:** do NOT fold the `:Evidence`/`SUPPORTED_BY` emitter into phase 1. That activation
is owner-reserved under `plans/backlog/l3l4-semantic-overlay-sequencing-plan.md`. Phase 1 must simply
avoid foreclosing it -- the projection should carry enough identity that an `:Evidence` node can later
be attached to it without rework.

## Build

### Phase 1 -- menhir only, no hook, independently testable

Triggered when `queue_episode_for_enrichment` receives a `turn_evidence_uuid` that resolves. Menhir
already holds the turn text (`:TurnEvidence.text`), so nothing new crosses the wire.

1. New marker on `:Episodic`, e.g. `is_evidence_projection: true`, plus the `ADMITTED_ON` edge to its
   `:TurnEvidence` (part 1 already draws that edge; this rides on it).
2. Create the projection episode from `t.text`, queue it for enrichment like any other episode.
3. Exclude it from: episode listing by scope, decay/lifecycle promotion-demotion, and any
   `build_context` path that reads `:Episodic` directly. **VERIFY EACH PATH DURING BUILD** -- this
   list is from reading, not from tracing every caller, and an unexcluded path is a silent leak of raw
   chat into a surface that should not have it.
4. Idempotency: one projection per `:TurnEvidence`, MERGE-keyed on the turn, so N memories citing one
   turn do not mint N projections.

Testable end-to-end on LME by passing `turn_evidence_uuid` directly -- no hook required.

### Phase 2 -- hook wiring (production reach)

`PostToolUse` fires AFTER the tool call, so it **cannot** add `turn_evidence_uuid` to an `add_memory`
that already happened. It must make a separate linking call.

1. `UserPromptSubmit` hook: `post_evidence` currently does `urlopen(...).read()` and DISCARDS the
   response (`menhir_turn_evidence_common.py:243`), even though the endpoint returns
   `TurnEvidenceResponse{turn_id, created, recorded_at}`. Parse it and stash `turn_id` keyed by Claude
   Code's `session_id`.
2. `PostToolUse` matcher on `add_memory`: parse `episode_id` from the tool response, read the stashed
   `turn_id` for this session, POST both to a small new endpoint that draws `ADMITTED_ON` and mints the
   projection.

**The session join here is exact, and this is the correction that makes the whole design viable.**
An earlier pass in this session concluded session identity was unusable. That was true only of
menhir's MCP `session_id`, which falls through to a derived constant (`auth.py:225`, seeded from
user/path/user-agent/client_id/api_key -- nothing per-conversation) because `.mcp.json` sends no
`X-Yawn-Session-Id`. Claude Code's own `session_id` is per-conversation and BOTH hooks see it. The
join is hook-to-hook, not hook-to-MCP.

## Designs that died, and why (do not re-derive these)

1. **Agent passes `turn_evidence_uuid` after the hook injects it into context.** Depends on the agent
   choosing to pass it, and more fatally, bridges to a memory that usually does not exist.
2. **Server-side join on `session_id`.** Menhir's MCP session id is a stable constant across every
   window; a join on it matches every turn ever captured.
3. **Hook calls `add_memory` on every triaged prompt.** Reaches ADR 0001's rejected Option A by
   another route -- raw chat at hook volume entering recall and decay, which is the exact consequence
   the ADR named.
4. **Proximity join (most recent turn in the namespace within a window).** Same shape as
   `backfill_admitted_on.py`, which joined on content equality and produced a graph where the #8 fix
   APPEARED to work while the live path was broken.
5. **Loader returns an episode uuid instead of a turn_id.** No join to substitute: `:TurnEvidence` is
   never enriched, so the hop to an admitted memory is the only route to an entity set.

## Open questions

1. ~~**Duplicate entities.**~~ **ANSWERED 2026-07-27 -- resolution merges across phrasings. See
   "Duplication measurement" below. Not a blocker.**
2. **Retention.** A projection outlives its memory or dies with it? Leaning: dies with it, since its
   only purpose is to host that memory's binding targets. Undecided.
3. **Backfill.** Existing `:TurnEvidence` with memories could be projected retroactively. Default NO,
   matching ADR 0001's no-backfill stance.
4. **Assistant turns.** Out of scope. The declarant boundary is load-bearing (ADR 0001) and an
   assistant restating a user fact must never be folded as evidence.

## Duplication measurement (2026-07-27, read-only, no new ingest)

The concern: a projection and its curated memory state the same fact in different words, so the
design depends on graphiti's entity resolution merging them. If it does not, every remembered fact
yields two near-identical entities and the cure is worse than the disease.

**No new corpus was needed.** The multismoke graph (`:7704`, 12 LME namespaces) already contains the
natural experiment: user turns and assistant turns restating the same facts, enriched as SEPARATE
episodes one at a time. That is the same question in the same machinery.

| Check | Result |
|---|---|
| Entities sharing a normalized name within one namespace | **0** across all 12 namespaces |
| Entities attached to BOTH a user and an assistant episode | **133** |
| Entities attached to a user episode at all | 255 (so 52% also span an assistant episode) |
| Entities attached only to assistant episodes | 650 |

Examples of single entities spanning many episodes: `Lumetri Color Panel` (11), `Curves panel` (8),
`Tableau` (7), `basmati rice` (7), `silver Honda Civic` (6), `winter clothes` (6), `audiobooks` (6).

Menhir enriches ONE episode per extraction pass, so an entity linked to episodes of both roles can
only have arrived there by resolution matching a new extraction onto an existing node. The merge is
the mechanism, not an artifact of batching.

**Verdict: the duplication risk is not real. Phase 1 is unblocked.**

### Caveat, stated honestly

This measures user-turn vs assistant-turn phrasing -- both conversational register. The projection
case is user-turn vs agent-MEMORY phrasing, and a memory reads more like "User owns 25 postcards"
than a chat reply does. Close, not identical. Strong evidence rather than proof; re-check entity
counts on the first real projection run rather than assuming it transfers.

## What is already done

- `fd1f13f` -- `ADMITTED_ON` is drawn for every source, not only user/manual. Prerequisite for both
  phases. Unit-tested only; LME cannot exercise it (the bench ingests as `source='user'`).
