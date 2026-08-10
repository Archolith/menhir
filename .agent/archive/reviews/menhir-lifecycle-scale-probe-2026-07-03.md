# Probe: lifecycle similarity-scale verification against the live graph (2026-07-03)

**Verdict: CONFIRMED — worse than predicted.** The cosine-calibrated correlation thresholds run
against graphiti RRF rank scores in production, auto-merge has executed ~2,679 absorptions on the
live graph, and the sharpness "uniqueness" signal has collapsed for ~59% of persistent nodes.
Read-only probe; no writes performed. Scripts: `probe_telemetry.py` / `probe_graph.py`
(job tmp; queries reproduced below where load-bearing).

## Scope note — TWO checkouts run this code
The production MCP server (`:8090`, `mcp__memory`) runs from **`projects/archolith/menhir`**
(`menhir.cli serve-watch`), not the reviewed `menhir-frontier`. Production carries the identical
defect (`search_scored` → `CORRELATION_MERGE_THRESHOLD` at its `lifecycle_service.py:335`;
same `_count_similar_nodes`). Every remediation plan written against frontier ALSO needs a
production landing path. Live graph: `bolt://neo4j.example.internal:7687` (per `menhir/.env`).

## Evidence

### 1. The scale fingerprint (decisive)
`RELATES_TO` edges store the `similarity` that fired (763 edges, distinct):
```
min 0.70   max 0.8333333…   avg 0.774   |   ≥0.95: 0   >1.0: 0
```
**Max is exactly 1/2 + 1/3** — the RRF sum of ranks (1,2) under rank_const=1. Min 0.70 = 1/2+1/5
(ranks 1,4). A cosine band would fill 0.70–0.85 continuously and not cap at the rational constant
0.8333. The similarity values are RRF rank sums; absolute similarity never entered the decision.
Corollary: any pair where a neighbor ranked #1 in a search method scored ≥1.0 → crossed the 0.95
merge threshold → **"nearest-by-rank" ⇒ merged**. The 0.85–0.95 conflict band is nearly empty on
this ladder, which matches the conflict outcomes below.

### 2. Merge damage (graph receipts: `merged_from`)
```
1,328 survivor nodes carry merged_from receipts; 2,679 absorbed nodes total (deleted).
Top absorbers: 'src/components/article' (56), 'src/main/resources/db/migration' (44),
'src/main/java/rip/yawn/market/' (26), 'suites' (18), 'Claude Code sessions' (12),
'.gitignore' (11), 'Score' (11), '.agent' (11), 'User' (10).
```
Generic names and path prefixes absorbing dozens of distinct memories is the predicted
false-merge signature (path-sibling and name-collision pairs rank each other top). An unknown
fraction are legitimate re-extraction dedups; the absorbed nodes' content is deleted, so
per-merge adjudication is impossible — only the uuid list survives. **No unmerge is possible**
(no snapshot was taken), confirming the audit-trail requirement in
`plans/ingest-identity-merge-gating.md` Part 3.

### 3. Sharpness collapse (uniqueness signal is a rank artifact)
```
PERSISTENT nodes: 32,399   sharpness NULL: 8,413
sharpness < 0.1: 18,990 (59%)   sharpness < 0.5: 19,012
```
Sharpness = 1/(1+count of hits ≥0.7 RRF over top-10). Values <0.1 require ~10 qualifying hits —
reachable because graphiti's multi-method RRF makes mid-rank sums exceed 0.7 routinely. Result:
"uniqueness" ≈ 0.09 for most of the graph. Consequences, both directions:
- **Consolidation promote-or-delete:** the ≥0.5 promote bar is failed by ~everything sharpness
  reaches → unflagged, low-edge session nodes deleted as "redundant" regardless of actual
  uniqueness. (Deletions leave no receipts; count unquantifiable — itself a finding.)
- **Decay GONE gate (<0.1):** passed by 18,990 nodes — the gate intended as a near-impossible
  last check is a near-universal pass. 115 `gone` deletions recorded in `lifecycle_actions`
  (with days>90 + edge_count<3 doing the remaining gating). 158 compressions, 189 rehydrations.

### 4. Conflict path starvation
354 conflict groups exist — **every one resolved `false_positive`, zero `unresolved`**. Matches
the band-collapse prediction: genuine near-duplicates score ≥0.95 (RRF) and get merged *instead
of flagged*, so the well-designed judge path receives only junk. The layer's best machinery is
starved by the broken scale upstream of it.

### 5. Operational scale (telemetry, mixed live+bench traffic)
2,049 consolidation runs and 668 decay runs since 2026-04-15; enrichment 22,290 episodes.
Recent runs include `replay-session-*` (bench farm on ports 8107–8121 was active during the
probe), so per-run attribution is mixed; the graph receipts above are production-graph state and
unaffected by attribution.

## Immediate recommendations (production is actively running this)
1. **Hotfix — disable auto-merge now** in BOTH checkouts: route the ≥0.95 band to the conflict
   flag (one-line change in `_check_contradictions_batch` and `correlation_service._action_for`)
   until judge-gated merging lands. Every nightly run merges more.
