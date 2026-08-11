# Menhir: Belief Supersession — Codex Research Plan Mapped to Shipped Code

> **Archived 2026-08-11.** Its extraction-context work was superseded by the completed
> extraction-context ablation campaign; durable temporal and supersession ownership now lives in
> the typed-scalar/event architecture and the remaining backlog plans.

**Status: PLAN (ready to execute Phase 1).** Produced by auditing
`.agent/reference/../../reference/menhir-belief-supersession-temporal-chains-research.md` (Codex, saved 2026-07-15)
against the actual `src/menhir` codebase, `research-vs-shipped-inventory.md`, and today's confirmed
RCA (`.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md`). This is the "actual plan" the
Codex research doc's own Step 1 ("Audit Existing Code," "Determine what pieces already exist,"
"Produce a gap analysis rather than assuming new work") asked for, now done against real code
instead of assumed.

**Headline finding: most of what the Codex plan proposes building already exists, and the
already-built version was already bench-tested on LongMemEval and shipped OFF because it measured
neutral-to-negative.** The real, still-open gap is upstream of everything that plan describes —
at extraction/admission time, not retrieval/ranking time. See "The corrected diagnosis" below.

---

## Step 1 — Audit Existing Code (the Codex plan's own Step 1, answered)

| Codex plan's question | Answer, with code |
|---|---|
| Do we already have any supersession edges? | **Yes, at two layers.** (1) graphiti-core native: `RELATES_TO` edges are bitemporal (`valid_at`/`invalid_at`); `resolve_extracted_edges` in `.venv/.../graphiti_core/utils/maintenance/edge_operations.py:325` detects `contradicted_facts` and sets `edge.invalid_at = resolved_edge.valid_at` (line 569) when a new edge supersedes an old one — this is graphiti's own temporal-invalidation mechanism, already running on every ingest. (2) menhir-side: `conflict_status` / `conflict_group_id` fields + `ConsolidationRepository.set_conflict` / `resolve_conflict_group` (`infrastructure/consolidation_queries.py:478-931`) — pairwise conflict detection, grouping, and three resolution actions (`keep_both`/`replace`/`discard_new`), with content absorption and edge-bridging on removal. |
| Do Evidence nodes already support this? | **Yes.** A first-class `:Evidence` node exists via `SUPPORTED_BY` edges (`infrastructure/artifact_repository.py`, `infrastructure/schema.py`) — landed in the 2026-06-28 L4 institutional-overlay session. `belief_evidence.py`'s `assemble_belief_evidence` separately turns candidate metadata into scoring-level `BeliefEvidence` (not a graph node, a scoring signal) — two related but distinct "evidence" concepts, both already built. |
| Does provenance already contain pieces we can reuse? | **Yes.** `source_confidence` (1.0/0.9/0.5), `source`, `user_flagged` (Tier 1, shipped); `mcp/tools/ops/get_provenance.py`; the four-timestamp bitemporal model in `domain/temporal.py` (`valid_at`/`invalid_at`/`created_at`/`expired_at` — world-time vs. belief-time, kept deliberately unflattened). |
| Are temporal edges already sufficient? | **The primitive is sufficient; what writes to it is the gap.** `domain/temporal.py` implements the full deterministic bitemporal filter set (`is_current_belief`, `is_valid_at`, `was_known_at`, `matches_query`, `temporal_role`, `order_by_world_time`) — this is genuinely the Chronostratum layer the Codex plan's "Temporal Reasoning" section describes wanting. It operates correctly on whatever facts exist. It cannot see a fact that was never extracted. |
| Is there existing consolidation logic that can be extended? | **Yes, extensively**, and split by purpose: `ConsolidationRepository` (decay/session-promotion/conflict resolution), `services/quantstate_consolidator.py` + `services/event_fold.py` (D1 QuantState — supersedable numeric counters, the *shipped* write-time consolidation primitive), `services/correction_resolver.py` (narrow, numeric-only, ingest-time correction detection — 9 regex patterns, binds only to counter-View entities). |

