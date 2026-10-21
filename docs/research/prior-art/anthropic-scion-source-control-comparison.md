# Anthropic Scion source control — Menhir prior-art assessment

**Reviewed:** 2026-10-13  
**Classification:** Dated external architecture account; proprietary implementation, not a code audit  
**Primary source:** [Anthropic, "Redesigning source control at Anthropic" (2026-10-13)](https://www.anthropic.com/engineering/redesigning-source-control)  
**Follow-on owner:** [Scion-inspired revision intelligence exploration](../../../.agent/plans/backlog/menhir-scion-exploration-2026-10-13.md)

## Verdict

**Borrow the server-side query, immutable-evidence, and incremental-projection patterns; do not replace Git with Scion or build a competing VCS.** Menhir already has much of the conceptual substrate. The useful gap is revision-aware, queryable change evidence that connects reliable Git identities to Menhir's code graph, knowledge provenance, currentness, and artifact drift. The first deliverable should be a measured, isolated study, not production ingestion or schema changes.

### Is Scion open source? Can Menhir use it instead of Git?

**No publicly available Anthropic Scion source release or redistributable license was identified on 2026-10-13.** The article describes an *in-house* system and says that its substantial divergence from Git would require replacing or adapting code review, search, and CI. It explicitly cautions that Scion is not a general-purpose Git replacement. The article is architectural disclosure, **not** evidence of a usable dependency or an open-source grant.

Name collision: [GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion) is Apache-2.0 open-source **AI-agent collaboration/orchestration**, not Anthropic's version-control Scion. It cannot serve as a substitute implementation.

An **optional local VCS frontend** is a different question. [Jujutsu (`jj`)](https://github.com/jj-vcs/jj) is open source with a production Git backend, allowing normal Git remotes and Git-compatible commits ([compatibility notes](https://github.com/jj-vcs/jj/blob/main/docs/git-compatibility.md)). [Meta Sapling](https://github.com/facebook/sapling) has an open-source GPL-2.0 main project and a Git-compatible CLI; its Mononoke and EdenFS server/filesystem components are explicitly not supported for outside production use ([README](https://github.com/facebook/sapling#readme)). Neither is a drop-in replacement for Menhir's Git-referenced provenance contracts. Evaluate frontend ergonomics separately if agent workflow friction is measured.

## What Anthropic actually built (source claims, not independently reproduced)

Anthropic reports growth from about 2,500 merged changes/day in May 2026 to over 30,000 in September; main-branch history nearing 100 GB, compressed working copy over 25 GB, and approximately 25 million CI checkouts/day. It migrated its monorepo from Git to Scion in five months.

| Mechanism | Anthropic's described contract | Relevance to Menhir |
|---|---|---|
| Centralized, online-first service | Servers own history, merges/rebases, diffs, merge-base, blame; clients are small and may never check out. | Agents could ask Menhir for bounded revision/impact *metadata* without a new clone or history traversal. |
| Changesets as a write-ahead log | Immutable changeset records changed paths relative to first parent and content-addressed blob pointers; server creates them; refs are independently CAS-updated. | Change observations plus explicit revision identity can back replayable projections; **Git remains source authority**. |
| Specialized derived data | Parent-recursive indexes for file/path history, blame, ancestry skip lists, search; eager or lazy materialization per index. | Extend Menhir's Event → Fold → View discipline to revision-keyed code metadata; do not conflate code indexing with belief truth. |
| Want-based object fetching | Client requests specific missing tree and blob objects; daemon and cache tiers serve immutable objects. | Future optimization for Menhir's full-bundle snapshot sync, after correctness and threat gates, with omitted-path guarantees intact. |
| Sparse/virtual working copy | Only requested files are materialized; some read and write workloads use APIs exclusively. | A metadata-only context request should not force a checkout or expose file bytes. |
| Integrated controls | Code review/IAM and testing suited to enormous automated change volume. | Relevant design caution, but Menhir is not a forge, merge queue, CI, or VCS. |

The article attributes **200× less CPU/memory infrastructure** than Anthropic's earlier Git mirrors, **two orders faster checkouts**, and **25% lower CI CPU** to *their environment*. These are vendor-reported workload-specific outcomes, **not Menhir performance expectations or benchmarks**. See [primary source, architecture and scaling](https://www.anthropic.com/engineering/redesigning-source-control).

## Correct architectural mapping

```text
Git object/commit/tree (authoritative source identity)
    + verified workspace or hook observation (qualified; may be dirty)
    -> bounded CodeChangeObservation with completeness/authority metadata
    -> deterministic, replayable revision-aware index/projection
    -> Menhir structure/evidence anchors and advisory currentness signals
    -> blast_radius / affected_tests / provenance / documentation review candidates
```

Scion's changesets **are its authoritative VCS write log**. Menhir's evidence/events are *not* an alternative commit ledger, and Menhir must not treat a reported file edit, uploaded snapshot, or model statement as proof a commit exists. These are distinct authority layers. Git commit identity, verified blobs/trees, and a known base/head relationship are required before labeling a projection commit-verified. Merge first-parent semantics may be reasonable for mainline blame but are **not automatically sufficient** for Menhir's cross-branch impact, merges, rebases, renames, or dirty workspaces.

### Existing Menhir mechanisms (code and owner docs checked 2026-10-13)

- [README](../../../README.md) and [.agent/architecture.md](../../../.agent/architecture.md): structural code graph; `query_structure`, `blast_radius`, `affected_tests`; Git-diff-assisted memory anchoring; source episodes, provenance, and Event → Fold → disposable View. **These are already present, not new Scion features.**
- [ToolEventRepository](../../../src/menhir/infrastructure/tool_event_repository.py): edit/rename events mark file anchors dirty without persisting file bytes. This is an advisory observation, not a commit event or complete history index.
- [Snapshot ingest owner](../../../.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md): deterministic manifest/tree digest, excluded-path policy, commit/tree and dirty provenance, immutable candidates, planned CAS publication and multi-workspace views. Current branch contains transport/client machinery ([protocol](../../../src/menhir/snapshot/protocol.py), [upload client](../../../src/menhir/snapshot/upload_client.py), [mode gate](../../../src/menhir/config/snapshot_mode.py)); **do not mistake configured mode enums or plans for production-enabled functionality**. Phase status and receiver behavior must be rechecked before any prototype.
- [Change-aware documentation](../../change-aware-documentation.md) and [Doc Drift Watch](../../roadmap/doc-drift-watch-mvp.md) already describe code-change → anchored-doc review candidates. Reuse one normalized event contract; do not build a second drift system.
- [Artifact reconciliation](../../../.agent/workflows/artifact_authoring.md) requires Git-backed identity, provenance and fail-closed handling on missing/unobservable trees. Source-control abstractions must preserve that behavior.

## What is genuinely missing or unproven

1. **Revision-addressed queries:** distinguish "what is known at revision X" from "what the current file hook observed"; track branch/workspace context and the limits of history coverage.
2. **Bounded change history as a projection:** retrieve change sets affecting a file/symbol/anchored decision without repeated full local Git history work. No duplicate source-of-truth store.
3. **Projection invalidation/rebuild:** deterministic dedup, event ordering and replay when a branch rewrites, a snapshot is replaced, a scanner changes, or evidence is erased.
4. **Content-addressed sync delta:** a measured, optional transport optimization to snapshot bundles, not a replacement for manifest verification, sealed candidates, quotas or publication CAS.
5. **Evidence of value:** Menhir workload measurements against Git + existing `query_structure` and against no-Menhir agents; an architectural analogy does not establish performance or answer-quality lift.

## Options and boundaries

| Option | Viability | Disposition |
|---|---|---|
| Adopt Anthropic Scion as Menhir's VCS | Not available as a public dependency; would also require a different development ecosystem. | **Reject for now.** Revisit only if source, license, interfaces, and independent operation are published. |
| Keep Git; build optional revision-aware Menhir read projections | Fits existing provenance/graph architecture; narrower than a VCS. | **Explore first, with measurements.** |
| Keep Git; improve snapshot transport with missing-object negotiation | Potentially useful after full snapshot path is correct and costly enough. | **Deferred experiment**, not a prerequisite. |
| Swap developers/agents to `jj` using its Git backend | Possible *client* workflow experiment; Git commits remain authoritative. | Optional, separate evaluation; not core Menhir dependency. |
| Use Sapling/Mononoke/EdenFS as infrastructure | OSS source exists, but externally unsupported server pieces, operational weight and GPL-2.0 licensing considerations. | Not justified by current Menhir workload; require dedicated feasibility/license review. |
| Reimplement Scion's VCS, blob service, review stack or merge queue | Substantial category and operational expansion. | **Out of scope.** |

## Failure modes to preserve in any experiment

- **Missing history is unknown, not no impact:** shallow clones, missing refs, unindexed paths, unobservable remote roots and incomplete graph generations must fail closed.
- **Source is not authority:** a self-reported clean branch or `source_head` does not prove bundled bytes equal the full commit; excluded/untracked paths and dirty changes matter.
- **No secret-bearing new index:** avoid raw source blobs, sensitive paths, full diffs, and tokens in Menhir telemetry or knowledge nodes by default; preserve bundle selection policy and namespace/tier isolation.
- **No cross-workspace overwrite:** immutable observations, stable workspace view identity and CAS promotion; rebases/force pushes do not silently rewrite provenance history.
- **No naive merge simplification:** first-parent-only views must be explicitly labeled and tested against parent-2 changes and rename/copy cases.
- **Currentness remains advisory:** changed code triggers verification, not automatic demotion of an otherwise valid decision or rewrite of docs.
- **Replay, erasure and versioning:** projections must be rebuildable or discardable, and erased evidence must not be silently resurrected by backfill/replay.

## Novelty and product positioning

**Scion is prior art for source-control scaling and incremental derived data**, not for Menhir's governed, cross-source memory and code-evidence relationships. Do not claim that immutable event logs, server-side repository queries, content-addressed stores, or lazy projections are Menhir inventions. Conversely, Anthropic's description does not claim to implement Menhir's lifecycle/authority model, source-episode reasoning history, and artifact supersession semantics.

The defensible product distinction stays: **Git provides the code history; Menhir relates history to attributable decisions, evidence, failures, affected tests, and current/historical knowledge.**

## Follow-up and evidence limits

The [exploration plan](../../../.agent/plans/backlog/menhir-scion-exploration-2026-10-13.md) owns the inventory, benchmarks, failure fixtures, and the go/no-go decision. No Scion binary, SDK, protocol specification, source code, license, or independently reproduced Scion numbers were inspected. Reverify release status before any dependency decision.

**External sources (checked 2026-10-13):**
- https://www.anthropic.com/engineering/redesigning-source-control
- https://github.com/GoogleCloudPlatform/scion
- https://github.com/jj-vcs/jj
- https://github.com/jj-vcs/jj/blob/main/docs/git-compatibility.md
- https://github.com/facebook/sapling
- https://sapling-scm.com/docs/introduction/