2. **Hotfix — stop sharpness-based session deletion**: treat below-threshold sharpness as
   "no promotion signal" (keep as SESSION / promote) rather than delete, until sharpness is
   recomputed on a lawful scale. Deletion is the irreversible arm; noise is the repairable one.
3. Fold the sharpness recomputation into the (now cross-layer) scale-contract plan — this is the
   5th symptom of the one root cause (floor, priors, lifecycle bands, sharpness, + the frontier
   copy of each).
4. The `merged_from` uuid lists are the only surviving merge evidence — preserve them (no cleanup
   jobs touching `merged_from`) for any future partial audit against episode MENTIONS.

## Caveats
- Merge legitimacy fraction unknown (receipts name survivors, not what the absorbed content was).
- Telemetry counts include bench traffic; graph state does not distinguish which runs wrote it.
- `freshness` NULL on 8,599 nodes (treated as ACTIVE by code defaults) — noted, not probed further.

## CORRECTION 2026-07-04 — the sub-0.1 sharpness population has a different primary writer

Follow-up reading of `graphiti_client.search_scored` and `episode_stamping.py` splits finding §3
into two distinct defects; the hotfixes remain correct, but the mechanism narrative changes:

1. **`search_scored` uses exactly TWO methods** (bm25 + cosine, `NodeReranker.rrf`) — confirming
   the 2.0 dual-method max and the 0.8333 fingerprint exactly. But on a two-method 1/(rank+1)
   ladder, `compute_sharpness` can only produce values ≥ ~0.2 (at most ~4 hits can score ≥ 0.7).
   **Computed sharpness cannot reach < 0.1.** The RRF miscalibration therefore corrupts the
   0.2–0.5 band — the consolidation promote-or-delete bar (≥0.5) and the compress gate (<0.3) —
   which is where the session-deletion damage came from.
2. **The 18,990 nodes below 0.1 are predominantly stamp-time defaults**: `stamp_ingest_metadata`
   sets `n.sharpness = coalesce(toFloat(n.sharpness), 0.0)` on every stamped Entity and Episodic
   (`episode_stamping.py:56,102`). Every memory is initialized at sharpness 0.0 — *maximally
   forgettable* — and keeps it until a consolidation/decay pass happens to recompute. Unknown
   uniqueness defaults to "delete me": the exact inversion of the protective direction
   (`memory-lifecycle-under-uncertainty.md` §3 — proxies must fail safe). This, not the RRF scale,
   is the primary feeder of the GONE gate (<0.1) and its 115 recorded deletions.

**Additional remediation item (queued, not urgent — both consumer gates are disarmed):** change
the stamp default to a protective value (null + null-protective gates, or 1.0 = "unique until
proven redundant") and make `fetch_decay_candidates`/`should_delete` treat missing sharpness as
protection, never as eligibility. Belongs in the lifecycle remediation plan alongside the lawful
sharpness recomputation.

Also verified in the same pass: the stamping choke point's trust writes are sound (coalesce-only,
with a `locked` guard preventing PERSISTENT/PROMOTED downgrades — the policy promise is real), and
`hybrid_retrieval.weighted_rrf` is scale-clean (k=60, min-normalized to leader=1.0, honest
open-item docstring on the vector-floor interaction). Two RRF conventions now coexist in the
codebase (opaque rank_const=1 in `search_scored`; k=60 normalized in the attributed hybrid) — the
scale-contract plan should name both.

## ADDENDUM 2026-07-04 — invocation topology (who actually ran the destructive sweeps)

Traced every invocation path for the lifecycle sweeps (both checkouts):
- **The maintenance scheduler runs NEITHER consolidation NOR decay.** Its roster: lease recovery /
  enrichment retries / queue health (30s), conflict auto-resolve (daily), confirm (hourly), review
  (weekly), structure refresh (30min), experience counters (hourly).
- **Consolidation's real engine is `recover_orphans` at runtime init** (`runtime.py:222-225`,
  fire-and-forget on every process start), plus explicit bench/session calls. With serve-watch
  reloads and a ~15-server bench farm, "every process start" ≈ continuously. This was the engine
  behind the merge/delete damage.
- **`apply_decay` has NO production invoker** — not the scheduler, not the API dispatch, not MCP
  tools, not cli/hook, not archolith-bench imports. Only tests and manual/bench calls reach it.
  The 115 GONE deletions therefore came through test/bench channels writing into the shared graph
  — which softens (but does not retract) the personal-memory damage attribution, and means the
  GONE disarm is defense-in-depth rather than stopping an active production timer.
- **Telemetry is a single shared sink** (`workspace_root()/.agent/mcp_telemetry.db`) for prod,
  bench, and tests alike — the 2,049/668 run counts mix all three. `MENHIR_MCP_TELEMETRY_DB`
  exists as an override; bench/test runs should set it (small hygiene item for the bench harness)
  so future forensics can attribute.
