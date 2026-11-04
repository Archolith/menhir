---
artifact_schema: 1
artifact_uuid: c7180b0c-e862-41d1-93fe-3b996a4123fa
artifact_type: plan
artifact_status: PROPOSED
---

# Try Menhir without external services: #461 → #109 → #258

Baseline: `63d707e7`, checked 2026-11-04. Planning and package inspection only;
no backend compatibility spike has run and no storage/default change has shipped.

## Why and scope

[#461](https://github.com/Archolith/menhir/issues/461) owns the first useful recall
without installing services or configuring a model. The owner's
[first deliverable](https://github.com/Archolith/menhir/issues/461#issuecomment-6594624066)
is a comparison before running a spike. This branch starts that work at the user's
request; it does not reorder the other onboarding issues named in that comment.

[#109](https://github.com/Archolith/menhir/issues/109) owns the embedded storage
candidate and its transaction go/no-go. [#258](https://github.com/Archolith/menhir/issues/258)
owns the Graphiti fork's backend-specific expiry guard. Treat #258 as a prerequisite
for correct FalkorDB operation, not proof that #109 or #461 is otherwise complete.

## One-page option comparison

These are candidate designs, not available first-run modes. Costs below describe
who pays; no runtime, latency, quality, or dollar measurements have been made.

| Candidate | User setup and cost | What it cannot do by itself | Windows support |
|---|---|---|---|
| Embedded FalkorDBLite (#109) | Python package plus a managed local Redis/FalkorDB process and persistent DB file. Local CPU/RAM/disk cost; extraction and embedding still need configured models and may incur provider charges. | Removes separate graph-service setup only. Does not supply a model, demonstrate full Menhir query compatibility, or prove transaction/retry correctness. | Current upstream documents WSL. PyPI 0.11.0 publishes Linux/macOS wheels, no native Windows wheel. Does not currently satisfy native Windows first-run requirements. |
| Bundled small local model, or clearly labelled offline canned demo | A real local model costs download size, disk, RAM and inference time; no per-call API bill. A canned demo has fixture/package and local execution cost. Both still need storage (embedded or an explicitly limited demo store). | A small model needs extraction/embedding quality and hardware validation. A canned demo can show only fixed examples: it cannot claim arbitrary-memory extraction, live enrichment or production recall quality. | Native inference packaging and minimum hardware are unverified. Static canned fixtures could avoid model runtime dependencies; that alone does not solve the graph backend's Windows gap. |
| Hosted disposable sandbox | No local DB/model install. Operator pays hosting and model usage; user needs internet and an access flow. | Cannot offer offline/local-only use. Needs namespace isolation, expiry/cleanup, usage limits and an export or discard contract; it does not implement local-file storage. | A compatible HTTP/MCP client avoids native DB requirements. The client connection journey still needs testing on Windows. |

Recommendation: continue evaluating embedded storage as the local-file direction,
but do not call it the complete #461 answer. First resolve native Windows feasibility;
WSL is a separate deployment choice, not evidence of native support. A canned demo
is a possible independent first experience; a hosted sandbox is an alternative if
the product chooses online use. Neither should become a second production storage
implementation just to make the demo work.

Evidence: [FalkorDBLite README](https://github.com/FalkorDB/falkordblite#building-from-source),
[PyPI 0.11.0 metadata](https://pypi.org/pypi/falkordblite/0.11.0/json), and
[#109's updated requirements](https://github.com/Archolith/menhir/issues/109#issuecomment-6594624292).
Metadata inspected on 2026-11-04: Python >=3.12, CPython 3.12–3.14 wheels for
Linux x86_64/aarch64 (`manylinux_2_38`) and macOS; source distribution also present.
Absence of a Windows wheel and WSL documentation do not prove a native port impossible.

## Proposed design and execution order

1. **Platform feasibility.** Identify a supported native Windows runtime/build path
   and redistribution obligations. Record exact package/native-module versions,
   license notices, supported OS/architectures and Linux libc baseline. Current
   published FalkorDBLite packaging is a blocker to claiming native Windows support.
2. **#109 transaction spike, after the comparison above.** Use disposable local files
   and the pinned `archolith-graphiti-core==0.31.2.post2` from `pyproject.toml`.
   Inventory `Neo4jRepository.execute_write` callers, especially projection lifecycle
   reads/writes. Prove equivalent commit/rollback and ownership fencing, or express
   each critical mutation in one atomic backend query. Do not equate a Redis pipeline
   with rollback, or retry a write whose commit outcome is ambiguous.
3. **#258 regression on the pinned fork.** First reproduce whether single and bulk
   FalkorDB saves can revive an expired edge. Test old/new/absent expiries and a
   concurrent stale-save interleaving. Implement any required query guard in
   `Archolith/graphiti`, with its own tests and release; consume the tested fork
   release in Menhir. Do not hide this fork fix inside an unrelated Menhir adapter.
4. **Adapter only if viable.** Introduce the narrow graph repository boundary around
   existing direct access and select its matching Graphiti driver in the existing
   composition root. Keep backend-specific schema/query variants explicit. Wire
   backend selection, local path ownership, startup/shutdown and readiness through
   canonical settings/runtime contracts. Preserve the existing Neo4j deployment path.
5. **First-run integration.** Prove fresh-install bootstrap, ingest/enrichment,
   entity/edge persistence, vector and full-text recall, scoped direct reads/writes,
   uniqueness/idempotency, projection lifecycle, saga/retry safety, process restart,
   backup/export and shutdown timing. Address model-free onboarding separately under
   #461 and state its limitations before choosing any new default.

Out of scope for this first increment: production migration, deployment, publishing
native runtime wheels, changing defaults, or claiming completion of any linked issue.

## Risks and invariants

`src/menhir/infrastructure/graphiti_client.py` still constructs `Neo4jDriver`;
`src/menhir/infrastructure/neo4j.py` supplies explicit transactional callbacks.
Projection lifecycle repositories call `execute_write` in both read and write
modules. Recheck all callers before designing an interface: wrapping query strings
alone cannot preserve this behavior.

Inspection of the installed pinned fork also confirms two unresolved seams:
`FalkorDriverSession.execute_write` directly awaits its callback, and its `run`
dispatches lists as separate queries; it supplies no begin/commit/rollback boundary.
Both FalkorDB entity-edge save query generators still perform full property
replacement without `_NEO4J_KEEP_STORED_EXPIRY`. This confirms #258's query gap is
present in the current pin. These are source/query-generation findings, not a live
rollback or stale-save reproduction and not proof that atomic rewrites are impossible.

Namespace isolation, authority/provenance, uniqueness, earliest stored expiry,
ownership fences, rollback and ambiguous-commit behavior must survive backend
selection. Multi-project and temporal queries must retain their current scope and
ordering. Do not emulate a successful unsupported operation. Stop the candidate
if it needs correctness compromises or a disproportionate architecture rewrite.

## Validation and docs to update

For this documentation increment: artifact metadata/index validation, local link
and baseline-anchor checks, and `git diff --check`. Pytest is unnecessary because
no runtime behavior or executable examples change. The structural project exists,
but its paths are stale and the documentation files are unindexed; actual checkout
inspection supplies the baseline, not an empty structural impact result.

Initial validation: `menhir artifacts validate . --repository menhir` reports 246
records and 19 pre-existing findings. The untouched baseline reports 245 records
and the identical 19 findings; the new plan introduces none. Full corpus validation
is therefore not clean, and unrelated artifact repairs are outside this increment.

For implementation: focused repository/Graphiti contract regressions and disposable
backend integration tests covering the acceptance cases above, including Windows
and Linux install/restart lanes. Required exact-SHA CI owns full integration;
record unrun platform/model lanes rather than inferring success.

Update architecture/data-model ownership, settings examples, `docs/post-install.md`,
the first-run usability guide and startup runbooks when behavior actually changes.
Keep the plan index and changelog current. The owning issues remain #461 (overall
experience), #109 (storage viability/integration) and #258 (fork expiry semantics).
