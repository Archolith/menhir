# prior-art — external comparison and related-work

Cluster: external systems, papers, and products compared against Menhir. Purpose: know what
already exists, what genuinely overlaps, which claims Menhir can no longer make safely, and
what implementation ideas are worth borrowing — not just summarization.

## Start here

Use the table to find the system closest to your question. Check each comparison's date and pinned revision before citing it, and re-verify unpinned or closed-system claims.

| Doc | Compared against | Status | Owns |
|---|---|---|---|
| `landscape-matrix-2026-11.md` | Waves 1 & 2 synthesis (12 systems + two adversarial skeptic passes) | Synthesis (2026-11-25) | Master `S1`–`S8` ahead/behind scorecards, skeptic-verified novelty verdicts, benchmark caveats, and prioritized borrows/actions. |
| `github-copilot-memory-comparison.md` | [GitHub Copilot Memory & Dreaming](https://docs.github.com/en/copilot) (`GA 2026-10-28`) | External comparison note (2026-11-25) | First-party JIT branch-citation verification, verification-gated 90d TTL, control-token rejection, and PR-gated `AGENTS.md`/`CLAUDE.md` Dreaming (`closed_contract_only`). |
| `claude-code-auto-memory-comparison.md` | [Claude Code Auto Memory](https://code.claude.com/docs/en/memory) (`v2.1.59`–`v2.1.81+`) | External comparison note (2026-11-25) | Zero-ops Markdown storage, hard 200-line/25KB `MEMORY.md` cap, `.claude/rules/` path globs, worktree sharing, and `PreCompact`/`PostCompact` hooks (`closed_contract_only`). |
| `honcho-comparison.md` | [`plastic-labs/honcho`](https://github.com/plastic-labs/honcho) (`v3.7.0`, AGPL-3.0) | Revision-pinned external comparison (2026-11-25) | Probabilistic Deriver/Dreamer + Dialectic (`reasoning_level`), `90.4%` LME-S (`honcho-benchmarks`), MIT Claude Code/Hermes plugins; validates `#423` erasure bug. |
| `gitnexus-comparison.md` | [`abhigyanpatwari/GitNexus`](https://github.com/abhigyanpatwari/GitNexus) (`v1.6.12`, PolyForm-NC) | Revision-pinned external comparison (2026-11-25) | 14-lang Tree-sitter + embedded LadybugDB/WASM + Leiden flows + `detect_changes`; surfaces Issue `#3500` empirical counter-eval on MCP code-graph ROI. |
| `codebase-memory-mcp-comparison.md` | [`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) (`v0.12.1`, MIT, `arXiv:2603.27277`) | Revision-pinned external comparison (2026-11-25) | Single C11 binary + SQLite WAL, 158 Tree-sitter langs, 9-lang Hybrid LSP, cross-service edges, `check_index_coverage`, `manage_adr`; documents ~9pp accuracy tradeoff. |
| `omega-memory-comparison.md` | [`omega-memory/omega-memory`](https://github.com/omega-memory/omega-memory) (`v1.5.20`) | External comparison note (2026-11-25) | Local-first SQLite+ONNX coding-agent MCP benchmark & `<3ms` Unix-socket hook prior art (`partial_public`). |
| `hindsight-comparison.md` | [`vectorize-io/hindsight`](https://github.com/vectorize-io/hindsight) (`v1.2.3`, `arXiv:2512.12818`) | Revision-pinned external comparison (2026-11-25) | Strongest MIT OSS conversational-memory competitor: single-engine Postgres/pg0, 4-way RRF + token-budgeted recall, Mental Models, and LME-S/LoCoMo harnesses. |
| `supermemory-comparison.md` | [Supermemory](https://supermemory.ai/) (`API v5` / `memorybench`) | External comparison note (2026-11-25) | Commercial-first vector-graph hybrid, single-binary local server, `/profile` lane, semantic `dryRun` erasure, and open `memorybench` (`partial_public`). |
| `letta-comparison.md` | [`letta-ai/letta`](https://github.com/letta-ai/letta) & [`letta-ai/letta-code`](https://github.com/letta-ai/letta-code) (`v0.34.x`) | Revision-pinned external comparison (2026-11-25) | Canonical OS-tiered memory, Sleep-Time Compute (`arXiv:2504.13171`), Git-backed MemFS, `$SECRET_NAME` runtime redaction, and LoCoMo flat-file sanity baseline. |
| `mem0-comparison.md` | [`mem0ai/mem0`](https://github.com/mem0ai/mem0) (`mem0ai` v2.2.1 / v3) | External comparison note (2026-11-25) | Dominant drop-in SDK & OpenMemory MCP; documents v3 OSS removal of Neo4j/graph drivers (Platform-only graph) and ADD-only extraction (`partial_public`). |
| `memtrace-comparison.md` | [`syncable-dev/memtrace-public`](https://github.com/syncable-dev/memtrace-public) (`v1.2.22+`) | Re-pinned 2026-11-25 (`partial_public`) | Strongest direct prior art for structural code memory + Cortex Decision Memory (`memtrace govern`/`recall_decision`); closed EULA core. |
| `anthropic-scion-source-control-comparison.md` | [Anthropic Scion source-control engineering account](https://www.anthropic.com/engineering/redesigning-source-control) | Dated proprietary-system comparison (2026-10-13) | Revision-aware derived indexes, Git authority, snapshot sync and open-source/license distinction; cross-links a gated exploration plan. |
| `repowise-comparison.md` | [`repowise-dev/repowise`](https://github.com/repowise-dev/repowise) (`v0.62.0`) | Re-pinned 2026-11-25 (`oss_primary_sources`) | Multi-language codebase intelligence, defect-risk scoring, human-only Pinned decisions enforced on PRs, and `repowise-bench`. |
| `fluxmem-connectivity-prior-art.md` | [FluxMem paper](https://arxiv.org/abs/2605.28773v1) / [LightMem](https://github.com/zjunlp/LightMem) | External comparison note | Validates Menhir's graph-connectivity and Event/Fold/View direction; changes Menhir's novelty claims. |
| `athena-public-comparison.md` | [`winstonkoh87/Athena-Public`](https://github.com/winstonkoh87/Athena-Public) | Revision-pinned external comparison | Benchmark baseline for model-written Markdown, hybrid RAG, session rituals, and governance protocols. |
| `m3-memory-comparison.md` | [`skynetcmd/m3-memory`](https://github.com/skynetcmd/m3-memory) | Revision-pinned external comparison | Closest direct architectural comparison for typed, temporal, provenance-aware memory. |
| `utopia-comparison.md` | [`deeplethe/utopia`](https://github.com/deeplethe/utopia) | Revision-pinned external comparison | Strongest adjacent prior art for governed bitemporal knowledge, ontology-backed review, and proposal-gated memory; defines the boundary between an enterprise world model and Menhir's code-linked evidence and impact system. |
| `atlaso-comparison.md` | [Atlaso](https://www.atlaso.ai/) and its public connectors | Dated closed-system comparison | Direct product comparison and source of operational and interface lessons; core memory engine is not public. |
| `tencentdb-agent-memory-comparison.md` | [`TencentCloud/TencentDB-Agent-Memory`](https://github.com/TencentCloud/TencentDB-Agent-Memory) | Revision-pinned external comparison | Strong prior art for hierarchical agent-memory composition and model-driven memory maintenance. |

**Staleness risk:** Anthropic Scion is a dated, proprietary design account without public source code; recheck release and licensing before proposing adoption.  Athena-Public, M3 Memory, TencentDB Agent Memory, and Utopia pin the exact
public revision analyzed, while the FluxMem paper link pins v1. Memtrace and Repowise capture
only Menhir's review date, and Atlaso is a dated product snapshot whose public connectors
expose only part of the system. Re-verify any unpinned or closed-system claims before using
them in a roadmap or positioning decision.

Master index: [`../README.md`](../README.md).
