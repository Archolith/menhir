# Session handoff — 2026-06-28 live verification pass (home env)

**Read `chain-handoff.md` first** (the canonical START-HERE). This doc is a focused
supplement: it records ONE session whose whole job was to **clear the live-verification
debt** the remote sandbox chains could not run (§2 of chain-handoff: "menhir's pytest
can't run here, graphiti_core isn't importable, nothing is verified live").

The headline: **that debt is now cleared.** The home environment imports the full dep
chain (`graphiti_core`, the private `cth_mcp_framework`, `neo4j`, `httpx`, `pytest`), so
everything the chains shipped "logic-checked, live-confirmation owed" was confirmed live.

---

## 0. TL;DR for the next session

- **R1 (hybrid retrieval + source-aware floor): VERIFIED LIVE.** Tests green; a real
  score-scale finding landed (the `0.15` floor is a **rank cut on an RRF scale**, not a
  cosine cutoff). 4 test/code-drift bugs found and fixed.
- **L4 commit-6 (artifact loop menhir port): VERIFIED LIVE.** 42/42 invariant assertions
  pass against real Neo4j.
- Two **bench-side research harnesses** were written (per the repo-split rule: research
  harnessing lives in archolith-bench, never in menhir src) and now sit on the bench's
  `claude/menhir-chain-handoff-doc-7iuat2` branch.
- **Nothing is merged.** All work is on the frontier + bench handoff branches. `main` and
  the parked `rung1a` branch were never touched.
- **What's genuinely left** is NOT more verification — it's the R1 **bench A-E ladder**
  (needs R0 traces, not yet built) and only THEN setting `hybrid_alpha`. That's new build
  work + a direction decision, see §5.

---

## 1. Branch / worktree state (exact)

Three isolated worktrees (kept deliberately separate all session):

| worktree | branch | role |
|---|---|---|
| `menhir` | `main` @ `99bff52` | untouched baseline |
| `menhir-frontier` | `claude/menhir-chain-handoff-doc-7iuat2` @ `1dc12e2` | **the frontier — work here** |
| `menhir-rung1a` | `rung1a-temporal-recall` @ `8c178f5` | **PARKED** (see §4) |

**Frontier commits this session** (on top of the handoff tip `3561736`):
```
1dc12e2 docs(l4-commit6): CONFIRMED LIVE — 42/42 invariant assertions pass
8230396 docs(deferred-verification): R1 score-scale CONFIRMED LIVE (RRF max 2.0)
a62e5d5 docs(deferred-verification): characterize R1 score-scale floor from code
5e6629b docs(deferred-verification): record R1 test suite executed live + green
bb0c34a test: fix frontier test/mock drift surfaced by local live verification
```

**Bench commits this session** (archolith-bench, on `claude/menhir-chain-handoff-doc-7iuat2`
on top of `e7e0e22`):
```
94cf0ae feat(probe): L4 commit-6 live-graph walk harness
cbf8a17 feat(probe): RRF score-scale harness for menhir recall floor verification
```
None of the above is pushed yet. The bench probes were authored on `master`, then moved
onto the handoff branch and dropped from master to match the chain-handoff convention.

---

## 2. What was verified (the substance)

### R1 test suite — GREEN, and it found bugs
- All 58 R1-dedicated tests pass live (`test_hybrid_retrieval.py`, `test_recall_service.py`,
  `test_scoring_service.py`).
- Full offline suite: **1389 passed, 29 skipped, 3 failed** after fixes. The 3 = 2 known
  deferred NaN-scoring failures + 1 artifact of running frontier `src` through the main
  worktree's venv (`test_expected_venv_python_resolves_project_root_not_src`) — NOT a code
  bug.
- The first run had **7** failures (handoff predicted 2). The extra 4 were **test/code
  drift the sandbox never ran** and are now FIXED (`bb0c34a`):
  - `test_api_routes::TestRecall` x2 — the `/api/recall` route now passes
    `include_session=False` to `backend.recall` (deliberate, `routes.py:113`); assertions
    weren't updated.
  - `test_structural_anchoring::TestRecallFileContext` x2 — `recall_service._resolve_file_context`
    now calls `resolve_structural_neighbors_bulk` (ported bulk-perf PR); the test
    `_MockStructure` lacked it.
- Safety: offline/stubbed run, `online` tests auto-skip unless `--run-online`. Zero
  production-graph risk.

### R1 score-scale floor — the real finding (code-proven AND live-confirmed)
`search_scored` returns graphiti's **RRF** `node_reranker_scores` (node path,
`graphiti_core/search/search.py:282`, uses the default `rank_const=1` => `score = Σ 1/(rank+1)`),
fed *directly* as `similarity` into `scoring_service`. But `MIN_SIMILARITY_THRESHOLD = 0.15`
and its code comment describe a **cosine** scale ("matches typically >0.3"). **Different
scales.**

