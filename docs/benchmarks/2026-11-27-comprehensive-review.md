# Menhir Comprehensive Technical & Strategic Review (2026-11-27)

**Scope:** Post-evidence reassessment of Menhir (`b6ad9639` on `feat/source-memory-session-window-1057` / [PR #1061](https://github.com/Archolith/menhir/pull/1061)) and `archolith-bench` (`0a9b532`) against the Wave 1–2 prior-art landscape ([landscape-matrix-2026-11.md](../research/prior-art/landscape-matrix-2026-11.md)).

---

## 1. Executive Verdict: What Actually Changed Today

Forty-eight hours ago, our adversarial Wave 1–2 prior-art audit flagged three existential vulnerabilities in Menhir's story:
1. **Unproven full-haystack scale (`S8`):** Menhir only had `LME-Oracle` numbers, not `LongMemEval-S`.
2. **Unproven coding-agent ROI (`Claim 1` / `S4 + S5`):** `GitNexus #3500` and `arXiv:2603.27277` argued that MCP code graphs either add cost without accuracy lift or trade ~`9 pp` of accuracy for tokens.
3. **68-tool MCP schema bloat (`S6.1`):** Exposing 68 tools by default inflated prompt tokens and distracted agents compared to compact 5–10 tool competitors.

**All three of those objections now have hard, commit-pinned empirical answers:**

| Proof Point | Exact Provenance | Measured Outcome |
|---|---|---|
| **1. `LongMemEval-S` Scale Resilience** | `menhir @ d89a0d48` + `#1057` (`48a6f551`, PR `#1061`), preserved under ADR 0011 (`#469`) | `24/30` (`80.0%`) base — **zero net degradation** vs. `o500` Oracle (`24/30`) across a `22.5×` larger haystack. With `#1057` (`source_memory_session_window=2`): **`26/30` (`86.7%`) across `3/3` independent runs**, `0` regressions across `72/72` baseline-passing trials. Every remaining miss audited as haystack contamination or known Oracle/gold behavior. |
| **2. Deterministic `S4 + S5` Structural Suite** | `archolith-bench @ d9521b5` (`scripts/run_engineering_evidence_bench.py`, `0` LLM calls) | **`1.0000` vs. `0.2233`** naive baseline (`+0.7767`) across 5 real production pillars (`StructureGraphWriter` coverage-aware refusal, `label_stale_anchors`, `domain.artifacts` L4 invariants, `plan_reconciliation` Git/hash fail-closed, and live `ProjectScanner` cross-domain join). |
| **3. 10-Tool `lean` MCP Profile Ablation** | `menhir @ 331f11b8` (`MENHIR_LOOPBACK_PROFILE=lean`, `69` unit tests passed) | Cutting MCP surface from `68 → 10` tools dropped `menhir_longitudinal` total tokens by **`-39.3%`** (`1.62M → 983k`) and turns by **`-15.9%`** (`44 → 37`), flipping the rubric verdict from `WIN_ACCURACY_ONLY` to **`WIN_ACCURACY_AND_EFFICIENCY`**. |
| **4. 2-Repo Live Longitudinal Coding Sprints** | `menhir @ 331f11b8`, `archolith-bench @ 8a5402a` / `0a9b532`, `openai/gpt-6-luna` via isolated OpenCode (`0` bulk LLM ingest) | **2 for 2 external OSS repos (`WIN_ACCURACY_AND_EFFICIENCY`):**<br>• **`simonw/llm @ 764dc386`:** **`+21.7 pp`** acc (`0.8158` vs. `0.5986`), **`-59.4%`** tokens, **`-39.7%`** turns, **`-39.9%`** tool calls, S5 comprehension **`-93.2%`** tokens.<br>• **`huggingface/smolagents @ 227ef5e4`:** **`+13.5 pp`** acc (`0.8878` rescored / `0.8386` raw vs. `0.7526`), **`-17.3%`** turns, **`-27.6%`** tool calls, **`-80.0%`** redundant file re-reads (`5` vs. `25`), S5 comprehension **`-60.5%`** tokens, and live **`verdict: drifted`** caught via `POST /api/tool-events` → `stale_anchor=true` (where `no_menhir` answered `current`). |

---

## 2. Updated Skeptic Verdicts on Menhir's 3 Moats

### Claim 1: Unified Engineering Evidence Graph (`S1 + S4 + S5`)
- **Before (2026-11-25):** *Fragile integration-bundle claim; practically unverified against `GitNexus #3500` / `arXiv:2603.27277`.*
- **Now (2026-11-27):** **EMPIRICALLY VALIDATED ON PYTHON REPOS — WITH ONE HUGE DISCOVERY.**
  - Our own ablation **confirmed** the skeptic's warning about standalone code graphs: `menhir_structure` alone (with 68 tools) scored **`TRADEOFF_ACCURACY_DROP`** (`0.5152` vs. `0.6953`).
  - **What actually wins is the unified loop** — deterministic AST code graph + live agent-authored memories & TODOs + `/api/tool-events` stale-anchor advisories + a **lean 10-tool MCP profile**. That combination beat `no_menhir` on **both** accuracy and efficiency across **2 of 2 external OSS repositories** with zero bulk LLM ingest.
  - **Remaining boundary:** Proven on Python (`simonw/llm`, `huggingface/smolagents`), not yet on polyglot TS/Kotlin/Go repos.

### Claim 2: Span-Grounded Perception → Deterministic Fold → Non-Interleaved Authority (`S2 + S3`)
- **Before:** *Architecturally unique, empirically untested on `LongMemEval-S`.*
- **Now:** **SCALE-VALIDATED AT PILOT LEVEL (`26/30` = `86.7%` across `3/3` runs), STILL NEEDS FULL-500 `LME-S` FOR HEADLINE PARITY.**
  - Zero degradation from Oracle → `LME-S` (`24/30` → `24/30` base, `26/30` with `#1057`) across a `22.5×` haystack jump proves the retrieval + scalar authority architecture does not collapse under haystack noise.
  - Still not a full 500-item `LME-S` headline against Honcho (`90.4%`) or Hindsight (`91.4%`).

### Claim 3: Fail-Closed Control Plane, Provenance Erasure (`#423`), and Self-Hosted OAuth 2.1 (`S3 + S1.3 + S6.2`)
- **Status:** **INTACT & STRENGTHENED BY `MENHIR_LOOPBACK_PROFILE=lean`.**
  - Fail-closed tool profiling is now not only a security posture advantage, it is a **measured `-39.3%` token-efficiency win** in live coding sessions.

---

## 3. Adversarial Self-Audit: What a Sharp Reviewer Will Still Attack

Be candid about these five gaps before presenting externally or cutting a release:

1. **Neo4j 5.26+ External Server Tax (`#109` FalkorDBLite) — *Still the #1 Adoption Blocker***
   - Even during our own benchmark runs today, standing up a disposable Neo4j container + SSH tunnel + `menhir serve` took real orchestration machinery. Every local-first competitor (`OMEGA`, `codebase-memory-mcp`, `GitNexus`, `Hindsight pg0`, `M3`, `Claude Code Auto Memory`) installs in one command with zero external DB. Shipping `#109` remains the single highest-leverage unlock for real-world adoption.
2. **Sample Size & Token Variance (`n = 1` per repo, `smolagents` total tokens `+21%`)**
   - On `simonw/llm`, `lean` Menhir cut total tokens by **`-59.4%`** (and `-41.5%` even excluding S5).
   - On `huggingface/smolagents`, `lean` Menhir won decisively on accuracy (`+13.5 pp`), turns (`-17.3%`), tool calls (`-27.6%`), redundant reads (`-80%`), S4 tokens (`-30%`), and S5 tokens (`-60.5%`), **but total tokens were `+21.1%`** (`1.27M` vs. `1.05M`) because S1 (`536k` vs. `347k`) and S2 (`371k` vs. `167k`) paid higher cached prompt-token volume from MCP tool outputs (`query_structure` returning rich blast-radius payloads) + memory-write steps.
   - **Actionable fix:** Compact `query_structure` output formatting (returning a concise file/test summary by default, matching Repowise/MemTrace LeanCTX) + `3×` repeats to establish tight confidence intervals.
3. **PR `#1061` Branch Hygiene (`feat/source-memory-session-window-1057`)**
   - Right now `feat/source-memory-session-window-1057` carries **four distinct logical changes** (`#1057` session-window `48a6f551`, prior-art/eval docs `1c9a7b87`, `MENHIR_LOOPBACK_PROFILE=lean` `331f11b8`, and `docs/benchmarks/` `7f31bbdc`..`b6ad9639`) — AND your local `menhir` working tree still has **4 unstaged concurrent edits** (`source_memory_spread.py`, `memory_queries.py`, `recall_pipeline.py`, `test_source_memory_session_window.py`).
   - Before merging PR `#1061`, split `331f11b8` (`lean` profile) and the docs commits onto clean PRs or squash/retitle PR `#1061` deliberately so reviewers aren't surprised.
4. **Python-Strongest Code Graph (`S5.1`)**
   - Both external sprint repos (`simonw/llm`, `huggingface/smolagents`) are Python. Until TypeScript/Kotlin/Go get Tree-sitter parity (or external AST ingestion), keep external claims explicitly scoped to Python repositories.
5. **Default Flip Decisions Still Pending**
   - `MENHIR_FRONTIER_SOURCE_MEMORY_SESSION_WINDOW` is still `0` (opt-in) pending an `o500` Oracle regression check.
   - `MENHIR_LOOPBACK_PROFILE` is still `"default"` (`49` tools) rather than `"lean"` (`10` tools) — worth deciding whether `"lean"` should become the recommended coding-agent profile in setup docs / CLI `menhir up`.

---

## 4. Updated Priority Stack (What to Tackle Next)

| Priority | Initiative | Why Now | Effort |
|---|---|---|---|
| **P0** | **Branch & PR cleanup (`#1061` + 4 unstaged `#1057` files)** | Land `#1057`, `MENHIR_LOOPBACK_PROFILE=lean`, and `docs/benchmarks/` cleanly onto `fix/mcp-error-contract` / `main`. | Small |
| **P1** | **Compact `query_structure` response payload (`LeanCTX` mode)** | Directly addresses the `smolagents` S1/S2 token overhead (`+21%` total tokens despite `-27.6%` tool calls and `-80%` redundant reads) so Menhir wins on *total* tokens across every repo, not just S4–S5. | Small–Medium |
| **P2** | **Ship `#109` (embedded FalkorDBLite single-process backend)** | Eliminates Menhir's #1 adoption barrier (Neo4j 5.26+ JVM/Docker) vs. every local-first competitor. | Large |
| **P3** | **`o500` Oracle regression check on `#1057` (`session_window=2`)** | Uses existing `o500` snapshot (zero ingest) to decide whether `session_window=2` earns default-on status. | Small |
| **P4** | **3× repeat replication on `simonw/llm` + `smolagents` (+ 1 larger repo)** | Upgrades our verified pilot numbers into statistically tight `HEADLINE-NUMBERS.md` launch claims. | Medium |
| **P5** | **Selective Tree-sitter parity (TS/Kotlin/Go)** | Extends the proven Python `S4 + S5` moat to polyglot codebases. | Large |

---

## 5. Bottom Line

Menhir's strongest, most defensible product story is now backed by real external-repo data:

> **Menhir is not a chat-history wrapper and not a standalone code graph. It is an engineering evidence substrate that joins deterministic code-impact analysis, cross-session agent memory & TODOs, live stale-anchor drift detection, and a lean 10-tool MCP control plane — delivering higher accuracy (`+13.5` to `+21.7 pp`), fewer turns (`-17%` to `-40%`), fewer tool calls (`-28%` to `-40%`), and up to `80%` fewer redundant file reads across multi-session coding work, with zero bulk LLM ingest.**
