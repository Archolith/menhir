# Verified Current Findings

> **SUPERSEDED FOR MVP (2026-07-10):** this doc was reconciled against `menhir-frontier`
> @ `e42e6053` (a stale ancestor of `main`). For the current MVP-blocker view, see
> **`verified-current-findings-main-2026-07-10.md`**, reconciled against `main` @ `2f607e3`.
> Notably: MA-03 ("perception is dark code") is **dropped** there (perception is the wired,
> default-off Phase 3 consumer on main), and the OAuth findings are all closed.

Reconciled against the current `menhir-frontier` tree on 2026-07-06, cross-referenced with the
Menhir Frontier Phase 11 audit (`.agent/reviews/menhir-frontier-11-validated-findings.md` in the
workspace root, HEAD `e42e6053`). This file lists what still exists in the current code; items the
audit confirmed but that have since been fixed are recorded under "Remediated" with the commit.

## Remediated (Phase 11 audit findings, fixed after the audit freeze)

- **SEC-01 / EC-01 (High) — readonly key reached write/delete REST + backend RPC.** Fixed in
  `4130e95`: `api/routes.py` gates destructive handlers on operator tier and applies a *total*
  per-operation tier policy on the internal backend dispatch (unmapped op fails the test suite);
  `mcp/contracts.py` enforces per-tool `required_tier`. Constant-time token compares landed too
  (SEC-H02).
- **AR-01 (High) — direct-provider deployments got no maintenance scheduler.** Fixed in `1858e9c`:
  the in-process `MaintenanceScheduler` now starts whenever `capabilities.enrichment_ready`, not
  only when the model endpoints are managed by the external yawn.scheduler (`uses_scheduler`).
- **AR-02 (High) — lease not heartbeated during long jobs.** Fixed in `1858e9c`: a background
  heartbeat renews the lease on a fixed cadence (<= lease/3) independent of job execution, so a job
  longer than the 90s lease can no longer let a second owner acquire it.
- **FC-02 / AR-04 (Med) — forced-takeover overlap.** Fixed in `1858e9c`: on renew failure the
  heartbeat marks the lease lost and the job batch fences out its remaining jobs, so a displaced
  owner stops promptly instead of finishing its whole in-flight batch.
- **SEC-02 (Med) — agent-tier ingest read arbitrary caller paths.** Fixed in `7bceb9a`:
  `core/ingest_guard.ensure_ingest_path_allowed`, enforced at the backend RPC choke point, confines
  non-operator ingest to allowlisted roots (default: working-directory tree; extend via
  `MENHIR_INGEST_ALLOWED_ROOTS`); operator tier bypasses.
- **TQ-01 (test gap) — six offline failures from stale mocks lagging production.** Resolved by
  post-audit commits (`adde330` `FACT_TEMPORAL_FIELDS` export contract; `4130e95` auth-touched
  `test_api_routes`/`test_mcp_server`; `6ff3649` state-machine/decay/lifecycle doubles). Full offline
  suite re-run 2026-07-06: **1924 passed, 31 skipped, 0 failed** (10m38s), including the exact
  audit-flagged files `test_cypher.py`, `test_api_routes.py`, `test_mcp_server.py`.

## Verified Hardening Concerns (still open)

These are real concerns in the current code, but they are more configuration-sensitive or
design-sensitive than the confirmed defects above.

### A. Neo4j transport defaults are unsafe beyond localhost development

- Files: `src/menhir/config/settings.py`, `src/menhir/infrastructure/neo4j.py`
- The default password was fixed (now empty, not `password` — audit "Prior A"), but the URI stays
  loopback `bolt://` and driver creation does not force encrypted transport. Acceptable for local
  development; unsafe as a production default posture.

### B. Explorer app has no auth (localhost default reduces exposure)

- File: `src/menhir/explorer/app.py`
- Serves graph/telemetry reads and candidate mutations without auth middleware. Default host is
  `127.0.0.1`; rebinding or proxying exposes it. (Audit SEC-H01.)

### C. Telemetry history can grow without retention cleanup

- File: `src/menhir/infrastructure/telemetry/store.py`
- Append-only tables accumulate and caller-supplied read limits are unbounded; only revision
  pruning exists. Operational debt rather than a correctness break. (Audit SEC-R01 / CG-01.)

### D. Context-window retry logic relies on free-text marker matching

- File: `src/menhir/infrastructure/episode_lifecycle.py`
- Retry treatment is triggered by substring matching on error text rather than structured error
  classification. Brittle; no demonstrated misclassification. (Audit "Prior D".)

## Other open audit items not addressed here

- **MA-01/MA-02 (maintainability risk):** `RecallService.recall` (~509 lines) and the broad
  backend protocol/impl mirroring carry synchronization cost.
- **MA-03 (dark code):** `services/perception.py` + fold modules are an intentional bench-first
  prototype with no production call path — keep or wire deliberately, don't treat as dead.
- **FC-R01 (design risk):** perception Law-3 can treat every post-anchor mention as additive;
  optional final verification is default-off.
- **Governance/test-infra gaps:** no model/version governance record (AI-G01), no coverage artifact
  (TQ-03), no LICENSE/SBOM.

## Scope Notes

- Based on direct inspection of the current code plus the Phase 11 validated-findings ledger.
- Excludes stale findings from prior generated audits and items contradicted by the current suite.
</content>
</invoke>
