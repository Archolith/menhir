# RCA: A 500-file scan cap silently drops ~60% of menhir's source from the structure graph

**Date:** 2026-08-09
**Severity:** High. Not for data integrity — nothing is corrupted — but because it defeats the tool
the workspace conventions instruct every agent to run *before editing code*. `blast_radius` and
`affected_tests` return confident empty answers for the dropped majority, and cannot distinguish
"nothing depends on this" from "this file is not indexed." It produced a false safety signal for me
during this session (§5).
**Status: ROOT CAUSE CONFIRMED — arithmetic, then verified by prediction.** The graph holds
*exactly* 500 file-ish entities, matching `_MAX_KEY_FILES` to the unit.

## Summary

`ProjectScanner` caps every project at 500 file entities and, when over budget, sorts so that
**tests outrank source code**. menhir has 343 tests and 361 source files. Tests plus entrypoints
consume 349 of the 500 slots, leaving 151 for everything else — and only **82** of those 151 are
`src/menhir/**/*.py`, against 361 on disk (the `file` role also covers markdown, JSON, and `.py`
outside `src/`; see the correction in §2). The remainder is discarded silently — no warning, no
marker, no partial-index flag.

The cut falls at an arbitrary alphabetical boundary, so entire packages vanish while their tests
remain indexed.

## 1. The cap and its ordering

`infrastructure/project_scanner.py:119` and `:221`:

```python
_MAX_KEY_FILES = 500
...
file_entries = _cap_files(file_entries, _MAX_KEY_FILES)
```

`_cap_files` (`:829-843`):

```python
role_priority = {"entrypoint": 0, "test": 1, "config": 3, "file": 2 if is_code else 4}
depth = f.rel_path.count("/")
return (role_priority.get(f.role, 5), depth, f.rel_path)
...
return files[:limit]
```

Ordering is `(role_priority, depth, rel_path)`. Note `test = 1` and code `file = 2`: **tests are
retained in preference to the source they test.** Beyond that, survival is decided by path depth
and then alphabetical position — neither of which correlates with importance.

## 2. The arithmetic matches the graph exactly

`query_structure("overview", project="menhir")`, 2026-08-09:

| Entity type | Count |
|---|---|
| `test` | 343 |
| `file` | 151 |
| `entrypoint` | 6 |
| **total** | **500** |

`343 + 151 + 6 = 500`, precisely `_MAX_KEY_FILES`. The budget is not merely near its limit; it is
saturated to the unit.

On disk: **361** `.py` files under `src/menhir/`, **325** under `tests/`.

**Correction (2026-08-09, same day):** the first version of this RCA read the 151 `file` entities as
151 indexed source files and reported "58% of the source tree is absent." That was wrong and
understated the damage. The `file` role also covers markdown, JSON, and `.py` outside `src/`.
Instrumenting the scanner to count `src/menhir/**/*.py` specifically gives **82** indexed of 361 on
disk — **77% of the source tree is absent**, not 58%. The 500-entity saturation in the table above
is unaffected.

This surfaced while verifying an adversarial review's counter-numbers; the review did not raise it.

## 3. Verified by prediction

The model predicts survival for depth-2 root modules and then depth-3 packages in alphabetical
order until the budget runs out, plus entrypoints regardless of position. Checked against the live
graph:

| Query | Predicted | Observed |
|---|---|---|
| `src/menhir/domain` | partial, cut mid-package | **10 files**, `__init__.py` … `edges.py` — of 57 on disk |
| `src/menhir/mcp` | dropped except entrypoints | **1 file**: `mcp/server.py` `[entrypoint]` — of 68 on disk |
| `src/menhir/infrastructure` | dropped | "No files found" — of 71 on disk |
| `src/menhir/services` | dropped | "No files found" — of 74 on disk |

The cut lands inside `domain/` at `edges.py`. Everything alphabetically later at that depth —
the rest of `domain/`, plus `explorer/`, `infrastructure/`, `mcp/`, `pipeline/`, `services/` — is
gone. `mcp/server.py` survives only because `entrypoint` outranks everything.

