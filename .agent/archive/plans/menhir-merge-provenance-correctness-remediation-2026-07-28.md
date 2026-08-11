# Merge Provenance Correctness Remediation

> **Archived 2026-08-11.** Complete provenance derivation, exact race guards, effective authority,
> and exact unmerge behavior are implemented and tested.

## Objective

Correct commit `01a10e4` without broadening its product design: merged provenance must be
conservative, complete across merge chains, concurrency-safe, and exactly reversible for new
merges. Fix the independently observed test-state leak as a separate final phase.

Starting point: Menhir `main` at
`01a10e4db7e9200c53c632e57ba0de4919c5cdb4`.

## Non-negotiable execution contract

- Treat every numbered issue, invariant, scope boundary, and acceptance criterion below as binding.
- Inspect the current implementations, callers, snapshots, and existing tests before editing.
- Make the smallest coherent diff. Do not introduce a new provenance framework, alternate merge
  path, migration system, or compatibility layer.
- Do not touch production Neo4j data or write a backfill.
- Do not weaken unmerge Guard 2 or reinterpret old merge snapshots to make them pass.
- Preserve the deliberate decision that pre-`01a10e4` merges may refuse exact unmerge because their
  formula differs.
- Preserve unrelated untracked files and existing user work. Do not commit or push; leave the diff
  ready for Codex review.
- If a correct fix requires a new durable provenance representation beyond the existing `sources`,
  `source`, `source_confidence`, and `corroboration` properties, stop and report the concrete
  blocker before implementing that schema expansion.

## Why

`01a10e4` correctly removes merge-count-based `source_confidence += 0.1`, but review found five
integration defects:

1. Exact unmerge always refuses new merges because the production survivor read omits the two new
   merge-owned properties.
2. Authority is recomputed from source-label ceilings and can raise a legitimately downgraded
   node's trust.
3. A previously merged node loses non-primary contributors when it is later absorbed.
4. The two-phase concurrency guard checks only the primary source and can overwrite a concurrent
   contributor update.
5. A source-less merge chain turns the synthetic label `merged` into a contributor and fabricates
   corroboration.

The independent full-suite run also exposed an unrelated module-global cache leak: one MCP
formatter test leaves `_stuck_count_cache=1`, causing a later `add_memory` assertion to fail. That
failure passes in isolation and must be fixed without weakening the production warning.

## Scope

In scope:

- Merge-domain provenance and authority derivation.
- The correlation repository's Phase 1 provenance read, Phase 2 race guard, parameters, and audit
  snapshot.
- Exact-unmerge survivor reads and round-trip coverage.
- Empty/legacy contributor normalization.
- Focused unit, repository-contract, chained-merge, concurrency, and exact-unmerge tests.
- The formatter-test cache isolation defect.
- `.agent/data_models.md` and `.agent/CHANGELOG.md`.

Out of scope:

- Production data changes or backfills.
- Re-enabling exact unmerge for merges made before `01a10e4`.
- Changing merge eligibility, similarity, text-selection, edge bridging, episode rebinding, scalar
  reconciliation, source-family policy, or confidence-tier constants.
- Changing the user-visible FAILED-memory warning.
- Refactoring unrelated merge, recall, lifecycle, or MCP code.

## Design

### Phase 1 — Preserve observed authority

The source tier returned by `source_confidence_for(label)` is a ceiling, not proof of the node's
actual confidence. Replace label-only authority derivation with an effective-authority calculation
that:

- uses each input node's observed `source_confidence` when it is a valid finite numeric value;
- caps that observed value by the lowest nominal tier of the node's contributors;
- falls back conservatively to the existing agent/default tier when provenance is missing or
  malformed; and
- computes merged authority as the minimum effective authority of both input nodes.

This must satisfy:

```text
merged_authority <= survivor effective authority
merged_authority <= absorbed effective authority
merged_authority <= every applicable contributor-label ceiling
```

Do not retain the invalid equality
`source_confidence_for(node.source) == node.source_confidence`; legitimate explicit downgrades mean
the stored confidence may be lower than the label's ceiling. `source` may remain the deterministic
lowest-nominal-tier contributor, while `source_confidence` carries the conservative effective
authority.

Regression example: merging
`{source: project-scan, source_confidence: 0.5}` with another such node must remain `0.5`, never
become `0.9`.

### Phase 2 — Make contributor normalization lossless

- Distinguish a missing/`null` legacy `sources` property from an explicitly empty list.
- Fall back to the legacy comma-separated `source` only when `sources` is absent or `null`, not when
  it is `[]`.
- Never treat the synthetic fallback label `merged` as a real contributor.
- Preserve ordered, de-duplicated contributors from both survivor and absorbed nodes.

Regression examples:

- Absorbing `{sources: [claude-code, project-scan], source: claude-code}` must carry both labels
  forward.
- Repeatedly merging source-less nodes must keep `sources=[]` and `corroboration=0`.

### Phase 3 — Read and guard every provenance input

Update `CorrelationRepository.merge_entity` so Phase 1 returns the complete provenance used by the
derivation for both nodes:

- `source`
- `sources`
- `source_confidence`
- `corroboration` where needed for an exact audit snapshot

Include the absorbed node's new provenance properties in the graph/sidecar audit entry so a
legacy/degraded restore does not silently erase them.

