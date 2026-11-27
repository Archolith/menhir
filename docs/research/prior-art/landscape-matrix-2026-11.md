# Menhir Prior-Art Landscape Matrix — Waves 1 & 2 (2026-11)

**Date:** 2026-11-25  
**Status:** External comparison synthesis (12 competitors across Waves 1 & 2 + two adversarial skeptic passes).  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`).

> **Reading the matrices:** Cell tags show where the **competitor** stands relative to Menhir `v0.3.15`:
> `AHEAD` = competitor leads Menhir · `PARITY` = comparable/tradeoff · `BEHIND` = Menhir leads · `N/A` = outside competitor scope.

---

## 1. Wave 1 Scorecard (General Memory + Code-Intelligence Re-pins)

| Section | OMEGA | Hindsight | Supermemory | Letta | Mem0 | MemTrace | Repowise |
|---|---|---|---|---|---|---|---|
| **S1. Ingest & Retain** | BEHIND | PARITY | PARITY | BEHIND | BEHIND | BEHIND | BEHIND |
| **S2. Typed State & Consolidation** | BEHIND | BEHIND | BEHIND | BEHIND | BEHIND | BEHIND | N/A |
| **S3. Retrieve & Decide** | BEHIND | AHEAD | PARITY | BEHIND | PARITY | PARITY | PARITY |
| **S4. Govern, Curate & Track Work** | BEHIND | BEHIND | BEHIND | BEHIND | BEHIND | BEHIND | PARITY |
| **S5. Code Graph & Impact** | N/A | N/A | BEHIND | BEHIND | N/A | AHEAD | AHEAD |
| **S6. Interfaces, Auth & Operations** | BEHIND | PARITY | PARITY | AHEAD | PARITY | BEHIND | AHEAD |
| **S7. Platform, Storage & Release** | AHEAD | AHEAD | AHEAD | AHEAD | AHEAD | AHEAD | AHEAD |
| **S8. Research & Eval Discipline** | AHEAD | AHEAD | AHEAD | AHEAD | AHEAD | PARITY | AHEAD |

**Wave 1 reports:**
- [OMEGA (`omega-memory`)](omega-memory-comparison.md) — `v1.5.20` (`partial_public`)
- [Hindsight (`vectorize-io/hindsight`)](hindsight-comparison.md) — `v1.2.3` (`oss_primary_sources`)
- [Supermemory](supermemory-comparison.md) — `API v5` (`partial_public`)
- [Letta + Sleep-Time Compute](letta-comparison.md) — `letta-code v0.34.x` (`oss_primary_sources`)
- [Mem0 + OpenMemory MCP](mem0-comparison.md) — `mem0ai v2.2.1` / v3 (`partial_public`)
- [MemTrace (2026-11 Re-pin)](memtrace-comparison.md) — `v1.2.22+` (`partial_public`)
- [Repowise (2026-11 Re-pin)](repowise-comparison.md) — `v0.62.0` (`oss_primary_sources`)

---

## 2. Wave 2 Scorecard (Coding-Agent & IDE Incumbents)

| Section | Copilot Memory | Claude Code | Honcho | GitNexus | codebase-memory-mcp |
|---|---|---|---|---|---|
| **S1. Ingest & Retain** | PARITY | BEHIND | BEHIND | N/A | BEHIND |
| **S2. Typed State & Consolidation** | BEHIND | BEHIND | PARITY | N/A | N/A |
| **S3. Retrieve & Decide** | BEHIND | BEHIND | AHEAD | BEHIND | BEHIND |
| **S4. Govern, Curate & Track Work** | AHEAD | BEHIND | BEHIND | BEHIND | BEHIND |
| **S5. Code Graph & Impact** | BEHIND | N/A | N/A | AHEAD | AHEAD |
| **S6. Interfaces, Auth & Operations** | AHEAD | PARITY | PARITY | BEHIND | BEHIND |
| **S7. Platform, Storage & Release** | BEHIND | AHEAD | AHEAD | AHEAD | AHEAD |
| **S8. Research & Eval Discipline** | BEHIND | BEHIND | AHEAD | BEHIND | PARITY |

**Wave 2 reports:**
- [GitHub Copilot Memory & Dreaming](github-copilot-memory-comparison.md) — GA / Preview `2026-10-28` (`closed_contract_only`)
- [Claude Code Auto Memory](claude-code-auto-memory-comparison.md) — `v2.1.59`–`v2.1.81+` (`closed_contract_only`)
- [Honcho (Plastic Labs)](honcho-comparison.md) — `v3.7.0` (`oss_primary_sources`)
- [GitNexus](gitnexus-comparison.md) — `v1.6.12` (`oss_primary_sources`)
- [codebase-memory-mcp](codebase-memory-mcp-comparison.md) — `v0.12.1` (`oss_primary_sources`)

---

## 3. Adversarial Skeptic Verdict on Menhir's 3 Candidate Novelty Claims

### Claim 1: Unified Engineering Evidence Graph (`S1 + S4 + S5`)
- **Wave 1 verdict:** VERDICT: PARTIALLY REFUTED / NARROWLY DEFENSIBLE ONLY AS AN INTEGRATION CLAIM. Broadly claiming a 'Unified Engineering Evidence Graph' combining code graphs, architectural decisions, blast radius, and file-event hooks in one MCP server is already occupied by prior art: Memtrace (v1.2.22+) unifies a 20+ language Tree-sitter AST graph, 18-language cross-repo non-HTTP topology, bi-temporal symbol history, ~200-800ms live file-save indexing, and Cortex Decision Memory ('memtrace govern' + 'recall_decision') in a single binary; Repowise (v0.62.0) unifies a 15+ language AST/call graph, NetworkX topology, Git history, 25 deterministic defect-risk biomarkers, 'get_why' archaeology, 'get_change_risk' blast radius, and human-pinned decisions enforced on PRs; OMEGA already surfaces memories on PostToolUse file events in <3ms via a Unix-socket daemon. Worse, Menhir's code-graph leg is objectively behind Memtrace and Repowise (Python-strongest scanner, 30-min watcher) while paying a Neo4j 5.26+ operational tax none of the 7 competitors require. What narrowly survives scrutiny is NOT the code graph or decision recall alone, but the specific open-source coupling of (1) fail-closed coverage-aware 'affected_tests'/'blast_radius' refusal semantics (refusing 'safe' on unindexed paths) with (2) diff-anchored episodic memories ('ANCHORED_TO'), Git-reconciled 'WorkArtifact' lifecycles, 'CANDIDATE/PERSISTENT/PROMOTED' belief curation, and graph-linked code TODOs. Even that narrower claim remains a bundling claim until Menhir proves empirically that cross-domain graph coupling outperforms pairing Memtrace/Repowise with Hindsight/OMEGA.
- **Wave 2 update:** FURTHER NARROWED AND PRESSURED INTO A FRAGILE INTEGRATION-BUNDLE CLAIM. Wave 2 attacks Claim 1 from both sides. On the code-graph side, codebase-memory-mcp (MIT, 158 Tree-sitter languages + 9-language Hybrid LSP + cross-service HTTP/gRPC/GraphQL edges + runtime traces + check_index_coverage + manage_adr linking ADRs into the graph) and GitNexus (14 languages + Leiden clustering + precomputed execution flows + live detect_changes git-diff mapping) decisively outclass Menhir's Python-strongest scanner and 30-min watcher, blocking any novelty claim around MCP code graphs, git-diff impact analysis, index-coverage diagnostics, or graph-linked ADRs. On the repo-memory and curation side, GitHub Copilot Memory & Dreaming (GA/Preview Oct 2026, closed SaaS) already ship branch-validated code-cited repository memories (JIT verified against active git state) and autonomous PR-gated curation of AGENTS.md/CLAUDE.md/skills from sessions and PR reviews, while Claude Code Auto Memory normalizes worktree-shared project memory and path-glob rules. What survives for Claim 1 is strictly the narrow open-source intersection of (1) coverage-aware affected_tests with fail-closed refusal on unindexed paths, (2) episodic/temporal ANCHORED_TO narrative links (richer than static ADRs or opaque Copilot citations), and (3) Git-reconciled WorkArtifact lifecycles + CANDIDATE/PERSISTENT/PROMOTED curation + graph TODOs. Adversarially, this now reads as a combinatorial bundle whose practical coding-agent ROI is directly challenged by GitNexus Issue #3500 and arXiv:2603.27277's ~9 pp accuracy tradeoff until Menhir proves end-to-end task-success lift.

### Claim 2: Span-Grounded Perception → Deterministic Fold → Non-Interleaved Authority (`S2 + S3`)
- **Wave 1 verdict:** VERDICT: STRONGEST SURVIVING ARCHITECTURAL NOVELTY (UNREFUTED IN WAVE 1), BUT EMPIRICALLY UNPROVEN. First, Menhir must strip out generic sub-claims that prior art already blocks: append-only/immutable raw fact storage (Mem0 v3 ADD-only, Supermemory raw docs + 'isLatest', Hindsight 'memory_units' + 'source_memory_ids'), dual/bi-temporal timestamps (Supermemory 'documentDate'/'eventDate', Memtrace 'valid_from'/'valid_to'), and separate non-vector profile/standing-state lanes (Supermemory '/profile', Hindsight 'Mental Models/Pages', Letta pinned Core/MemFS blocks). What genuinely survives all 7 Wave 1 audits is the end-to-end deterministic state machine: K-sample span-grounded scalar perception -> immutable 'TypedAssertion' log -> deterministic, rebuildable 'Event -> Fold -> View' algebraic projections ('ScalarStateView'/'ScalarHistoryView' with counting consolidation and anchored time algebra) + transactional repair receipts + a strictly non-interleaved 'ScalarAuthorityLayer'. Every conversational competitor (OMEGA, Hindsight, Supermemory, Letta, Mem0) relies on non-deterministic LLM prose/JSON synthesis or heuristic in-place text mutation (OMEGA 55-95% append) to consolidate state. However, the skeptic's decisive objection stands: Hindsight (91.4%/94.6% LME-S), Mem0 v3 (94.4% LME), OMEGA (93.2% raw LME), and Supermemory (81.6%-85.2% LME-S) all achieve strong published scores without deterministic folding, and Letta hits 74.0% on LoCoMo with flat-file grep. Until Menhir publishes full-haystack LongMemEval-S ablations showing measurable wins on Knowledge Updates, Temporal reasoning, and Counting (justifying K-sample ingest cost/latency), this remains a clean architectural hypothesis rather than a proven performance breakthrough.
- **Wave 2 update:** ARCHITECTURALLY INTACT (STILL STRONGEST SURVIVING CLAIM), BUT EMPIRICALLY FLANKED BY HONCHO. Zero Wave 2 incumbents implement span-grounded scalar perception, an immutable TypedAssertion log, deterministic rebuildable Event->Fold->View projections, or a non-interleaved ScalarAuthorityLayer: Copilot Memory/Dreaming stores opaque unstructured text scopes; Claude Code Auto Memory/Auto Dream mutates untyped Markdown via an opaque 4-phase LLM subagent; GitNexus and codebase-memory-mcp have no episodic or scalar state at all; and Honcho v3—the closest conceptual neighbor with typed reasoning tiers (explicit/deductive/inductive/abductive/contradiction conclusions), Peer Cards, and Dream Histories—is 100% probabilistic LLM reasoning end-to-end. However, Wave 2 introduces a severe empirical challenge to Claim 2's necessity: Honcho achieves 90.4% on full-haystack LongMemEval-S and 89.9% on LoCoMo (LLM-judge) using purely probabilistic Dreamer + agentic Dialectic retrieval, backed by a public commit-pinned benchmark repo (plastic-labs/honcho-benchmarks). Because Menhir's ~90% LME-Oracle score is not full-haystack LongMemEval-S, Claim 2 is currently architecturally unique but empirically unproven against Wave 2's best probabilistic alternative; Menhir must demonstrate concrete accuracy, latency, cost, or determinism/non-regression gains on full-haystack LongMemEval-S to justify its heavy typed-state machinery.

### Claim 3: Fail-Closed Retrieval Control Plane, Provenance Erasure & OAuth 2.1 (`S3 + S1.3 + S6.2`)
- **Wave 1 verdict:** VERDICT: SURVIVES ON EPISTEMIC FAIL-CLOSED WARDENS + MULTI-SOURCE RE-DERIVATION + SELF-HOSTED OAUTH 2.1, AFTER PRUNING BLOCKED SUB-CLAIMS. Several adjacent pieces are already commoditized or surpassed: hybrid vector+lexical+graph+cross-encoder retrieval (Hindsight 4-way RRF, Supermemory, Mem0, Memtrace), pre-ingest regex secret scrubbing (Hindsight OSS Memory Defense with 44-45 patterns + redact/422-block; Letta Code's runtime '$SECRET_NAME' placeholder injection + stdout/stderr redaction is actually stronger because plaintext secrets never enter LLM transcripts), and coarse cascading document forget (Supermemory API v5 'DELETE .../semantic' with 'dryRun' and cascading forget). Three concrete differentiators survive all 7 reports: (1) Fail-closed epistemic retrieval governance via 6 read-only Oracles + 'LogSpaceOracleCombiner' + 4 assertion-boundary Wardens (no OSS competitor ships verified fail-closed abstention; OMEGA's Oracle Intelligence Pro is closed-source, and Hindsight relies on prompt-enforced LLM Directives); (2) Provenance-aware erasure with automatic multi-source re-derivation (#423), which is strictly stronger than Supermemory's blunt cascading forget and Hindsight's historical orphaned-observation bugs; and (3) Embedded self-hosted OAuth 2.1 (PKCE S256, CIMD/DCR, rotating refresh, digest-bound client policy), whereas every competitor uses no-auth locally (Repowise, Hindsight default, OpenMemory), static bearer tokens/passwords (OMEGA, Letta), or gates OAuth to proprietary clouds (Hindsight Cloud, Supermemory Cloud). Caveat: Frame OAuth 2.1 as an enterprise self-hosted security posture advantage rather than 'research novelty,' and benchmark Warden false-abstention vs. hallucination-suppression tradeoffs so fail-closed gating is proven not to silently crater recall.
- **Wave 2 update:** INTACT ACROSS ALL SURVIVING PILLARS—WITH ERASURE (#423) STRENGTHENED, WARDENS FLANKED BY SIMPLER PARADIGMS, AND AUTH SCOPED STRICTLY TO SELF-HOSTED. First, provenance-aware multi-source re-derivation on erasure (#423) is actively validated as a real differentiator by Wave 2: Honcho exhibits the exact failure mode #423 prevents (deleting a conclusion allows the Dreamer to silently re-derive it from surviving source messages, forcing raw PostgreSQL purges), and neither Copilot Memory, Claude Code, nor the code graphs provide cascading re-derivation guarantees. Second, Menhir's 6 read-only Oracles + 4 fail-closed Wardens remain uncloned, but face two sharp architectural counter-arguments: Claude Code argues hard invariants belong in deterministic shell lifecycle hooks (PreToolUse/PreCompact) rather than inside retrieval, while Copilot Memory adds JIT branch-citation verification (a code-drift check Menhir's Wardens lack) and Honcho replaces fixed oracle/warden pipelines with tunable agentic Dialectic loops. Third, Menhir's embedded self-hosted OAuth 2.1 (PKCE S256, CIMD/DCR, rotating refresh, digest-bound policy) remains clearly ahead of all OSS Wave 2 peers (Honcho self-hosted uses JWT_SECRET/API keys; GitNexus uses static bearer tokens; codebase-memory-mcp is local stdio only), but Menhir cannot claim enterprise multi-tenant governance leadership against Copilot Memory's repo-RBAC/billing-entity isolation or Honcho v3.6+'s /v3/tenants provisioning.

---

## 4. Overclaimed Points, New Threats & Blind Spots

### Wave 1 Overclaims & Weak Points
- Mandatory Neo4j 5.26+ external server dependency is a major local-first liability: all 7 Wave 1 competitors run zero-infra locally (SQLite, embedded pg0, PGlite/Bun binary, Git MemFS, MemDB, LanceDB; Mem0 even deleted its ~4k LOC Neo4j/Memgraph/Kuzu OSS drivers in v3).
- S8 benchmark credibility deficit: Menhir lacks a published full-haystack LongMemEval-S headline (relying on LME-Oracle-only reporting), leaving its core epistemic claims unverified against Hindsight, OMEGA, Mem0, and Supermemory.
- Code-intelligence parity overclaim: Menhir's Python-strongest scanner and 30-minute incremental watcher are decisively outclassed by Memtrace (20+ languages, 18-language non-HTTP topology, ~200-800ms live save indexing) and Repowise (15+ languages, NetworkX topology, 25 deterministic defect-risk biomarkers).
- MCP tool-surface bloat: Exposing 68 MCP primitives by default incurs heavy agent context/tool-selection overhead compared to Repowise's 10 task-shaped tools ('lean'/'core' profiles cut tool-list overhead by 41%) or Supermemory's compact MCP surface.
- Hook latency and secret-isolation gaps: Menhir lacks OMEGA's <3ms/<15ms fail-open Unix-socket hook daemon (avoiding per-hook Python cold starts) and trails Letta Code's execution-time '$SECRET_NAME' placeholder injection (keeping secrets out of transcripts entirely rather than scrubbing only at ingest).
- Conflating integration/protocol engineering with algorithmic novelty: Bundling 6 subsystems in Neo4j (Claim 1) and implementing standard OAuth 2.1 RFCs (part of Claim 3) are valuable product/security engineering, but overclaiming them as novel AI memory inventions weakens credibility around Menhir's genuinely distinct Event->Fold->View and fail-closed Warden/re-derivation mechanisms.

### Wave 2 New Threats & Blind Spots
- Zero-ops deployment squeeze (Neo4j 5.26+ albatross): Every Wave 2 coding-agent incumbent either needs zero external DB (Claude Code Markdown, GitNexus embedded LadybugDB/WASM, codebase-memory-mcp single C11/SQLite binary), runs on mainstream Postgres+pgvector (Honcho), or is native SaaS (Copilot Memory). Until #109 (embedded graph backend) ships, Menhir's Neo4j requirement is a fatal local-ergonomics handicap.
- Full-haystack benchmark credibility gap vs. Honcho: Menhir's internal ~90% LME-Oracle result is not apples-to-apples with Honcho's public, commit-pinned 90.4% full-haystack LongMemEval-S, 89.9% LoCoMo, and 100K-10M BEAM scaling curves. Without a public full-haystack LongMemEval-S ablation proving what ScalarAuthorityLayer and Wardens buy over probabilistic agentic retrieval, Menhir's deterministic complexity is vulnerable to dismissal.
- Empirical ROI threat to MCP code graphs (GitNexus Issue #3500 & arXiv:2603.27277): Wave 2 surfaces hard counter-evidence that MCP code graphs can produce zero coding-agent accuracy lift while raising token cost (GitNexus #3500) or incur a ~9 pp answer-quality penalty (83% vs 92% in codebase-memory-mcp's preprint) to save tokens. Menhir must empirically prove its S5 code-graph tools improve end-to-end coding accuracy or risk violating its own #399 evidence-gated retirement discipline.
- First-party distribution & context-budget bloat: Copilot Memory/Dreaming and Claude Code Auto Memory own the native developer loop with zero setup and hard startup budgets (200 lines / 25KB), while Honcho ships thin cross-harness plugins (~10 MCP tools). Menhir's 68-tool MCP surface risks tool-schema bloat and adoption friction unless aggressively profiled and budget-capped.
- Single-language scanner & 30-min staleness obsolescence: Menhir's Python-strongest scanner and 30-minute watcher are severely outpaced by codebase-memory-mcp (158 Tree-sitter languages, 9-language Hybrid LSP, cross-service HTTP/gRPC/GraphQL edges, runtime trace overlays, continuous content-hash sync) and GitNexus (14 languages, Leiden clusters, live detect_changes).
- Write-path prompt-injection & multi-tenant governance blind spots: Menhir scrubs secrets on write but does not strip/reject LLM model-control tokens (unlike Copilot Memory), and its single-owner auth model lacks Copilot's repo-RBAC/billing-entity isolation and Honcho's /v3/tenants multi-tenant provisioning.

---

## 5. Benchmark Methodology Caveats Found Across Wave 1

- OMEGA LongMemEval (95.4% headline): Vendor-reported research setup using a GPT-4.1 reader (stronger than standard GPT-4o baselines), where task-averaging inflates raw accuracy by +2.2pp (95.4% task-averaged vs. 93.2% raw, 466/500). Split/judge reproducibility and operational latency claims (<15ms SessionStart, <3ms PostToolUse, <50ms retrieval) lack independent verification.
- Hindsight paper vs. vendor harness drift: arXiv:2512.12818 reports 91.4% LongMemEval and 89.61% LoCoMo, whereas later vendor-maintained agent-memory-benchmark runs (v0.4.19+) report 94.6% LongMemEval-S and 92.0% LoCoMo-10 using LLM-as-a-judge without independent peer review; 50-500ms latency and 'up to 50x' v1.2.x speedups are unverified vendor doc claims.
- Supermemory headline inflation & closed-backend opacity: Standard single-pass API v5 LongMemEval-S scores are 81.6% (GPT-4o), 84.6% (GPT-5), and 85.2% (Gemini 3 Pro), while higher ~94% end-to-end / ~97% Recall@20 figures reflect experimental multi-agent ASMR or retrieval-only/multi-variant configs; closed-source backend internals prevent independent source-level ablation verification.
- Mem0 closed-platform execution & high retrieval budget: The 94.4% LongMemEval and 92.5% LoCoMo headlines (mem0ai/memory-benchmarks) come from the closed Managed Platform (not Apache-2.0 OSS, which removed external graph drivers in v3) using a generous top_200 retrieval budget (<7,000 tokens/retrieval) and LLM-as-a-judge; the earlier arXiv:2504.19413 LoCoMo paper also suffered methodology disputes with Zep over default tuning and Category 5 adversarial denominators.
- LoCoMo ground-truth fragility (affects Hindsight, Supermemory, Mem0, Letta): LoCoMo dialogues average ~16k-26k tokens (fitting inside modern context windows), independent audits document ~99 label/date-math errors in ground truth, and Letta's minimalist flat-filesystem + grep baseline scores 74.0% without any specialized memory store.
- Letta domain mismatch: Sleep-Time Compute gains (~5x test-time compute reduction, +13% Stateful GSM-Symbolic, +18% Stateful AIME) are demonstrated on synthetic stateful math benchmarks with high query predictability rather than longitudinal conversational memory; Terminal-Bench (42.5%) measures CLI tasks, not memory accuracy; no official LongMemEval-S full-haystack score exists.
- Memtrace & Repowise vendor-run code evals & name collision: Both syncable-dev/memtrace-public/benchmarks/ and repowise-bench are vendor-run without independent third-party reproduction and test static code retrieval/defect prediction/token economy rather than cognitive memory (zero LongMemEval coverage). Additionally, Syncable's Memtrace must not be conflated with the unrelated academic paper arXiv:2610.04838 ('MemTrace', SJTU/MemTensor).
- Menhir's own evaluation gap: Menhir currently reports LME-Oracle-only numbers without a published full-haystack LongMemEval-S score, fixed retrieval token-budget reporting, or a flat-filesystem+grep sanity baseline, making superiority claims over Wave 1 competitors empirically unverified.

---

## 6. Highest-Leverage Strategic Actions & Borrows

### Top 5 Strategic Actions (Wave 1)
- 1. Close the S8 benchmark credibility gap with an open, full-haystack evaluation suite: Run and publish Menhir v0.3.15 on an open full-haystack LongMemEval-S harness (vectorize-io/agent-memory-benchmark or supermemoryai/memorybench with standard GPT-4o reader/judge) plus omega-memory/memorystress (1,000 sessions) and a Letta-style flat-filesystem+grep sanity baseline. Report both raw and task-averaged accuracy, retrieval token budget, honest losses, and targeted ablations proving Claim 2 (Event->Fold->View on Knowledge Updates/Temporal/Counting) and Claim 3 (Warden abstention precision vs. recall).
- 2. Eliminate the Neo4j operational tax via #109 (FalkorDBLite) and single-command local packaging: Ship zero-infra embedded local installation ('pipx'/'uv' + local model bootstrap) matching OMEGA/Hindsight/Repowise ergonomics while preserving Menhir's open-source auditability, paired with zero-downtime re-embedding (Hindsight) and portable single-file state export/import (Letta '.af').
- 3. Anchor positioning strictly on Menhir's uncontested epistemic & governance moat (stop competing head-on on raw AST indexing): Center Menhir's identity on deterministic span-grounded Event->Fold->View state, fail-closed Oracles/Wardens, provenance-aware multi-source re-derivation (#423), Git-reconciled WorkArtifact/belief curation, and embedded OAuth 2.1. Retain Menhir's unique coverage-aware 'affected_tests' refusal semantics and speed up file-save watching, but ingest external code-health/AST signals rather than chasing 20-language Tree-sitter parity against Memtrace/Repowise.
- 4. Modernize coding-agent ergonomics with task-shaped MCP profiles, a Unix-socket hook daemon, and token-budgeted recall: Introduce 'lean'/'core' MCP profiles (~10 task-shaped tools returning line-cited decision/belief briefs and compressed context modes, borrowing Repowise and Memtrace LeanCTX), a fail-open Unix-domain-socket hook daemon (borrowing OMEGA 'fast_hook.py'), dynamic token-budgeted recall packing (borrowing Hindsight), and a fast always-on non-lexical workspace/user profile endpoint (borrowing Supermemory '/profile').
- 5. Complete the security & governance loop across execution, PR enforcement, and erasure UX: Add Letta-style execution-time '$SECRET_NAME' placeholder injection + stdout/stderr redaction ahead of TurnEvidence ingest (plus optional 422-block mode per Hindsight), extend Menhir's PROMOTED/pinned beliefs into Repowise-style PR/pre-edit enforcement gates, and layer semantic natural-language erasure with 'dryRun' preview (Supermemory) and MCP 'destructiveHint: true' annotations (Hindsight) atop Menhir's #423 multi-source re-derivation.

### Highest-Leverage Borrows from Wave 2
- Just-in-time git-tree/HEAD citation verification + verification-gated TTL reset (from GitHub Copilot Memory): verify ANCHORED_TO and file-cited memories against the active branch/working tree before injection, immediately downweighting drifted facts, and reset decay/retention clocks only on verified use rather than passive retrieval hits.
- Zero-ops embedded storage (#109) + event-driven content-hash watching + Tree-sitter multi-language detect_changes & check_index_coverage (from codebase-memory-mcp & GitNexus): eliminate the Neo4j 5.26+ ops tax and 30-min watcher lag, map uncommitted git diffs directly to impacted flows and ANCHORED_TO decisions, and pair explicit index-coverage diagnostics with Menhir's fail-closed affected_tests refusal.
- Hard startup context-budget cap + path-glob-scoped memory attachment + git-worktree canonicalization + PreCompact/PostCompact hooks (from Claude Code Auto Memory): enforce a strict bootstrap cap (e.g., 200 lines / 25KB) with overflow warnings, auto-surface architectural invariants only on matching file globs, canonicalize worktrees to one workspace key, and keep hard invariants in deterministic lifecycle hooks rather than advisory recall text.
- Public commit-pinned full-haystack LongMemEval-S benchmark + tunable recall depth knob (reasoning_level) + auditable consolidation diff logs (from Honcho): publish reproducible full-haystack LongMemEval-S ablations to answer Honcho's 90.4% headline, expose caller-selectable latency/depth tiers with include_evidence traces, and emit deterministic per-cycle consolidation diff logs (Dream History equivalent) plus a compact cached Peer/Workspace Card for zero-latency bootstrap.
- Write-path LLM model-control-token rejection + opt-in PR-gated doc curation + lightweight cross-harness plugins (from Copilot Memory/Dreaming & Honcho): harden Menhir's secret scrubber against indirect prompt injection via control tokens, add an opt-in background job proposing human-reviewed PRs/diffs for checked-in AGENTS.md/CLAUDE.md from resolved conflict-inbox and WorkArtifact history, and ship thin MIT plugins for Claude Code and Hermes.

---

## 7. 2026-11-27 Addendum — 30-Item Stratified `LongMemEval-S` Pilot (`d89a0d48`)

To begin closing the `S8` full-haystack evaluation gap flagged above without committing to a full 500-item `LME-S` spend first, Menhir `d89a0d48` was ingested and scored on a 30-item stratified `LongMemEval-S` pilot (`5 × 6` question types, `1,497` sessions, `15,018` turns — a `22.5×` larger haystack than Oracle). The graph and sidecars are preserved as a verified ADR 0011 baseline (`lme-snap-lmes-pilot30-d89a0d48-base`, 1:1 restore verified; see [tracker #469](https://github.com/Archolith/menhir/issues/469#issuecomment-7219756798)).

| Arm / Variant | Score (30-item pilot) | Notes |
|---|---|---|
| `no_memory` | `1 / 30` (`3.3%`) | Sanity baseline |
| `o500` Oracle `s4-ship` (`dcf17cb`, same 30 items) | `24 / 30` (`80.0%`) | Oracle reference on the exact same 30 questions |
| `LME-S` base (`menhir_recall`) | `24 / 30` (`80.0%`) | Zero net degradation across a `22.5×` larger haystack |
| `LME-S` + `drill_down` | `25 / 30` (`83.3%`) | `+1` on `852ce960` |
| `LME-S` + `#1057` (`source_memory_session_window=2`, 3× runs) | `26 / 30` (`86.7%`) | `3/3` consistent; `51a45a95` `3/3` win, `6d550036` `2/3` win, `0` regressions across `72/72` baseline-passing trials ([PR #1061](https://github.com/Archolith/menhir/pull/1061), opt-in default `0`) |

**Audited miss breakdown (post-`#1057`):**
- `51a45a95` was the sole scale-induced retrieval gap (`spread_by_session` starving same-session context at 50+ sessions), fixed in [#1057](https://github.com/Archolith/menhir/issues/1057) / [PR #1061](https://github.com/Archolith/menhir/pull/1061).
- `75832dbd` and `6d550036` are `LongMemEval-S` haystack-contamination artifacts where spliced ShareGPT/distractor sessions explicitly state preferences/leadership roles that contradict the Oracle-only gold rubric; Menhir accurately recalled what was in the namespace.
- `0a995998` (known LME gold double-count bug), `gpt4_59c863d7` (`count_object="diorama"` split), and `852ce960` (`#564`, intentionally on hold, fail-closed policy) match established Oracle behavior.

**Scope caveat:** This is a 30-item stratified pilot, not a full 500-item `LongMemEval-S` headline. It validates scale resilience and closes the `#1057` session-window gap, while full-500 `LME-S` and `S4 + S5` engineering-evidence-graph benchmarking remain open.
