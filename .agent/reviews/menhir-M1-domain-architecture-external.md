# Menhir M1 Domain Architecture Audit — External Pass 1 of 2

- **Repository:** `Archolith/menhir`
- **Pinned source commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`
- **Audit branch:** `audit/m1-domain-architecture-external`
- **Scope:** exactly the 26 files named in the brief; 21 root-domain files plus all five files under `src/menhir/domain/truth/`
- **Measured scope:** **5,601 lines**
- **Method:** independent static reading plus a control-tested Python-stdlib AST probe; no application behavior was inferred from execution

## 1. Executive Summary

The scoped M1 domain layer has **no imports into `menhir.services`, `menhir.api`, `menhir.mcp`, `menhir.infrastructure`, or `menhir.cli`**, including imports under `TYPE_CHECKING`, function-local imports, and conditional/optional imports. The whole-tree import graph also has **no strongly connected component containing any scoped module**. These are evidenced negatives, not omissions. The final probe parsed 791 Python modules with zero parse failures and resolved package re-exports when calculating importer counts (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:122-213`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:270-404`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:491-552`).

The layer is nevertheless not fully independent. `merge_snapshot.py` has a runtime dependency on Neo4j driver value classes, so its serialization algebra is coupled to an infrastructure vendor package (`src/menhir/domain/merge_snapshot.py:32-33`, `src/menhir/domain/merge_snapshot.py:88-158`). More importantly, several invariants over domain types are enforced again—or only—in infrastructure: merge eligibility, WorkArtifact relationship/state transitions, and survivor-merge content selection. The merge-eligibility duplication has already diverged semantically: the pure domain evaluator permits freshness values other than `COMPRESSED`/`GONE` and normalizes whitespace/case in several fields, while the mutation-time Cypher admits only exact `ACTIVE`/uppercase/lowercase/raw namespace values (`src/menhir/domain/merge_eligibility.py:44-57`, `src/menhir/domain/merge_eligibility.py:119-143`; `src/menhir/infrastructure/correlation_queries.py:569-584`).

Change cost is concentrated in four small facades/types: `models.py` has 29 importer modules, `session.py` 28, `namespace.py` 23, and `ingest.py` 16. The next-highest count is 14. These files are not necessarily flawed; they are the most expensive contracts to change (`src/menhir/domain/models.py:11-76`, `src/menhir/domain/session.py:10-37`, `src/menhir/domain/namespace.py:36-90`, `src/menhir/domain/ingest.py:9-25`).

`artifact_reconciliation.py` contains five distinguishable responsibilities and 36 public symbols, but its caller topology does **not** support a simple caller-disjoint split. Every production consumer uses at least three groups, the planner group is used by all seven importers, and every pair of responsibility groups shares callers (`src/menhir/domain/artifact_reconciliation.py:47-256`, `src/menhir/domain/artifact_reconciliation.py:286-712`, `src/menhir/domain/artifact_reconciliation.py:823-1805`, `src/menhir/domain/artifact_reconciliation.py:1813-1880`). A split could still improve internal organization, but it would be a dependency redesign rather than extraction along an existing seam.

## 2. Findings, severity-ordered, each with file:line

### HIGH — M1-ARCH-01: Merge eligibility has two authorities, and their predicates differ

**Domain type / invariant.** `NodeSignals` is the material input and `evaluate()` is presented as the deterministic merge-eligibility policy. It rejects structural roles, namespace mismatch, only `COMPRESSED`/`GONE` freshness, promoted scope, flags, and protected conflicts (`src/menhir/domain/merge_eligibility.py:60-84`, `src/menhir/domain/merge_eligibility.py:91-145`).

**External enforcement.** Infrastructure first maps graph values into `NodeSignals` and calls the pure evaluator (`src/menhir/infrastructure/correlation_queries.py:125-177`), but the mutation query then independently re-enforces the mutable subset (`src/menhir/infrastructure/correlation_queries.py:569-584`).

**Measured divergence.** The two implementations are not equivalent:

| Predicate | Domain authority | Mutation-time Cypher |
|---|---|---|
| Freshness | Uppercases and vetoes only `COMPRESSED` or `GONE` (`src/menhir/domain/merge_eligibility.py:119-125`) | Allows only exact `ACTIVE` or null (`src/menhir/infrastructure/correlation_queries.py:575-576`) |
| Namespace | Converts to string, strips whitespace, defaults empty to `default` (`src/menhir/domain/merge_eligibility.py:50-57`, `src/menhir/domain/merge_eligibility.py:111-117`) | Raw `coalesce(namespace, group_id, 'default')` equality (`src/menhir/infrastructure/correlation_queries.py:583-584`) |
| Scope | Uppercases before comparing with `PROMOTED` (`src/menhir/domain/merge_eligibility.py:127-130`) | Exact comparison with uppercase `PROMOTED` (`src/menhir/infrastructure/correlation_queries.py:577-578`) |
| Conflict state | Strips and lowercases (`src/menhir/domain/merge_eligibility.py:137-143`) | Exact lowercase membership (`src/menhir/infrastructure/correlation_queries.py:581-582`) |