**Conclusion on Step 1: this is not greenfield.** The Codex plan's "audit first, don't assume new
work" instinct was correct, and the audit result is that belief-chains, temporal bitemporal
facts, evidence, and conflict-group consolidation are Tier 1/2 shipped code
(`research-vs-shipped-inventory.md` lines 111-154), not net-new.

---

## Step 2 — Current Pipeline Review (mapped to real files)

```
Extraction              graphiti_core.graphiti.add_episode -> extract_nodes/extract_edges
                         (prompts/extract_nodes.py; menhir patches via infrastructure/graphiti_patches.py,
                         schema-tolerance only, no content-level hooks today)
        |
Normalization            (folded into extraction; menhir has no separate normalization pass)
        |
Embedding                 graphiti-core's own embedder (name_embedding); menhir also maintains
                         content_embedding (domain/retrieval_tuning.py CandidateSource.CONTENT_VECTOR)
        |
Entity resolution         resolve_extracted_nodes (graphiti-core) -- graph-wide dedup search,
                         NOT limited to the previous_episodes window
        |
Memory creation            graphiti-core writes Entity/Episodic nodes + RELATES_TO edges
        |
Graph linking             MENTIONS (Episodic->Entity), RELATES_TO (Entity->Entity, bitemporal)
        |
Consolidation              ConsolidationRepository, quantstate_consolidator, correction_resolver
                         (services/maintenance_scheduler.py -- scheduler-driven, DISABLED in
                         MENHIR_BENCHMARK_MODE=1, i.e. disabled for every LME run)
```

**Where the Codex plan's "possible candidates" question ("extraction / post-ingest / consolidation
/ retrieval / hybrid — do not assume the answer") actually resolves, per today's confirmed RCA:**
**extraction.** `graphiti_core.graphiti:1087-1090` builds `previous_episodes` via
`retrieve_episodes(last_n=RELEVANT_SCHEMA_LIMIT)`, `RELEVANT_SCHEMA_LIMIT = 10`
(`search_utils.py:64`). Once an entity's establishing mention falls outside that 10-episode
recency window (~3-4 raw turns at graphiti's ~3-episodes-per-turn expansion rate), extraction's own
"when in doubt, do NOT extract" instruction (`prompts/extract_nodes.py:157`) causes the model to
under-propose entities for a re-mention — confirmed via a controlled A/B test (identical message,
5 entities extracted with 1 prior episode of context vs. 1 entity with 0). This is BEFORE dedup,
BEFORE conflict detection, BEFORE any consolidation pass — none of the Step 1 machinery above ever
gets a candidate to operate on, because the candidate was never created.

**Important correction to the Codex plan's implicit framing:** entity resolution (Step 2's third
stage) is *not* the bottleneck — `resolve_extracted_nodes` searches the whole graph, not just the
local window. The bottleneck is one stage earlier: whether the extractor proposes the mention at
all. This matters because it rules out the Codex plan's "Candidate Retrieval Research" section
(embedding similarity / shared entities / graph distance to find related memories) as a fix for
*this* failure — that machinery helps rank/link candidates that already exist; it cannot conjure a
candidate extraction never produced.

---

## The corrected diagnosis: retrieval-time belief machinery was already tried and shipped OFF

This is the finding that should reshape the Codex plan, and it was not visible to whoever wrote it
because it requires reading `research-vs-shipped-inventory.md`'s 2026-07-11 reconciliation and
`config/settings.py`, not just `src/menhir`'s presence/absence:

