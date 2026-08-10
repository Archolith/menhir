# Artifacts, Provenance & Governance — current-state ledger

**Status:** living snapshot — last verified 2026-07-11 against `main` + production Neo4j.
**Companion to:** `memory-governance.md` (the *constitution* — the design ideal). This doc is the
*state ledger* — what is actually **wired / default-off / tested-but-unwired / absent** right now, with
code anchors and live measurements. When the two disagree, the constitution is the target and this doc
is the reality; reconcile before trusting either.

> **One-line summary.** The governance *substrate* is largely built and much of it is live (candidate
> tier, source-confidence tiering, view supersession, deterministic merge veto, perception
> span-grounding). The **institutional artifact + first-class Evidence layer is tested-but-unwired**
> (zero data in prod), and the **read-side judiciary (wardens / belief gate) ships default-off**. The
> gap set is mostly "wire and turn on what exists," not "invent."

---

## Live measurements (production Neo4j, 2026-07-11)

| Metric | Value |
|---|---|
| Entity nodes | 50,228 |
| Episodic nodes | 1,860 |
| L4 artifacts (`artifact_type` present) | **0** — property key not in the DB |
| `:Evidence` nodes / `SUPPORTED_BY` edges | **0** |
| Nodes with a structural `ANCHORED_TO` | **1,869 (~3.7%)** |

These four numbers drive most of the reprioritization below: the institutional/evidence layer holds no
data, and provenance anchoring is sparse.

---

## 1. Artifacts (L3/L4 institutional layer)

**Status: TESTED-BUT-UNWIRED.** The layer is real code with full test coverage, but has **no runtime
callers** — it is the "menhir-side projection of the bench-first L4 slice" (`domain/artifacts.py`
docstring).

| Piece | State | Anchor |
|---|---|---|
| L4 types DECISION/FAILURE/INCIDENT | built | `domain/artifacts.py:51-53` |
| R9-lite trust policy (pure functions) | built + unit-tested | `domain/artifacts.py` `decide_status`/`can_promote` |
| `:Evidence` node + repository | built | `infrastructure/artifact_repository.py`, `schema.py` |
| `ArtifactService` (single-writer facade) | built | `services/artifact_service.py` |
| `MemoryOracleService` (L4 read oracle) | built | `services/memory_oracle_service.py` |
| **Runtime construction** | **MISSING** | not built in `RuntimeProvider`/`bootstrap.py` |
| **Emitter** (something that creates artifacts) | **MISSING** | no LLM proposer, no ingest hook, no MCP tool |
| **Read surface** (oracle wired into recall) | **MISSING** | `MemoryOracleService` never constructed |
| L3 semantic types (capability/policy/constraint/invariant) | **ABSENT** | not in `domain/artifacts.py` |

**Evidence of the gap:** `ArtifactService` is referenced only by its own definition, docstrings, and
`tests/test_artifact_service.py` + `tests/test_l4_artifact_loop_integration.py`. No production caller.
Result: **0 artifacts in prod.**

