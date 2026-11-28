# Claude Code Auto Memory vs Menhir — Prior-Art / Architecture Comparison

**Date:** 2026-11-25  
**Status:** External comparison note (Wave 2 coding-agent & IDE incumbents audit); use for positioning, roadmap triage, and benchmark planning.  
**Pinned version / state:** Claude Code v2.1.59 (Auto Memory GA, Feb/Mar 2026) through v2.1.74 (autoMemoryDirectory) and v2.1.81+ (Auto Dream rollout); audited 2026-11-25  
**License:** Proprietary (© Anthropic PBC, All rights reserved; Commercial Terms of Service)  
**Evidence tier:** `closed_contract_only`  
**Menhir baseline:** `v0.3.15` (`S1`–`S8` in `.agent/work-sections.md`)

---

## 1. Executive Verdict

Claude Code Auto Memory (v2.1.59+) is a zero-dependency, local-Markdown persistence contract that beats Menhir v0.3.15 on zero-ops ergonomics, strict startup context budgeting (200 lines / 25KB), enterprise/VCS instruction layering, and deterministic lifecycle hooks, while trailing Menhir sharply on typed state, deterministic consolidation, ranked/governed retrieval, provenance/erasure, code-graph impact, cross-harness service architecture, and benchmark falsification discipline. Because the runtime is proprietary and closed-source, only its local filesystem and hook contracts are verifiable; its extraction, on-demand recall, and Auto Dream consolidation remain opaque model-driven black boxes.

## 2. Architecture Summary

Claude Code splits persistent context into two local-filesystem layers: (1) human/org-authored static instruction hierarchies (enterprise Managed Policy, user ~/.claude/CLAUDE.md, repo CLAUDE.md, gitignored CLAUDE.local.md, @path imports, and glob-scoped .claude/rules/) concatenated into session context, and (2) Claude-authored Auto Memory (shipped v2.1.59) stored as plain Markdown under ~/.claude/projects/<project>/memory/ (shared across git worktrees, relocatable via autoMemoryDirectory, toggleable via autoMemoryEnabled or CLAUDE_CODE_DISABLE_AUTO_MEMORY). At startup, only the first 200 lines or 25KB of MEMORY.md are loaded into the system prompt; deeper topic files are read on demand via standard file tools at the closed model's discretion, and custom subagents can declare isolated memory: user|project|local directories with the same 200-line/25KB cap. Background consolidation ("Auto Dream" / /dream, rolling out around v2.1.81 behind server-side flags) runs a 4-phase LLM subagent pass (Orient, Gather Signal from JSONL transcripts, Consolidate/absolutize dates, Prune & Index) during idle windows. Crucially, only the local filesystem layout, CLI/settings contract, and hook events are publicly documented; extraction heuristics, write triggers, topic-file selection, and dream gating remain closed-source model/client behavior.

## 3. Section-by-Section Comparison (`S1`–`S8`)

Ratings are relative to Menhir `v0.3.15` (`[AHEAD]` = competitor ahead of Menhir, `[PARITY]` = roughly comparable, `[BEHIND]` = competitor behind Menhir, `[N/A]` = out of competitor scope).

