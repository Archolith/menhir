# HANDOFF REVIEWED WITH FINDINGS — Menhir recall hygiene live cutover

**Created:** 2026-07-13 (Codex)

**For:** the next Menhir implementation/operator session

**Type:** implementation + live-operations handoff (not a wrapup)

**Review status:** REVIEWED WITH FINDINGS (2026-07-13)

## Start here

The approved plan is:

`C:\Users\you\IdeaProjects\.agent\plans\menhir-recall-hygiene-and-scoped-bootstrap.md`

The code plan landed across three repositories. Menhir was restarted afterward, and the live test
proved that the new contract is loaded. It also found a legacy-data compatibility gap that keeps the
live recent bootstrap from satisfying the plan's structural-leakage gate.

## Commit anchors and repository state

| Repository | Branch / state | Commit | Purpose |
|---|---|---|---|
| `projects/archolith/menhir` | `main`, clean, 15 ahead of `origin/main` | `2ccce26d3d225e2a0a398385c24a14877cb7225c` | Scoped bootstrap, recent filtering, score provenance, migration utility, APIs, hooks |
| `projects/archolith/menhir` | live `HEAD` | `4394eb69bb8be6eaf0a1184e5657873f4c7626b7` | Contains `2ccce26d`; later graph-operation fencing commit is also loaded |
| `projects/archolith/archolith-bench` | `master`, clean, 1 ahead of `origin/master` | `7303b6a9f3b21e1e43e72a0d4fe356de92179c2d` | Offline/live bootstrap-hygiene gate |
| workspace root | `master`, dirty with unrelated work, 7 ahead of `origin/master` | `d2be66adc9fa1643ec68857484df7745cae23cb4` | Canonical `archolith` / `ctharvey` / `yawn` workspace-key documentation |

None of these commits is present on a remote branch. Do not bulk-stage or clean the workspace root;
its unrelated tracked and untracked work belongs to other tasks.

## What shipped

- Startup pins now distinguish retention (`user_flagged`) from injection (`bootstrap_scope`).
- Valid bootstrap selections are `general` and `workspace:<normalized-key>`; no workspace means
  general-only.
- Bootstrap receipts and version hashes are keyed by reader plus selection.
- Recent context accepts a namespace and attempts to exclude structural nodes.
- MCP/REST recall results expose `retrieval_score`, `retrieval_score_kind`, and
  `relevance_basis="legacy_rrf_threshold_unvalidated"` without changing ranking.
- The Palworld negative query remains report-only; this plan did not tune thresholds or ranking.
- `archolith-bench menhir bootstrap-hygiene --offline` is the deterministic policy gate.
- `scripts/migrate_flagged_bootstrap_scope.py` provides reviewed, fingerprinted, backup-gated
  production classification. It was applied on 2026-07-14; the cutover receipt is recorded below.

## Live runtime state after restart

Restart command used:

```powershell
Set-Location C:\Users\you\IdeaProjects\projects\archolith\menhir
.\scripts\start-server.ps1 restart
```

Observed after restart:

- backend PID `47800`; watchdog PID `21800`;
- `/api/health`: `status=ok`, `startup_mode=full`;
- `/api/ready`: `status=ready`; Neo4j, embedder, LLM, scheduler, reads, queue writes, and enrichment
  capability flags all `true`;
- the scheduler reclaimed the lease from the dead pre-restart PID as designed;
- `start-server.ps1 status` incorrectly printed `http=unreachable` even while direct health and
  readiness requests returned successfully. Treat this as launcher-status diagnostic drift.

The readiness response also included an internally inconsistent embedding warning: it claimed a
dimension mismatch while reporting zero wrong entities, communities, and relationships. Do not run
the repair script based on that string alone; diagnose the readiness predicate first.

## Live smoke-test evidence

### Before restart

- Global flagged bootstrap returned 20 mixed records, including Yawn and Palworld data.
- All 10 recent slots were structural/project-scan directory records.
- The Palworld dedicated-server negative query returned 10 unrelated Archolith/OAuth records.
- The response lacked the new workspace selection and retrieval-score provenance fields.

### After restart

- The same-task MCP call returned `bootstrap_selection="general"`, `flagged_count=0`, proving the
  new backend contract is active.
- An authenticated REST bootstrap using `workspace="archolith"` returned
  `bootstrap_selection="workspace:archolith"`, a matching context receipt/version, and no Yawn/TCG
  cross-workspace records.
