# HANDOFF — ScalarStateView e2e blocker is an INGESTION/PROVENANCE defect (not Piece C)

**Date:** 2026-07-20
**From:** Claude Code (archolith-bench, scalar-state e2e harness)
**To:** Menhir ingestion / Graphiti-integration owner
**Class:** NEW ingestion/provenance integration defect. **Do NOT treat as a reopen of the frozen Piece C
bind/fold/View machinery** — that machinery is proven to work (see section 6). This is upstream, at the
menhir<->Graphiti entity-extraction/linkage boundary.

---

## 1. One-paragraph summary

The archolith-bench `menhir-scalar-state` harness proved Piece C's `bind -> fold -> scalar_state View`
path works end to end (a `wake_time` View materialized against a correctly-linked `Alice` entity). But
on a multi-fact first-person/third-person ingest, typed-scalar assertions almost never bind, because the
**subject entity they need is never persisted on its own episode.** A bounded per-call root-cause pass
(ingest the three "Alice" statements one at a time, snapshot the graph after each) shows the FIRST
incorrect boundary is at **individual `add_episode` extraction**: a lone `"Alice owns 37 coins."`
persists **zero** entities. The subject only ever materializes later, when a subsequent episode pulls
prior episodes in as context and re-extracts those entities **attributed to the latest episode** — so
provenance/linkage lands on the wrong episode, and the binder (which is episode-scoped) can never find a
candidate for the originating episode.

---

## 2. Exact reproducer

Repo `archolith-bench` (branch master). A running throwaway is brought up + torn down by the script.

```bash
# A) Per-call provenance capture (the decisive evidence; ingests 3 Alice statements one at a time):
SS_DIAG=1 bash scripts/run_scalar_state_e2e.sh

# B) View-level repro (materializes a View for the one episode that DOES get a linked entity):
SS_FIXTURE=third-party bash scripts/run_scalar_state_e2e.sh --keep
python scripts/inspect_scalar_state_graph.py bolt://localhost:7691 scalarthrowaway
```

