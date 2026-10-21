---
artifact_schema: 1
artifact_uuid: a1caf47c-3bad-44d3-9621-85bf7c7dc9ff
artifact_type: plan
artifact_status: PROPOSED
---

# Scion-inspired Git revision intelligence — bounded exploration

**Created:** 2026-10-13  
**State:** PROPOSED research/backlog owner, **not** permission to implement a new production subsystem  
**Prior art:** [Anthropic Scion assessment](../../../docs/research/prior-art/anthropic-scion-source-control-comparison.md)  
**Existing owners:** [snapshot ingest](../menhir-mcp-snapshot-ingest-2026-09-16.md), [change-aware docs](../../../docs/change-aware-documentation.md), [Doc Drift Watch](../../../docs/roadmap/doc-drift-watch-mvp.md), [runtime Event → Fold → View](../../architecture.md)

## Why / decision to investigate

Anthropic's 2026-10-13 write-up describes immutable source-control changesets, parent-derived metadata indexes, server-side history APIs and object-on-demand transfer. Menhir already uses Git identities, structural scans, file-event dirty marking, repository snapshots, deterministic projections, and evidence-aware recall. We must **measure an actual Menhir deficiency before copying Scion mechanisms**.

Question: Can Menhir cheaply and correctly answer revision-qualified questions — changed files/symbols/tests; historical or stale anchors; affected decisions and docs — without repeated Git history walks or full source transfers? Separately, would a Git-compatible frontend such as Jujutsu offer useful agent ergonomics?

**Scion availability finding:** Anthropic's Scion is not an adoptable open-source implementation as of this plan date. Reject using it *instead of Git* absent a future public release, suitable license, and migration case. GoogleCloudPlatform/scion is a different project (agent orchestration). This plan is for **borrowing ideas while Git remains authoritative**.

## Scope

**In:** read-only repository + code-contract inventory; representative baseline measurements; one bounded, deterministic, repository-local/bench-only revision-index spike if earned; a separate missing-object sync feasibility calculation; optional jj/Sapling comparison *as Git-facing workflows*, not Menhir's VCS backend.

**Out:** production graph migrations, new MCP/REST tools, raw source ingestion outside existing allowlists, replacing Git/GitHub, building a forge/merge queue/SCM, changing auth or default feature flags, changing currentness authority, live production replay, deploying object servers or distributed caches.

## Explore in gates, stop when evidence says stop

### E0 — Contract and status inventory (read-only)

- Inspect live code and current plan status for `snapshot.protocol`/`bundler`/`receive`/`upload_client`/`snapshot_mode`, `ProjectScanner`, structure graph generations and `query_structure`, `ToolEventRepository`, Git-backed artifact reconciliation, `get_provenance`, and doc-drift producers. **Do not infer live activation from a plan or enum.**
- Inventory source identity fields already persisted: repo, branch, base/head commit, tree OID, dirty flag, omission count, snapshot digest, workspace/view id, scan generation and provenance quality. Map producer → trust → consumer; note missing/duplicate fields.
- Trace existing (a) Git diff/commit retrieval, (b) changed-file → related-memory retrieval, (c) impacted-tests query, and (d) doc-review proposals. Mark implementation status `SHIPPED / FLAGGED-OFF / PLAN-ONLY / UNKNOWN` with file/line evidence.
- Decide whether existing artifact reconciliation or snapshot synchronization owns a proposed contract; prefer extending an owner, not introducing another source of truth.
- **Exit artifact:** one concise inventory and a testable problem statement. If there is no repeatable gap, **STOP**.

### E1 — Measure Git + current Menhir baselines (no new service)