| Section | Assessment |
|---|---|
| **S1. Ingest & Retain** | [BEHIND] Documented local mechanics persist Claude-written Markdown under ~/.claude/projects/<project>/memory/ (shared across git worktrees, configurable via autoMemoryDirectory, toggleable via autoMemoryEnabled/CLAUDE_CODE_DISABLE_AUTO_MEMORY) alongside deterministic SessionStart/PreCompact/PostCompact hooks, but write decisions are opaque closed-model behavior. It lacks Menhir's async Neo4j/Graphiti extraction pipeline, secret scrubbing, canonical self-binding, retention/decay pins, and provenance-aware erasure with derived re-derivation (#423). |
| **S2. Typed State & Consolidation** | [BEHIND] Local storage is untyped, mutable Markdown prose with no immutable TypedAssertion log or deterministic Event->Fold->View projections. Background consolidation ("Auto Dream" / /dream, rolled out around v2.1.81 behind server-side flags) runs an opaque 4-phase LLM subagent over JSONL transcripts (Orient, Gather Signal, Consolidate/absolutize dates, Prune & Index) rather than Menhir's deterministic counting consolidation, K-sample span-grounded perception, and anchored temporal state. |
| **S3. Retrieve & Decide** | [BEHIND] Documented startup retrieval is strictly a hard prefix read of the first 200 lines or 25KB (whichever comes first) of MEMORY.md into the system prompt, with topic files read on demand via standard file tools solely at the closed model's discretion. It has none of Menhir's hybrid dual-lane retrieval, ONNX cross-encoder reranking, 6 read-only Oracles + LogSpaceOracleCombiner, 4 fail-closed Wardens, non-interleaved ScalarAuthorityLayer, or recall_history/recall_timeline APIs. |
| **S4. Govern, Curate & Track Work** | [BEHIND] Governance consists of human-editable Markdown via /memory, read-only enterprise Managed Policy CLAUDE.md files, glob-scoped .claude/rules/, and explicit docs stating memory is advisory context rather than enforcement (delegating hard rules to PreToolUse hooks). It lacks Menhir's CANDIDATE/PERSISTENT/PROMOTED tiers, provenance inspection, automated conflict scan + resolution inbox, WorkArtifact lifecycle state machine, and graph-linked code TODOs. |
| **S5. Code Graph & Impact** | [N/A] Claude Code Auto Memory has no code-graph index, AST/symbol scanner, coverage-aware blast_radius/affected_tests engine, or diff-anchored staleness advisories; code context relies on ad-hoc agent file/grep tools and static path-glob rules. |
| **S6. Interfaces, Auth & Operations** | [PARITY] Ahead in developer-facing ergonomics (interactive /memory command, CLAUDE.md multi-scope hierarchy + @path imports, subagent memory: user\|project\|local frontmatter, deterministic lifecycle hooks, enterprise allowManagedMcpServersOnly, and OAuth 2.0+PKCE MCP client support), but behind Menhir as an operational memory service because Auto Memory exposes no standalone MCP server, REST API, embedded OAuth 2.1 provider, multi-client memory daemon, or recoverable saga scheduler. |
| **S7. Platform, Storage & Release** | [AHEAD] Plain-text local Markdown files require zero external database infrastructure (vs. Menhir's Neo4j 5.26+ dependency and soft-forked Graphiti stack), enabling instant startup, trivial Git sharing for CLAUDE.md/.claude/rules/project-scoped subagent memory, and human-readable debugging, though Claude Code itself is a proprietary closed-source binary with opaque server-gated feature rollouts. |
| **S8. Research & Eval Discipline** | [BEHIND] Anthropic publishes no reproducible benchmarks, ablations, or LongMemEval/LoCoMo/SWE-bench evaluations isolating Auto Memory or Auto Dream (all public comparisons come from unverified third-party memory vendors), trailing Menhir's archolith-bench falsification harness and evidence-gated feature retirement (#399). |

## 4. Benchmarks Audited

- Anthropic official benchmarks for Claude Code Auto Memory / Auto Dream: NONE PUBLISHED. Neither code.claude.com nor anthropics/claude-code publishes LongMemEval, LongMemEval-S, LoCoMo, or SWE-bench ablations isolating native Auto Memory or Auto Dream.
- Third-party/community evaluations (unverified, not native-memory controlled): Vendor blogs and community posts compare external memory plugins (e.g., Mem0, Hindsight, ContextStream, repo-history layers) on LongMemEval/LoCoMo/SWE-bench Verified and report qualitatively that bounded/pruned memory indices outperform bloated ones, but these do not constitute audited benchmarks of Claude Code's closed-source Auto Memory pipeline.

## 5. Where Claude Code Auto Memory Is Ahead vs. Behind

### Ahead of Menhir
- Zero-ops, zero-DB plain-Markdown local storage (~/.claude/projects/<project>/memory/) with instant startup, full human inspectability, and automatic git-worktree sharing across checkouts of the same repo.
- Hard startup context-budget enforcement (first 200 lines or 25KB of MEMORY.md, whichever comes first) with immediate post-write overflow warnings and on-demand topic-file progressive disclosure.
- Multi-layered instruction/rule hierarchy (enterprise MDM Managed Policy, ~/.claude/CLAUDE.md, repo CLAUDE.md, gitignored CLAUDE.local.md, @path imports, and path-glob-scoped .claude/rules/ files) plus per-subagent memory scoping (memory: user | project | local).
- Deterministic shell lifecycle hooks (SessionStart, PreCompact, PostCompact, PreToolUse) cleanly decoupled from advisory memory so hard invariants are enforced in code rather than left to model compliance.

### Behind Menhir
- No typed assertion log, immutable event store, or deterministic Event->Fold->View consolidation; Auto Memory stores mutable untyped Markdown prose and relies on opaque LLM judgment (and server-gated Auto Dream) to avoid contradictions.
- No hybrid/vector/graph retrieval, cross-encoder reranking, multi-oracle fusion, fail-closed wardens, or temporal history/timeline query APIs; retrieval is a fixed 200-line/25KB prefix load plus ad-hoc model file reads.
- No provenance tracking, secret scrubbing, retention/decay policies, promotion tiers, conflict resolution inbox, or provenance-aware erasure/re-derivation.
- No code-graph index, AST scanner, coverage-aware blast-radius/affected-tests analysis, or diff-anchored staleness advisories.
- Not a standalone cross-harness memory service (no MCP memory server, REST API, or OAuth 2.1 provider for external clients) and no published benchmark or ablation discipline proving memory efficacy.

## 6. What Menhir Should Borrow

- Enforce a hard, transparent startup context-pack budget (modeled on MEMORY.md's 200-line / 25KB cap) with immediate overflow warnings when pinned/bootstrap memories exceed the budget.
- Support path-glob-scoped memory/rule attachment (like .claude/rules/ YAML paths frontmatter) so architectural invariants auto-surface only when an agent touches matching file globs.
- Adopt automatic git-worktree canonicalization for workspace keys and optional 3-tier subagent memory scoping (user, VCS-committable project, gitignored local).
- Pair Menhir's TurnEvidence hooks with explicit PreCompact/PostCompact snapshot and re-injection contracts, keeping hard enforcement in deterministic hooks rather than advisory recall text.
- Implement an auditable, deterministic scheduler job inspired by Auto Dream's idle consolidation (normalizing relative dates to absolute timestamps and pruning stale bootstrap index entries) backed by Menhir's provenance and TypedAssertion guarantees instead of an opaque subagent rewrite.

## 7. Novelty Claims Menhir Cannot Make

- Automatic cross-session project memory scoped per repository and shared across git worktrees without manual note-taking.
- Two-tier progressive-disclosure memory layout (bounded startup index + on-demand topic files) with a hard startup line/byte budget.
- Multi-scope instruction/memory layering combining enterprise Managed Policy, user global, VCS-shared project, and gitignored local overrides.
- Path-glob-scoped contextual rules/memories auto-loaded only when working on matching file paths (.claude/rules/).
- Per-subagent persistent memory scoped across user, VCS-shared project, and gitignored local tiers.
- Deterministic PreCompact/PostCompact/SessionStart lifecycle hooks for pre-compaction state snapshotting and post-compaction context re-injection.
- Idle background memory consolidation that mines session transcripts, converts relative dates to absolute dates, resolves contradictions, and prunes the startup index (Auto Dream).

## 8. Primary Sources

- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/sub-agents
- https://github.com/anthropics/claude-code
- https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md
- https://github.com/anthropics/claude-code/blob/main/LICENSE.md