Throwaway launch profile (in the script): fresh ephemeral Neo4j (bolt 7691), menhir serve with
`MENHIR_BENCHMARK_MODE=0`, `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED=1`,
`MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED=1`, short interval, isolated `MENHIR_MCP_TELEMETRY_DB`
(so the throwaway scheduler is not blocked by the operator's live-menhir maintenance-scheduler lease),
model `gpt-4o-mini`.

---

## 3. Per-call arguments at the boundary

Menhir generates the `:Episodic` UUID and passes to Graphiti (enrichment_steps.py `add_episode_with_timeout`
-> `graphiti_client.add_episode`): `name`, `episode_body` (`"user: <stmt>"`), `source_description`,
`reference_time`, `episode_uuid` (menhir-generated), `group_id`. **Menhir passes NO `previous_episodes`
payload** — any cross-episode context is fetched INSIDE graphiti_core, keyed on `group_id`. Each of the
three ingests used a DISTINCT episode UUID (so this is NOT episode-key reuse / bucket 1).

---

## 4. Graph state after each call (run A) + the extraction payloads

Namespace `mentions-diag-16ce754048`. Server-side Graphiti extraction payloads (from
`menhir.infrastructure.graphiti_patches` logs) shown inline — they are the smoking gun.

**After call 1 — `"Alice owns 37 coins."`** (episode `679163f7…`)
- Graphiti LLM returned: `extracted_entities=[{"name":"Alice's coins"}]`,
  `edges=[{source:"Alice", target:"Alice's coins", OWNS, episode_indices:[0]}]`.
- Menhir logged: **`Zero-extraction enrichment (success) — Graphiti returned no nodes or edges`**.
- Snapshot: 1 episode; **linked non-View entities on episode 1: (none); plain-KG entity census: (none)**.

**After call 2 — `"Alice has read 12 books."`** (episode `85c3afdb…`)
- Graphiti LLM returned: `extracted_entities=[{"name":"Alice's books"}]`,
  `edges=[{source:"Alice", target:"Alice's books", HAS_READ}]`.
- Menhir logged: **`Zero-extraction enrichment (success) — Graphiti returned no nodes or edges`**.
- Snapshot: 2 episodes; **linked entities on episode 2: (none); plain-KG census: (none)**.

**After call 3 — `"Alice wakes up at 7:30 AM."`**
- Graphiti LLM returned (note the CONTEXT re-extraction of episodes 1-2's entities):
  `extracted_entities=[{"name":"Alice"},{"name":"37 coins"},{"name":"12 books"}]`, edges include
  `{source:"Alice", target:"37 coins", OWNS}` …
- **Enrichment FAILED**: `pydantic ValidationError: edges.2.target_entity_name Field required [missing]`
  -> `CombinedExtraction` rejected the whole batch -> `Background enrichment failed episode_id=7b36…`.
- Snapshot: still only 2 episodes (episode 3's node not persisted); plain-KG census: (none).

**Cross-check (run B, harness, where call 3 did NOT hit the ValidationError):** episodes 1-2 STILL had
zero linked entities; on call 3 Graphiti persisted `Alice`, `37 coins`, `12 books` and MENTIONS-linked
**all three to episode 3 only**; the `wake_time` typed-scalar assertion bound to that `Alice` UUID and a
`scalar_state` View materialized, while the `coins` assertion (episode 1, no linked entity) stayed
permanently `binding_pending` (the episode-scoped `repair_pending_bindings` re-resolves against
episode-1's empty link set and cannot rescue it).

---

## 5. Earliest incorrect state + classification

**Earliest incorrect boundary: after ingest call 1.** `"Alice owns 37 coins."` persists ZERO entities,
even though the extractor named `Alice's coins` and an `OWNS` edge sourced at `Alice`. The base subject
entity the binder needs is never created for its own episode. Two compounding mechanisms:

1. **Per-episode single-mention drop (earliest / most fundamental).** The extraction emits a derived
   possessive entity (`Alice's coins`) plus an edge whose SOURCE (`Alice`) is absent from
   `extracted_entities`; graphiti_core's node/edge resolution yields zero persisted nodes/edges, and
   menhir classifies it `Zero-extraction (success)` — masking the drop as a success.
2. **Context attribution to the latest episode (bucket 4 — context/provenance contract).** The subject
   entities only materialize when a LATER episode's add_episode pulls episodes 1-2 in as context and
   re-extracts `Alice`/`37 coins`/`12 books`, MENTIONS-linking them to the latest episode.
3. **Whole-batch extraction fragility.** One malformed edge (`missing target_entity_name`) makes
   graphiti_core's `CombinedExtraction` reject the ENTIRE episode's extraction (call 3 in run A) rather
   than dropping the bad edge — so a single bad row zeroes the episode.

Against the handoff decision table: **"distinct episodes, links appear only after the third call" +
"earlier messages included as context on call 3, their entities attributed to call 3"** — i.e. the
menhir<->Graphiti **context/provenance contract**, sitting on top of a per-episode extraction that drops
the single-mention subject.

**Invariant violated:** an entity mentioned in episode 1 must retain a provenance/linkage path to
episode 1. Context from later episodes may aid resolution/dedup, but it must not be the ONLY way the
entity appears, and resolution onto a shared node must not collapse mention-provenance onto the latest
episode.

---

## 6. Suspected owning functions

- `graphiti_core.graphiti.add_episode` — node/edge resolution that drops an edge referencing a
  non-extracted source entity (and, here, the entire extraction), and the previous-episode CONTEXT
  inclusion + episode attribution of re-extracted entities.
- `menhir.infrastructure.graphiti_patches` — the OpenAI-compatible extraction prompt that yields a
  possessive-only entity + an edge to a non-listed source; and `CombinedExtraction` strictness (one
  missing `target_entity_name` rejects the whole batch).
- `menhir.services.enrichment_steps` — the `Zero-extraction enrichment (success)` classification that
  reports success while persisting nothing (hides the drop from health/telemetry).
- Binding side is CORRECT and should not change: `episode_lifecycle.fetch_linked_entities_for_episode`
  (`(:Episodic)-[]-(:Entity)` minus views) and `typed_scalar_perception._bind_subject` (exact-name,
  fail-closed) both did the right thing — they simply had no candidate to bind because extraction never
  produced one for the episode.

---

## 7. Two causal defects + one robustness issue (keep separate)

1. **Current-episode extraction closure** (causal): entities referenced by ACCEPTED relationships must
   exist in the extracted entity set — or be recoverably added — before resolution. Today an edge
   sourced at a non-extracted `Alice` yields zero persisted nodes.
2. **Context provenance isolation** (causal): prior episodes used as extraction CONTEXT must not have
   their mentions attributed solely to the current episode. Today episode-1/2 entities land MENTIONS-only
   on episode 3.
3. **Malformed-edge whole-payload rejection** (robustness, separate): one edge missing
   `target_entity_name` discards the entire episode's valid entities via `CombinedExtraction`. Track
   apart from the two causal blockers.

## 8. Menhir-side acceptance matrix

Do NOT accept merely because `Alice` eventually exists. The same three-call reproducer must prove:

- [ ] Call 1 creates or resolves `Alice` and links `Alice` to episode 1.
- [ ] Call 2 retains episode-1 provenance and links `Alice` to episode 2.
- [ ] Call 3 does not move or collapse earlier MENTIONS edges onto episode 3.
- [ ] Prior context may aid entity resolution but cannot become current-episode provenance.
- [ ] A relationship referencing a missing extracted endpoint cannot silently produce
      "zero-extraction success."
- [ ] One malformed relationship does not discard otherwise-valid entities unless the entire payload is
      explicitly failed and retried.
- [ ] The episode-1 scalar assertion binds WITHOUT requiring a later episode.
- [ ] The resulting View uses episode-1 evidence/provenance.
- [ ] Re-running ingestion is idempotent — no duplicate entities or MENTIONS.

**Minimal regression (subset):** ingest ONLY `"Alice owns 37 coins."` into a fresh namespace; expect a
persisted non-View `:Entity` MENTIONS-linked to THAT episode, and (scheduler on) exactly one
`scalar_state` View bound to it. Current actual: zero entities, zero Views.

### Bench post-fix verification (2026-07-20) — evidence for the owner to mark rows

Fix verified present: menhir `bddf0fc` (close combined-extraction edge endpoints, detect collapse) +
`7fe480c` (extraction receipt in the parent task, before the `wait_for` boundary). Re-run against a
throwaway (menhir HEAD `7fe480c`, real LLM). Bench commit `fe89416`.

SUPPORTED by the `SS_DIAG=1` three-call reproducer (per-episode graph snapshots) — rows 1-6:
- Call 1 (`"Alice owns 37 coins."` alone) persists `Alice` + `Alice's coins` MENTIONS-linked to
  episode 1 (was: ZERO entities / "Zero-extraction success"). This is also the minimal-regression
  ENTITY leg, green.
- Calls 2/3 keep each episode's own entities (books→`Alice,12 books`; wake→`Alice,7:30 AM`); no prior
  object is moved/collapsed onto a later episode. `Alice` correctly accrues MENTIONS to all three
  (canonical subject, not the bug). Prior context did not become current-episode provenance.

PARTIALLY supported — still needs a dedicated run before the owner checks these:
- "Episode-1 scalar assertion binds WITHOUT a later episode" + "View uses episode-1 evidence": only the
  `wake_time` (episode 3) assertion emitted+bound+materialized this run; `coins`/`books` did NOT emit a
  typed assertion (perceiver yield / per-job LLM budget starved by self-config perception — see §5 of the
  Bench plan). So the ENTITY leg of the coins minimal-regression is green but the coins→assertion→View
  leg is NOT yet confirmed. Re-run the coins-only minimal regression in isolation to close it.
- "Re-running ingestion is idempotent": assertion-level idempotent (`dup_keys=0 dup_slots=0`), but the
  third-party run showed 2 `:Episodic` per statement and some duplicate possessive entities — confirm
  entity/MENTIONS-level idempotency in the isolated minimal regression.

Third-party fixture harness (`SS_FIXTURE=third-party`): verdict PASS (`controls_clean`, no dup
keys/slots, no default-silo leak, `Alice` binds on all three episodes, one clean `wake_time` assertion).

## 9. Post-fix Bench re-run sequence (only AFTER the matrix passes)

1. Re-run the three-call diagnostic (`SS_DIAG=1`) — matrix must pass.
2. Re-run the third-party fixture (`SS_FIXTURE=third-party`) — expect a View per Alice slot.
3. Re-run the first-person default fixture — measure how much is still unbound.
4. ONLY THEN is the canonical self/`user` entity the next lever (before the fix it would MASK this).

---

## 10. Levers explicitly kept paused (per direction)

Do NOT, before this defect is fixed: add a canonical `user`/self entity (would MASK the linkage gap by
making first-person pass while ordinary named entities still fail to link); tune the scalar perceiver;
or start Phase D recall authority. This linkage defect gates whether episode-scoped binding/repair can
ever see legitimate candidates, so it precedes broader ingest-quality interpretation.

---

## 11. clock_time `view_value=0.0` — expected, CONFIRMED 2026-07-20

**Confirmed:** the post-fix `SS_DIAG` run enriched the wake episode and materialized the clock_time View
with `ss_kind='clock_time' ss_value='07:30' ss_display='07:30' view_value=0.0`. `ss_value`/`ss_display`
carry the real value; `view_value=0.0` is the intended numeric mirror for a string kind, NOT a
materialization bug. (Guard remains: a consumer reading `view_value` for a non-numeric kind is the real
bug to prevent.)


`view_value` is a numeric compatibility mirror; string kinds like `clock_time` deliberately get `0.0`,
and the real value belongs in `ss_value`/`ss_display`. In run A the wake episode failed to enrich, so no
`clock_time` View was available to confirm `ss_value='07:30'`/`ss_display`. The diagnostic already
queries these — a run where the wake episode enriches will confirm. Treat as EXPECTED (not a
materialization bug) unless `ss_value`/`ss_display` are ALSO wrong; a consumer that reads `view_value`
for a non-numeric kind would be the actual bug to guard against.

---

## 12. Harness assets (archolith-bench master)

- Reproducers: `scripts/run_scalar_state_e2e.sh` (`SS_DIAG=1` per-call mode; `SS_FIXTURE=third-party`),
  `scripts/diagnose_mentions_provenance.py`, `scripts/inspect_scalar_state_graph.py`.
- Harness: `archolith_bench/harness/menhir_scalar_state.py` (+ `menhir-scalar-state` CLI benchmark).
- Commits: 9aa7346, 967672c, 90da43b, a825189, 30f41ea (+ this pass).