This is an architectural split of policy, not merely awkward placement. A policy change requires synchronized edits across domain and infrastructure, and the current versions can produce a preflight “allowed” decision followed by a mutation abstention for values such as lowercase freshness, or inconsistent treatment of surrounding whitespace. The role predicate is also infrastructure-owned (`src/menhir/infrastructure/correlation_queries.py:125-131`), though that separation is explicitly represented by `NodeSignals.ineligible_role` and is therefore structural rather than hidden.

### HIGH — M1-ARCH-02: WorkArtifact aggregate transitions and relationship invariants are mostly repository-owned

**Domain type / invariant.** `work_artifact.py` defines the ordinary status graph (`src/menhir/domain/work_artifact.py:79-176`), relationship kinds and type legality (`src/menhir/domain/work_artifact.py:271-323`), supersession constants/assertions (`src/menhir/domain/work_artifact.py:296-305`), question states (`src/menhir/domain/work_artifact.py:326-347`), and source-medium vocabulary (`src/menhir/domain/work_artifact.py:136-150`, `src/menhir/domain/work_artifact.py:447-477`).

**External sites enforcing aggregate rules.** `work_artifact_repository.py` performs the following domain decisions:

| Type / operation | Invariant enforced outside `domain/` | External site |
|---|---|---|
| WorkArtifact creation | Artifact type and initial status must be known | `src/menhir/infrastructure/work_artifact_repository.py:118-125` |
| ArtifactSourceSpec | Medium must be in `ARTIFACT_MEDIA` | `src/menhir/infrastructure/work_artifact_repository.py:187-196` |
| Artifact relationship | Source and target namespaces must match | `src/menhir/infrastructure/work_artifact_repository.py:1238-1247` |
| Supersession | Same type, not self, non-terminal target, same namespace; then status and edge change together | `src/menhir/infrastructure/work_artifact_repository.py:1260-1301` |
| Subject link | Artifact must be persistent and target must not be structural | `src/menhir/infrastructure/work_artifact_repository.py:1311-1323` |
| Todo link | Todo and artifact namespaces must match | `src/menhir/infrastructure/work_artifact_repository.py:1332-1348` |
| ArtifactQuestion creation | Status is defaulted to OPEN | `src/menhir/infrastructure/work_artifact_repository.py:1413-1423` |
| ArtifactQuestion answer | Only OPEN can become ANSWERED; evidence edge is created with transition | `src/menhir/infrastructure/work_artifact_repository.py:1439-1471` |
| ArtifactQuestion defer | Only OPEN can become DEFERRED | `src/menhir/infrastructure/work_artifact_repository.py:1473-1495` |

The ordinary WorkArtifact status transition is a useful counterexample: the repository delegates legality to domain `can_transition()` before writing (`src/menhir/infrastructure/work_artifact_repository.py:1155-1198`). The escaped logic finding is therefore specific to the richer aggregate operations above, not a claim that every transition is misplaced.

### MEDIUM — M1-ARCH-03: Survivor content selection is duplicated between domain replay and Cypher mutation

`replay_survivor_merge()` defines the survivor summary/content transition and the 1.2 richness threshold in domain code (`src/menhir/domain/merge_delta.py:52-94`). The mutation query repeats the same summary/content decision in Cypher (`src/menhir/infrastructure/correlation_queries.py:595-610`). The unmerge guard relies on the domain replay to reconstruct expected post-merge state and compares all owned properties (`src/menhir/domain/merge_delta.py:150-172`).

The same write path already centralizes provenance by calling `derive_merged_provenance()` in Python (`src/menhir/infrastructure/correlation_queries.py:546-559`), demonstrating that the infrastructure layer can consume one domain authority. Content selection has not received the same treatment. A change to the 20% threshold or fallback order can make the mutation and inverse guard disagree.

### MEDIUM — M1-ARCH-04: Domain snapshot serialization depends at runtime on Neo4j driver types

`merge_snapshot.py` imports `neo4j.spatial` and `neo4j.time` at module import time (`src/menhir/domain/merge_snapshot.py:32-33`) and dispatches directly on driver classes in the serializer/deserializer (`src/menhir/domain/merge_snapshot.py:88-158`). This is not one of the five forbidden upward Menhir-layer edges, but it is a runtime vendor dependency inside the domain algebra. Eight repository modules import this file, so the coupling is not isolated to an adapter boundary (blast-radius probe: `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:504-552`).

The size ceiling and schema checks themselves are correctly domain-owned (`src/menhir/domain/merge_snapshot.py:199-250`); the issue is the placement of Neo4j-specific codecs.

### MEDIUM — M1-ARCH-05: Several domain vocabularies are advisory rather than closed types

The scoped layer repeatedly declares a vocabulary but stores the corresponding value as an unrestricted string:

- `EdgeType` exists, but `Edge.type` and `Edge.scope` are strings (`src/menhir/domain/edges.py:10-30`).
- `EVIDENCE_KINDS` exists, but `Evidence.kind` is a string and `is_promotable`/`has_promotable_evidence` treat every unknown value except `agent_inference` as promotable (`src/menhir/domain/artifacts.py:34-41`, `src/menhir/domain/artifacts.py:67-84`, `src/menhir/domain/artifacts.py:106-108`). Arbitrary mapped strings enter through repository/service constructors (`src/menhir/infrastructure/artifact_repository.py:45-75`; `src/menhir/services/artifact_service.py:46-53`, `src/menhir/services/artifact_service.py:76-99`).
- `ArtifactMedium`/`ARTIFACT_MEDIA` exist, but `ArtifactSourceSpec.medium` is a string; validation occurs in the repository (`src/menhir/domain/work_artifact.py:136-150`, `src/menhir/domain/work_artifact.py:447-477`; `src/menhir/infrastructure/work_artifact_repository.py:187-196`).
- Shape severity/status constants coexist with string fields (`src/menhir/domain/artifact_shape.py:31-38`, `src/menhir/domain/artifact_shape.py:96-109`).
- Reconciliation action/basis/conflict vocabularies coexist with string-valued fields (`src/menhir/domain/artifact_reconciliation.py:85-146`, `src/menhir/domain/artifact_reconciliation.py:589-613`).

This does not prove a runtime bug for every field. Architecturally, it means invalid vocabulary members remain representable and forces validation to be repeated by callers—or omitted.

### MEDIUM — M1-ARCH-06: Change cost is concentrated in four small domain contracts

The re-export-aware importer graph ranks `models.py` at 29 modules, `session.py` at 28, `namespace.py` at 23, and `ingest.py` at 16; the next files are 14. The probe counts unique repository modules and includes tests/scripts because the brief asks for the whole repository (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:294-367`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:491-552`).

These four files total only 228 lines (`src/menhir/domain/models.py:1-76`, `src/menhir/domain/session.py:1-37`, `src/menhir/domain/namespace.py:1-90`, `src/menhir/domain/ingest.py:1-25`), but they are the contracts the repository cannot change cheaply. The concentration is the finding; no defect in those definitions is implied.

### LOW — M1-ARCH-07: `artifact_reconciliation.py` is multi-responsibility, but caller topology disproves a simple extraction seam

The file contains routing/media rules, document parsing and hashing, plan value types, a multi-phase reconciliation planner, and write-side projection values (`src/menhir/domain/artifact_reconciliation.py:47-256`, `src/menhir/domain/artifact_reconciliation.py:286-712`, `src/menhir/domain/artifact_reconciliation.py:720-1805`, `src/menhir/domain/artifact_reconciliation.py:1813-1880`). It defines 36 public top-level symbols. Static caller grouping finds 4–7 callers per responsibility and non-empty overlap for every pair; the planner group is used by all seven importers. See Section 6.

A split may still reduce internal cognitive load, but it would require deliberately redesigning import surfaces. The requested hypothesis—“a credible split exists if callers are disjoint”—is not supported by the current topology.

### LOW — M1-ARCH-08: Two scoped domain abstractions are not production boundaries

`edges.py` defines `Edge` and `EdgeType`, but its only repository importer is the `menhir.domain` facade that re-exports them (`src/menhir/domain/edges.py:1-30`; `src/menhir/domain/__init__.py:15`, `src/menhir/domain/__init__.py:52-75`). No source or test module constructs `Edge(...)` in the analyzed tree. Its measured blast radius is one module.

`repo_snapshot.py` defines durable file identity and snapshot reconciliation (`src/menhir/domain/repo_snapshot.py:23-95`), but its only importer is `tests/domain/test_repo_snapshot.py` (`tests/domain/test_repo_snapshot.py:5-10`). It has zero production importers. These are evidenced wiring facts, not claims about future intent.

## 3. Inverted Dependency Table — edge, both ends, structural or type-only

The probe enumerated all AST imports from the 26 scoped modules and classified `TYPE_CHECKING`, function-local, optional, and conditional sites (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:76-213`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:514-519`).

| Source end | Target end | Import site | Runtime / type-only | Result |
|---|---|---|---|---|
| — | `menhir.services`, `menhir.api`, `menhir.mcp`, `menhir.infrastructure`, or `menhir.cli` | — | — | **No edges found** |

This negative was independently checked with a literal search after first proving that search could find the visible `namespace_to_group_id` definition at `src/menhir/domain/namespace.py:36`. No `importlib` or `__import__` use exists in the 26 files.

Adjacent but outside the requested five-layer table: `src/menhir/domain/merge_snapshot.py:32-33` has runtime imports to the third-party Neo4j driver; this is Finding M1-ARCH-04.

## 4. Cycles — one row per SCC with members

