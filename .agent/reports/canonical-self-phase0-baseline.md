# Canonical-self remediation — Phase 0 baseline

Evidence record for Phase 0 of
`ctharvey/workspace-meta/.agent/plans/menhir-canonical-self-remediation-plan.md`.

- **Date:** 2026-09-03
- **Branch:** `fix/canonical-self-remediation`
- **Worktree:** `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.agent\worktrees\canonical-self-remediation`
- **Base commit:** `50b177357` (`main`, "chore: pin the interpreter at 3.12 so fresh worktrees match production"), which builds on `6ff184e9` (graphiti pin)
- **Plan revision implemented against:** workspace-meta `80753036`

## Baseline repository state

`main` was at `6ff184e9`, clean apart from an untracked `adtest/` directory unrelated to this
work. The plan's recorded base commit `55b53201` is one behind; the branch was cut from current
`main` as the plan's Phase 0 instructs, so the graphiti pin is included in the baseline.

`.agent/worktrees/` is not covered by the repository `.gitignore`. It was added to
`.git/info/exclude` in the main checkout rather than to the tracked ignore file, so the worktree
does not appear as untracked state and no tracked file changed to accommodate it.

## Environment parity with production

The worktree has its own `.venv`, created with `uv sync --frozen`. This matters: the accepted RCA
records that a venv-less worktree runs on the global interpreter, which is how a stale
`graphiti-core` 0.28.2 was mistaken for project state in an earlier revision. A worktree without
its own venv must not be used for this work.

Measured in the worktree venv:

