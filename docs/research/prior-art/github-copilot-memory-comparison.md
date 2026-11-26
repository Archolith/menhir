# GitHub Copilot Memory & Copilot Dreaming vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 2 coding-agent & IDE incumbents audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** Copilot Memory GA & Copilot Dreaming Public Preview (GitHub Universe 2026, 2026-10-28; initial Copilot Memory public preview 2026-01-15)  
**License:** Proprietary closed-source SaaS (requires paid GitHub Copilot Pro, Pro+, Business, or Enterprise plan)  
**Evidence tier:** `closed_contract_only`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

GitHub Copilot Memory (GA Oct 2026) and Copilot Dreaming (Preview Oct 2026) are strong closed-backend SaaS prior art for coding-agent memory, outperforming Menhir v0.3.15 on multi-tenant enterprise governance, native cross-surface distribution, just-in-time branch-citation verification, and autonomous PR-gated curation of checked-in instruction files (AGENTS.md/CLAUDE.md/.github/copilot-instructions.md). However, their backend architecture is entirely opaque, lacks Menhir's typed scalar state machine, deterministic code-impact graph, fail-closed retrieval/authority architecture, and open falsification discipline, and rests only on unreproducible internal A/B lift claims (+7% PR merge rate, +2% code-review feedback) with zero published open memory benchmarks.

## 2. Architecture Summary