Live measurement (`probe_rrf_scale.py`, throwaway graph, 10 nano-ingested memories):
**max RRF = 2.0000** (= dual-method 1/1+1/1, exactly as predicted), range ~0.05-2.0, and
the 0.15 floor dropped 12/24 and 6/15 candidates **as a rank cut** (~top 12-13), not a
similarity cut.

**Implication for whoever tunes `hybrid_alpha` next:** the floor should be re-documented
as a rank cutoff (or rescaled). R1's source-aware exemption is load-bearing precisely
because BM25/file-linked candidates would otherwise be rank-floored. Full writeup in
`deferred-verification.md` under "Live-graphiti verification".

### L4 commit-6 artifact loop — 42/42 invariants LIVE
Ran the full `l4-commit6-live-verification.md` checklist against real Neo4j
(`probe_l4_walk.py`). All pass: 5 artifact indexes ONLINE; first-class `:Evidence` via
`SUPPORTED_BY`; LLM-never-trusted-on-create (inv.4); human-trusted-iff-evidence (inv.5);
promote fail-closed incl. agent_inference-only (inv.3+4); supersede marks historical +
links `SUPERSEDES` + never deletes (inv.7); no resurrection by re-capture; oracle ranks
anchor>topic, status intact, structurally write-free.
**Decay/recall coupling (the gating watch item): confirmed by code** — a trusted artifact
is a PERSISTENT `:Entity` carrying every field `ENTITY_METADATA_FIELDS` reads, so
`fetch_candidate_metadata`'s `MATCH (n:Entity)` recalls it like any node. It's
`user_flagged=false` => decays normally (the decay-exempt decision remains a deliberate
future choice, not a defect). The L4 doc header now carries the CONFIRMED LIVE status.

---

## 3. How to re-run the live checks (next session)

