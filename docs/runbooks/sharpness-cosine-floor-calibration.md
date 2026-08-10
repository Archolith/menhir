# Sharpness Cosine-Floor Calibration — runbook

`SHARPNESS_COSINE_FLOOR` (in `src/menhir/services/lifecycle_service.py`) is the genuine
cosine-similarity threshold used to count a memory's true neighbors. Sharpness
(`1 / (1 + neighbor_count)`) is the sole gate on the lifecycle compress/promote arms, so this
floor decides how aggressively lifecycle treats memories as duplicates vs unique.

This runbook is how the floor was chosen, and how to re-choose it when its assumptions break.

```
sample PERSISTENT :Entity nodes -> for each, count true-cosine neighbors above candidate floors
-> sharpness distribution per floor -> pick the smallest floor that DISCRIMINATES
-> set SHARPNESS_COSINE_FLOOR + record the winning row here
```

## When to re-run

The current floor is calibrated against a specific embedder and corpus. **Re-run and re-decide when
either changes:**

- **Embedder change.** The floor is embedder-specific (values live on that model's cosine scale).
  Current embedder: OpenAI `text-embedding-3-small`. Any change invalidates the floor.
- **Corpus drift.** Calibrated on the `default` namespace (dense, ~49.7k nodes). If the graph's
  composition shifts materially, re-sample.

Do NOT re-run for routine operation — the constant is stable between these triggers.

## How to run (read-only)

The probe is `scripts/probe/probe_sharpness_cosine_floor.py`. It is **READ-ONLY**: no graph writes,
no lifecycle actions — only MATCH reads and cosine searches. It exercises the exact production path
(`GraphitiClient.count_similar_by_cosine`, namespace-scoped). Requires the live graphiti client
(reads ready); aborts if unavailable.

```
cd projects/archolith/menhir
.venv/Scripts/python.exe scripts/probe/probe_sharpness_cosine_floor.py \
  --sample 150 --seed 0 \
  --floors 0.75,0.80,0.85,0.90 \
  --out .agent/test_tmp/f2-cosine-floor-probe.md
```

- Omit `--namespace` to sample PERSISTENT `:Entity` nodes across ALL namespaces (each node's
  neighbor count is still scoped to its own namespace, matching production). Pass `--namespace <ns>`
  to calibrate against one silo.
- `.agent/test_tmp/` is gitignored — the raw report is transient scratch. Promote the winning row
  into the "Last result" section below (do not commit the raw probe output).

## Selection criteria

Choose the **smallest** floor meeting all three (the script auto-suggests it):

1. zero-neighbor share `=1.0` ≤ **60%** — not everything is marked "unique" (under-trim).
2. compress-eligible share `<0.3` ≤ **25%** — not a mass-compress cliff (over-trim).
3. the `0.2–0.5` band is populated — promote/compress actually discriminate.

## Apply the decision

1. Set `SHARPNESS_COSINE_FLOOR = <winner>` in `src/menhir/services/lifecycle_service.py`.
2. Update the constant's comment there with the new winning-row numbers and date.
3. Record the run in "Last result" below (table + rationale).

## Last result

**Decision (2026-07-10, @ctharvey via Claude): `SHARPNESS_COSINE_FLOOR = 0.80`.**

- Namespace: `default` | sampled nodes: 150 | seed: 0 | skipped (search unavailable): 0
- Floors: 0.75, 0.80, 0.85, 0.90
- sharpness = 1/(1+count of true-cosine neighbors strictly above the floor)

| floor | n | median | <0.2 | 0.2-<0.3 | 0.3-<0.5 | 0.5-<1.0 | =1.0 | compress(<0.3) | promote(>=0.5) | zero(=1.0) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.75 | 150 | 0.200 | 67 | 27 | 13 | 21 | 22 | 63% | 29% | 15% |
| 0.80 | 150 | 1.000 | 18 | 18 | 7 | 23 | 84 | 24% | 71% | 56% |
| 0.85 | 150 | 1.000 | 5 | 0 | 10 | 20 | 115 | 3% | 90% | 77% |
| 0.90 | 150 | 1.000 | 1 | 1 | 7 | 16 | 125 | 1% | 94% | 83% |

**Winning row:** `floor 0.80 | median 1.000 | compress 24% | promote 71% | zero 56%` — the only floor
meeting all three criteria and the smallest that discriminates. 0.75 is rejected as a mass-compress
cliff (63% compress-eligible); 0.85 and 0.90 mark 77–83% of memories "unique" (under-trim). The
0.75→0.80 transition is steep, reflecting a dense `default` corpus; 0.80 is the balanced knee.

**Caveats:** calibrated on `default` (49.7k nodes); other namespaces are ≤150 nodes each and share the
same [0,1] cosine scale, so the floor transfers. Embedder: OpenAI `text-embedding-3-small` — a future
embedder change invalidates this floor and requires a re-run.

## Related

- Constant + inline comment: `src/menhir/services/lifecycle_service.py` (`SHARPNESS_COSINE_FLOOR`).
- Implementation plan (P3): `plans/lifecycle-f2-lawful-sharpness-implementation.md`.
