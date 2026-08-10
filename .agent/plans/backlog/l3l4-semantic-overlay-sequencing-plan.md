# Plan: L3/L4 semantic overlay + task-oracle runtime + Cold Start Brief (SEQUENCING)

<!-- Filename convention: <feature>-plan.md -->

**Status:** backlog — proposed 2026-07-11 — **ACTIVATION OWNER-RESERVED**
**Gap source:** `docs/research/schemas/layer4-knowledge-artifacts.md`,
`docs/research/schemas/cold-start-brief.md`, `docs/research/retrieval/oracle-runtime-interfaces.md`
(SOS Program B semantic / D institutional / E context-assembly).
**Related direction/frames:** `docs/research/direction/semantic-operating-system.md`,
`docs/research/direction/oracle-architecture.md`.

> **Reserved-sequencing banner.** Every source doc states this cluster is "the unsequenced GAP —
> ctharvey's to sequence before building." This plan records the **current default, promotion
> criteria, and a dependency-ordered path**; it does **not** authorize a build. No phase starts
> without an explicit sequencing call from the owner. It exists so the criteria/path are captured, not
> so the work is greenlit.

---

## The gap (one line)

Menhir has the L4 institutional overlay and the retrieval-oracle kernel, but the **L3 semantic
overlay + generic knowledge-artifact store + LLM proposer + task-altitude oracle runtime +
ColdStartOracle→Brief→Context Engine** are spec-only.

## Current default (code-anchored)

Shipped (reuse, do not rebuild) — **more of the L4 side is built than a first read suggests**:
- **L4 institutional types** `DECISION/FAILURE/INCIDENT` — `domain/artifacts.py:51-53`.
- **L4 R9-lite trust policy is encoded** (`domain/artifacts.py`): LLM-sourced artifact **never trusted
  on create** (review-gated, invariant 4); human artifact trusted iff **≥1 evidence anchor** (inv 5);
  promotion to TRUSTED **fail-closed without evidence** (inv 3); supersession → HISTORICAL, **never
  deletes** (inv 7). This is the same admission discipline as `foundation-typed-admission-plan.md`.
- **First-class `:Evidence` node** — `infrastructure/artifact_repository.py`, `memory_queries.py`,
  `infrastructure/schema.py`.
- **L4 service + read oracle shipped and wired into recall (gated):** `services/artifact_service.py`
  (`ArtifactService`) and `services/memory_oracle_service.py` (`MemoryOracleService` / `ArtifactMatch`);
  recall treats `artifact_type`/`anchors` as **gated L4 `Artifact` nodes** (`recall_service.py:547`).
- **Substrate**: CANDIDATE tier + promote/approve/reject, `source_confidence`, supersession/conflict,
  Entity decay, `ANCHORED_TO`/`CREATED_FROM` edges, and the **R7 OraclePacket** (the retrieval-shaped
  evidence kernel a brief is built *from*).

