---
artifact_schema: 1
artifact_uuid: 2c37393a-1bb4-4ae3-84ea-f4f60c1624f3
artifact_type: plan
artifact_status: IMPLEMENTING
---

# menhir: two-tier self identity (anchored + agent-inferred user)

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Current state (phases 1-3 and 5 shipped in #355, flag default off; phase 4 is report-only (8360b3c), and the fold tool is not built and is tracked in Menhir #371) (was: DRAFT, design decisions recorded (section 5); implementation not yet approved; no code changed. Partial: phases 1-3 and 5 merged in #355; phase 4 report-only)
Last verified: 2026-10-21 - PARTIAL (high); PR #355 merged 2026-10-21 (fe147510745c) with commits 8d03fde, dfa2421, 8360b3c, 6b17324, ca06c9f, f10560a, 28c0820; Menhir main scan finds the inferred-tier symbols and 0 fold symbols; #343 closed; #353 merged (the Phase 0 merge fact loss is fixed); #169 still open
Date: 2026-10-21. Related: #343 (duplicate user facts under enforce), #169, #168 (E2E-19 D4 finding).
Code anchors are from branch `feat/168-fresh-install-e2e` @ b05efde.

## 1. Goal

Every namespace has at most two deterministic "user" identities:

- **Anchored self**: today's canonical self. Written only by binding from a verified,
  verbatim user-turn projection (the turn-capture hook path).
- **Inferred self**: new. Written when an agent-authored episode (no hook, or the client's
  own `add_memory`) refers to the human it works for, and the extraction model chose the
  inferred marker for it.

Safety property: **no fact, node or authority ever flows from the inferred tier into the
anchored tier**, and neither tier is ever merged into the other or into an ordinary node by
name matching. Clients without the hook get one stable "user" node instead of forks;
clients with the hook keep the anchored guarantees unchanged.

## 2. Constraints from current code

- **A name is never provenance.** `infrastructure/self_binding.py:14-20, 238-245`: no name
  shape ("user", "I") promotes a node; a "user" can be an RBAC role or a `users` table, and
  "I" can be reported speech. The inferred tier must therefore be declared by the model
  through a marker, exactly like the anchored tier, not by renaming self-like names.
- **The anchored marker is projection-only.** `domain/self_identity.py:288-326`: the
  endpoint is built only for a verbatim, single-evidence, user-sourced projection; a
  reserved-prefix collision refuses the episode.
- **Unmarked self-like endpoints take the ordinary path.** `graphiti_extraction_policy.py:833-840`
  rewrites "I/me/user" endpoints to an ordinary node named "user" ("availability recovery, not
  identity resolution"). Assistant-turn self echoes are dropped (`:818-823`). Tests encode this:
  `tests/test_turn_role_extraction_policy.py`, `tests/corpus_self_binding.py`, `tests/test_self_binding.py`.
- **The anchored node is a deterministic plain `:Entity`.** `episode_lifecycle.py:1240-1284`
  (`ensure_self_entity`): `uuid5`, `is_self=true`, `entity_role='self'`, name "user". Forks are
  only reported (`SELF_FORKS_REQUIRE_MIGRATION`); consolidation is an operator-only journaled
  migration with an approved UUID manifest (`:1286-1319`, `tests/test_self_fork_non_destructive.py`).
- **Merge protection keys on `is_self` / `entity_role='self'`.** `correlation_queries.py:146-152`
  (`_INELIGIBLE_ROLE_PREDICATE`) -> `domain/merge_eligibility.py:177` `INELIGIBLE_ROLE`. This is
  why #343's two "user" nodes never consolidate.
- **Resolution bypass keys on the same attributes.** `graphiti_resolution_policy.py:113-118, 148-153`.
- **Recall's first-person routes target the anchored uuid only.**
  `services/recall_pipeline.py:1282-1295` (View subject gate) and `:2521-2537`
  (event-history authority). For a client without the hook, the anchored node is empty, so
  these routes find nothing about "me" except via display-name matching of observations.
- **Binding mode is off/observe/enforce, fail-safe to off** (`self_binding.py:57-79`), default
  `enforce` in `config/settings_model.py:220`, registered in `config/feature_flags.py:42`.

## 3. Proposed steps

### Phase 0: reproduce the Qwen loss deterministically (precondition)

E2E-19 on qwen3.6-35b-a3b: binding fired (`endpoints=1`), yet the canonical node held only a
similarity link and the fact sat on an ordinary "user" node. Identify why before designing
around it: edge dedup across endpoints, ordering, or the anchored rewrite losing the edge.

- Files: none changed. Replay the saved episode texts through the deterministic provider with
  a fake that ALSO extracts "user" from the unmarked client episode (the Qwen shape), against a
  throwaway Neo4j, and trace `resolve_extracted_edges` for the projection episode.
- Output: one-paragraph root cause appended to this plan. Phases 1-2 depend on it.

**Phase 0 result (2026-10-21, from the saved Qwen run logs + code; not yet reproduced
deterministically).** Not a self-identity bug. Sequence in
the E2E run's server log:
1. Client episode `53b7ba69` (source claude-code) created ordinary `user` (165fd843),
   `user's cobalt postcards` (45c67951), `north cabinet` (ade993c9) and the OWNS fact.
2. Projection `ed6aadbc` bound the marker (`endpoints=1`), pre-resolved the canonical self, and
   the identity gate vetoed merging its `cobalt postcards` into `user's cobalt postcards`, so a new
   node 35eb1b28 was created. Both edges resolved as new (`duplicate_facts: []` twice): the
   canonical OWNS fact WAS written.
3. Post-extraction correlation then judge-confirmed and MERGED 35eb1b28 into 45c67951 (log :178,
   :183). `CorrelationQueries.merge_entity` (`correlation_queries.py:691-730`) replaces every
   relationship of the absorbed node with a fact-less `RELATES_TO {type:'bridged'}` from the
   survivor, then `DETACH DELETE`s the absorbed node, deleting its Graphiti fact edges, including
   the canonical self's OWNS fact. The canonical's remaining edge is that bridge, not a
   similarity link. The facts survive only in the merge snapshot (`capture_node_state`, :203-235).
4. The ordinary-`user` -> canonical merge proposal was vetoed (log :177), as designed.

Consequences: (a) every judge-confirmed correlation merge drops the absorbed node's facts from
the live graph, not only self facts; no open issue tracks it (#250 is the related unmerge bug);
`bridged` behavior appears in 18 test files. (Update 2026-10-21: fixed by Menhir #353, merged, "correlation merge keeps the absorbed node's fact edges".) (b) The two-tier design does not fix the Qwen case;
the merge fix does. (c) Open risk "can an ordinary user resolve into the anchored node" is
answered NO for enforce: the candidate filter excludes canonical-self candidates
(`graphiti_resolution_policy.py:347-351`) and the pre-resolution hook refuses undeclared
canonical markers (:400-407).

### Phase 1: inferred-self identity and marker (behind a new flag, default off)

1. `domain/self_identity.py`: add `inferred_self_uuid_for_namespace(ns)` (uuid5, distinct seed
   from the anchored one), an `INFERRED_ENDPOINT_MARKER_PREFIX`, and
   `inferred_subject_endpoint_for_claim(claimed)`. Eligible only when: the anchored endpoint
   is `None` for this claim, the episode is not an evidence projection, and the turn role is not
   assistant. One prompt carries at most one marker.
2. `infrastructure/graphiti_extraction_policy.py`: carry the inferred endpoint on the receipt
   next to `self_subject_endpoint` (`:456-458`); add the inferred marker to the prompt with the
   instruction "use this for the human the writer works for; not the writer, not a role, table
   or account type". Handle it in the endpoint loop like the anchored marker (`:826-832`). Leave
   the unmarked "user" normalization (`:833-840`) unchanged.
3. `infrastructure/self_binding.py`: generalize the rewrite into a tiered bind (anchored |
   inferred), sharing the rollback and marker-scrub code (`:325-426`). The inferred rewrite sets
   uuid `inferred_self_uuid_for_namespace`, name `user-inferred`, `self_tier='inferred'`,
   `entity_role='inferred_self'`, and **never** `is_self`. Its marker scrub writes "the user"
   into fact text (Decision 5).
4. `infrastructure/graphiti_resolution_policy.py:113-153`: bypass candidate search for the
   inferred node as well, so Graphiti never resolves it by name into the anchored node or an
   ordinary "user".
5. `infrastructure/correlation_queries.py:146-152`: make `entity_role='inferred_self'` /
   `self_tier` merge-ineligible, both as survivor and as absorbed.
6. `infrastructure/episode_lifecycle.py`: `ensure_inferred_self_entity(ns)`, and one structural
   edge anchored -> inferred (proposed type `INFERRED_VIEW_OF`) created only when both exist.
   `detect_self_forks` (`:1306-1319`) excludes the inferred uuid.
7. Config: `MENHIR_INFERRED_SELF_MODE` off/observe/enforce in `config/feature_flags.py`,
   `config/settings_model.py`, `.env.example` (the registry doc test requires the line).

### Phase 2: recall reads both tiers, by tier

- `services/recall_pipeline.py:1284-1295`: a first-person query adds the inferred uuid to
  `query_subjects` too, labelled `self_tier=inferred` in the result's provenance.
- `:2521-2537` (event-history authority): **anchored only**. An agent's paraphrase must not
  produce an authority verdict.
- Cross-tier near-duplicate facts (#343 direction 1): return the anchored fact, mark it
  corroborated, drop the inferred copy from the result. Recall-time only; nothing is deleted.

### Phase 3: skip verbatim duplicate extraction of hook turns (Decision 3)

When `add_memory` carries a `turn_evidence_uuid` whose evidence yields a projection, and the
client episode text equals the evidence text exactly (same comparison as
`self_identity.py:311-314`), store the client episode but skip semantic extraction. Otherwise
extract it normally; it is eligible for the inferred marker, so
`inferred_subject_endpoint_for_claim` must accept a client episode with a sibling projection.

- The `add_memory` -> projection code path is **not yet located** (see Coverage); name the
  file and the durable fact that links the two episodes before implementing.
- The skip must key on that durable link plus exact text equality, decided at claim time,
  never on timing or on whether the projection has already been extracted.
- If the projection later fails permanently, the skipped client episode must become
  extractable again; otherwise the turn's content is lost from the graph. Define that
  transition before implementing.

### Phase 4 (operator-only, approved by Decision 4): fold old ordinary "user" forks into the inferred node

Ordinary forks already carry agent-level trust, so folding them into the inferred node grants
no authority (folding them into the anchored node would). Use the existing journaled,
manifest-driven migration; never a write-path side effect.

Per-fork review is required (Decision 4): produce a read-only report per candidate (uuid,
namespace, facts, source episodes), and only uuids an operator approves enter the manifest.
Rejected forks stay ordinary nodes.

### Phase 5: tests and E2E

- Unit: tiered bind rollback; inferred marker never emitted on a projection; anchored and
  inferred never both in one prompt; merge_entity refuses anchored<->inferred and
  inferred<->ordinary; cross-namespace inferred declaration refused (mirror `self_binding.py:288-296`).
- Counterexamples: client episode extracted BEFORE the projection (the Qwen order) still leaves
  the fact on the anchored node; assistant-role "I fixed the bug" never binds inferred; a
  "users table" sentence with no inferred marker use stays ordinary.
- E2E-19: the test fake answers the inferred marker for the client episode; add criteria
  "anchored holds the postcard fact", "inferred holds the agent fact", "no ordinary user nodes",
  "no marker of either prefix persisted". One manual real-model pass on the local Qwen, as for D4.

## 4. Risks

- **Blocked**: Phase 0 root cause unknown. If the anchored fact is lost by edge dedup across
  endpoints, the inferred tier alone does not fix the Qwen case.
- **Blocked until answered**: whether an ordinary "user" episode can resolve INTO the anchored
  node today (Graphiti candidate search vs `graphiti_resolution_policy.py`). If it can, that is
  an existing trust leak and comes before this work.
- **Mitigable**: model puts the inferred marker on a role/table/agent "I". Damage is limited to
  the lower-trust tier; measure with `observe` before `enforce`; keep the prompt rule explicit.
- **Mitigable**: rolling deploy. Old workers' merge predicate does not know `inferred_self`, so
  the inferred node is merge-eligible there. Enable the flag only after every worker runs the
  new predicate.
- **Mitigable**: a second marker raises prompt confusion on small local models (Qwen already
  needs 1200 s timeouts). Only one marker per prompt by construction.
- **Mitigable**: a skipped verbatim client episode whose projection then fails permanently
  would leave the turn unextracted. Phase 3 must define the re-enable transition.
- **Acceptable**: paraphrased client memories still store user facts on both tiers; Phase 2
  hides the duplicate at recall, imperfectly for loose paraphrases.

## 5. Decisions (2026-10-21, owner)

1. **One inferred node per namespace.**
2. **Inferred facts feed Views/semantic recall, labelled; event authority stays anchored-only.**
3. **Verbatim skip** (revised 2026-10-21). Skip semantic extraction of a client episode only
   when its text exactly equals its linked turn evidence text (the projection already carries
   it). Anything else is extracted onto the inferred tier, and Phase 2 recall dedup handles
   paraphrases. In #343's eval every client memory was verbatim, so this alone removes that
   duplication at no extraction cost.
4. **Yes, fold existing ordinary "user" forks into the inferred node** (Phase 4), through the
   existing operator-only journaled manifest, **with per-fork review**: a node named "user" is
   not proof it is the human (role, `users` table, quoted speech). Each fork's facts are
   reviewed and its uuid approved individually; never "every node named user". Namespaces are
   chosen per run in the manifest.
5. **Inferred display name: `user-inferred`.** Consequences, applied below:
   - recall cannot find it through `SELF_TOKENS` display matching
     (`recall_pipeline.py:1292`), so Phase 2 adds its uuid explicitly (already planned);
   - `detect_self_forks` matches the exact name "user" (`episode_lifecycle.py:1312`), so it
     will not report the inferred node; the planned exclusion becomes a guard, not a fix;
   - fact prose: the marker in `fact`/`name` text is rewritten to "the user", not to
     `user-inferred`, so recalled facts read naturally. Only the node name carries the tier.
     (Judgment call; change here if you want the tier visible in fact text.)

Still open: none blocking the design. Phase 0's two checks remain preconditions.

## 5a. Implementation notes (2026-10-21, branch `feat/two-tier-self-identity`)

- **Found during Phase 1: agent-quoted words reach the anchored tier today.** `add_memory(user_statement=...)`
  records a `:TurnEvidence` with `role=user, declarant=user, source_kind=agent_quoted`;
  `create_evidence_projection` projects it as `source='user'`; neither the claim's
  `subject_endpoint_eligible` nor `self_subject_endpoint_for_claim` checks `source_kind`, so under
  the default `enforce` the agent's text binds to the canonical self. Phase 1 closes this only when
  `MENHIR_INFERRED_SELF_MODE=enforce` (`exclude_agent_quoted`): such projections then go to the
  inferred tier. With the flag off the behavior is unchanged; fixing it unconditionally is a
  separate decision.
- **Inferred marker failure mode.** The tier carries no authority, so an invalid marker transport
  (duplicate, malformed, orphaned, prefix in text/context) withholds or quarantines the marker and
  its edges instead of failing the episode. Nothing marker-shaped reaches resolution.
- **First person.** The inferred marker takes I/me/my only when the structured turn role is
  `user`; otherwise the prompt says the writer's own I is not the human.
- **Resolution.** Inferred-self nodes are excluded from every candidate pool and refused at
  pre-resolution unconditionally (no prior behavior to preserve); only the bound node resolves.
- **Link edge.** `(anchored)-[:INFERRED_VIEW_OF]->(inferred)`, MATCH-only on both nodes, written
  after an episode binds either tier while the flag is not off.
- **Phase 3 design.** Skip decided right after claim, under the owner's lease, by one statement:
  client episode (not a projection, no diff, not previously revoked) with exactly one
  `ADMITTED_ON` turn whose text equals its content, and a projection of that turn with equal
  content, same namespace, not FAILED. It marks the client READY with
  `extraction_skipped_for = <projection uuid>`; source-memory embedding still runs first.
  Re-enable is an idempotent sweep over durable state only ("skipped client" AND "its projection
  FAILED"): reset the client to PENDING with `verbatim_skip_revoked=true` and enqueue it. Run for
  the failed projection right after its failure and periodically. A sweep needs no lock ordering,
  so a skip that commits after the projection's failure is still caught on the next sweep.
  Re-enabling on any FAILED (FAILED is retryable) can duplicate if the projection later succeeds;
  that is the pre-feature behavior and Phase 2 hides it at recall.
- **As built.** P1 8d03fde, P2 dfa2421, P3 6b17324, P4 8360b3c. Phase 3 also re-enables when the
  projection is deleted, resets `processing_attempts` to 0 on revoke, fences the skip on the
  claim's start instant as well as the owner (same worker id re-claiming cannot reuse a stale
  attempt), and sweeps on every lease-recovery pass whatever the mode. `force_reenrich` accepts
  only FAILED/PENDING, so the sweep is the only path back for a skipped client.
- **Phase 2 dedup scope.** Only entity-level `temporal_facts` on the two self items of one result;
  an inferred fact is dropped only when the anchored copy is visible in that result. Standalone
  fact mode and verb-facet items (edge uuid as item uuid) are not collapsed.
- **Phase 4.** Read-only report and manifest only; the fold is blocked (no journaled
  consolidation tool; a fold must also keep fact edges, #353).

## 6. Validation

- Phase 0: deterministic repro reproduces "canonical has no fact edge" before any fix.
- Phase 1: focused tests (`test_self_binding.py`, `test_turn_role_extraction_policy.py`,
  `test_self_fork_non_destructive.py`, `test_feature_flag_registry.py`) pass unchanged with
  the flag off; new tier tests pass with it on.
- Phase 2: recall tests show first-person queries return inferred facts labelled, event
  authority unchanged, cross-tier duplicates collapsed.
- End: E2E-19 green on the fake; one real-model pass records the graph shape in evidence.
  Full suite in CI only.

## Coverage summary

Inspected: `domain/self_identity.py:260-339`, `infrastructure/self_binding.py` (whole),
`infrastructure/graphiti_extraction_policy.py:440-534, 800-860`, `services/enrichment_steps.py:465-524`,
`infrastructure/correlation_queries.py:135-174`, `infrastructure/episode_lifecycle.py:1240-1319`,
`services/recall_pipeline.py:1270-1299, 2510-2539`, `graphiti_resolution_policy.py` (grep
context only), issue #343, test files by grep count.

Not inspected: the `add_memory` -> turn-evidence projection path (file not located);
`domain/merge_eligibility.py` beyond line 177; `services/typed_scalar_perception.py` self seam;
Graphiti's `resolve_extracted_edges`; the operator fork-migration code.

Assumptions without code evidence: the Qwen fact loss is dedup-related (Phase 0 tests it);
the resolution bypass at `graphiti_resolution_policy.py:113-153` covers every candidate path;
typed-scalar first-person assertions bind to the anchored uuid regardless of the hook.