| Check | Result |
|---|---|
| Python interpreter | `3.12.10` (matches `deploy/Dockerfile`'s digest-pinned `python:3.12-slim`) |
| `graphiti-core` installed | `0.29.3` |
| `pyproject.toml` specifier | `graphiti-core==0.29.3` |
| `uv.lock` specifier | `==0.29.3` |
| Exact-before-entropy resolver present | yes (`normalized_existing` in `_resolve_with_similarity`) |

### Interpreter parity is part of environment parity, not a given

The first attempt at this worktree got the interpreter wrong, and it produced a false baseline.
`uv sync --frozen` resolved **Python 3.14.3**, because `requires-python = ">=3.12"` was an open
lower bound and the repository carried no `.python-version`. Production and the existing project
venv are 3.12.

The failure was not obviously environmental. Three tests in `tests/test_hook_center_tool_events.py`
failed on assertions about path normalization, using entirely synthetic paths, which reads like a
logic bug. The actual cause: Python 3.13 changed `ntpath.isabs` so a drive-less rooted path such as
`/repo/src/foo.py` is no longer absolute on Windows. `menhir_file_event._structure_relative_path`
therefore skipped its normalization branch and returned the path unchanged.

Rebuilt with `uv sync --frozen --python 3.12`: the three failures disappear and the graphiti
fingerprint digests below are byte-identical across both interpreters — so the graphiti check alone
would **not** have caught this.

Fixed at the root in `main` `50b17735` by adding `.python-version` (`3.12`), verified against a
fresh `uv venv`. This is the same defect shape as the `graphiti-core>=0.29.2,<0.30` drift closed in
`6ff184e9`: an open lower bound resolving live while the deployed image is pinned.

**Carry into Phase 9.** The plan's rehearsal step requires "the same Menhir image, Graphiti wheel,
schema, and migration code" and does not name the interpreter. Add the Python version to the
rehearsal and cutover fingerprint set.

### Reusable artifact fingerprint

Re-run this to prove a checkout is executing the production dedup implementation rather than a
pre-0.29 package. The digests below are the Phase 0 baseline for `graphiti-core` 0.29.3.

```bash
.venv/Scripts/python.exe - <<'PY'
import hashlib, inspect
from graphiti_core.utils.maintenance import node_operations as no
for name in ("_resolve_with_similarity", "_collect_candidate_nodes", "_resolve_with_llm"):
    src = inspect.getsource(getattr(no, name))
    print(f"{name}: sha256={hashlib.sha256(src.encode()).hexdigest()[:16]} lines={len(src.splitlines())}")
print("exact-before-entropy present:", "normalized_existing" in inspect.getsource(no._resolve_with_similarity))
PY
```

| Symbol | sha256 (first 16) | Lines |
|---|---|---:|
| `_resolve_with_similarity` | `47c46c3e6558762d` | 60 |
| `_collect_candidate_nodes` | `3673c3e79bf734d5` | 9 |
| `_resolve_with_llm` | `a897e27036082b81` | 158 |

## Structure-graph unavailability

Menhir structure ingest is not set up on the relocated (VPS-hosted) memory server. Restoring it is
separate tracked work (`.agent/plans/menhir-remote-structure-scanning.md`, draft). Phase 0 does not
block on it; impact below is derived by direct repository search instead.

Two distinct facts, deliberately kept apart:

1. `query_structure(query_type="projects")` labels **all 60** ingested projects
   `[STALE: root_path missing]`. The server stat-checks recorded Windows paths from the VPS
   filesystem, so the banner describes the server's vantage point and carries no per-project
   signal. It is not evidence about the Menhir checkout.
2. The Menhir index is nevertheless genuinely behind the checkout — established independently of
   that banner. The four files added by the recent todo-lifecycle commits
   (`link_memory_to_todo.py`, `reopen_todo.py`, `resolve_todo.py`, `supersede_todo.py`) are absent
   from the indexed `src/menhir/mcp/tools/ops/` listing, which returns 35 files without them.

The previously cited extraction-patch blast radius of **163 files / 9 affected tests** is therefore
historical last-scan evidence only, retained for later comparison, and is not authoritative.

## Impact inventory (direct search)

Commands, run at the worktree root:

```bash
grep -rln "import .*\b<module>\b\|from .*\b<module>\b" --include=*.py src/ scripts/   # importers
grep -rln "import .*\b<module>\b\|from .*\b<module>\b" --include=*.py tests/          # covering tests
```

| File to be edited | Non-test importers | Test importers |
|---|---:|---:|
| `domain/namespace.py` | 12+ (workspace-wide; `episode_lifecycle`, `episode_stamping`, `candidate_repository`, `routes`, …) | 12+ incl. `test_cf76_default_namespace_single_source`, `test_cf150_normalize_namespace_single_source`, `test_namespace_isolation` |
| `infrastructure/graphiti_extraction_patches.py` | 2 (`graphiti_model_patches`, `graphiti_patches`) | 7 incl. `test_graphiti_combined_extraction_patch`, `test_graphiti_combined_extraction_closure`, `test_turn_evidence` |
| `infrastructure/graphiti_model_patches.py` | 1 (`graphiti_patches`) | 5 incl. `test_graphiti_adaptive_dedupe`, `test_graphiti_structural_isolation` |
| `infrastructure/graphiti_client.py` | 12 (lifecycle/recall/explorer/bootstrap) | 10 incl. `test_graphiti_client`, `test_cf177_client_reuse` |
| `services/enrichment_steps.py` | 6 (ingest intake/models/queue/service/worker, `scheduler_tasks`) | 7 incl. `test_services_pipeline`, `test_graphiti_combined_extraction_closure` |
| `infrastructure/episode_lifecycle.py` | 1 (`episode_repository`) | 6 incl. `test_episode_lifecycle`, `test_cf158_entity_writes_are_uuid_idempotent` |
| `services/project_ingest.py` | 2 (`backend_runtime_data_ops`, `mcp/tools/ingest/ingest_project`) | 2 |
| `services/recall_pipeline.py` | 1 (`recall_service`) | 4 incl. `test_recall_service`, `test_cf106_stale_anchor_tenancy` |

Note the shape: `namespace.py` and `graphiti_client.py` have wide fan-out, while the two files
carrying the actual identity defect (`episode_lifecycle.py`, `recall_pipeline.py`) each have a
single non-test importer. The risky edits are narrow; the shared primitives are not.

## Self-identity call-site reconciliation

The plan states its named inventories are a floor. Re-running the searches on the implementation
branch confirms them and adds one caller layer the plan does not name.

### Runtime self-UUID derivations — exactly three

```bash
grep -rn "menhir-self:" --include=*.py src/ scripts/ tests/
grep -rn "uuid5(" --include=*.py src/
```

| Location | Role |
|---|---|
| `infrastructure/episode_lifecycle.py:875` | writer (`ensure_self_entity`) |
| `services/recall_pipeline.py:835` | reader, first-person scalar authority |
| `services/recall_pipeline.py:1801` | reader, event-history authority |

Matches the plan's D2/Phase 1 target set exactly. The only other `uuid5` in `src/` is
`structure_queries.py:59`, which derives an inferred project id from a different namespace
constant and is correctly out of scope.

Test-side derivations to allowlist as documented vectors, not convert:
`test_episode_lifecycle.py:271`, `test_recall_event_authority_runtime.py:189` and `:242`,
`test_typed_scalar_self_binding.py:315`.

### `ensure_self_entity` callers — one more layer than the plan names

```bash
grep -rn "ensure_self_entity\|_absorb_self_entity_forks" --include=*.py src/ scripts/
```

| Location | Kind | Named in plan |
|---|---|---|
| `infrastructure/episode_lifecycle.py:856` | definition (calls absorber at `:893`) | yes |
| `infrastructure/memory_graph_adapter.py:598-601` | **adapter delegate — the surface every caller actually reaches** | **no** |
| `services/event_consolidation.py:84` | `Protocol` method declaration | implied |
| `services/event_consolidation.py:256` | caller | yes |
| `services/typed_scalar_service.py:686` | caller | yes |
| `scripts/replay_fold_flags.py:235` | operator script caller | yes |

**Addition for Phase 4:** `memory_graph_adapter.ensure_self_entity` is the seam all three callers
bind to (`adapter.ensure_self_entity(ns)`), and `event_consolidation.py:84` is a `Protocol`
declaring that method shape. Phase 4's ensure/detect/migrate split has to land on both — the
adapter method and the protocol — or callers will still type-check against the old single-method
contract. Neither is named in the plan's impacted-areas list.

## Production baseline counts (from the accepted RCA; no live query issued)

Carried forward without re-measurement, per the plan's instruction not to invoke recall or mutate
`last_accessed`:

- 15-candidate cosine cap per extracted entity; the `user` window is saturated at cosine 1.0,
  so `len(existing_matches) == 1` is arithmetically unreachable.
- 66 nodes sharing the exact string `user`; 70 named `user` carrying 1,670 edges, 4 flagged;
  71 user-like. The 66/70/71 inconsistency is what the Phase 7 census must replace.
- 52 non-structural entities lack `name_embedding` — invisible to cosine dedup acquisition.
- Production release `menhir-prod-0.2.0-8` at `5ca51acd`, which is not an ancestor of `main`;
  canonical-self code is identical in both.

## Baseline test suite

```bash
.venv/Scripts/python.exe -m pytest tests/ -m unit -q
```

Marker selection verified before the run: `-m unit` selects **5476 of 8835** collected tests
(3359 deselected). The `unit` marker is declared in `pytest.ini`, not `pyproject.toml`.

**Baseline result (Python 3.12.10, run serially):**

```
5466 passed, 10 skipped, 3359 deselected, 3 warnings in 299.49s (0:04:59)
```

Zero failures, zero errors. This is the reference the implementation must not regress.

The discarded 3.14.3 run is recorded here only so the false baseline is not mistaken for a real
one: `3 failed, 5463 passed, 10 skipped` — the three hook path-normalization failures described
above. It must not be cited as a baseline.

## Phase 0 exit gate

| Gate item | Status |
|---|---|
| Clean worktree | met — branch `fix/canonical-self-remediation` at `50b17735`, no tracked modifications |
| Baseline test result recorded | met — 5466 passed / 10 skipped / 0 failed |
| Per-file impact and covering tests by direct search, commands recorded | met — see inventory above |
| Structure-graph unavailability recorded as limitation, not blocker | met |
| Development Graphiti behavior matches deployed artifact | met — 0.29.3, resolver digests recorded |
| No production mutation | met — no live query issued; production counts carried from the accepted RCA |

## Findings carried into later phases

1. **`memory_graph_adapter.ensure_self_entity` (`:598-601`) and the `event_consolidation.py:84`
   `Protocol`** are a caller layer the plan's impacted-areas list does not name. Phase 4's
   ensure/detect/migrate split must land on both, or callers keep type-checking against the old
   single-method contract.
2. **Interpreter version belongs in the environment fingerprint.** Phase 9 names image, wheel,
   schema, and migration code, but not Python. The graphiti digests were identical across 3.12 and
   3.14 while behavior differed, so the graphiti check does not cover this.
3. **`_absorb_self_entity_forks` drops would-be self-loops** via its `m.uuid <> $self_uuid`
   predicates (`episode_lifecycle.py:896-1010`, ending in `DETACH DELETE f`). Already captured in
   plan D6/Phase 8; re-confirmed on this branch.
4. **Second wrong-partition site:** besides `ensure_self_entity` writing `n.group_id = $namespace`
   (`:882`), the fork-detection query at `:1034` uses the same equality. Both need the namespace
   SSOT, and only after the destructive path is gone.

---

# Phase 2 — producer inventory and evidence disposition

Required by the plan's Phase 2 first task. Derived by direct search on this branch:

```bash
grep -rn "queue_episode(" --include=*.py src/
grep -rn "queue_episode_for_enrichment(" --include=*.py src/
grep -rn "create_pending_episode" --include=*.py src/ scripts/
```

## The trust boundary already existed

Every producer funnels through `ingest_intake.queue_episode_for_enrichment`, which is the only
production writer of `create_pending_episode`. That function runs every `user`/`manual` claim
through `evaluate_user_tier_claim`, requiring Menhir-owned `TurnEvidence` with `role == "user"`,
a matching session/namespace, and text grounded in that turn. Ungrounded claims, and any error
evaluating one, are rewritten to `agent_inference` **before persistence**.

So the persisted `source` is a gate receipt, and the evidence survives the asynchronous queue with
no new field and no schema change.

**The trap.** `evaluate_user_tier_claim` also returns `granted=True` for the passthrough case
(`reason="passthrough (not user/manual)"`). Keying self-evidence on `verdict.granted` would make
every producer self-eligible, project scans included. The signal is the effective source after the
gate, never `granted`.

**The fragility.** This holds only while `create_pending_episode` has exactly one production
writer. `test_pending_episode_has_exactly_one_production_writer` pins it; if a second writer
appears, re-derive the evidence contract before relaxing that test.

## Disposition table

| Producer | Trusted role source | Self eligible? | Status |
|---|---|---|---|
| `add_memory` (`source='user'`/`'manual'`) | admission gate over `TurnEvidence.role` | **yes, on grant only** | `TRUSTED_USER_TURN` reconstructed from persisted source |
| `add_memory` (any other source) | none — caller string, ungated | no | `UNKNOWN`, no evidence |
| `add_memory` (`user` claim, gate denied) | gate downgraded to `agent_inference` pre-persistence | no | indistinguishable from agent source by design |
| `add_memory_and_track` | same gated intake | same as `add_memory` | inherits the gate |
| API `routes.py:366` (`body.source`) | same gated intake | same as `add_memory` | inherits the gate |
| legacy `ingest_episode` (`ingest_intake.py:355`) | same gated intake | same as `add_memory` | inherits the gate |
| `ingest_document` | writes `source="document-ingest"` | no | never self; also in the never-self set |
| `project_ingest` | writes `source="project-scan"` | no | never self; also in the never-self set |
| retry / repair / replay | re-reads the same persisted source | same as original | cannot strengthen; pinned by test |
| any producer supplying no context | none | no | fails closed — `self_identity=None` |

### Defect found by building this table

`_NEVER_SELF_SOURCE_KINDS` was first written with underscore spellings (`project_scan`) while
production writes hyphens (`project-scan`, `document-ingest`), which made that guard decorative.
Now pinned to the literal strings, compared with `-`/`_` folded, and covered by a test that reads
the producers' own source literals. The guard remains defense in depth: these sources never reach
`GATE_APPROVED_HUMAN_SOURCES`, so they already failed closed.

## Phase 2 status

Complete: evidence derivation, receipt field, parent-task propagation into extraction, producer
inventory, and the disposition of every producer.

**Not complete:** observe-only telemetry for self-like extractions lacking trusted evidence, and
the `off | observe | enforce` bind mode. Both are required before Phase 2's exit gate is met. The
bind mode is most naturally added with Phase 3's binding seam, since there is nothing to gate
until binding exists.

---

# Phases 1-4 — implementation record

| Phase | Commit | State |
|---|---|---|
| 1 identity primitives | `7410b34d` | complete |
| 2 evidence propagation | `e1bc6626`, `648c45d5` | complete except observe-telemetry |
| 3 deterministic binding | `d49c1dd9`, `d7a50e9f` | complete, ships `off` |
| 4 absorber neutralization | pending commit | complete |

## Decisions taken

**Self-alias sets are NOT consolidated (approved).** The three sets answer different questions
over different inputs, so merging them would widen admission in three subsystems:

| Set | Domain | Distinguishing tokens |
|---|---|---|
| `graphiti_extraction_patches` | extracted entity *names* | has `my`, `mine` |
| `typed_scalar_rules.SELF_TOKENS` | scalar proposal `subject_text` | omits `my`/`mine` (malformed as subjects) |
| `event_consolidation._SELF_TOKENS` | event proposal subject | adds `speaker` (only that producer emits it) |

Each definition now carries a comment naming its domain and forbidding the merge.

**Phase 4 scope extended (approved)** to `memory_graph_adapter.ensure_self_entity` -- the surface
every caller actually binds to -- and the `EventConsolidationGraph` Protocol they type-check
against. Neither is named in the plan; without both, callers keep the old single-method contract.

## Phase 4 changes

- `_absorb_self_entity_forks` **deleted**, not merely unreferenced. Its `DETACH DELETE` and the
  `m.uuid <> $self_uuid` predicates that dropped fork-to-canonical edges are gone from executable
  Cypher; a test parses the module's AST and fails if either returns to a query literal (prose
  mentions are allowed and deliberate).