- Existing flagged memories have null `bootstrap_scope`, so Archolith currently receives zero pins.
  This is fail-closed and expected until reviewed migration.
- Recall results now expose `retrieval_score_kind="graphiti_rrf"` and the unvalidated legacy
  relevance label.
- The negative query still returned 10 false positives. That metric is explicitly non-gating in the
  approved plan.
- Recent context improved but still failed the structural gate: 6 of 10 returned rows were legacy
  project-scan directories (`scheduler-fence-*`, `components`, `mcp-telemetry-*`, `features`,
  `scheduler-lock-*`, and `full`).

## Unresolved defect: legacy structural rows are unmarked

`src/menhir/infrastructure/memory_queries.py:71` filters recent membership with:

```text
n.structure_role IS NULL
```

That works for current structural fixtures and marked nodes, but the production directory records
observed above have:

- label `Entity` only;
- `source="project-scan"`;
- content shaped like `Directory: <path>`;
- no `structure_role` property.

Therefore they satisfy `n.structure_role IS NULL` and leak into recent context. Do not exclude every
`project-scan` row: the live graph also contains useful semantic project-scan records. Inventory the
legacy Directory/File/Project shapes first, then choose one of these bounded fixes:

1. add a compatibility exclusion for positively identified legacy structural shapes and cover it
   with repository, REST, MCP, and live black-box tests; or
2. produce a reviewed, reversible backfill that marks those legacy nodes with the correct
   `structure_role`, then retain the simpler query predicate.

Whichever path is chosen, rerun the live workspace bootstrap and require zero Directory/File/Project
rows before claiming the plan complete.

## Production bootstrap-scope migration — APPLIED 2026-07-14

Do not apply this without reviewing every manifest row. The intended sequence is:

```powershell
Set-Location C:\Users\you\IdeaProjects\projects\archolith\menhir

# Read-only logical backup. Record the emitted path.
.\.venv\Scripts\python.exe scripts\export_graph_backup.py

# Read-only candidate manifest.
.\.venv\Scripts\python.exe scripts\migrate_flagged_bootstrap_scope.py plan `
  --out .agent\test_tmp\recall-hygiene-20260714.jsonl

# Review every row. Structural rows require target_structure_role; semantic flagged rows require
# target_bootstrap_scope=general, workspace:<key>, or none.
# First run the apply command without --yes for a mutation-free rehearsal.
.\.venv\Scripts\python.exe scripts\migrate_flagged_bootstrap_scope.py apply `
  --manifest .agent\test_tmp\recall-hygiene-20260714.jsonl `
  --backup C:\Users\you\IdeaProjects\backups\prod-neo4j-export-20260714_052900-quiescent.jsonl.gz

# Apply only after reviewing the rehearsal and obtaining explicit operator approval.
.\.venv\Scripts\python.exe scripts\migrate_flagged_bootstrap_scope.py apply `
  --manifest .agent\test_tmp\recall-hygiene-20260714.jsonl `
  --backup C:\Users\you\IdeaProjects\backups\prod-neo4j-export-20260714_052900-quiescent.jsonl.gz `
  --yes

# Read-only verification and idempotence check.
.\.venv\Scripts\python.exe scripts\migrate_flagged_bootstrap_scope.py verify `
  --manifest .agent\test_tmp\recall-hygiene-20260714.jsonl