This also explains the packages that *did* index (`api/`, `cli/`, `config/`, `core/`, and the root
modules): they sort before `edges.py`.

## 4. Why the TESTS edges collapsed

`_detect_test_edges` (`:645-665`) builds a basename index **from indexed source files** and maps
`test_foo.py → foo.py` through it:

```python
source_files = {f.rel_path: f for f in files if f.role not in ("test", "config")}
```

When the source file was dropped by the cap, the lookup fails and no edge is written.

**Two different numbers appear for this, and both are real.** The live graph reports `TESTS: 36`
(`query_structure("overview")`, 2026-08-09) — that is persisted state from an earlier scan, against
a slightly different file set. Running the current scanner in-process on the same tree the same day
produces **32**. The graph figure is historical; 32 is what today's code yields. Neither is a
miscount; they are observations of different things, and the gap is itself mild evidence that the
persisted index is stale relative to the working tree.

Either way: **~32-36 TESTS edges for 343 indexed tests.**

The failure compounds. Tests are preferentially *kept*, so they occupy the budget that would have
held the source they point at, and then cannot be linked to it. A project is penalised for being
well tested: the more tests it has, the less of its own source is indexed, and the more of its test
mappings break.

## 5. Observed harm

Before editing `project_scanner.py` in this session I ran the workspace-mandated check:

```
query_structure("blast_radius", project="menhir", path="src/menhir/infrastructure/project_scanner.py")
→ Changed files (1) ... Affected tests: none found ... Total impact: 1 files, 0 tests
```

Both claims are false. `tests/test_project_scanner.py` exists and contains 37 tests importing
`ProjectScanner`, `_classify_file_role`, `_detect_test_edges`, `_first_paragraph`, `_parse_imports`,
and `FileEntry` from that exact module. The file itself was not in the graph, so the query had no
basis for either answer and reported absence rather than ignorance.

I caught it only because the test filename was conventional enough to guess. An agent following
`.agent/README.md`'s instruction to prefer `query_structure` over Grep — and trusting the result —
would have edited a 1,100-line module believing nothing covered it.

## 6. Root cause

**A fixed global file cap with a priority order that ranks tests above source, applied silently.**

Three independent design choices combine:

1. **The cap is absolute, not proportional.** 500 is generous for a small repo and far too small
   for menhir; nothing scales it or reports when it binds.
2. **The tie-break is lexical, not semantic.** `depth` then `rel_path` decides which code survives,
   so package name spelling determines indexing. `services/` loses to `api/` for no reason but the
   letter s.
3. **Truncation is silent.** `_cap_files` returns `files[:limit]` with no log, no warning, no
   `partial_index` marker on the project node, and no signal in any downstream query response.

Contributing:

4. **Consumers report absence as fact.** `blast_radius` / `affected_tests` say "none found" where
   the truthful answer is "not indexed." `.agent/README.md` warns humans to treat these as possibly
   un-ingested, but the tool itself does not distinguish the cases.
5. **No coverage metric.** Nothing compares indexed file count against files on disk, so a
   77%-missing source index looks identical to a complete one.

## 7. What is *not* the cause

- **Not *merely* a stale scan.** The cap is applied on every scan, so re-scanning does not recover
  the dropped files — that is the defect here. Staleness is a real but separate contributor: the
  persisted graph reports 36 TESTS edges where today's scanner yields 32 (§4), and the fingerprint
  is paths + mtimes only (`project_scanner.py:192-198`), so a rules change does not trigger a
  rewrite at all. Fixing the cap therefore does not propagate on its own.
- **Not `.gitignore` or `_ALWAYS_SKIP_DIRS`.** Those exclude `__pycache__`-style paths; the dropped
  files are ordinary source in tracked packages.
- **Not `_MAX_FILE_BYTES`.** That 2 MB guard skips symbol extraction for huge files; these files are
  small and absent entirely, not merely symbol-less.
- **Not specific to menhir**, though menhir is the worst case in the workspace. Any project past
  500 files is silently truncated by the same rule.

## Remediation

See `.agent/plans/menhir-structure-graph-coverage-2026-08-09.md`.
