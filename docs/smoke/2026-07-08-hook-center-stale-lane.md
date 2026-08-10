# Real DB Smoke Receipt — Hook Center Stale Anchor Lane

**Date:** 2026-07-08 (run captured 2026-07-09T01:41Z UTC on the throwaway server clock)
**Branch / commit:** `test/real-db-stale-lane-smoke-v1` (base `4c39732`)
**Server URL:** `http://127.0.0.1:8099` (self-served throwaway; real FastAPI router over a real Neo4j graph adapter — no full runtime, no embedder, no scheduler)
**DB / namespace / project:** throwaway Neo4j `bolt://localhost:7688` (disposable container `menhir-smoke-neo4j`, separate from the shared workspace DB); project namespace `smoke-hook-center-stale-lane`

## What this proves

The full stale-file-anchor lane against a **real** backend:

```
file/tool event
-> file marked dirty
-> stale file-anchored memory detected
-> recall labels stale memory
-> formatter/context warns agent to inspect current file
-> stale verification receipt records inspection outcome
-> receipt enriches stale recall only when path-aware and post-dirty
```

Core invariant proved: **a wrong current-state view is worse than a miss.** A receipt never
marks a stale memory fresh; wrong-path, pre-dirty, and malformed receipts never reassure.

## How the real backend is exercised

- **HTTP endpoints** (`POST /api/tool-events`, `GET /api/tool-events/dirty|stale`,
  `POST|GET /api/tool-events/stale-verifications`) run against a throwaway Menhir server that
  mounts the **real** `menhir.api.routes.router` over a **real** `MemoryGraphAdapter`
  backed by a **real** `Neo4jRepository`.
- **Recall labeling, the MCP formatter advisory, the context-builder warning, and the
  receipt-matching / enrichment logic** run in-process through the **real**
  `RecallService.recall()`, `ContextBuilderService.build_context()`, and
  `menhir.mcp.formatters._compact_scored_item` against the same throwaway Neo4j. Only the
  embedding-dependent graphiti vector search is **seeded** — it decides *which* memory is a
  candidate, not the behavior under test. Everything downstream (stale detection Cypher,
  `latest_stale_anchor_verifications` path-aware matching, advisory selection) is the shipped
  code path against real Cypher.

  This approach was chosen because the live `/recall` vector search needs an embedding
  provider that is not wired to a throwaway instance here. Seeding only the vector hit keeps
  the entire stale/verification behavior real while removing the embedder dependency.

## Commands run

```bash
# Throwaway Neo4j (disposable, alt ports, NOT the shared workspace DB)
docker run -d --name menhir-smoke-neo4j -p 7688:7687 -p 7475:7474 \
    -e NEO4J_AUTH=neo4j/smokepass neo4j:5-community

# Smoke (self-serves the throwaway Menhir server in a subprocess)
python scripts/smoke/hook_center_stale_lane_smoke.py \
    --neo4j-uri bolt://localhost:7688 --neo4j-password smokepass --port 8099
python scripts/smoke/hook_center_stale_lane_smoke.py \
    --neo4j-uri bolt://localhost:7688 --neo4j-password smokepass --port 8099 --json

# Unit tests (no Neo4j; HTTP + backend mocked)
python -m pytest tests/test_hook_center_stale_lane_smoke.py -q
```

## Human output

```
server ready: http://127.0.0.1:8099
fixture created: project=smoke-hook-center-stale-lane path=src/smoke_target.py memory_uuid=smoke-memory-001
[1] tool_event_accepted: accepted=True marked_dirty=True
[2] dirty_file_visible: True dirty_at=2026-07-09T01:55:28.16Z op=edit hash=smoke-synthetic-hash
[3] stale_anchor_visible: True path=src/smoke_target.py
[4] recall_stale_label: stale=True control_stale=False
[5] formatter_stale_advisory: action=verify_current_file_before_relying
[6] context_warning_atomic: both_present=True neither_present=True
[9/10] wrong_path + pre_dirty ignored: no_enrich=True verification=None
[7] verification_receipt_recorded: recorded=True listed=True
[8] post_dirty_receipt_enriches: stale=True outcome=still_valid
[11] malformed_timestamp_conservative: status=400
[12] outdated: action=do_not_rely_update_or_supersede no_mutation=True
smoke data cleaned

Result: PASS
```

## JSON output