**The trust policy itself is sound** (this is the part worth keeping): status is *computed* from
`(source, evidence)`, never accepted — an LLM-authored artifact can never be born TRUSTED; `agent_inference`
(the model's own say-so) is in `NON_PROMOTING_EVIDENCE` so it can back a CANDIDATE but never justify
trust; promotion is fail-closed without promotable evidence. **Caveat:** `source` and `evidence` are
**caller-declared and unverified** — no layer checks that a claimed `git` anchor resolves or that
`source=HUMAN` is true. So the policy is *convention-sound, not adversarially-sound*.

**Next step (not the big L3 build):** wire the existing L4 slice into runtime + give it one emitter +
one MCP surface, then see whether artifacts/evidence accrue. See
`plans/backlog/l3l4-semantic-overlay-sequencing-plan.md` (activation owner-reserved).

## 2. Provenance

**Status: PARTIAL — links exist and are now queryable; coverage is sparse; evidence-node model is
unused.**

| Piece | State | Anchor |
|---|---|---|
| `(:Episodic)-[:MENTIONS]->(node)` source links | live (written on ingest/fold) | `view_repository.py` `_link_episodes` |
| `ANCHORED_TO` structural anchors | live but **~3.7% coverage** | `memory_queries.fetch_candidate_provenance` |
| `source` + `source_confidence` per node | **live at ingest** | `ingest_service.py:468` (`source_confidence_for`) |
| First-class `:Evidence` / `SUPPORTED_BY` | **built, 0 in prod** (see §1) | `artifact_repository.py` |
| **`get_provenance` MCP tool** (receipts by id) | **NEW, live** (readonly) | `mcp/tools/ops/get_provenance.py` |

**`get_provenance(node_uuid)`** (shipped 2026-07-11) is the "show me the receipts" affordance: given a
node, it returns the source episodes that `MENTIONS` it (+ evidence + anchor paths), so an agent gets a
compact summary by default and can deterministically verify it against its sources on demand. Verified
end-to-end against prod (returned 3 real source episodes for a live node). This is the inspection-right
primitive the View-substitution gap (`plans/backlog/view-summary-substitution-plan.md`) builds on.

**What provenance still lacks:** char-offset spans (links are whole-episode, not offset-level);
verification that a declared anchor/evidence actually grounds the claim (the span-grounding checker is
shipped only on the numeric path — see §3); and the unused first-class Evidence node.

## 3. Governance layer (admission / assertion / merge / lifecycle)

**Status: MIXED — write-side substrate largely live; read-side judiciary default-off; admission by
provenance not generalized.**

| Mechanism | Wired? | Default | Anchor |
|---|---|---|---|
| CANDIDATE review tier (propose→review→accept) | **live** | on | `bootstrap.py:220` `CandidateService(...)` |
| `source_confidence` tiering (user 1.0 / structural 0.9 / agent 0.5) | **live** | on | `ingest_service.py:468`, `domain/truth/kinds.py` |
| View supersession (`view_current` / `expired_at`) | **live** | on | `cypher.py:290-303` |
| Deterministic merge veto (structural/path-shaped never merge) | **live** | on | `correlation_queries.py:326`; `correlation_service` fail-safe never-merge |
| Perception span-grounding + veto-gate (numeric path) | **live in THIS deployment** | **code default OFF** | see note below |
| Warden chain / `_apply_frontier` (drop REFUSED / label FLAGGED) | wired, **gated** | **OFF** | `settings.py:235` `frontier_warden_gate=False` |
| Belief gate (CurrentnessWarden) | wired, **gated** | **OFF** (needs warden_gate) | `settings.py:238` `frontier_belief_gate=False` |
| Evidence-anchor warden (Guard 5) | wired, **gated** | **OFF** | `settings.py:239` `frontier_evidence_anchor=False` |
| Foundation-typed admission (basis gate on main ingest) | **ABSENT** | — | `plans/backlog/foundation-typed-admission-plan.md` |

**Correction / important nuance — perception span-grounding.** The span-grounding + coreference +
verify guards are pinned ON *inside* the personal-memory consolidation job
(`scheduler_tasks.py:424-427`), and `sum_grounding` defaults on (`settings.py:218`). **But the job
itself defaults OFF** (`personal_memory_consolidation_enabled = False`, `settings.py:197`). So in a
stock deployment none of it runs. **This deployment enables it** (`.env`:
`MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED=true`), so grounding *is* live here — but that is a
deployment choice, not a code default. Earlier notes that called it "live default-on" were half-right:
the *guards* are default-on within a job that is default-off.

**The read-side judiciary is built but dark.** `domain/warden.py` (CurrentnessWarden / ExhaustionWarden
/ ScopeWarden / WardenChain) and the oracle combiner are wired into `recall_service._apply_frontier`,
but every frontier flag defaults OFF (LME proved the read-side levers neutral-to-negative; they were
flipped off deliberately). So with no `MENHIR_FRONTIER_*` set, recall is byte-for-byte baseline scoring
— governance verdicts are *computed in shadow at most, not enforced*.

---

## What this means for the roadmap (reprioritized)

1. **Provenance is the healthiest leg** — links exist, and `get_provenance` now exposes them. Extend it
   (View-substitution `plans/backlog/view-summary-substitution-plan.md`; char-offset spans).
2. **The L4 artifact layer needs wiring, not building** — construct it in runtime + one emitter + one
   MCP surface before any L3 / ColdStartBrief work. Zero data today.
3. **Governance enforcement is a turn-on-and-measure question, not a build** — the wardens exist and are
   off by policy; foundation-typed admission (#7) is the one genuinely new write-side rule, and it has
   little to verify until evidence/anchor coverage rises.
4. **Verification underlies the trust story** — the L4 trust tier and #6/#7 verification are only
   meaningful once anchors/evidence are actually populated and *checked* (they are caller-declared and
   unverified today).

## Cross-references

- Constitution / design ideal: `memory-governance.md`
- Truth tiers + wardens (design): `domain/truth/`, `domain/warden.py`
- Forensic-admissibility transfer (source of the write-side constitution):
  `IdeaProjects/.agent/reviews/menhir-frontier-transfer-forensic-admissibility.md`
- Gap plans: `plans/backlog/` — `l3l4-semantic-overlay-sequencing-plan.md`,
  `foundation-typed-admission-plan.md`, `view-summary-substitution-plan.md`,
  `identity-keying-layer-plan.md`, `admission-capability-separation-plan.md`
