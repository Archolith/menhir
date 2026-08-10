# Structure Graph Coverage

Status: **planned; implementation not started**

Revision history:
- v1 — led with raising `_MAX_KEY_FILES`.
- v2 — re-sequenced after measurement; led with non-code exclusion.
- v3 — rewritten after adversarial review round 1. The staged sequence in v2 was unsafe: its
  phases are not independently shippable, and shipping the priority change alone **regresses**
  TESTS edges. Reviewer findings verified and adopted; see "What the review changed".
- v4 — adversarial review round 2. Three fixes: coverage counts moved into Phase 1
  (v3's Phase 1 depended on fields Phase 3 created); the deny-list stated exactly and re-measured
  at **843 eligible**, superseding v3's 819, which belonged to the rejected allowlist; and
  `is_code` removed from the priority order, where it had reintroduced `_CODE_EXTENSIONS` and
  would have ranked SQL/Vue/Svelte/C#/Ruby/PHP below tests.
- **v5 (current)** — adversarial review round 3. Deny-list rewritten as an ordered precedence:
  caches and *root-level-only* artifact dirs, then preserved manifests/configs, then documentation
  and binary extensions. Fixes three implementation blockers — `CLAUDE.md` lost to the `.md` rule,
  `uv.lock` lost to `.lock`, `requirements*.txt` lost to `.txt` despite being parsed for
  dependencies, and generic dir names pruning nested source packages. Re-measured: **845 eligible**.

RCA: `.agent/reviews/rca-structure-graph-file-cap-2026-08-09.md`

## Why

`_MAX_KEY_FILES = 500` truncates every scan, `_cap_files` ranks tests above source, and the
truncation is silent. menhir's graph holds exactly 500 file entities — 343 tests, 6 entrypoints,
151 `file` — while only **82 of 361** `src/menhir/**/*.py` are indexed. `blast_radius` and
`affected_tests` therefore answer "none found" for most of the codebase.

The cap is defensible; answering a safety query from a truncated index without saying so is not.

**Origin:** original to `29a7ad7` (2026-03-21), the first `ingest_project` commit. No rationale in
the commit body, no comment on the constant, no design doc — while `_MAX_FILE_BYTES` on the next
line does carry one. The name `_MAX_KEY_FILES` hints at an intent to index a curated set, but that
is inference from naming, not recorded intent.

## Measured (2026-08-09)

`ProjectScanner` run in-process against menhir, read-only. Single runs on one machine — the ratios
are structural, the wall-clock figures are unreplicated.

| Configuration | files | TESTS edges | `src/menhir/**/*.py` |
|---|---|---|---|
| current ordering, cap 500 | 500 | 32 | **82** / 361 |
| v2's priority change alone, cap 500 | 500 | **22** | 352 / 361 |
| priority change + cap 2000 | 1,196 | **136** | 361 / 361 |

Scan time 3.42s → 7.74s uncapped. Symbols 8,198 → 11,093 (1.35x). Imports 792 → 2,013. Directories
unchanged at 77.

Composition of the uncapped 1,196: 778 match `_CODE_EXTENSIONS`; 418 do not — 312 markdown (238 in
`.agent/`, 70 in `docs/`), 27 json, 22 html, 20 `.ruff_cache`.

**The middle row is the finding that forced this rewrite.** Raising source above tests while the
cap still binds starves the *test* side of `_detect_test_edges`, which needs both endpoints. Source
coverage improves (82 → 352) but edges fall (32 → 22). v2 presented that change as independently
shippable defence-in-depth; it is a regression.

## What the review changed

All six findings verified against the scanner and the code; all adopted.

1. **Phases are not independently shippable.** Filtering, priority, and the cap must land together
   (above). v2's "Phase 2 puts the project under the cap" was simply wrong — 778 > 500.
2. **"Indexed" does not make a negative answer safe.** An indexed file's importers or tests may
   themselves have been dropped.
3. **`_CODE_EXTENSIONS` is the wrong eligibility predicate** — an allowlist that discards
   structurally meaningful files and omits whole languages.
4. **Coverage needs three counts**, not one ratio, or intentional exclusions leave every project
   permanently "partial".
5. **Existing graphs will not self-heal** — the fingerprint is paths + mtimes only.
6. **The lexical tie-break survives** the priority change, contradicting v2's own stated stance.

## Design stance

1. **Honesty before completeness.** Phase 1 remains independently shippable and ships first; it
   removes the false safety signal even if nothing else lands.
2. **Never starve either side of a TESTS edge.** Both endpoints must be indexed for the edge to
   exist, so any priority scheme that trades one for the other is wrong while the cap binds.
3. **Eligibility is a deny-list, not an allowlist.** Exclude known documentation and binary
   classes; retain everything else.
4. **Make the cap stop binding rather than removing it.** A runaway guard is still worth having.
5. **When the cap does bind, survival ordering is arbitrary — and we say so.** v2 claimed "survival
   must not be decided by spelling" while leaving the depth-then-alphabetical tie-break in place.
   Rather than build package-balanced selection for a case that should now be rare, this plan drops
   that claim: if the cap binds you are in a pathological repo, and what you need is the loud signal
   from Phase 1, not a cleverer ordering. The contradiction is resolved by abandoning the
   requirement, not by pretending it is met.

## Scope

In scope: consumer-side honesty, eligibility policy, priority order, cap value, coverage metadata
and its plumbing, and the rollout mechanism.

Out of scope: symbol-level caps (`_SYMBOL_PER_FILE_CAP`, `_MAX_CALLS_PER_SYMBOL`); package-balanced
selection (see stance 5).

## Phase 1 — coverage counts + honest answers (independently shippable, ships first)

Phase 1 owns the counts as well as the semantics: the honest-answer rule below is expressed in
terms of `partial_index` and a coverage ratio, so those fields must exist before it can work, and
Phase 2's acceptance also asserts `partial_index == false`. v2 deferred them to a later phase,
making Phase 1 unimplementable on its own.

**1a. Three counts and the flag.** Record on the scan result and persist to the project node:

- `files_discovered` — walked, after gitignore and `_ALWAYS_SKIP_DIRS`
- `files_eligible` — survived the deny-list (Phase 2a; before Phase 2 lands this equals
  `files_discovered`)
- `files_indexed` — actually written
- `partial_index = files_indexed < files_eligible`
- optionally source-only counts, since that is the number `blast_radius` depends on

One ratio cannot express this: `82/361` is source-tree coverage while `500/1196` is discovered-file
coverage, and after intentional exclusions "indexed vs everything on disk" would mark every project
permanently partial.

Plumb through every boundary that drops unknown fields: `ProjectScanResult`, the JSON
reconstruction boundary at `core/backend_shared.py:98`, and project-node persistence.

**1b. Honest answers.** Three-tier rule, applied while `partial_index` is true:

| Case | Response |
|---|---|
| Requested path not in the index | **"cannot answer — not indexed"**. Never a zero/empty result. |
| Path indexed, query is completeness-sensitive | Return known results, explicitly marked incomplete with the coverage ratio. |
| Any completeness-sensitive query | **No unqualified negative assertion.** |

This applies beyond the four queries v2 named: `imports`, `tests`, `context`, `endpoints`, and
cross-project references are all completeness-sensitive. The formatter emits
`"Affected tests: none found"` at `mcp/tools/recall/query_structure.py:318`; that string is exactly
the unqualified negative this phase forbids.

Also: `_cap_files` logs a warning naming the project, discovered/eligible/indexed counts, and the
first dropped path.

Acceptance:

- All three counts survive a round trip through the JSON boundary and land on the project node.
- `blast_radius` on `src/menhir/infrastructure/project_scanner.py` reports not-indexed, not `0 tests`.
- No completeness-sensitive query returns a bare negative while `partial_index` is true.
- A test asserts an un-indexed path never yields a zero-impact answer.

## Phase 2 — atomic change: eligibility + priority + cap

**These land in one change.** Any subset is a regression or a no-op (see Measured).

**2a. Eligibility as a deny-list.** Replace the proposed `_CODE_EXTENSIONS` allowlist with an
exclusion policy. Must **retain**: `src/menhir/explorer/templates/*.html`,
`src/menhir/explorer/static/*.css`, `deploy/*.sh`, `scripts/*.ps1`, and classified configs — all
structurally meaningful. Must not omit languages absent from `_CODE_EXTENSIONS` (SQL, Vue, Svelte,
C#, Ruby, PHP).

The exclusions must be stated exactly, and **as an ordered precedence** — a flat extension list
silently drops structural manifests. Apply in this order; the first match wins:

**Step 1 — skip caches and root-level artifact directories.**

- Prune at any depth (unambiguous): `.ruff_cache`, `htmlcov`
- Prune **only at the repository root**: `results`, `logs`, `coverage`

The root-only restriction matters. `_ALWAYS_SKIP_DIRS` is matched by directory *basename* during
the walk (`project_scanner.py:174`), so adding generic names there prunes every directory with that
name at any depth — which would silently delete a legitimate `src/<pkg>/logs/` or
`src/<pkg>/results/` package. menhir happens to have none today, so this costs nothing here and
protects every other project.

**Step 2 — preserve known structural manifests and configs.** These win over Step 3 regardless of
extension:

- Anything the scanner already classified `config` or `entrypoint` — this is what keeps
  `CLAUDE.md`, which is in `_CONFIG_NAMES` and would otherwise be lost to the `.md` rule
- `requirements*.txt`, `constraints*.txt` — **load-bearing**: `_parse_dependencies` reads
  `requirements.txt` line by line at `project_scanner.py:514-518`, so excluding it via `.txt` would
  break dependency detection outright
- Lockfiles: `*.lock`, `package-lock.json`

**Step 3 — exclude documentation and binaries by extension.**

- Documentation: `.md`, `.rst`, `.txt`, `.log`
- Binary/artifact: `.png .jpg .jpeg .gif .ico .pdf .zip .gz .tar .whl .so .dll .dylib .pyc .pyo
  .bin .db .sqlite .sqlite3 .pma`
- Note `.lock` is **not** here — Step 2 owns lockfiles.

**Everything else is eligible**, including `.html .css .sh .ps1 .sql .vue .svelte .cs .rb .php`,
config extensions, and extensionless files (`Dockerfile`, `Makefile`, `Procfile`).

**Measured against menhir (2026-08-09):** 1,196 discovered → **845 eligible**; 351 excluded — 311
documentation, 20 cache, 18 root-level artifact, 2 binary. Preserved by Step 2: 24 classified
config/entrypoint files plus 1 manifest (`uv.lock`). Verified retained: `CLAUDE.md`, `uv.lock`,
`pyproject.toml`, 21 files under `explorer/templates`, 6 under `explorer/static`, 6 under
`deploy/`, 7 under `scripts/`.

menhir has no `requirements.txt` or `constraints.txt` (it uses `pyproject.toml` + `uv.lock`), so
that preserve rule is correct but unexercised here; it matters for other workspace projects.

**Implementation note (2026-08-09):** as built, step 1 prunes during the directory walk, so
cache/artifact files never reach `files_discovered`. The shipped counts are therefore
**1,158 discovered → 845 eligible → 845 indexed**, not `1,196 → 845`. Both are correct against
their own definitions — the plan's `files_discovered` is specified as "after gitignore and
`_ALWAYS_SKIP_DIRS`", and step-1 dirs now live in that set — but it means `discovered - eligible`
(313) counts only documentation and binaries. The 38 cache/artifact files are invisible to the
coverage counts by construction. If step-1 exclusions ever need to be auditable, they require a
fourth counter; they are deliberately not one today.

Supersedes two earlier figures: **819** (v3) was measured under the *rejected* allowlist and
undercounted what the deny-list retains; **843** (v4) applied extension rules as a flat list with no
precedence, and so lost `CLAUDE.md` to the `.md` rule and `uv.lock` to a `.lock` rule that has now
been removed.

**2b. Priority order.** The deny-list has already removed documentation and binaries, so every
surviving file is structural. Rank all of them ahead of tests, with **no `is_code` term**:

```python
role_priority = {"entrypoint": 0, "file": 1, "config": 2, "test": 3}
```

v2's version used `1 if is_code else 4`, which reintroduced `_CODE_EXTENSIONS` through the back
door and would have ranked SQL, Vue, Svelte, C#, Ruby, and PHP source *below* tests — contradicting
"source at least equal to tests". Dropping the term removes the second allowlist dependency
entirely.

Stated plainly, because it is in tension with stance 2: when the cap binds, this ordering drops
tests and TESTS edges degrade. That is still strictly better than today's inverse, which retains
tests while dropping their targets and yields unlinkable orphans. Per stance 5, the answer to a
binding cap is Phase 1's signal, not a cleverer ordering.

**2c. Cap.** Raise `_MAX_KEY_FILES` to **2,000** — comfortably above 845 eligible, so it stops
binding for menhir while remaining a runaway guard. Do not remove it.

Acceptance (all must hold together):

- All 361 `src/menhir/**/*.py` indexed.
- TESTS edges ≥ 136; `test_project_scanner.py → project_scanner.py` exists.
- `explorer/templates/*.html`, `explorer/static/*.css`, `deploy/*.sh`, `scripts/*.ps1`, and configs
  present.
- No `.md` under `.agent/`, no `.ruff_cache`.
- `partial_index` false for menhir.

**Check before implementing:** confirm `ingest_document`'s `structure_role: "document"` entities do
not overlap the excluded markdown, and that `file_context` recall does not read doc files from the
code graph.

## Phase 3 — coverage reporting and standing monitoring

The counts themselves now land in Phase 1, which needs them. This phase surfaces them.

- Report `files_discovered` / `files_eligible` / `files_indexed` and `partial_index` in `overview`
  and `list_projects`.
- Optionally warn during the watcher's periodic re-scan when a project flips to `partial_index`,
  so truncation is noticed when it starts rather than months later.

Acceptance: a truncated project is visible from a single `overview` call; `partial_index` is false
for a fully-indexed project despite intentional exclusions.

## Phase 4 — rollout (required; the change does not propagate on its own)

The scan fingerprint is built from **paths and mtimes only** (`project_scanner.py:192-198`), and
both manual ingest and the watcher skip on an unchanged fingerprint
(`core/backend_runtime_data_ops.py:364-368`). Changing cap, filter, or priority rules therefore does
**not** trigger a rewrite — existing graphs keep their truncated state indefinitely.

Two options; do at least one, prefer both:

- **Fingerprint includes a scanner schema version.** Bump it whenever eligibility, priority, or cap
  semantics change, so every project re-scans automatically. This is the durable fix.
- **The queued 31-project batch runs `force=True`.** Already required for the encoding fix, which
  likewise does not alter fingerprints.

Acceptance: after landing Phase 2, a project scanned under the old rules re-indexes without manual
intervention (schema version), or the rollout batch is recorded as having used `force=True`.

## Risks

- **Phase 2 is a single larger change** with no safe partial landing. Verify against the scanner
  in-process before writing to the graph, as the measurements here were.
- **The deny-list could drop something load-bearing.** It is the main behavioural change; enumerate
  what it excludes for menhir and eyeball the list rather than trusting the predicate.
- **Phase 1 will turn many current answers into "not indexed"** — correct, but reads as a
  regression to anyone who trusted the old output.
- **Workspace-wide scan cost**: ~4s extra per project per 30-minute watcher cycle, across ~31
  projects. Single-run measurement; confirm aggregate duty cycle.
- **Graph churn on re-scan**: entity sets shift, so anything cached against them must be
  invalidated deliberately.

Retired by measurement: symbol/edge explosion (1.35x, not proportional) and scan latency as a
blocker (7.74s uncapped, before eligibility filtering brings it down).

## Follow-ups (not this plan)

- `_SYMBOL_PER_FILE_CAP` / `_MAX_CALLS_PER_SYMBOL` — same silent-degradation family.
- `affected_tests` falling back to filename convention when no TESTS edge exists, as defence in
  depth against index gaps.
- Which other workspace projects exceed the cap today. Phase 3's counts make this answerable in one
  call.