GitHub Copilot Memory (public preview Jan 15, 2026; GA Oct 28, 2026 at GitHub Universe) and Copilot Dreaming (opt-in public preview Oct 28, 2026) form a proprietary, closed-backend context layer under GitHub Intelligence. Publicly documented contracts define two memory scopes—repository-level facts (shared with repo readers, created only by writers) and user-level preferences (portable across repos, bound to the originating billing entity)—backed by explicit code or user-statement citations. At prompt time, Copilot performs just-in-time verification of citations against the active branch, using an internal upvote/downvote mechanism where verified use resets a rolling 90-day TTL (initially 28 days in early preview; configurable 7-180 days for Enterprise/Org) and repeated downvotes hide stale memories. Write paths reject detected secrets and model-control tokens, and CLI/admin tooling supports versioned JSONL bulk import and export. Copilot Dreaming runs asynchronously in the background across three modes (Personal Memory Curation, Repository Customization, Code Review Customization): it mutates backend memories automatically, while proposing updates to checked-in instruction/skill files (AGENTS.md, CLAUDE.md, GEMINI.md, REVIEW.md, .github/copilot-instructions.md, .github/instructions/**, .github/skills/**) exclusively via pull requests. Storage schema, retrieval ranking, embedding/graph topology, and Dreaming scheduling internals remain completely closed and unverified.

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [PARITY] Verified docs confirm automatic background capture plus explicit user/PR-comment creation, write-path secret and model-control-token rejection, CLI JSONL bulk import (`copilot memories import`), and reinforcement-resetting rolling expiry (90-day default, 7-180d enterprise-configurable; 28d in Jan 2026 preview). Unlike Menhir's TurnEvidence hooks and provenance-aware erasure with derived re-derivation (#423), extraction and retention internals are closed-backend with no verifiable re-derivation guarantees. |
| **S2. Typed State & Consolidation** | [BEHIND] Public contracts expose only two unstructured text scopes (repository-level facts and user-level preferences) plus asynchronous Copilot Dreaming deduplication and pruning. It provides no verifiable equivalent to Menhir's K-sample span-grounded scalar perception, immutable TypedAssertion log, or deterministic Event->Fold->View projections. |
| **S3. Retrieve & Decide** | [BEHIND] While Copilot Memory's just-in-time branch-citation verification and upvote/downvote usefulness ranking are strong drift defenses, its retrieval and ranking pipeline is an opaque black box. It lacks Menhir's inspectable dual-lane retrieval, local ONNX cross-encoder reranking, 6 read-only Oracles, 4 fail-closed Wardens, separate ScalarAuthorityLayer, and temporal recall_history/recall_timeline APIs. |
| **S4. Govern, Curate & Track Work** | [AHEAD] Verified docs detail enterprise/org billing-entity isolation, most-restrictive multi-org policy precedence, admin JSONL export/bulk-delete, and Copilot Dreaming's PR-gated curation of checked-in AGENTS.md, CLAUDE.md, GEMINI.md, REVIEW.md, .github/copilot-instructions.md, and .github/skills/** files plus Code Review Customization. Although it lacks Menhir's WorkArtifact state machine and graph-linked code TODOs, its multi-tenant governance and autonomous PR curation of repo guidance surpass Menhir's single-owner curation workflow. |
| **S5. Code Graph & Impact** | [BEHIND] Copilot Memory attaches file/code citations checked just-in-time against the active branch and sits alongside GitHub's cross-repository code search, but exposes no deterministic code graph. It offers no coverage-aware blast_radius or affected_tests analysis and no fail-closed refusal on unindexed paths. |
| **S6. Interfaces, Auth & Operations** | [AHEAD] First-party integration across VS Code, Copilot CLI, coding agent, PR code review, GitHub Mobile, Slack, and Teams—enforced by GitHub repo RBAC (write-to-create, read-to-retrieve) and enterprise policies—outstrips Menhir's single-owner MCP/REST server on distribution and multi-tenant auth. Conversely, it exposes no open MCP memory server, self-hosted daemon, or inspectable local ops plane. |
| **S7. Platform, Storage & Release** | [BEHIND] Fully closed-source proprietary SaaS locked to paid GitHub Copilot subscriptions with undisclosed storage engines and zero self-hosting or offline capability. Menhir's inspectable Neo4j/SQLite storage, feature_flags SSOT, and hash-verified wheelhouse/Docker releases provide reproducibility and ownership Copilot cannot match. |
| **S8. Research & Eval Discipline** | [BEHIND] GitHub cites internal A/B gains (+7% coding-agent PR merge rate, +2% positive code-review feedback) and separate harness/ReviewBench posts, but publishes no open memory-ablation datasets, no LongMemEval/LoCoMo scores, and no reproducible falsification harness. This trails Menhir's archolith-bench falsification discipline and evidence-gated feature retirement (#399). |

## 4. Benchmarks Audited

- Vendor A/B test (closed-backend, unreproducible; 'Building an agentic memory system for GitHub Copilot'): reported +7% PR merge rate for Copilot coding agent and +2% positive thumbs-up/down feedback on Copilot code review comments when agentic memory was enabled vs baseline. No sample sizes, confidence intervals, repo breakdowns, or public traces were released.
- Companion harness/review posts ('Evaluating performance and efficiency of the GitHub Copilot agentic harness across models and tasks' on SWE-bench Verified/Pro & Terminal-Bench; ReviewBench for PR review): evaluate full Copilot harnesses and review quality, not isolated memory or Dreaming ablations.
- Open memory benchmarks (LongMemEval, LoCoMo, full-haystack retrieval): None published for Copilot Memory or Copilot Dreaming.

## 5. Where GitHub Copilot Memory & Copilot Dreaming Is Ahead vs. Behind

### Ahead of Menhir
- Just-in-time branch-citation verification: Copilot checks code citations attached to repository memories against the active branch state before applying them and dynamically discards or downvotes contradicted facts.
- Reinforcement-gated TTL & voting: Rolling 90-day expiry (7-180d enterprise-configurable) resets only when a memory is validated/upvoted rather than on passive retrieval, with downvoted memories hidden past a negative threshold.
- Copilot Dreaming PR-gated instruction curation: Autonomous background analysis of past sessions and merged PR review feedback that opens human-reviewed PRs to update checked-in AGENTS.md, CLAUDE.md, GEMINI.md, REVIEW.md, .github/copilot-instructions.md, and .github/skills/** files.
- Multi-tenant Enterprise/Org governance & billing-entity isolation: Repo RBAC (write-to-create, read-to-retrieve), most-restrictive multi-org precedence, off-by-default managed user preferences, billing-entity ownership isolation on org transfer, and admin bulk JSONL export/delete.
- First-party cross-surface distribution: Native shared memory across coding agent, PR code review, Copilot CLI, VS Code, GitHub Mobile, Slack, and Teams without local daemon setup.

### Behind Menhir
- No typed state or deterministic consolidation: Lacks span-grounded scalar perception, immutable TypedAssertion logs, and deterministic Event->Fold->View projections (ScalarStateView/ScalarHistoryView).
- Opaque retrieval and no fail-closed authority layer: No inspectable dual-lane candidate generation, local cross-encoder rerank, read-only Oracles, fail-closed Wardens, or separate non-interleaved scalar authority layer.
- No deterministic code graph or coverage-aware impact analysis: Lacks AST/symbol graph scanning, coverage-aware blast_radius/affected_tests, and fail-closed refusal on unindexed paths.
- No provenance-aware erasure with derived re-derivation or first-class WorkArtifact lifecycle tracking / graph-linked code TODOs.
- Closed-source proprietary SaaS lock-in: Cannot be self-hosted, audited, extended via open MCP memory tools, or benchmarked reproducibly on open memory evaluation suites.

## 6. What Menhir Should Borrow

- Just-in-time branch/code citation verification: When recalling ANCHORED_TO or file-cited repository memories, verify citations against the active git working tree/HEAD before injection and downvote/flag drifted facts immediately.
- Verification-gated TTL reset: Reset decay/retention clocks only when a memory is positively verified or reinforced in use, rather than on passive retrieval hits.
- PR-gated Dreaming workflow for checked-in agent docs: Add an opt-in background job that mines resolved conflict-inbox items, session corrections, and WorkArtifact/git history to propose human-reviewed diffs/PRs updating AGENTS.md, CLAUDE.md, or skill files.
- Write-path model-control-token rejection: Extend Menhir's ingest secret scrubber to also strip or reject known LLM control/special tokens to harden memory writes against indirect prompt injection.
- Versioned JSONL bulk export/import contract: Provide a portable, schema-versioned JSONL import/export format for bootstrapping and auditing workspace/user memories alongside Menhir's graph store.

## 7. Novelty Claims Menhir Cannot Make

- Claiming novelty for dual-scoped (repository-level shared facts + user-level portable preferences) cross-agent coding memory shared between coding agents, CLI, IDE, and PR code review.
- Claiming novelty for citation-backed repository memories validated just-in-time against the active git branch to detect code drift before prompt injection.
- Claiming novelty for reinforcement-resetting rolling TTLs where verified use (upvoting) rather than passive reads extends memory lifetime.
- Claiming novelty for asynchronous background 'dreaming' / context maintenance that mines past agent sessions and merged PR review feedback to autonomously open PRs updating AGENTS.md, CLAUDE.md, copilot-instructions.md, and skill files.

## 8. Primary Sources

- https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/
- https://docs.github.com/en/copilot
- https://github.blog/changelog/