```json
{
  "result": "PASS",
  "project": "smoke-hook-center-stale-lane",
  "path": "src/smoke_target.py",
  "memory_uuid": "smoke-memory-001",
  "checks": {
    "tool_event_accepted": true,
    "dirty_file_visible": true,
    "stale_anchor_visible": true,
    "recall_stale_label": true,
    "formatter_stale_advisory": true,
    "context_warning_atomic": true,
    "verification_receipt_recorded": true,
    "post_dirty_receipt_enriches": true,
    "wrong_path_receipt_ignored": true,
    "pre_dirty_receipt_ignored": true,
    "malformed_timestamp_conservative": true,
    "outdated_receipt_recommends_no_lifecycle_mutation": true
  },
  "safety": {
    "throwaway_project_used": true,
    "no_file_content_uploaded": true,
    "no_transcript_captured": true,
    "no_phase_3_changes": true,
    "no_turn_evidence_changes": true,
    "no_dirty_clearing": true,
    "no_auto_refresh": true
  },
  "limitations": []
}
```

## Result

**PASS** — all 12 lane checks passed against the real backend. Exit code 0.

### What passed

| # | Check | What it validates |
|---|-------|-------------------|
| 1 | `tool_event_accepted` | `POST /api/tool-events` accepted the file event and marked the file node dirty |
| 2 | `dirty_file_visible` | `GET /api/tool-events/dirty` shows the path + `dirty_at` + `operation=edit`; the provenance `after_hash` is confirmed persisted on the node (the endpoint surfaces operation but not the hash) |
| 3 | `stale_anchor_visible` | `GET /api/tool-events/stale` shows the stale anchor (memory_uuid, path, dirty_at, anchored_at) |
| 4 | `recall_stale_label` | Real `RecallService.recall()` labels the memory `stale_anchor=true` (control `false`) |
| 5 | `formatter_stale_advisory` | Real formatter emits `stale_action=verify_current_file_before_relying` + advisory; non-stale item omits them |
| 6 | `context_warning_atomic` | Real `build_context()`: at a large budget the stale memory body **and** its warning both appear; at a tight budget **neither** appears (never the memory without its warning) |
| 7 | `verification_receipt_recorded` | `POST` receipt accepted and returned by `GET /api/tool-events/stale-verifications` |
| 8 | `post_dirty_receipt_enriches` | A valid post-dirty same-path receipt enriches recall (still stale, never marked fresh) |
| 9 | `wrong_path_receipt_ignored` | A receipt for a different path does not enrich the stale anchor |
| 10 | `pre_dirty_receipt_ignored` | A receipt with `verified_at < dirty_at` does not reassure |
| 11 | `malformed_timestamp_conservative` | A malformed `verified_at` is rejected (HTTP 400); never stored, never reassures |
| 12 | `outdated_receipt_recommends_no_lifecycle_mutation` | An `outdated` receipt yields `do_not_rely_update_or_supersede` but deletes/expires/clears nothing; memory still exists, dirty flag still set, still stale |

### What failed

None.

### What was skipped

None in the full run. (`--skip-recall` / `--skip-context-builder` flags exist for
environments without the menhir package importable; not used here.)

## Limitations

- The graphiti vector-search candidate hit is seeded rather than produced by a live embedder,
  so the *retrieval* step (which memory becomes a candidate) is not exercised end-to-end. All
  *stale-lane behavior* downstream of retrieval runs against the real service + real Cypher.
- The throwaway server mounts the real router directly (no full runtime bootstrap), so
  startup capabilities/health wiring is out of scope for this smoke.
- Unit tests mock HTTP and the in-process backend; they assert script behavior, not backend
  correctness (backend correctness is covered by `tests/test_stale_anchor_verifications.py`
  and `tests/test_recall_stale_labels.py`).

## Safety confirmation

- **No file content uploaded** — the tool event carries only a synthetic provenance hash.
- **No transcript captured.**
- **Throwaway server/project used** — disposable Neo4j container on alt ports; disposable
  project namespace `smoke-hook-center-stale-lane`. Every fixture node carries a
  `smoke_project` marker and cleanup deletes strictly by that marker (+ the receipt's
  `project`), never by bare UUID — so a custom `--memory-uuid` can never widen the blast
  radius. Verified: with a custom UUID, marker-scoped cleanup removed all 3 fixture nodes.
- **No Phase 3 changes.**
- **No TurnEvidence changes.**
- **No dirty clearing** — the smoke never clears dirty flags globally; teardown deletes only
  the smoke project's nodes.
- **No auto-refresh, no down-ranking, no deletion/expiration, no lifecycle mutation.**