```

After apply, inspect general-only, Archolith, Yawn, and a nonexistent workspace. Confirm that `none`
scope remains retention-only, every reviewed structural row has its canonical role, no structural row
is flagged/bootstrap-scoped, and the verifier reports zero pending or unreviewed candidates.

## Fresh Codex acceptance is still owed

This Codex task cached the pre-change tool schema. Restarting Menhir refreshed backend behavior but
did not add `workspace` to this already-open task's generated tool signature. Start a fresh Codex
task rooted at the Menhir repository and run, in order:

```text
read_flagged_memories(reader_id=<stable>, workspace="archolith")
recall_context_memories(
  reader_id=<same>, workspace="archolith", namespace="archolith",
  query="Menhir recall hygiene scoped bootstrap"
)
recall_memories(
  query="How do I set up a dedicated Palworld game server?",
  namespace="archolith"
)
```

Acceptance requires a matching `workspace:archolith` receipt/version, only general + Archolith pins,
no Yawn/TCG recent records, and zero structural recent records. Rate the recalls honestly.

## Verification already completed

- Menhir broad suite excluding the unrelated state-machine case: `2996 passed, 99 skipped`.
- Remaining regression file with that case deselected: `22 passed, 1 deselected`.
- Focused affected Menhir slice: `316 passed, 4 skipped`.
- Archolith-bench full suite: `455 passed, 6 skipped`.
- Offline hygiene gate: PASS; structural/cross-workspace leakage `0`, general/workspace pin recall
  `1.0`, stale advisory `1.0`, negative returned `2`, negative false-positive rate `0.286`, bootstrap
  tokens `37`.
- One literal full-suite Menhir failure remains unrelated to this work:
  `TestSessionLowValueDelete::test_low_value_expired_ttl_deleted` uses a stub without
  `capture_node_state`.
- Production migration, live throwaway benchmark mode, fresh-task Codex dogfood, and pushes were not
  performed.

## Recommended next order

1. Fix or backfill legacy structural-node recognition and prove the live recent gate reaches zero.
2. Diagnose the launcher `http=unreachable` and contradictory embedding-readiness warnings; do not
   mutate embeddings unless the underlying counts prove a mismatch.
3. Generate and review the bootstrap-scope manifest; apply only with explicit operator approval and
   a recorded backup.
4. Start a fresh Codex task to reload the MCP schema and run the first-party acceptance sequence.
5. Run the guarded live benchmark only against a throwaway Menhir/Neo4j target, never production.
6. Push the three repository commits only after confirming the broader ahead-of-origin histories are
   intended to publish together.

## Review Findings — 2026-07-13 (Codex)

### P2 — The shipped recent predicate does not satisfy the live structural-membership contract

The handoff accurately discloses this defect. `fetch_recent_memories()` excludes only nodes whose
`structure_role` is non-null. The observed legacy `source="project-scan"` Directory rows have no
`structure_role`, so they still enter `recent`; the post-restart result of 6 structural rows in 10
therefore leaves Phase 1 and the Definition of Done open. Completion still requires a bounded legacy
shape compatibility rule or reviewed backfill plus a live zero-leakage rerun.

### P2 — The live benchmark's structural gate cannot detect the disclosed production leak

`archolith_bench/bootstrap_hygiene/runner.py` skips every fixture record with a non-null
`structure_role` instead of seeding it, then counts only returned rows whose `structure_role` is
non-null. The production defect is specifically an unmarked Directory row with
`structure_role=null`, so even a returned legacy structural leak increments the metric by zero. The
live artifact also reports `structural_probe_seeded=false`. The runner needs a black-box-supported
structural seed/probe and content/shape assertions capable of detecting the legacy rows before its
structural metric can be treated as an acceptance gate.

### P2 — The benchmark's stale-advisory gate is vacuous

The offline runner selects only fixture rows that already have a truthy `stale_advisory` and then
asserts that all selected rows are truthy. The live runner looks for stale metadata only among the
negative-query recall results and treats `all([])` as success; it never creates a stale anchored
memory or requires one to be observed. Consequently `stale_anchor_advisory_preserved=1.0` can pass
without exercising stale-anchor behavior. Seed a real stale anchor in live mode, require at least one
matching returned item, and assert the exact preserved advisory/action contract.

### P2 — The documented migration command cannot process the stated 751-node corpus

`migrate_flagged_bootstrap_scope.py` defaults both `plan --limit` and `apply --max-rows` to 500 and
refuses an export above that bound. The handoff's commands omit both overrides, while the approved
plan requires inventorying 751 flagged semantic nodes, so the intended sequence stops at manifest
generation (and would also reject a larger reviewed manifest at apply). In addition, a fresh
second-pass `plan` will re-export every intentionally null retention-only row rather than demonstrate
zero pending classifications. Set an explicit reviewed bound above the live count in both commands
and make the verification/idempotence output distinguish reviewed retention-only rows from
unreviewed candidates.

### Verification

- Commit anchors checked: Menhir `2ccce26d3d225e2a0a398385c24a14877cb7225c`,
  Archolith-bench `7303b6a9f3b21e1e43e72a0d4fe356de92179c2d`, workspace docs
  `d2be66adc9fa1643ec68857484df7745cae23cb4`.
- Menhir focused recall/bootstrap/migration slice: `223 passed`.
- Menhir stale-anchor guard: `81 passed`.
- Archolith-bench focused bootstrap suite: `2 passed`.
- Archolith-bench full suite: `455 passed, 6 skipped`.
- Offline CLI output reproduced the reported PASS and metrics, but the two gate findings above mean
  those particular metrics are not acceptance evidence yet.
- Current Menhir `HEAD` differs from the implementation anchor only by the handoff and unrelated
  graph-operation files; the reviewed recall/bootstrap files are unchanged.
- Live health/readiness recheck was not possible: `127.0.0.1:8080` refused connections during this
  review. The handoff's earlier live observations were therefore assessed from its recorded evidence
  and code, not independently reproduced.

### Scores

- **Implementation score: 74/100 (C)** — core scoped-bootstrap and score-provenance work is sound and
  focused tests pass, but the live structural contract, two benchmark gates, migration operability,
  fresh-task acceptance, and production cutover remain open.
- **Wrapup/handoff score: 89/100 (B)** — unusually clear and honest about the primary live defect and
  unperformed operations, with strong anchors and evidence; it missed the benchmark blind spots and
  the migration command's corpus-size/idempotence problems.

## Remediation Update — 2026-07-14 (Codex)

- Implemented one combined, review-gated manifest for legacy structural-role assignment, invalid
  structural flag/scope cleanup, and semantic bootstrap-scope classification. The apply path never
  writes `namespace`, `group_id`, or relationships and verifies their fingerprints after mutation.
- Increased the bounded default to 1,000 and made unreviewed targets fail closed. Structural rows
  require an explicit reviewed role; semantic rows require `general`, `workspace:<key>`, or `none`.
- Added post-apply verification plus manifest-aware idempotence checks that distinguish reviewed
  retention-only rows from unreviewed candidates.
- Verified logical backup:
  `C:\Users\you\IdeaProjects\backups\prod-neo4j-export-20260714_052900-quiescent.jsonl.gz`;
  54,896 nodes, 103,647 relationships, 8,561,188 bytes, SHA-256
  `0E3BD8A05A149712C7E0D5BA7F9D5AC42BEF8801950526A267B6CFD566EB7D67`.
  Menhir and its scheduled watchdog were stopped for the export; gzip/JSONL totals were independently
  recounted before restoring the scheduled task to its prior enabled state.
- Generated the read-only review manifest at
  `.agent/test_tmp/recall-hygiene-20260714.jsonl`: 766 candidates total (748 semantic bootstrap
  scopes, 18 legacy structural directories, 0 already-marked structural cleanup rows).
- Reviewed every candidate into
  `.agent/test_tmp/recall-hygiene-20260714-reviewed.jsonl` with a row-level audit at
  `.agent/test_tmp/recall-hygiene-20260714-review-audit.csv`. Semantic targets: 506 retention-only,
  15 general, 108 `workspace:archolith`, 8 `workspace:ctharvey`, 109 `workspace:yawn`, and 2
  `workspace:palworld-rp`; all 18 structural targets were reviewed as `directory`. Manifest SHA-256:
  `E1B3F80E386E5E5135A699E968C4553EEF7D89FC3F73C139AF841EF0C5BC6CCB`; audit SHA-256:
  `7FBB4F62EBD112E1914F343C577C0A5CE8C2267D5B986624271A836888BB5830`.
- Quiesced Menhir with the scheduled task disabled. The no-write rehearsal validated all 766 rows
  with 260 pending (242 semantic scope writes plus 18 legacy structural roles), 506 unchanged,
  zero missing, and zero drift. The approved `--yes` apply changed exactly those 260 rows and did
  not write `namespace`, `group_id`, or relationships.
- Post-apply manifest verification reported 766/766 verified, zero pending, zero unexpected
  candidates, zero drift, and zero missing. A second apply rehearsal reported zero pending writes.
- Live scoped reads after restart returned only allowed scopes and zero structural rows: general
  15; Archolith version set 123; Yawn 124; ctharvey 23; Palworld 17; nonexistent workspace 15
  general-only. The Archolith context probe returned 8 relevant and 10 recent rows with zero wrong
  namespaces and zero structural rows.
- The `menhir-watchdog` task is restored to `enabled/Ready`; `/api/ready` returns HTTP 200.
