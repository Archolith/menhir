# retrieval — candidate → oracle → combine → rails

Cluster 3 of the research corpus. The read-side retrieval pipeline: candidate generation, the oracle/combiner reasoning layer, the write boundary, and the control rails. (Passive L3/L4 data structures live in `../schemas/`.)

> **2026-07-11:** this pipeline is **built but no longer the active build direction** — the oracle/warden stack benched neutral-to-negative on LongMemEval and ships default-off (`config/settings.py` frontier_* all False); the active direction moved to write-time consolidation. See the master index's dated correction: [`../README.md`](../README.md). The statuses below track each mechanism's implementation/bench state, not current build priority.

| Doc | Status | Owns |
|---|---|---|
| `retrieval-tuning-stack.md` | speculative | EmbeddingDimensionSweep, HybridAlphaSearch, CrossEncoderRerankOracle, ProjectionCalibrationLayer. |
| `facet-retrieval.md` | supported-by-spike | MemoryFacetIndex, MeetPointReranker, ExpansionDriftBreaker, RetrievalTransferFixture. |
| `facet-extraction-plan.md` | supported-by-spike | The extractor-improvement path for R2's "extracted facets fail" (deterministic / git-inferred / LLM-interpreted facets, hybrid extractor). |
| `oracle-amplified-retrieval.md` | supported-by-spike | RetrievalOracle, OracleResult, OracleExecutor, OracleCombiner, OracleAmplifiedRetrieval, MeasurementBudgetGate. |
| `oracle-runtime-interfaces.md` | supported-by-spike | OracleInput/OracleFinding runtime contract, primitive-vs-composite taxonomy, the two oracle altitudes. |
| `oracle-execution-and-performance.md` | supported-by-spike | Oracle/Combiner/Mutator write boundary, observe→decide→write rule, query-snapshot rule, oracle cost model + caps, source-aware candidate priors. |
| `retrieval-control-rails.md` | speculative | CostAwareOracleScheduler, SelfReinforcementGuard, ProductiveTouchGate, EvidenceAnchorGate, RetrievalSpiralGuard. |
| `intent-warden.md` | supported-by-eval | Intent-aware retrieval — the IntentOracle (RELEVANCE family). Bench graduated embedder-invariantly; shipped in `default_oracles()`. |

Master index: [`../README.md`](../README.md).