The Phase 2 mutation must fail closed if any provenance value used by Phase 1 changed before the
write. At minimum, guard both survivor and absorbed `source`, `sources`, and `source_confidence`.
Use null-safe/list-safe comparisons. Checking only `source` is insufficient because a concurrent
merge may add a higher-tier contributor while leaving the same lowest-tier primary source.

Add a repository-contract test that proves the query and parameters contain the complete guard.
Add a race regression for:

```text
Phase 1 reads survivor source=codex, sources=[codex]
another merge writes source=codex, sources=[codex, claude-code]
the stale mutation must abstain rather than overwrite the second contributor
```

Do not solve this with a second merge implementation. If the existing repository abstraction
cannot express the guard atomically, stop and report that architectural blocker.

### Phase 4 — Restore exact unmerge for new merges

`MERGE_OWNED_SURVIVOR_PROPERTIES` now contains six fields. Update
`CorrelationRepository.fetch_survivor_properties` to return all six:

- `summary`
- `content`
- `source`
- `source_confidence`
- `sources`
- `corroboration`

Add an adapter-level test that would fail with the current four-field query. Add a real
merge→unmerge round-trip test using the stood-up test Neo4j path, covering the new provenance
properties on both survivor and absorbed nodes. The round trip must restore the exact pre-merge
properties and must not loosen newer-state protection.

Old formula snapshots remain deliberately unsupported; do not change that policy.

### Phase 5 — Lock the behavior with tests and documentation

Required focused tests:

1. Explicit confidence downgrade never increases through one or repeated merges.
2. Nominal label ceilings still cap inflated legacy confidence.
3. Prior `sources` lists are preserved on both the survivor and absorbed sides.
4. Contributor order remains deterministic and duplicates remain removed.
5. Same-family labels count once; independent families count separately.
6. Empty contributor lists remain empty across chained merges.
7. The mutation race guard covers both nodes' complete provenance inputs.
8. A new exact merge/unmerge round trip succeeds and restores all six merge-owned properties.
9. Existing structural-memory recognition still finds `project-scan` inside a preserved `sources`
   list after a chained merge.

Update `.agent/data_models.md` to document `sources` and `corroboration`, including the distinction
between nominal label ceiling and effective stored authority. Update `.agent/CHANGELOG.md` with only
the files changed in this remediation.

### Phase 6 — Isolate the unrelated formatter cache test

Reproduce the order dependency involving:

- `tests/test_mcp_formatters.py::test_queue_summary`
- `tests/test_mcp_server.py::test_add_memory_queues_episode_for_background_enrichment`
- `menhir.mcp.formatters._stuck_count_cache`

Fix test isolation, preferably by explicitly configuring `fetch_memory_overview()` and resetting
the cache around tests that mutate it. Do not remove caching, change its TTL, suppress the warning,
or weaken the `add_memory` assertion merely to obtain green tests. Add an order regression if a
small deterministic test can express the leak.

## Risks

- A label-only representation cannot preserve arbitrary per-contributor confidence. This plan
  intentionally requires only conservative aggregate authority across merge chains. Stop before
  adding a parallel per-contributor confidence schema.
- Phase 1 and Phase 2 are separate Neo4j operations. Every value used to derive Phase 2 output must
  be guarded, or concurrent merges can lose provenance.
- Online tests are destructive by nature. Run only the existing stood-up test instance fixtures;
  never use operator production Neo4j.
- The full suite has a known order-dependent cache failure at the starting commit. Fix it separately
  and do not misattribute it to provenance work.

## Validation

Run, in this order:

1. Focused pure/repository tests:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest tests/test_merge_delta.py tests/test_correlation_service.py -q
   ```

2. Exact merge/unmerge tests, with online tests enabled only against the configured test Neo4j:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest tests/test_merge_coordinator_live.py tests/test_unmerge_coordinator_live.py --run-online -m online -q
   ```

3. Formatter order regression:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest tests/test_mcp_formatters.py tests/test_mcp_server.py -q
   ```

4. Full non-online suite through the project-supported environment:

   ```powershell
   .\scripts\menhir.ps1 test
   ```

5. `git diff --check`, `git status --short`, and a complete diff review.

If online prerequisites are unavailable, report those tests as unrun; do not claim the
merge/unmerge production path is verified.

## Acceptance criteria

- All five merge defects above have a failing-before/passing-after regression test.
- A downgraded `project-scan @ 0.5` node never rises above `0.5` because of a merge.
- An already-merged absorbed node retains every contributor and correct corroboration.
- A concurrent provenance change causes the stale mutation to abstain.
- Exact unmerge succeeds for a new merge and restores all six merge-owned properties.
- Source-less merge chains never create a synthetic contributor.
- The formatter/MCP test order is deterministic without changing production warning behavior.
- Focused and full non-online suites pass; online results are stated accurately.
- Data-model and changelog documentation match the implemented graph properties.
- No production data, unrelated files, commits, or pushes are made.

## Required completion report

At completion, report:

- each numbered issue and the exact fix;
- every file changed and why;
- exact commands and pass/fail/skip counts;
- any unrun online checks and the missing prerequisite;
- `git diff --check` and final `git status --short`;
- remaining risks or deviations from this plan.

Before declaring completion, reread this plan and inspect the entire diff. A green unit suite alone
is not sufficient.