Tarjan SCC analysis ran over the 791-module repository graph after resolving relative imports and explicit/star re-exports (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:111-213`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:294-404`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:491-512`). **No SCC contains a scoped module**, so there is no cycle within the 26 files and no cycle crossing from them into the rest of the tree. For completeness, the whole-tree scan found these three SCCs, all outside scope:

| SCC | Size | Touches scoped module? | Modules |
|---:|---:|:---:|---|
| 1 | 3 | No | `menhir.infrastructure`<br>`menhir.infrastructure.memory_graph_adapter`<br>`menhir.services.scalar_state_service` |
| 2 | 22 | No | `menhir.core.bootstrap`<br>`menhir.services`<br>`menhir.services.candidate_service`<br>`menhir.services.context_builder`<br>`menhir.services.event_consolidation`<br>`menhir.services.hybrid_retrieval`<br>`menhir.services.ingest_intake`<br>`menhir.services.ingest_models`<br>`menhir.services.ingest_queue`<br>`menhir.services.ingest_service`<br>`menhir.services.ingest_worker`<br>`menhir.services.lifecycle_conflicts`<br>`menhir.services.lifecycle_consolidation`<br>`menhir.services.lifecycle_decay`<br>`menhir.services.lifecycle_models`<br>`menhir.services.lifecycle_service`<br>`menhir.services.maintenance_scheduler`<br>`menhir.services.recall_pipeline`<br>`menhir.services.recall_policies`<br>`menhir.services.recall_service`<br>`menhir.services.recall_support`<br>`menhir.services.scheduler_tasks` |
| 3 | 61 | No | `menhir.mcp.contracts`<br>`menhir.mcp.tools`<br>`menhir.mcp.tools.base`<br>`menhir.mcp.tools.conflict`<br>`menhir.mcp.tools.conflict.list_conflicts`<br>`menhir.mcp.tools.conflict.requeue_for_review`<br>`menhir.mcp.tools.conflict.resolve_conflict`<br>`menhir.mcp.tools.conflict.run_llm_review`<br>`menhir.mcp.tools.conflict.scan_conflicts`<br>`menhir.mcp.tools.ingest`<br>`menhir.mcp.tools.ingest.add_candidate`<br>`menhir.mcp.tools.ingest.add_memory`<br>`menhir.mcp.tools.ingest.add_memory_and_track`<br>`menhir.mcp.tools.ingest.close_memory`<br>`menhir.mcp.tools.ingest.delete_memory`<br>`menhir.mcp.tools.ingest.flag_memory`<br>`menhir.mcp.tools.ingest.ingest_document`<br>`menhir.mcp.tools.ingest.ingest_project`<br>`menhir.mcp.tools.ingest.promote_memory`<br>`menhir.mcp.tools.ingest.unflag_memory`<br>`menhir.mcp.tools.ops`<br>`menhir.mcp.tools.ops.add_todo`<br>`menhir.mcp.tools.ops.audit_artifact_corpus`<br>`menhir.mcp.tools.ops.close_stale_todos`<br>`menhir.mcp.tools.ops.close_todo`<br>`menhir.mcp.tools.ops.delete_namespace`<br>`menhir.mcp.tools.ops.force_reenrich`<br>`menhir.mcp.tools.ops.force_release_lease`<br>`menhir.mcp.tools.ops.force_scheduler_takeover`<br>`menhir.mcp.tools.ops.get_artifact`<br>`menhir.mcp.tools.ops.get_artifact_relationships`<br>`menhir.mcp.tools.ops.get_client_context`<br>`menhir.mcp.tools.ops.get_enrichment_status`<br>`menhir.mcp.tools.ops.get_episode_trace`<br>`menhir.mcp.tools.ops.get_memory_stats`<br>`menhir.mcp.tools.ops.get_provenance`<br>`menhir.mcp.tools.ops.get_todo`<br>`menhir.mcp.tools.ops.link_artifacts`<br>`menhir.mcp.tools.ops.list_artifact_questions`<br>`menhir.mcp.tools.ops.list_artifacts`<br>`menhir.mcp.tools.ops.list_clients`<br>`menhir.mcp.tools.ops.list_enrichment_queue`<br>`menhir.mcp.tools.ops.list_todos`<br>`menhir.mcp.tools.ops.mint_client`<br>`menhir.mcp.tools.ops.pause_scheduler`<br>`menhir.mcp.tools.ops.rate_recall`<br>`menhir.mcp.tools.ops.recover_orphans`<br>`menhir.mcp.tools.ops.relocate_artifact_source`<br>`menhir.mcp.tools.ops.repair_stale_enrichment`<br>`menhir.mcp.tools.ops.resume_scheduler`<br>`menhir.mcp.tools.ops.revoke_client`<br>`menhir.mcp.tools.ops.supersede_artifact`<br>`menhir.mcp.tools.ops.transition_artifact`<br>`menhir.mcp.tools.ops.view_entropy`<br>`menhir.mcp.tools.ops.watch_enrichment`<br>`menhir.mcp.tools.recall`<br>`menhir.mcp.tools.recall.build_context`<br>`menhir.mcp.tools.recall.query_structure`<br>`menhir.mcp.tools.recall.read_flagged_memories`<br>`menhir.mcp.tools.recall.recall_context_memories`<br>`menhir.mcp.tools.recall.recall_memories` |

The out-of-scope SCCs are mechanically reported only; they were not audited for cause or severity in this pass.

## 5. Blast Radius — all 26 files ranked by importer count

Definition: number of unique Python modules anywhere in the repository whose import resolves to the scoped module, including package-facade re-exports, function-local imports, tests, and scripts. A module is counted once regardless of how many symbols it imports (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:294-367`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:491-552`).

| Rank | Scoped file | Importer modules |
|---:|---|---:|
| 1 | `src/menhir/domain/models.py` | 29 |
| 2 | `src/menhir/domain/session.py` | 28 |
| 3 | `src/menhir/domain/namespace.py` | 23 |
| 4 | `src/menhir/domain/ingest.py` | 16 |
| 5 | `src/menhir/domain/belief.py` | 14 |
| 6 | `src/menhir/domain/truth/kinds.py` | 14 |
| 7 | `src/menhir/domain/git_staleness.py` | 13 |
| 8 | `src/menhir/domain/work_artifact.py` | 12 |
| 9 | `src/menhir/domain/bootstrap_scope.py` | 10 |
| 10 | `src/menhir/domain/merge_snapshot.py` | 8 |
| 11 | `src/menhir/domain/truth/labels.py` | 8 |
| 12 | `src/menhir/domain/artifact_reconciliation.py` | 7 |
| 13 | `src/menhir/domain/artifacts.py` | 5 |
| 14 | `src/menhir/domain/merge_delta.py` | 5 |
| 15 | `src/menhir/domain/merge_eligibility.py` | 5 |
| 16 | `src/menhir/domain/scope.py` | 5 |
| 17 | `src/menhir/domain/warden.py` | 5 |
| 18 | `src/menhir/domain/artifact_role.py` | 4 |
| 19 | `src/menhir/domain/legacy_snapshot.py` | 4 |
| 20 | `src/menhir/domain/truth/admission_gate.py` | 3 |
| 21 | `src/menhir/domain/truth/attestation.py` | 3 |
| 22 | `src/menhir/domain/artifact_shape.py` | 2 |
| 23 | `src/menhir/domain/belief_evidence.py` | 2 |
| 24 | `src/menhir/domain/truth/__init__.py` | 2 |
| 25 | `src/menhir/domain/edges.py` | 1 |
| 26 | `src/menhir/domain/repo_snapshot.py` | 1 |

## 6. `artifact_reconciliation.py` Responsibility Map — group, symbols, callers

The probe enumerated 36 non-private top-level symbols (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:521-530`). Caller counts below are unique modules that both import and reference at least one symbol in the group; tests are included. The seven total importers enter at `src/menhir/infrastructure/artifact_corpus_scanner.py:19`, `src/menhir/infrastructure/memory_graph_adapter.py:1426`, `src/menhir/infrastructure/work_artifact_repository.py:32`, `src/menhir/services/artifact_reconciliation_service.py:21`, `tests/test_artifact_reconciliation.py:12`, `tests/test_artifact_reconciliation_live.py:20`, and `tests/test_artifact_source_reconciliation_io.py:16`.