- `domain/belief.py`, `domain/warden.py` (`CurrentnessWarden`, `OracleAdmissionWarden`,
  `ContradictionWarden`, `WardenChain`), and the oracle combiner (`domain/oracle_combiner.py`,
  `services/oracle_executor.py`) are **built and wired into production recall**
  (`recall_service._apply_frontier`), gated behind `MENHIR_FRONTIER_*` env flags
  (`config/settings.py:273-292`) — including `frontier_belief_gate` ("add CurrentnessWarden + belief
  scoring to the chain") and `frontier_contradiction_interrupt`.
- **All of these flags default `False` as of 2026-07-11**, specifically because
  `research-vs-shipped-inventory.md`'s reconciliation states: *"After the LME campaign proved
  read-time levers neutral-to-negative, the defaults were flipped OFF."*
- The archived `aggregation-as-consolidation.md` thesis (2026-07-02) is explicit about why the
  still-active write-side pivot happened:
  *"Every read-time lever we pulled this session landed neutral-to-negative... You cannot re-rank
  or re-format your way to information candidate generation never assembled."*

**In other words: the exact retrieval-time belief-ranking architecture the Codex plan proposes
building — a classifier that outputs SUPERSEDES/REFINES/CORRECTS/CONTRADICTS, tentative vs. final
links, a "Current Belief Explorer" — is architecturally the same shape as `belief.py` + `warden.py`
+ the oracle stack, which menhir already built, already ran on LongMemEval, and already found did
not help.** Building a second version of this layer without first understanding *why* the first
one didn't move the needle would very likely reproduce a neutral-to-negative result a second time,
at real implementation cost.

**The "why" is now understood, and it's the same root cause as the extraction-admission RCA:**
belief/warden machinery re-ranks and gates *existing* candidates. If knowledge-update questions are
failing because the updated fact was never extracted (confirmed for 3/3 checked cases,
`rca-lme-stale-fact-retention-2026-07-15.md`), then there is nothing for `CurrentnessWarden` to
suppress and nothing for the oracle combiner to re-rank — both sides of the belief pair need to
exist in the graph before "which one is current" is even a question the warden layer can answer.
This is the same "selection vs. representation" framing the archived consolidation thesis already
established for the counting-question failures, now shown to also apply to knowledge-update
failures, which that document had *not* yet connected to the same root cause.

### A historical correction to `aggregation-as-consolidation.md`

That document's failure-mode census (line ~104) currently reads:

```
Current-value lookup (Rachel→suburbs) | ~2 | currentness (belief-gate)
```

This is the same `830ce83f` case this plan and the RCA cover, and it names the *already-tested and
shipped-off* belief-gate as the fix. Today's RCA shows that's not the applicable fix for this
mechanism: `frontier_belief_gate` operates on facts that exist; `830ce83f`'s "suburbs" fact never
exists in the first place. **This line should be corrected** (not here — it's that document's own
territory) to point at extraction-admission, not the currentness/belief-gate, the next time that
census is revised. Flagging it here rather than editing it directly, since that file is a distinct
owned research artifact with its own status/provenance conventions.

---

## Where the real, still-open gap is

Extraction-time admission — specifically, the extractor's inability to recognize "this bare mention
is a known, established entity" once that entity's establishing context has scrolled outside the
10-episode `RELEVANT_SCHEMA_LIMIT` window. This is a **new** primitive, not represented in Tier 1/2/3
of `research-vs-shipped-inventory.md` at all (that inventory audits retrieval/consolidation/belief
machinery; it has no "extraction-time candidate lookup" row because nothing like it exists yet).

Two independent research angles already exist for this exact gap and should NOT be re-derived:

1. `.agent/archive/plans/menhir-extraction-prompt-recency-recall-research.md` (Codex, saved 2026-07-15) —
   prompt-level fix: stop conflating "identity resolution uncertain" with "do not extract," via
   5 prompt variants (A-E) and a proposition-level recall/precision evaluation methodology.
2. This RCA's own "fix candidates, in order of increasing scope" (`rca-lme-stale-fact-retention
   -2026-07-15.md`): (a) raise `RELEVANT_SCHEMA_LIMIT` — cheap, doesn't scale, just moves the cliff;
   (b) a lightweight name/entity-match lookup against the graph, independent of the recency window,
   feeding the extractor a "this name is already known" signal before its own conservatism applies.

---

## Recommended actual build order

Matches menhir's established promotion pattern for exactly this kind of change: **prototype in
Recall Labs, measure against LongMemEval, promote only with a demonstrated lift** — the same gate
`frontier_belief_gate`, `frontier_oracle_ranking`, etc. were held to (and failed, hence OFF).
Nothing here is approved for production ingestion yet.

**Phase 0 — instrumentation (before any prompt/schema change), built as a Recall Labs extension.**

Recall Labs (`explorer/recall_lab.py`) is the natural home for this, but it does not cover
extraction today — checked directly: `RecallLabTuning` is explicitly scoped to *"the tuning
controls that have an implemented effect in `RecallService`"* (`recall_lab.py:25`), and every arm
in `run_recall_lab` calls `recall_service.recall(...)` (`recall_lab.py:496`). That's retrieval-arm
comparison over an *existing* graph; the prompt variants this phase needs to compare operate on
`extract_nodes`/`extract_edges` at ingest time — a different call site entirely, closer to the
manual patched trace already built and used this session (`archolith-bench/.../trace_extraction_830ce83f.py`)
than to anything `recall_lab.py` wraps.

**Decision: extend Recall Labs with a parallel extraction-arm mode rather than building a
throwaway standalone script**, mirroring the existing arm/judge skeleton so it gets the same
concurrent-run, history-persistence, and Explorer-UI treatment retrieval arms already have:

```
menhir/src/menhir/explorer/extraction_lab.py   (new, sibling to recall_lab.py)

ExtractionLabTuning   -- mirrors RecallLabTuning's shape, but for extraction:
                         prompt_variant: Literal["baseline","minus_when_in_doubt","minimal_recall_patch",
                                                  "mention_first","update_aware","proposition_first",
                                                  "mention_first_update_aware","proposition_first_structured"]
                         model: str = "gpt-4o-mini"   (matches the harness's current LME_EXTRACT_MODEL)
                         context_episode_count: int = 10   (lets Phase 3's schema-limit question be
                                                             tested as just another tuning knob, not a
                                                             separate mechanism)
                         temperature: float = 0.0

ExtractionLabArm      -- id/label/enabled/tuning, same shape as RecallLabArm

ExtractionLabRequest  -- current_message: str
                         previous_episodes: list[EpisodeFixture]  (text + timestamp, hand-built or
                                                                    pulled from a real namespace)
                         gold: GoldExtraction  (expected mentions, expected propositions, update_language
                                                 markers -- from the plan's "Required Test Messages" set)
                         arms: list[ExtractionLabArm]

_run_extraction_arm() -- applies menhir's graphiti_patches.py monkey-patches (mandatory --
                         trace_extraction_830ce83f.py's first version crashed by skipping this; do
                         not repeat that), builds an isolated EpisodicNode + previous_episodes list
                         (NO graph writes -- same read-only contract recall_lab.py already has),
                         calls extract_nodes then extract_edges directly with the arm's
                         prompt_variant/model/context_episode_count substituted in, serializes
                         extracted entities + edges uniformly (mirrors _serialize_result's shape).

judge_extraction_lab() -- NOT the existing blind comparative judge (recall_lab.py's judge only ever
                         picks a winner among anonymous arms with no ground truth). This phase HAS
                         ground truth per fixture (the plan's expected mentions/propositions), so it
                         needs an absolute gold-aware scorer instead -- the same shift already made
                         once this session for `recall_lab_investigate.py` (built to add an
                         absolute, gold-aware judge alongside Recall Lab's comparative one). Score
                         per arm: mention recall/precision, proposition recall/precision,
                         update_capture_rate, unsupported_inference_rate -- the exact metric family
                         the extraction-prompt-recency plan's "Evaluation Criteria" section
                         specifies. No LLM-judge variance on the pass/fail axis: recall/precision
                         against a fixed gold set is deterministic set comparison, not a judged
                         comparison -- avoids re-importing the noisy-llm-judge problem
                         the archived consolidation thesis already diagnosed and fixed via D0 entropy
                         for the counting-question campaign. An LLM is only needed to classify a
                         raw extracted mention/proposition against the gold set when wording differs
                         (e.g. "the suburbs" vs "suburban area") -- a narrow, single-purpose
                         classification call, not an open-ended quality judgment.

Wiring (mirrors recall_lab.py's existing route pattern in explorer/app.py:645-691):
  GET  /explorer/extraction-lab              -- dashboard HTML (new template, sibling to recall_lab.html)
  POST /explorer/api/extraction-lab/run      -- run all enabled arms concurrently, return per-arm
                                                 extraction + gold-comparison scores
  GET  /explorer/api/extraction-lab/history  -- run history (reuse the same store pattern as
                                                 recall_lab_store, e.g. extraction_lab_store)
```

**Fidelity contract: identical to production except for the one tuning knob under test.** This is
not a nice-to-have — it's the exact failure mode that already burned this investigation twice this
session. The first `trace_extraction_830ce83f.py` skipped `graphiti_patches.py` and reported a
production bug that was actually a harness divergence (patches not applied). The second version
applied patches but hand-picked "exactly 1 prior episode" instead of replicating production's real
`retrieve_episodes(last_n=RELEVANT_SCHEMA_LIMIT)` selection, and reported a clean pass that turned
out to be unrepresentative (a different harness divergence — arbitrary context instead of the real
selection logic). Both were false results caused by the harness quietly not matching production,
not by anything about extraction itself. `extraction_lab.py` must not repeat either mistake:

- **Client construction:** every arm builds its extraction call through the same
  `GraphitiClient.from_settings()` production uses (patches, LLM client config, base_url,
  retry/timeout, structured-output mode, registered entity/edge types) — never a hand-rolled
  client. If `from_settings()` changes, every arm (including baseline) picks that up automatically;
  a harness that reconstructs its own client can silently drift out of sync with prod and nobody
  would notice until the numbers stopped meaning anything.
- **Context selection:** when `context_episode_count` is NOT the parameter under test, the arm
  must call graphiti-core's own `retrieve_episodes(last_n=RELEVANT_SCHEMA_LIMIT)` against the
  supplied `previous_episodes` fixture list — not a hand-picked or arbitrarily-truncated subset —
  so the baseline arm's context selection is byte-identical to what a real ingest would compute for
  the same episode history. Only the Phase 3 arm(s) that explicitly test raising the limit override
  this, and only that one parameter.
- **Prompt source:** the `baseline` arm's prompt must be pulled live from
  `graphiti_core.prompts.extract_nodes` at call time (import and call the real function), not a
  string copied into the harness. Each named variant (`update_aware`, `mention_first`, etc.)
  overrides that same function's output for its own arm only — via the same monkey-patch mechanism
  `graphiti_patches.py` already uses (module-level class/function replacement before the call),
  not a parallel reimplementation — so a variant arm's diff from baseline is provably just the
  prompt text, nothing else.
- **Model/temperature:** default to whatever `LME_EXTRACT_MODEL`/`settings.llm_model` resolves to
  in production at run time, not a value hardcoded in the harness, so the harness doesn't need a
  manual update every time the production default changes (as it just did, nano → `gpt-4o-mini`).

The practical effect: the `baseline` arm (default prompt_variant, default model, default
context_episode_count) should be provably indistinguishable from a real production extraction call
on the same input — not "close," not "should behave the same," but constructed from the identical
code path. Every other arm changes exactly one declared parameter off of that shared baseline
construction. If a result ever looks surprising, "is this a harness divergence or a real finding"
should be answerable by inspection of the one changed parameter, not by re-auditing the whole
harness the way this session had to do twice already.

Fixture set: the plan's required test messages (direct named update, pronoun update, informal
location, corrected monetary value, reversal, generic non-fact, unsupported implication) plus
`830ce83f`, `852ce960`, `2698e78f` as real cases, each with hand-built gold mentions/propositions.

**Phase 1 — prompt ablation (cheapest, no architecture change).**
Run `menhir-extraction-prompt-recency-recall-research.md`'s 8 conditions against `gpt-4o-mini` (the
harness's current default, not the nano baseline the plan was written against) through the new
extraction-lab harness. This alone may close much of the gap at zero schema/pipeline cost —
Variant C (update-aware triggers: "actually," "moved back," "again") is specifically well-matched
to `830ce83f`'s own phrasing, worth prioritizing first as a sanity check before running the full
matrix.

**Phase 2 — extraction-time candidate lookup (only if Phase 1 recall is still insufficient).**
A cheap, targeted lookup — exact-name or embedding match against known `PERSISTENT` entities,
independent of `previous_episodes` — fed to the extractor as an explicit "this name is already
known to the graph" signal, so its own "when in doubt" conservatism has a reason not to fire on a
bare re-mention. Reuse `domain/retrieval_tuning.py`'s `CandidateSource` pattern (a new source, e.g.
`ENTITY_NAME_MATCH`) rather than inventing a parallel lookup mechanism — the module explicitly
exists to make exactly this kind of "different candidate source, own prior" addition uniform.
Gate behind a new `MENHIR_FRONTIER_EXTRACTION_CANDIDATE_LOOKUP`-style flag, off by default, same as
every other frontier lever.

**Phase 3 (fallback only, not a primary fix) — raise `RELEVANT_SCHEMA_LIMIT`.**
Only if Phases 1-2 don't close the gap sufficiently. Explicitly a stopgap per the RCA's own
ranking: doesn't scale, just shifts the cliff to a larger N. Do this last, not first, and don't
present it as "the fix" if it ships — see the earlier conversation turn in this session on why
this is the option in tension with the Codex plan's own "never do O(N) global scans" principle.

**Explicitly out of scope for now:** rebuilding SUPERSEDES/REFINES/CORRECTS classifiers, tentative-
link promotion, or a "Belief Explorer" UI. That layer already exists in substance
(`belief.py`/`warden.py`/`ConsolidationRepository`) and already has a negative LME result behind its
current default-off state. If it's revisited, the right framing is "why didn't the shipped
belief-gate help, now that extraction-admission is fixed" — re-run the existing
`frontier_belief_gate` A/B *after* Phase 1/2 land, rather than building a second implementation of
the same idea from scratch.

---

## Non-goals (unchanged from the Codex plan, still correct)

- No production ingestion prompt/pipeline change without a demonstrated Phase 0/1 improvement.
- No graph schema changes.
- No retrieval reranking changes (that layer is built, tested, and off for a documented reason —
  see above).

## Cross-references

- `.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md` — the confirmed root cause this plan
  is built on.
- `.agent/archive/plans/menhir-extraction-prompt-recency-recall-research.md` — Phase 1's source plan.
- `.agent/reference/../../reference/menhir-belief-supersession-temporal-chains-research.md` — the plan this document
  maps against; its retrieval/consolidation architecture proposal is superseded in practice by
  already-shipped, already-bench-tested code (see "The corrected diagnosis" above), but its
  temporal-reasoning and candidate-retrieval framing remains useful background.
- `docs/research/process/research-vs-shipped-inventory.md` — canonical shipped-vs-research map;
  re-audit before trusting the Tier assignments cited here if significant time has passed.
- `.agent/plans/menhir-research-execution-ladder.md` Track W — the current write-time-
  consolidation status this plan's Phase 1/2 continues. The archived thesis remains at
  `.agent/archive/plans/aggregation-as-consolidation.md` and owes the historical failure-census
  correction noted above; its body is intentionally not rewritten.
- `domain/retrieval_tuning.py`, `config/settings.py:273-292` — the `CandidateSource` /
  `MENHIR_FRONTIER_*` patterns Phase 2 should follow, not reinvent.
- `explorer/recall_lab.py`, `explorer/app.py:645-691` — the arm/judge/route skeleton Phase 0's new
  `extraction_lab.py` mirrors; `archolith-bench/.../trace_extraction_830ce83f.py` — the manual,
  patches-applied trace script this formalizes into a repeatable multi-arm harness.