Record reproducible fixtures in `archolith-bench` or a named disposable workspace; consult `.agent/scripts-index.md` before writing instruments. Compare:
- Git local commands / GitHub API as available versus Menhir's existing `query_structure`, `blast_radius`, `affected_tests`, and code-linked `recall_memories`.
- Cold vs warm queries; small and moderately large representative repositories; bounded history intervals; single-path and multi-path change sets; a hypothetical clean 2nd remote sync vs today's actual bundle bytes.
- End-to-end p50/p95 latency, CPU, peak memory, transferred bytes, number of Git/file/graph lookups, freshness delay, index storage, and correctness/coverage. Run fixed queries and repeat enough times to report variation.
- Correctness fixtures: new/removed/renamed file, cross-project import, altered symbol with unchanged path, merge touching second parent, rebased/force-pushed branch, shallow or missing objects, moved doc anchor, dirty staged and unstaged changes, ignored/untracked and secret-denied files, concurrent workspace views.
- Verify currentness and provenance labeling under omissions and missing repository state. Never score an unobserved path as safe just because a query returns zero rows.
- **Exit artifact:** baseline table, coverage matrix, and recommendation whether either optimization is worth a spike. Vendor claims (e.g. 200× CPU) are not acceptance targets.

### E2 — Minimal nonproduction spike *only if E1 identifies a bottleneck*

Prefer **one** experiment selected by the evidence:

**A. Revision-aware derived index (default candidate).** In an offline adapter/bench fixture, accept **verified Git commits** or explicit `self_reported` workspace observations as distinct input kinds. Normalize a bounded `CodeChangeObservation` with repo+namespace, revision id and parent(s), path operations, optional digest/tree basis, observed time, Git commit time, provenance level, completeness and coverage. Deduplicate by stable event identity, compute a small file-history/changed-in-interval projection, and support replay/rebuild and query as-of revision. Connect to existing anchored decisions/tests **read-only**. No new Neo4j labels or production API yet.

**B. Content-addressed transfer estimate.** On the **existing sanitized manifest and selection policy**, calculate missing `path+digest` / content objects across two snapshots, resumption cost, and percent wire bytes potentially saved. Model CAS-published immutable snapshots, omission/delete lists, per-file verification, quotas and path protections. This is a feasibility simulation, **not** an alternate server upload path.

A separate optional `jj`/Sapling client trial may be conducted on a disposable Git clone only; require Git remote round-trip and source identity parity. Do **not** make either a runtime dependency. Sapling's unsupported-for-external-use server stack and GPL licensing require a separate review before any integration proposal.

### E3 — Evidence-backed go/no-go and separately gated implementation proposal

Report a decision for each axis: (1) revision-addressed metadata, (2) delta sync, (3) optional Git-compatible agent frontend, (4) full VCS replacement. Full replacement should remain **NO** unless its premise has fundamentally changed. If any candidate earns implementation, write a separate, reviewable feature plan covering API/graph schema, backward compatibility, feature flag, shadow mode, migration, security and rollback. No default-on changes as part of this exploration.

## Hypotheses and falsifiers

| Hypothesis | Confirming evidence | Stop / reject if |
|---|---|---|
| H1: A bounded revision index improves Menhir code-context queries | Repeated, correct revision queries materially reduce latency/total work on the *observed* workload; ideally ≥2× for identified bottleneck | No material gain, large extra storage, ambiguous branch semantics, or missing-history false safety |
| H2: A derived event index can be replayed safely | Same output after rebuild, duplicate/reorder, restart, branch rewrite and evidence removal; same explicit coverage caveats | Outputs drift, merge or namespace contamination, or silent resurrection of erased evidence |
| H3: Missing-object sync saves real resources | On second and subsequent sanitized snapshots, ≥50% fewer bytes in a representative changed-file workload with manageable negotiation overhead | Little byte saving, high CPU/storage/network round trips, compromised manifest/security rules |
| H4: An alternate frontend improves agent work | Disposable `jj`/Sapling trial shows useful fewer conflicts/less wasted work with identical Git-origin evidence | Requires nonportable history, breaks hook/worktree clients, or has no observed ergonomic advantage |