- `detect_self_forks()` replaces it: read-only, returns uuids, mutates nothing.
- `ensure_self_entity` now reports `SELF_FORKS_REQUIRE_MIGRATION` and leaves forks untouched.
- **Mapping hazard fixed** (only after the destructive path was gone, per plan sequencing): the
  canonical write used `group_id = $namespace`, so logical `default` would have created the node
  in group `"default"` -- a partition holding none of the production data. Now uses
  `namespace_to_group_id()`.
- Detection reads **both** physical spellings (`""` and the logical name), because the old writer's
  forks live under the wrong one; a single-spelling read would report zero forks on exactly the
  population that has them.
- `scripts/replay_fold_flags.py` needs no change: it reaches only the adapter, and the absorber no
  longer exists, so its non-destructiveness is structural rather than conventional.

Tests cover 0, 1, 2, 15 and 70 forks (the production-shaped count) and assert the *absence* of
writes rather than the presence of a result.

## Open

- Phase 2 observe-only telemetry for self-like extractions lacking trusted evidence.
- Phase 5 observability (dedup branch counters).
- Phases 7-11 remain blocked on the census and restored-copy rehearsal, per the plan.
- `test_concurrent_enrichment_parallelizes_across_namespaces` asserts exact `peak == 3` and is
  load-sensitive: 2 failures across 31 runs on this tree, 0 across 12 on baseline, but 20/20 in
  isolation and the change adds 1.15us per episode. Pre-existing fragility, not a regression;
  worth loosening or marking `timing`.