Both harnesses live in **archolith-bench/scripts/** and import menhir as a library (menhir
src untouched). They point at the **throwaway** Neo4j (bolt 7688), never prod.

```bash
# 0. ensure Docker is up (see the GOTCHA below), then the throwaway neo4j:
docker compose -f menhir/docker-compose.benchmark.yml up -d   # neo4j-bench, bolt 7688 / http 7475
# wait for http://localhost:7475 to answer

# 1. L4 walk — pure Cypher, no LLM, seconds, free:
python archolith-bench/scripts/probe_l4_walk.py        # expect "42 PASS, 0 FAIL"

# 2. RRF score-scale — needs nano (OPENAI_API_KEY from menhir/.env), ~1 min, ~free:
python archolith-bench/scripts/probe_rrf_scale.py      # expect max RRF ~2.0
```
Run with the menhir venv:
`C:\Users\you\IdeaProjects\projects\archolith\menhir\.venv\Scripts\python.exe`.
`probe_l4_walk.py` is hardcoded to import from the **menhir-frontier** worktree's `src`
(that's where the L4 code lives); the venv's editable install points at `main`.

### GOTCHA — Docker dies on every session boundary
Docker Desktop runs on the WSL2 backend. A session re-login / WSL sleep kills the Docker
Desktop process (the `com.docker.service` Windows service stays "Running" but the engine
pipe is down, and the `docker-desktop` WSL distro can deprovision). Symptom: `docker ...`
hangs ~2 min then "cannot find the file specified", or neo4j on 7688 refuses connection.
Fix sequence that worked:
1. `Get-Process "Docker Desktop","com.docker.backend" | Stop-Process -Force`
2. `wsl --shutdown`
3. relaunch `"C:\Program Files\Docker\Docker\Docker Desktop.exe"`, wait ~1-2 min for the
   engine to provision (poll `docker info --format '{{.ServerVersion}}'` bounded, don't
   hammer). A clean restart re-provisions the missing `docker-desktop` distro.
Starting `com.docker.service` alone is NOT enough — Docker Desktop's UI process provisions
the engine.

---

## 4. The parked `rung1a` branch (don't lose, don't merge blind)

Before the frontier line was discovered, this session built **Rung 1 of the Chronostratum
temporal plan** (`.agent/plans/menhir-temporal-chronostratum-plan.md`) on a branch forked
from the OLD `main` (`99bff52`), with delegated Sonnet agents + review:
```
8c178f5 feat: Rung 1B - include_invalidated flag (current-belief default)
0797012 fix: temporal-fact formatter dict-path + Rung 1C happened-vs-learned render
e582871 feat: Rung 1A - surface per-fact bi-temporal timestamps in recall
```
What it does: surfaces the four bitemporal stamps (`valid_at/invalid_at/created_at/expired_at`)
per RELATES_TO fact edge in `RecallMemory`, an `include_invalidated` flag (current-belief
default), and a happened-vs-learned formatter. 27 unit tests green.

**Reconciliation finding (verified against the frontier code):** this is a GENUINE gap —
the frontier does NOT surface bitemporal stamps in production recall (its R1 added
`CandidateSource` attribution; the structured Temporal oracle is a BENCH prototype, not
production recall). BUT the branch is on a pre-R1 base and overlaps the same
`recall_service.py` / `ScoredMemory`, so it must be **rebuilt on the frontier**, not merged
— and under §8 discipline it's R3-shaped (belief buckets / currentness), to be sequenced,
not slotted into production recall ad hoc. Decision for ctharvey: rebuild on frontier vs
let the frontier's own temporal work (the bench Temporal oracle path) subsume it.

---

## 5. What's actually next (NOT verification — it's done)

From `deferred-verification.md`, the remaining R1 items are **new build work**, not checks:
- **R1 bench A-E ladder** — depends on R0 traces (retrieval observability), which aren't
  built. This is the gate before tuning.
- **Then** set `hybrid_alpha` (ships at neutral `0.5` as a seam, not a tuned value).

So the next session faces a **direction choice** (chain-handoff §12 menu), e.g.:
1. Build R0 traces -> run the R1 A-E ladder -> set `hybrid_alpha` (continues R1 to done).
2. R2 facet promotion (blocked on a real embedder + hardened fixture — human/live work).
3. Rebuild the parked Rung-1 temporal surfacing on the frontier (§4), if ctharvey wants the
   bitemporal stamps in production recall.
4. The L4 overlay's next slice (LLM proposer / ColdStartOracle / L3) — biggest scope risk,
   needs ctharvey to sequence; do NOT invent rungs.

Recommended: surface this menu to ctharvey and let them pick; the verification foundation
is now solid enough to build on either way.

---

## 6. Hard rules honored this session (keep honoring)
- **Research harnessing goes in archolith-bench, never menhir src** (repo split, §8).
- **Writes only ever touched the throwaway Neo4j (7688), never prod.** No `--run-online`
  write-suite against prod.
- **Branch isolation:** `main` and `rung1a` untouched; all work on the frontier + bench
  handoff branches.
- Bench-side probes import menhir as a library; menhir source was not modified at all this
  session (only frontier `.agent` docs + the drift test fixes).

## 7. Loose ends for the next session
- **Push** the frontier + bench handoff-branch commits (none pushed yet) if ctharvey wants
  them on the remote.
- The throwaway neo4j container (`menhir-bench-neo4j`) was left **up** at session end; tear
  down with `docker compose -f menhir/docker-compose.benchmark.yml down -v` if reclaiming
  resources.
- `archolith-bench/scripts/_overnight_ab.py` is an untracked leftover orchestrator from the
  earlier LongMemEval A/B work — not part of this verification track; ignore or clean up.