Thresholds are **initial acceptance hypotheses**, not claims. Freeze fixture mix and benchmark protocol before measurement; report both wins and regressions. A true partial-index response must say **inconclusive** rather than silently returning an empty dependency set.

## Proposed design constraints (only after spike)

- **One writer/owner:** Git/GitHub remains authoritative for code history; Menhir stores bounded derived references and qualification. Do not create another mutable repository truth store.
- **One event normalization seam:** if `CodeChangeObservation` is earned, reuse it for blast-radius freshness, snapshot comparison and document drift, with trust metadata. Tool/file hooks remain unverified observations.
- **Keep identities separate:** Git commit OID, tree OID, clean/dirty working tree, bundle digest, scan generation, project/workspace pointer and currentness receipt are not interchangeable.
- **Correctness first:** missing base commits, unobservable remote roots, incomplete indexes, disputed branch ancestry and excluded content must not produce "safe"/"unchanged" claims.
- **Security:** existing scope/tier/namespace boundaries, sanitized manifest, no full repo blobs or secrets in the memory graph or telemetry, explicit source authorization, fail-closed path handling and bounded work.
- **Temporal/provenance consistency:** avoid confusing event observation time, source commit time, knowledge validity, and artifact review time. Derived indexes must be disposable/rebuildable and compatible with erasure.
- **No implicit document authority changes:** code-change matches emit review candidates only; review receipts, not model guesses or drift signals, settle documentation currentness.

## Alternatives considered

1. **Keep Git + existing indexing:** zero new complexity, must be the measured control.
2. **Improve existing Git queries / indexes directly:** preferred if E1 shows a narrow query bug or obvious missing cache.
3. **Scion-inspired revision projection:** investigate only if current query path has repeatable deficits.
4. **Adopt Anthropic Scion:** not public and unsuitable for general Git replacement; not executable.
5. **Jujutsu with Git backend / Sapling CLI:** potential alternate *agent frontend*, not Menhir server/backend authority.
6. **Use Mononoke/EdenFS / build a new VCS:** cost and migration vastly exceed measured Menhir need; reject without a different organizational scale.

## Risk and validation

Risks: accidentally duplicating current snapshot/doc-drift owners; building a novel VCS abstraction; overtrusting client-declared hashes or commits; materializing secret bytes; cross-tenant/cross-project leakage; losing second-parent history and rebased evidence; introducing unsized server work. Require explicit negative fixtures for each.

- **Exploration checks:** source links and code-status inventory; reproducible bench scripts and frozen fixture manifest; correctness property fixtures; measured results with exact Git/ Menhir revisions and command lines.
- **If future production implementation is approved:** direct unit/property replay tests; disconnected/offline read parity; disposable Neo4j tests for scope/erasure/graph generations; fail-closed coverage and false-negative cases; bounded resource tests; shadow output comparison; rollback receipt; required CI on exact SHA.
- **Docs-only phase:** run `menhir artifacts validate . --repository menhir` and check index/link paths; no pytest required until executable code or fixtures change. Never claim command success if a remote docs-only session cannot run it.

## Deliverables and ownership

1. Code/status map for current Git and snapshot provenance.
2. Bench report (baseline, negative cases, query costs, snapshot bandwidth).
3. Optional isolated spike and explicit pass/fail data.
4. A decision memo: **adopt specific pattern / optimize existing path / park**, including cost and alternatives.
5. If warranted, a **new approved implementation plan** owned by the existing snapshot/index/documentation subsystem, not an automatic promotion of this research proposal.

**Documentation to update if promoted to an implementation plan:** `.agent/architecture.md`, `.agent/data_models.md`, `.agent/endpoints.md`, snapshot owner and doc-drift owner contracts, `docs/research/prior-art/README.md`, relevant roadmap/plan indexes, `CHANGELOG.md`. Maintain per-revision citations; do not rewrite the prior-art note into an unversioned marketing assertion.
