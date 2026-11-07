# process — how research and eval work

Cluster 1 of the research corpus. The workflow that governs every other doc and the menhir↔archolith-bench division of labor.

## Start here

- **How research works here:** `research-process.md`.
- **Before planning a build:** `research-vs-shipped-inventory.md`. It's a snapshot, so check the code too.
- **Where a new result goes:** [Where research results go](../../../.agent/workflows/artifact_authoring.md#where-research-results-go).

| Doc | Status | Owns |
|---|---|---|
| `research-process.md` | canonical | Research workflow: sources, claims, baselines, evals, and the publication ladder. |
| `archolith-bench-operational-model.md` | canonical | Eval-harness responsibilities. Keeps `menhir = proposes/implements`, `archolith-bench = proves/falsifies`. |
| `research-vs-shipped-inventory.md` | canonical (snapshot) | EXISTS / PARTIAL / NEW reconciliation of the whole corpus vs `src/menhir` (audited 2026-06-28). Read BEFORE planning a build; it drifts — re-audit. |

Master index: [`../README.md`](../README.md).