| Responsibility group | Public symbols | Caller count | Callers |
|---|---|---:|---|
| Vocabulary and schema (`src/menhir/domain/artifact_reconciliation.py:47-156`) | `ARTIFACT_SOURCE_SCHEMA_VERSION`<br>`ARTIFACT_METADATA_SCHEMA`<br>`INTEGRITY_ALGORITHM`<br>`ResolutionStatus`<br>`VersionKind` | **5** | `menhir.infrastructure.artifact_corpus_scanner`<br>`menhir.infrastructure.work_artifact_repository`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_reconciliation_live`<br>`tests.test_artifact_source_reconciliation_io` |
| Corpus routing and media classification (`src/menhir/domain/artifact_reconciliation.py:56-256`) | `CorpusLane`<br>`CORPUS_LANES`<br>`EXECUTABLE_LANES`<br>`CorpusRoute`<br>`CORPUS_ROUTES`<br>`INDEX_FILENAMES`<br>`MEDIA_BY_SUFFIX`<br>`route_for_path`<br>`is_index_document`<br>`medium_for_path` | **5** | `menhir.infrastructure.artifact_corpus_scanner`<br>`menhir.infrastructure.memory_graph_adapter`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Frontmatter, metadata, and digest (`src/menhir/domain/artifact_reconciliation.py:263-492`) | `DERIVED_KEYS`<br>`DocumentMetadata`<br>`parse_frontmatter`<br>`read_document_metadata`<br>`sha256_bytes` | **4** | `menhir.infrastructure.artifact_corpus_scanner`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Reconciliation plan algebra and planner (`src/menhir/domain/artifact_reconciliation.py:85-1805`) | `ActionKind`<br>`SAFE_ACTION_KINDS`<br>`MatchBasis`<br>`ConflictKind`<br>`CorpusEntry`<br>`ArtifactSourceSnapshot`<br>`WorkArtifactIdentitySnapshot`<br>`GitRename`<br>`ReconciliationAction`<br>`LaneContradiction`<br>`ReconciliationReport`<br>`plan_reconciliation`<br>`compute_plan_digest` | **7** | `menhir.infrastructure.artifact_corpus_scanner`<br>`menhir.infrastructure.memory_graph_adapter`<br>`menhir.infrastructure.work_artifact_repository`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_reconciliation_live`<br>`tests.test_artifact_source_reconciliation_io` |
| Write-side source projection (`src/menhir/domain/artifact_reconciliation.py:1813-1880`) | `locator_key`<br>`SourceObservation`<br>`observation_from_action` | **6** | `menhir.infrastructure.memory_graph_adapter`<br>`menhir.infrastructure.work_artifact_repository`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_reconciliation_live`<br>`tests.test_artifact_source_reconciliation_io` |

### Caller overlap test

| Group A | Group B | Shared callers | Shared modules |
|---|---|---:|---|
| Vocabulary and schema | Corpus routing and media classification | 3 | `menhir.infrastructure.artifact_corpus_scanner`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Vocabulary and schema | Frontmatter, metadata, and digest | 3 | `menhir.infrastructure.artifact_corpus_scanner`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Vocabulary and schema | Reconciliation plan algebra and planner | 5 | `menhir.infrastructure.artifact_corpus_scanner`<br>`menhir.infrastructure.work_artifact_repository`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_reconciliation_live`<br>`tests.test_artifact_source_reconciliation_io` |
| Vocabulary and schema | Write-side source projection | 4 | `menhir.infrastructure.work_artifact_repository`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_reconciliation_live`<br>`tests.test_artifact_source_reconciliation_io` |
| Corpus routing and media classification | Frontmatter, metadata, and digest | 4 | `menhir.infrastructure.artifact_corpus_scanner`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Corpus routing and media classification | Reconciliation plan algebra and planner | 5 | `menhir.infrastructure.artifact_corpus_scanner`<br>`menhir.infrastructure.memory_graph_adapter`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Corpus routing and media classification | Write-side source projection | 4 | `menhir.infrastructure.memory_graph_adapter`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Frontmatter, metadata, and digest | Reconciliation plan algebra and planner | 4 | `menhir.infrastructure.artifact_corpus_scanner`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Frontmatter, metadata, and digest | Write-side source projection | 3 | `menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_source_reconciliation_io` |
| Reconciliation plan algebra and planner | Write-side source projection | 6 | `menhir.infrastructure.memory_graph_adapter`<br>`menhir.infrastructure.work_artifact_repository`<br>`menhir.services.artifact_reconciliation_service`<br>`tests.test_artifact_reconciliation`<br>`tests.test_artifact_reconciliation_live`<br>`tests.test_artifact_source_reconciliation_io` |

**Conclusion:** no pair is caller-disjoint; the plan-algebra/planner group overlaps with the write-side group in six of seven importers. Every production importer uses at least three groups. A source-file split could still be worthwhile, but the present callers do not provide a low-churn extraction boundary.

## 7. Bug-Class Sweep — probe output and self-test quoted verbatim

The committed probe is `.github/workflows/menhir-m1-domain-architecture-external-probe.yml`. It uses only Python stdlib `ast`, `json`, `subprocess`, `tarfile`, `tempfile`, `dataclasses`, `pathlib`, and collections. Its synthetic fixture explicitly covers relative imports, `TYPE_CHECKING`, function-local imports, aliases, explicit and `__all__`/star re-exports, `try/except ImportError`, and conditional imports (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:407-459`). It also asserts that only the report and probe differ from the pinned source commit (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:469-477`).

Final successful run output, verbatim:

```text
SELFTEST relative_import: PASS
SELFTEST type_checking: PASS
SELFTEST function_local: PASS
SELFTEST aliased_import: PASS
SELFTEST __all__reexport: PASS
SELFTEST optional_import: PASS
SELFTEST conditional_import: PASS
SELFTEST OVERALL: PASS (7/7)
  1880 src/menhir/domain/artifact_reconciliation.py
   477 src/menhir/domain/work_artifact.py
   414 src/menhir/domain/belief.py
   314 src/menhir/domain/merge_snapshot.py
   310 src/menhir/domain/warden.py
   218 src/menhir/domain/artifact_shape.py
   194 src/menhir/domain/git_staleness.py
   178 src/menhir/domain/merge_delta.py
   150 src/menhir/domain/legacy_snapshot.py
   145 src/menhir/domain/merge_eligibility.py
   133 src/menhir/domain/artifacts.py
    95 src/menhir/domain/repo_snapshot.py
    90 src/menhir/domain/namespace.py
    83 src/menhir/domain/artifact_role.py
    77 src/menhir/domain/belief_evidence.py
    76 src/menhir/domain/models.py
    58 src/menhir/domain/bootstrap_scope.py
    46 src/menhir/domain/scope.py
    37 src/menhir/domain/session.py
    30 src/menhir/domain/edges.py
    25 src/menhir/domain/ingest.py
    51 src/menhir/domain/truth/__init__.py
   178 src/menhir/domain/truth/admission_gate.py
   210 src/menhir/domain/truth/attestation.py
   105 src/menhir/domain/truth/kinds.py
    27 src/menhir/domain/truth/labels.py
  5601 total
PROBE_SUMMARY modules=791 parse_errors=0 inverted_edges=0 scope_sccs=0 reexports=resolved
```

Empty-search control, verbatim:

```text
$ rg -n "^def namespace_to_group_id" src/menhir/domain/namespace.py
36:def namespace_to_group_id(namespace: str | None) -> str:
47:def namespace_to_group_ids(namespace: str | None) -> list[str] | None:
$ rg -n "menhir\.(services|api|mcp|infrastructure|cli)" <26 scoped files>
[no matches; exit 1]
$ rg -n "(__import__|importlib\.)" <26 scoped files>
[no matches; exit 1]
$ rg -n "^class Edge" src/menhir/domain/edges.py
10:class EdgeType(str, Enum):
20:class Edge:
$ rg -n "\bEdge\(" src tests
[no matches; exit 1]
$ rg -n "menhir\.domain\.repo_snapshot|from menhir\.domain import .*RepoSnapshot" src tests
tests/domain/test_repo_snapshot.py:6:from menhir.domain.repo_snapshot import (
```

An earlier draft of the probe passed the seven synthetic assertions but failed while traversing a repository lambda because it treated the lambda expression body as a statement list. That run was discarded. The committed probe removes that faulty override and is the only run used for the report.

## 8. Disproved Candidates, with the evidence that disproved them

| Candidate investigated | Result | Evidence |
|---|---|---|
| The scoped domain imports a higher Menhir layer | **Disproved.** Zero forbidden edges, including type-only/local/optional/conditional sites. | Probe classification and filter at `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:122-213`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:514-519`; final output in Section 7. |
| The scope participates in an import cycle | **Disproved.** Zero scope SCCs. | SCC implementation/result at probe `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:370-404`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:491-542`; Section 4. |
| `artifact_reconciliation.py` has a caller-disjoint split | **Disproved.** Every pair of responsibility groups shares callers. | Section 6; public areas at `src/menhir/domain/artifact_reconciliation.py:47-256`, `src/menhir/domain/artifact_reconciliation.py:286-712`, `src/menhir/domain/artifact_reconciliation.py:823-1880`. |
| Hash-based reconciliation can silently steal identity from a copy or ambiguous match | **Disproved.** Hash matching requires the old path to be absent plus one unclaimed source and one unclaimed entry; multiplicity becomes `AMBIGUOUS_CONTENT_MATCH`. | `src/menhir/domain/artifact_reconciliation.py:1402-1460`. |
| Moving an artifact between corpus lanes silently mutates lifecycle | **Disproved.** Lane/lifecycle disagreement is emitted as sorted contradiction data; it performs no transition. | `src/menhir/domain/artifact_reconciliation.py:1630-1683`. |
| Merge snapshots rely on storage code to enforce the maximum size | **Disproved.** `dumps()` checks serialized byte size and raises before emission. | `src/menhir/domain/merge_snapshot.py:223-238`. |
| All WorkArtifact status transitions escaped the domain | **Disproved as stated.** Ordinary status transitions call domain `can_transition`; only the aggregate operations listed in M1-ARCH-02 are external. | `src/menhir/infrastructure/work_artifact_repository.py:1155-1198`; domain graph at `src/menhir/domain/work_artifact.py:151-176`. |

## 9. Open Questions

- **Correctness question, not analyzed here:** unknown `Evidence.kind` values are promotable because the predicate rejects only `agent_inference` (`src/menhir/domain/artifacts.py:81-84`, `src/menhir/domain/artifacts.py:106-108`). A correctness pass should determine whether this is intended fail-open behavior.
- **External-contract question:** comments in `work_artifact.py` and `artifact_shape.py` refer to external artifact/wrap-up contracts (`src/menhir/domain/work_artifact.py:96-103`; `src/menhir/domain/artifact_shape.py:39-64`). The external contract was not available in the pinned Python source mirror, so comments were not treated as implementation evidence.
- **Dynamic loading:** literal search found no `importlib` or `__import__` in scope. The AST graph does not model module names constructed and loaded by arbitrary external code.
- **Out-of-scope observation:** the three whole-tree SCCs in Section 4 may warrant their own architecture pass; no severity is assigned here.

## 10. Coverage Table — every file, reconciled to a measured 5,601

`wc`-equivalent counts use physical lines (`sum(1 for _ in file.open('rb'))`) and are hard assertions in the probe (`.github/workflows/menhir-m1-domain-architecture-external-probe.yml:41-66`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:482-489`). Every file below was read in full; none inherits coverage from a directory or related file.

| File | Measured lines | Coverage |
|---|---:|---|
| `src/menhir/domain/artifact_reconciliation.py` | 1,880 | READ |
| `src/menhir/domain/artifact_role.py` | 83 | READ |
| `src/menhir/domain/artifact_shape.py` | 218 | READ |
| `src/menhir/domain/artifacts.py` | 133 | READ |
| `src/menhir/domain/belief.py` | 414 | READ |
| `src/menhir/domain/belief_evidence.py` | 77 | READ |
| `src/menhir/domain/bootstrap_scope.py` | 58 | READ |
| `src/menhir/domain/edges.py` | 30 | READ |
| `src/menhir/domain/git_staleness.py` | 194 | READ |
| `src/menhir/domain/ingest.py` | 25 | READ |
| `src/menhir/domain/legacy_snapshot.py` | 150 | READ |
| `src/menhir/domain/merge_delta.py` | 178 | READ |
| `src/menhir/domain/merge_eligibility.py` | 145 | READ |
| `src/menhir/domain/merge_snapshot.py` | 314 | READ |
| `src/menhir/domain/models.py` | 76 | READ |
| `src/menhir/domain/namespace.py` | 90 | READ |
| `src/menhir/domain/repo_snapshot.py` | 95 | READ |
| `src/menhir/domain/scope.py` | 46 | READ |
| `src/menhir/domain/session.py` | 37 | READ |
| `src/menhir/domain/truth/__init__.py` | 51 | READ |
| `src/menhir/domain/truth/admission_gate.py` | 178 | READ |
| `src/menhir/domain/truth/attestation.py` | 210 | READ |
| `src/menhir/domain/truth/kinds.py` | 105 | READ |
| `src/menhir/domain/truth/labels.py` | 27 | READ |
| `src/menhir/domain/warden.py` | 310 | READ |
| `src/menhir/domain/work_artifact.py` | 477 | READ |
| **Total** | **5,601** | **26/26 READ** |

## 11. Citation Self-Check — what was re-opened and whether it resolved

After drafting, I mechanically parsed every backticked `path:line` and `path:start-end` citation in this report, verified that the file exists in the pinned mirror and that every endpoint is within the measured file length. I then manually re-opened this representative sample:

- `src/menhir/domain/merge_eligibility.py:44-57`, `src/menhir/domain/merge_eligibility.py:119-143`
- `src/menhir/infrastructure/correlation_queries.py:125-177`, `src/menhir/infrastructure/correlation_queries.py:569-610`
- `src/menhir/domain/merge_delta.py:52-94`, `src/menhir/domain/merge_delta.py:150-172`
- `src/menhir/domain/merge_snapshot.py:32-33`, `src/menhir/domain/merge_snapshot.py:88-158`, `src/menhir/domain/merge_snapshot.py:223-238`
- `src/menhir/domain/work_artifact.py:79-176`, `src/menhir/domain/work_artifact.py:296-347`, `src/menhir/domain/work_artifact.py:447-477`
- `src/menhir/infrastructure/work_artifact_repository.py:118-125`, `src/menhir/infrastructure/work_artifact_repository.py:187-196`, `src/menhir/infrastructure/work_artifact_repository.py:1155-1198`, `src/menhir/infrastructure/work_artifact_repository.py:1238-1301`, `src/menhir/infrastructure/work_artifact_repository.py:1439-1495`
- `src/menhir/domain/artifact_reconciliation.py:232-256`, `src/menhir/domain/artifact_reconciliation.py:329-492`, `src/menhir/domain/artifact_reconciliation.py:823-894`, `src/menhir/domain/artifact_reconciliation.py:1726-1805`, `src/menhir/domain/artifact_reconciliation.py:1813-1880`
- `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:407-459`, `.github/workflows/menhir-m1-domain-architecture-external-probe.yml:482-552`

**Result:** every sampled citation landed on the cited symbol or predicate. No line offset was reconstructed or corrected after the fact.

## 12. What Was Checked, and what could not be verified in this environment

**Checked:**

- exact pinned commit and branch ancestry;
- source-read-only diff assertion permitting only this report and the committed probe;
- all 26 files read in full and measured to 5,601 physical lines;
- all static imports in scope, with relative resolution and type/local/optional/conditional classification;
- whole-repository importer graph, package re-exports, SCCs, and blast radius across 791 parseable modules;
- all 36 public symbols in `artifact_reconciliation.py`, their importing/using modules, and pairwise caller overlap;
- targeted static tracing of defaulting, validation, invariant checks, and state transitions over scoped types outside `domain/`;
- literal empty-result controls and citation resolution.

**Not run / not verified:**

- Application tests: **NOT RUN**. This is a static architecture pass, and no dependencies were installed. No static reading is presented as an executed behavioral count.
- Runtime importability: **NOT RUN**. In particular, the Neo4j package was not imported; its coupling is established from source imports and type dispatch.
- Dynamic imports generated as strings by code outside scope: not modeled beyond the explicit no-`importlib`/`__import__` search in scope.
- Other audit reports: **NOT READ**. The source artifact deliberately excluded `.agent/`; only this audit's own draft report was present on the branch.
- Direct local Git push: the local environment returned `Could not resolve host: github.com`. Repository reads and writes were completed through the authenticated GitHub connector; the branch write itself succeeded.

## 13. Review Confidence (/100)

**94/100.** Import direction, line counts, SCCs, and blast radius have high confidence because they are mechanically derived by a self-tested probe over 791 modules with zero parse errors and hard source-diff/count assertions. The responsibility map is supported by symbol-level AST import/use analysis and manual source reading. Confidence is lower for the escaped-domain-logic sweep because static analysis cannot prove the absence of dynamically selected enforcement paths or external contracts not present in the pinned tree.