Unbuilt (this plan's scope):
- **L3 semantic types** `CAPABILITY/POLICY/CONSTRAINT/INVARIANT` — absent from `domain/artifacts.py`.
- **Generic `KnowledgeArtifact` store** + a clean `status`/`review_state` lifecycle field.
- **LLM proposer** (semantic-node candidate emitter) — none in `src`.
- **Task-oracle runtime** — `OracleInput`/`OracleFinding`, primitive-vs-composite taxonomy,
  composite oracles per RunMode — spec-only.
- **ColdStartOracle → ColdStartBrief → Context Engine** — spec-only.

## Notes — code re-verification (2026-07-11)

Grounding notes from reading the L4 code, so a future sequencer knows this is **additive onto a prepared
foundation, not a rearchitecture**:

1. **The ColdStartBrief seam is already reserved in code.** Both the store and the read oracle explicitly
   leave the synthesis hole for it: `artifact_repository.py:241` — "collecting them into
   fact/hypothesis/stale is the **ColdStartBrief's job**, not the store's"; `memory_oracle_service.py:13`
   — same. So the `ColdStartOracle`/brief drops into an anticipated seam; it does not rewrite the L4
   read path. The brief + task-runtime are confirmed **absent** (the retrieval `OracleResult` in
   `domain/oracles.py` is a different altitude — do not mistake it for `OracleFinding`).
2. **L3 is the genuinely-empty half.** L4 (institutional: decision/failure/incident) is largely shipped
   and wired; **L3 (semantic: capability/policy/constraint/invariant) is entirely absent** from
   `domain/artifacts.py`. When sequenced, L3 is the net-new type work; the generic store is mostly a
   `status`/`review_state` generalization of the existing artifact model.
3. **The LLM proposer rides shipped rails.** A semantic-node proposer is "just another candidate emitter
   on the existing path" — it reuses the CANDIDATE tier + the L4 R9-lite trust policy (LLM never trusted
   on create) that already exist. This is the **same admission discipline** as
   `foundation-typed-admission-plan.md` (#7); if #7 lands its basis-classifier-routes-to-CANDIDATE work
   first, the L3 proposer inherits it.
4. **Why still reserved (unchanged):** the *ordering and scope* of L3 (how much is LLM-proposed vs
   structure-imported — the highest-scope-risk call) is the owner's to sequence. These notes lower the
   *build* risk (foundation is prepared) but not the *sequencing* decision.
5. **The trust policy is convention-sound, not adversarially-sound.** `artifact_service.create` takes
   `source` and `evidence` as **caller-declared** params (`artifact_service.py:68-74`); **no layer
   verifies** that an `evidence.kind="git"` ref resolves, or that `source=HUMAN` is true (grep for
   evidence verification: empty). So the R9-lite policy correctly refuses to trust `agent_inference`,
   but it trusts *declared* source/evidence. Adversarial soundness needs `admission-capability-
   separation-plan.md` (#1: gate who may claim HUMAN) + `foundation-typed-admission-plan.md` (#7:
   verify the anchor resolves). At single-user scale the real protection is honest self-tagging by the
   harness (non-adversarial); it breaks under a buggy/adversarial writer or a multi-writer deployment.
6. **Usefulness caveat — this matters most in an all-LLM authorship regime.** In practice ~all writes
   are `source=LLM`, so `decide_status` parks nearly everything in CANDIDATE and the HUMAN/LLM axis is
   near-constant. The **only real discriminator then is whether an artifact carries a promotable
   (git/test/log/human) anchor** — which, per note 5, is currently *unverified*. Consequence: **the
   TRUSTED tier is only meaningful once evidence is verified**; without verification it is either
   near-empty (nothing earns a real anchor) or self-declarable (an LLM attaches a fake `git` ref and
   promotes itself). **Recommendation: do not invest further in L3/L4 trust *tiering* until evidence
   verification (#6/#7) lands** — in an all-LLM world the tier's value is gated on it. Verified anchors
   turn "does this LLM claim have a real, checked git/test anchor?" into a genuinely useful,
   hard-to-fake trust signal; unverified, the tier is largely decorative.

## Live measurement (2026-07-11, production Neo4j) — the decisive datum

Measured directly against the graph (50,228 Entity nodes, 1,860 Episodic):

- **L4 artifacts: ZERO.** `artifact_type` is not even a property key in the database — no
  decision/failure/incident artifact has ever been written. The L4 domain model + service + read
  oracle are shipped, but **nothing emits into them.**
- **Evidence: ZERO.** No `:Evidence` nodes and no `SUPPORTED_BY` edges exist anywhere.
- **Structural anchor coverage: ~3.7%** (1,869 / 50,228 nodes have an `ANCHORED_TO`).

**Root cause found (2026-07-11) — the L4 layer is TESTED-BUT-UNWIRED, not "shipped".** Tracing callers:
`ArtifactService` is referenced only by its own definition, docstrings, and **tests**
(`tests/test_artifact_service.py`, `tests/test_l4_artifact_loop_integration.py`); `MemoryOracleService`
(the read side) is **never constructed** anywhere in runtime; and **no MCP tool / REST route / ingest
path** calls `create_artifact`. The `artifacts.py` docstring confirms it: "the menhir-side projection of
the **bench-first L4 slice**." So the domain model + repository + service facade + read oracle were
ported into `src/` with full unit/integration coverage (fake/in-memory adapters) but **three missing
connections**:

1. **No runtime construction** — `ArtifactService` / `MemoryOracleService` are never built into
   `RuntimeProvider`.
2. **No emitter** — nothing proposes artifacts (no LLM proposer, no ingest hook, no MCP create tool).
3. **No read surface** — the L4 read oracle is not wired into recall as an active source.

**Consequence — this hard-reprioritizes the plan.** The trust tier isn't merely "decorative in an
all-LLM regime" (notes 5–6); the whole L4 layer is a **well-tested island with no runtime callers**, so
it holds zero data. The L3/L4 sequencing question is therefore *downstream* of a smaller, concrete one:
**wire the existing L4 slice into runtime + give it one emitter + one MCP surface**, then see whether
artifacts/evidence actually accrue. Only after that does trust *tiering* (and #6/#7 verification, which
today has ~nothing to verify — no evidence edges, 96% unanchored) have anything to operate on. **Do not
build L3 types or the ColdStartBrief until the shipped L4 slice is wired and emitting.** The next step
is a small wiring/emitter task, not the big L3 build.

## Promotion criteria (per the source docs' own gates)

- **supported-by-spike** when a generic knowledge-artifact store lands with **≥1 artifact type through
  the MemoryMutator write boundary (R9)** carrying provenance/confidence/valid-time/review state; and
  a `ColdStartOracle` produces a `ColdStartBrief` from primitive findings + the R7 OraclePacket
  **without mutating state**.
- **supported-by-eval** when archolith-bench measures **oracle interpretation over these artifacts /
  brief completeness / Decision-Accuracy-per-Retrieved-Token** — a *task* fixture, **not** retrieval
  recall.

## Path (dependency-ordered — each gate is owner-sequenced, not auto-started)

1. **L3 types + generic KnowledgeArtifact store.** Extend `domain/artifacts.py` `ArtifactType` with
   `CAPABILITY/POLICY/CONSTRAINT/INVARIANT`; add the `status`(OBSERVATION→…→HISTORICAL) /
   `review_state` lifecycle; reuse the CANDIDATE tier + `:Evidence` node. Deterministic
   `StructuralAnchor`s stay Layer-2, never LLM-derived.
2. **LLM proposer.** A semantic-node candidate emitter on the *existing* candidate path — proposes L3
   artifacts as CANDIDATE hypotheses; promotion to TRUSTED needs evidence + review (no LLM-minted
   facts).
3. **Task-oracle runtime.** `OracleInput`/`OracleFinding` value objects; primitive oracles (read one
   evidence class) vs composite oracles (synthesize per RunMode); deterministic reduction order.
   Reuses the shipped primitive oracles in *task mode*.
4. **ColdStartOracle → Brief → Context Engine.** Composite oracle reasons over primitives + L3/L4
   artifacts + the R7 OraclePacket → epistemic-bucketed `ColdStartBrief`; Context Engine packs the
   smallest useful provenance-tagged context (never promotes HYPOTHESIS→FACT).

All writes go through the R9 MemoryMutator boundary (see `oracle-execution-and-performance.md`);
oracles observe, combiner/ColdStartOracle decide, only the Mutator writes.

## Non-goals

- Do not build any phase without an owner sequencing call (reserved banner).
- Do not collapse the two oracle altitudes (retrieval vs task) into one object.
- Do not re-own the four-layer SOS vision, evidence model, or the combiner math (owned elsewhere).
- Do not invent a numbered execution-ladder rung from the spec docs.

## Risks

- **Scope + sequencing risk** is the whole reason for the reserved banner — L3 (capability/policy) is
  the highest-scope-risk part (how much is LLM-proposed vs structure-imported).
- Feature accumulation: gate every new artifact type / composite oracle through a query-class the D0
  view-entropy probe measures as high-cost (the knowledge-compilation-registry discipline).

## Source

`schemas/layer4-knowledge-artifacts.md`, `schemas/cold-start-brief.md`,
`retrieval/oracle-runtime-interfaces.md`; code state confirmed 2026-07-11 (L4 types + `:Evidence`
present; L3 types + generic store + proposer + task runtime absent).
