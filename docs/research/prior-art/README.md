# prior-art — external comparison and related-work

Cluster: external systems, papers, and products compared against Menhir. Purpose: know what
already exists, what genuinely overlaps, which claims Menhir can no longer make safely, and
what implementation ideas are worth borrowing — not just summarization.

| Doc | Compared against | Status | Owns |
|---|---|---|---|
| `memtrace-comparison.md` | [`syncable-dev/memtrace-public`](https://github.com/syncable-dev/memtrace-public) | External comparison note | Strongest direct prior art for structural code memory for AI coding agents. |
| `repowise-comparison.md` | [`repowise-dev/repowise`](https://github.com/repowise-dev/repowise) | External comparison note | Neighboring-category check triggered by an organic-adoption claim. |
| `fluxmem-connectivity-prior-art.md` | [FluxMem paper](https://arxiv.org/abs/2605.28773v1) / [LightMem](https://github.com/zjunlp/LightMem) | External comparison note | Validates Menhir's graph-connectivity and Event/Fold/View direction; changes Menhir's novelty claims. |
| `athena-public-comparison.md` | [`winstonkoh87/Athena-Public`](https://github.com/winstonkoh87/Athena-Public) | Revision-pinned external comparison | Benchmark baseline for model-written Markdown, hybrid RAG, session rituals, and governance protocols. |
| `m3-memory-comparison.md` | [`skynetcmd/m3-memory`](https://github.com/skynetcmd/m3-memory) | Revision-pinned external comparison | Closest direct architectural comparison for typed, temporal, provenance-aware memory. |
| `utopia-comparison.md` | [`deeplethe/utopia`](https://github.com/deeplethe/utopia) | Revision-pinned external comparison | Strongest adjacent prior art for governed bitemporal knowledge, ontology-backed review, and proposal-gated memory; defines the boundary between an enterprise world model and Menhir's code-linked evidence and impact system. |
| `atlaso-comparison.md` | [Atlaso](https://www.atlaso.ai/) and its public connectors | Dated closed-system comparison | Direct product comparison and source of operational and interface lessons; core memory engine is not public. |
| `tencentdb-agent-memory-comparison.md` | [`TencentCloud/TencentDB-Agent-Memory`](https://github.com/TencentCloud/TencentDB-Agent-Memory) | Revision-pinned external comparison | Strong prior art for hierarchical agent-memory composition and model-driven memory maintenance. |

**Staleness risk:** Athena-Public, M3 Memory, TencentDB Agent Memory, and Utopia pin the exact
public revision analyzed, while the FluxMem paper link pins v1. Memtrace and Repowise capture
only Menhir's review date, and Atlaso is a dated product snapshot whose public connectors
expose only part of the system. Re-verify any unpinned or closed-system claims before using
them in a roadmap or positioning decision.

Master index: [`../README.md`](../README.md).
