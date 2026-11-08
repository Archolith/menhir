---
artifact_schema: 1
artifact_uuid: 7fc25994-26a7-4b29-93a2-5a2597b55fae
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir ingest decision cost: Jev vs single-vendor levers

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Current state (partial: Phase 0 in part via #205; Phase 1 not built; Phase 2 not evidenced; Phase 3 conditional) (was: DRAFT, planning only, nothing implemented - 2026-09-29)
Last verified: 2026-10-21 - PARTIAL (medium). #205 (f034bfc5) keeps merge lineage (merge_audit, merged_from, last_merge_op_id) out of Graphiti prompts; it is not the plan's allowlist design, other bookkeeping fields (last_accessed, session_id, namespace, ...) were not checked, and the Phase 0 replay acceptance gate (>=97% duplicate agreement, no extra false merges, AMA ep90 counts unchanged) is not evidenced. Phase 1: graphiti_llm_adapter.py:366-383 still has only the global MENHIR_GRAPHITI_DISABLE_REASONING switch.
Scope: Menhir enrichment calls after extraction - EdgeDuplicate, NodeResolutions, EdgeTimestamps /
BatchEdgeTimestamps. CombinedExtraction is out of scope except as a cost reference.

## Decision

Do not add Jev (TypeSafe) as a required second vendor. Pursue two single-vendor levers first; they are
larger than Jev's best measured case. Keep Jev as an optional, off-by-default backend behind a decision
interface, and revisit it only if a trigger in section 5 fires.

## Evidence (all measured this session, public AMA-Bench data)

Luna capture `jevcap`: 4 AMA episodes (ep0/30/90/141), 3,307 calls, $0.99. Spend share and cost split:

| Step | Share of ingest spend | Avg input tok | Avg reasoning tok | Cost that is input / reasoning |
|---|---|---|---|---|
| EdgeDuplicate | 35% | 1,055 | 250 | 43% / 51% |
| CombinedExtraction | 28% | 3,564 | 666 | 39% / 37% |
| NodeResolutions | 22% | 10,467 | 137 | 92% / 6% |
| Batch/EdgeTimestamps | 8% | 434-730 | 27-125 | 37-64% / 20-31% |

Official Jev (jev-1.13.0) on 160 sampled calls, 1,273 decisions ($0.018 vs Luna ~$0.066 for the same calls):
- Contradiction: 91% agreement (majority 82%), recall 94%; at noul >= 0.8 decides 72% of pairs, 0 missed.
- Duplicate recall 44%. Entity match (choice) 78% agreement, recall 92%. Timestamps: below majority.
- Whole Luna calls skippable (every decision confident): EdgeDuplicate 10% at 0.8 / 5% at 0.9; NodeResolutions
  15% at 0.95. Candidate pruning at 0.9 (0 misses) keeps 87% of EdgeDuplicate input chars, and input is only
  43% of that step's cost: the fixed ~2,450-char prompt and the reasoning output dominate.
- Net: Jev costs ~27% of Luna on every call it screens, so skip/prune savings roughly break even.
- Open clones (AgentJev-0.6B, open-jev-deberta) were below majority baselines: dropped.

Two findings from the same data:
1. NodeResolutions candidates are serialized as `{**candidate.attributes, candidate_id, name, entity_types,
   summary}` (fork `graphiti_core/utils/maintenance/node_operations.py` ~line 569). Menhir's node bookkeeping
   rides along: `merge_audit` alone is 64% of the EXISTING ENTITIES block, plus last_accessed, session_id,
   namespace, source_confidence, user_id, scope, etc. EXISTING ENTITIES is 88% of the user prompt; keeping
   only candidate_id/name/entity_types/summary shrinks that block to 16% of its size.
2. 34 of 3,352 NodeResolutions candidates came from another namespace (`ama-measure-dsflash-low-ep90` inside
   `ama-jevcap-ep90`). Not yet diagnosed.

## Bloat inventory (deep dive, jevcap capture + fork prompt builders)

| # | Where | Finding | Size | Fix under test (proxy arm) |
|---|---|---|---|---|
| B1 | NodeResolutions | Candidate `**attributes` spill (merge_audit 64% of candidate block) | 22% of spend; prompt -> 27% with fix | `node_slim` |
| B2 | Menhir merge judge (`LLMAdapter.confirm_same_entity`, correlation_service, 3 votes/proposal) | `max_tokens=64` with a reasoning model: 364/650 calls (56%) spent all 64 tokens reasoning -> empty -> `None` vote. 91 proposals got [None,None,None]. **Correctness bug**: merges rejected for lack of an answer. Graphiti's `MENHIR_GRAPHITI_DISABLE_REASONING` does not cover these calls. | 650 calls, 5% of spend | `text=none` effort |
| B3 | EdgeDuplicate | Fixed boilerplate is 66% of the prompt (rules stated twice + example block after the data) | 35% of spend; prompt -> 71% with fix | `edge_compact` |
| B4 | EdgeDuplicate | Reasoning = 51% of step cost for mostly-empty answers | ~18% of spend | `EdgeDuplicate=none` effort |
| B5 | CombinedExtraction | ~12k chars (~2.9k tok) of static rules + LoCoMo-style negative examples = ~80% of input. Static prefix is cache-eligible (Luna cache read $0.01/M vs $0.10/M) - whether it is actually cached is unmeasured (proxy now logs cached_tok). | 28% of spend | `extraction_no_negative_examples`, effort low |
| B6 | EdgeTimestamps | 217 single-fact calls pay ~1k chars of fixed text each although a batch prompt exists | 1.5% | not proxy-testable (needs batching in fork) |
| B7 | Ingest worker | `LlmBudgetExceeded` raised 13 times (per-job cap 40 in the AMA config) - some enrichment is silently cut | quality | config review |
| B8 | Retries | 175 calls refused 402 (OpenRouter in-flight credit) and retried | cost/latency | top-up / lower concurrency |

Tooling (bench branch, uncommitted at the time): `archolith_bench/ci/prompt_transforms.py` (named transforms +
per-schema effort), budget proxy flags `--transforms`, `--schema-effort`, `--price-cache-read`, full-request body log;
`scripts/ama/replay_ab.py` (control arm = noise floor vs captured original; per-schema decision agreement + tokens);
tests `tests/test_prompt_transforms.py`, `tests/test_replay_ab.py`. Runner: a local script
(stage 1 capture ep90 all bodies, stage 2 replays). Stage 3 (end-to-end ingest + graph/QA compare) follows for winners.

## A/B results (abcap = ep90 Luna-default capture, 226 calls, full requests logged)

- Single call, `node_slim` (NodeResolutions #79, 20 candidates, no merge_audit yet): identical decisions, input 3,966 -> 2,206 tok (-44%).
- Prompt caching (B5): 0 cached tokens across the whole ingest and across 6 sequential distinct CombinedExtraction
  prompts sharing a ~3.3k-token prefix, with or without `prompt_cache_key` (provider=OpenAI). Only exact repeats cache.
  Closed for this route. Caveat for A/B cost: replayed control arms are exact repeats and bill as cached - compare raw tokens.
- Prompt caching root cause (2026-09-30, direct OpenAI + OpenRouter, ~$0.01): layout is already cache-friendly
  (CombinedExtraction ~3.4k-token identical prefix = 97% of the prompt, identical response_format schema;
  EdgeTimestamps 83% but < 1,024 tokens; EdgeDuplicate 25%; NodeResolutions 4%). Fresh-prefix test, 3 prompts
  sharing ~3k tokens with different tails: gpt-6-luna 0/0/0 cached, gpt-5.6-luna 0/0/0, gpt-4o-mini 0/2176/2176.
  Exact repeats cache fully on every route. ~~Luna only caches whole-prompt repeats~~ **Corrected same day:**
  GPT-5.6+ (incl. Luna) matches cached prefixes only at message boundaries / explicit breakpoints (OpenAI prompt
  caching guide), and our prompts keep the static rules inside the user message after a ~170-token system
  message, so no eligible boundary exists. Same 4 real distinct CombinedExtraction prompts, direct OpenAI:
  today's layout 0 cached; static as a separate user message 0; **static rules moved into the system/developer
  message: 3,478 of ~3,560 tokens cached (98%) on every call after the first**; explicit
  `prompt_cache_breakpoint`: 3,479. Payoff from the abcap trace: CombinedExtraction is 34% of ingest spend and
  ~47% of that is input -> **~13.5% of total ingest spend saved**. Other steps' static parts are 130-280 tokens
  (below the 1,024 minimum), so they don't benefit. Fix belongs in the Graphiti fork prompt templates (move
  static instructions into the system message), with a replay A/B to confirm outputs are unchanged.
- Single call, `edge_compact` (#126): same answer as the replayed original, input -22%, but reasoning 26 -> 176 tok, so the
  call cost more. Doubtful as a standalone change.
- EdgeDuplicate reasoning off, 30 calls (all 13 flagged + 17 unflagged), arms A/A2 = control twice, B = effort none:
  - Noise floor: A vs A2 agree 85.0% (60 units). Luna-default is unstable on dedup judgments.
  - B vs controls 78.3%. Contradiction recall vs "either control flagged": A 9/11, A2 8/11, **B 4/11**; B also adds
    wrong contradictions on calls neither control flagged. Duplicates too few to judge (2-3).
  - Cost: B $0.0036 vs A $0.0068 (-47%).
  - **Verdict: reject effort=none for EdgeDuplicate** (contradictions drive stale-answer quality).
- EdgeDuplicate effort=low, same 30 calls, two more control runs (4 control runs total):
  - low vs the 4 controls 92.5% (controls vs each other 86.7-88.3%); contradiction recall 7/10 on calls >=2 controls
    flag (controls 7-9/10); 0 contradiction flags on unflagged calls (none-arm: 4).
  - Cost -9% (reasoning 4,671 vs ~5,850 tok per 30 calls).
  - **Verdict: safe (within noise) but small: ~3% of ingest spend.** Low priority; not worth a per-step policy on its own.
- Merge judge (B2), all 36 captured `confirm_same_entity` calls, arms A = as shipped (max_tokens 64), B = reasoning off,
  R = reasoning on with max_tokens 1024:
  - A: 23/36 empty (64%) -> None votes; bug reproduced on replay. (Capture itself: 26/36 empty + 1 truncated "S".)
  - B: 0 empty, SAME 24 / DIFFERENT 12, cost -43% vs A. R: 0 empty, SAME 16 / DIFFERENT 20, cost +70% vs A.
  - B vs R agree 26/36; 9 of 10 disagreements are B=SAME where R=DIFFERENT (e.g. X_ATTRIBUTES vs X_ATTRIBUTES/TIMESERIES,
    "CYBERSYN schema" vs "CYBERSYN" directory). Cases are ambiguous, but reasoning-off merges more.
  - **Verdict: fix by giving the judge room to answer (max_tokens ~1024, reasoning kept), not by disabling reasoning.**
    Merges need 3 unanimous votes and are hard to undo, so the conservative reasoned judge is the safer default.
    Cost impact ~+3.5% of ingest (judge is ~5% of spend). Same bug shape likely in `confirm_contradiction` (max_tokens 64).
  - Extra bloat seen: the 3 votes per proposal send the identical prompt at temperature 0 - candidates for 1 vote + tie-break.
  - Filed: https://github.com/Archolith/menhir/issues/203 (None votes route proposals to conflict, not just "no merge").

- NodeResolutions on merge-heavy calls (jevcap calls carrying merge_audit, request template grafted from abcap - one
  identical template), 60 calls / 102 decisions where controls A and A2 agree:
  - A fresh control (A3) matches the A/A2 consensus 93.1% (7 deviations).
  - `node_drop_audit` (drop merge_audit / merged_from / last_merge_op_id only): **93.1%**, 6 wrong-merge deviations,
    merges proposed 81 (controls 81/81/83). Input 1,046k -> 222k tok (-79%), cost -75%. **Winner: at noise.**
  - `node_slim` (keep only id/name/types/summary): 87.3%, 13 wrong-merge deviations, 85 merges. Input -88%.
    **Rejected: over-strips** (e.g. resolved the function `_words_group()` to the file `util.py`).
  - Across all 195 jevcap NodeResolutions calls drop_audit cuts prompt chars 56% -> est. **~11.6% of total ingest spend**.
  - merge_audit grows with every merge (largest captured NodeResolutions prompt: 131k chars), so the saving grows with
    graph age and it also removes a context-overflow risk.
  - Lineage also reaches batch summaries (`'attributes': node.attributes`) and typed attribute extraction.
  - Implemented Menhir-side (no fork release): `_ProviderExtrasCompletions.create` strips the three keys from tagged
    JSON blocks, re-serialized with the fork's `to_prompt_json`. Commit 069467d6, branch
    fix/strip-merge-lineage-from-prompts. Full suite 9,924 passed / 0 failed. Live ep141: 0/60 NodeResolutions
    requests carried lineage (baseline 46/55) with 13 merged entities in the graph; judge fix (#204) also visible live
    (26 confirmed merges, 0 all-None rejections, 0 empty completions). Follow-up: native exclusion in the fork.

## Plan

### Phase 0 - Slim the NodeResolutions candidate payload (no vendor, largest safe win)
- Change: project candidate attributes to an allowlist before the dedup prompt. Preferred: only the typed
  entity-schema attributes Graphiti intends to show (custom entity types), never Menhir bookkeeping
  (merge_audit, merged_from, last_merge_op_id, last_accessed, session_id, namespace, user_id, source*,
  scope, user_flagged, corroboration, edge_count). Location: fork `node_operations.py` candidate context,
  or a new fork seam Menhir fills (preferred over a denylist in the fork).
- Also check: whether `merge_audit` snapshots can carry erased or cross-namespace content into prompts
  (see `menhir/domain/erasure.py`). If yes, this is a privacy fix as well as a cost fix.
- Estimate: NodeResolutions input -~74% -> step cost -~68% -> ~15% of total ingest spend.
- Acceptance: replay the captured NodeResolutions prompts slimmed vs original on Luna; agreement on
  duplicate_candidate_id >= 97% and no increase in false merges; AMA ep90 entity/fact counts unchanged
  within run-to-run noise.

### Phase 1 - Per-step reasoning effort (no vendor)
- Change: `_provider_extra_body` in `menhir/infrastructure/graphiti_llm_adapter.py` already injects
  provider extras per request and sees `response_format`. Add an opt-in policy keyed by schema name, e.g.
  EdgeDuplicate / NodeResolutions / *Timestamps -> effort none or low; CombinedExtraction unchanged.
  Today only a global switch exists (MENHIR_GRAPHITI_DISABLE_REASONING).
- Why: reasoning is 51% of EdgeDuplicate cost for mostly-empty answers. The earlier global "low" run lost
  outcome facts; that loss is more likely extraction than dedup, but unverified - hence per-step.
- Estimate if quality holds: EdgeDuplicate -~50% -> ~18% of total; timestamps ~2%.
- Acceptance: replay the captured EdgeDuplicate prompts at effort none/low vs Luna-default labels:
  contradiction recall >= 97% of default and duplicate recall >= 95%; then the AMA current-state
  stale-answer check (pools repeats) must not regress.

### Phase 2 - Eval harness into Bench (prep for every phase)
- Move the scratch tools into Bench: proxy body logging (`--log-bodies`, uncommitted at the
  time), `jev_replay.py` decision builder, `jev_score.py` scorer (per-step agreement,
  positive recall, coverage/error/missed at cutoffs, call-level coverage), and a "replay prompts through a
  model/effort" mode. Keep the jevcap capture as the fixed test set.

### Phase 3 - Optional decision backend (only if a trigger fires)
- Interface: a Menhir decision gate with backends `off` (default) | `rules` | `jev`.
  - Nodes: reuse the fork's existing `NodePreResolutionHook` (decide before search) and `CandidateFilterHook`
    (drop candidates). Rules backend: normalized exact-name match.
  - Edges: needs a new fork seam, an edge-candidate filter mirroring `CandidateFilterHook`, called in
    `resolve_extracted_edge` before the EdgeDuplicate prompt.
  - Contradiction screen only removes candidates Jev clears at >= 0.9; duplicates stay with the LLM.
- Kill criteria: net cost saving < 10% of the gated step, any missed contradiction on the replay set, or
  AMA stale-answer regression.

### Deferred
- Batch EdgeDuplicate (several new facts per call to amortize the fixed prompt) - larger fork change.
- A rules/NLI tier for timestamps (reference-time default + date parser).
- The cross-namespace candidate leak (finding 2) - separate bug; needs its own diagnosis before any fix.

## Vendor question

Requiring users to hold a TypeSafe key alongside their LLM provider is not realistic for Menhir's setup
story. Options, in order of preference:
1. Single-vendor levers (Phases 0-1): same provider, config-only for users.
2. Jev reachable through OpenRouter as a typed-decision model: no second vendor for OpenRouter users.
   Today only `typesafe/jev-router` (a chat router) is listed; `openrouter.ai/~typesafe/jev-latest` 404s.
3. Optional power-user backend with a user-supplied TypeSafe key, off by default.

## Triggers to revisit Jev
- Jev callable via OpenRouter (or another provider Menhir already supports) with typed questions.
- After Phases 0-1, the remaining gated-step spend still justifies a screen (re-measure first).
- TypeSafe publishes zero-retention terms for non-enterprise use (needed before real memories are sent).

## Risks
- Reasoning-off may lower contradiction recall -> stale answers; the acceptance gate above is binding.
- Payload slimming removes context the LLM may have used (e.g. entity types beyond labels); replay measures it.
- All numbers come from 4 AMA episodes and one Jev sample (seed 11); treat estimates as +/- a few points.
