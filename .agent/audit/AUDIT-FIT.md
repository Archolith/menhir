# Audit Type Fit for Menhir

Assessment of every audit type in the workspace library against Menhir's actual
stack (Python 3.12, asyncio, Neo4j, FastAPI/FastMCP, SQLite sidecars,
single-operator LAN deployment) and against what its prerequisites require.

Verified at commit `eebf6d6dd83f15083167bf847b639d24b953fdc9`.

## Summary

| Audit type | Fit | Used | Probe support | Note |
|---|---|---|---|---|
| functional-correctness | good | 11 modules | `--type a1` | highest yield so far |
| security | good | M2 | `--type a2` | |
| architecture | good | M3 | `--type a3` | external pass @ 7aa977e1 |
| maintainability | good | M2, M10 | core checks | |
| performance | **good** | none | `--type a5` + a2 | JVM version superseded by workspace `.agent/audit/performance-audit-python-asyncio.md`; a5 plugin verified running 2026-08-14 |
| test-coverage | **good** | none | none | coverage.xml regenerated 2026-08-13: line-rate 0.8439, 369 classes. Verified 2026-08-14 |
| llm-ai | good | M6 | none (judgement) | |
| compliance | good | none | none | all artifacts present, fully actionable |
| architecture-sql-storage | **N/A** | - | - | JVM + Postgres only |
| spring-best-practices | **N/A** | - | - | JVM only |
| ai-code-quality-guardrails | good | none | overlaps core | large overlap with maintainability |
| upstream-pr-review | good | none | - | relevant for Graphiti PRs |
| archolith-session-grade | N/A | - | - | grades sessions, not code |

## Where the library does not fit, and why

### performance-audit - RESOLVED 2026-08-14

**The rewrite described below was done.** Workspace `.agent/audit/performance-audit-python-asyncio.md`
replaces the JVM version. Its Lanes A-D cover every concern the table further down marks
"uncovered": sequential awaits and N+1 (Lane B), unbounded result sets and unbounded in-memory
structures including caches without eviction (Lane C), repeated per-request work (Lane D). It
also splits evidence into STRUCTURAL vs MEASURED, so a lane with no profiler still produces
real findings instead of inventing numbers; Lane E is the optional measured lane.
The original analysis is kept below - it is still the correct statement of what Menhir's
performance questions actually are.

### Original analysis - why the library version did not fit

Its Lane A asks for "top 10 hottest code paths by CPU profile (flame graph)",
Lane B for "heap profile", "GC pause analysis (mode, frequency, duration)", and
"boxing/unboxing overhead". Those are JVM concepts. Menhir has no profiler
configured -- `grep -cE "pytest-benchmark|py-spy|cProfile|scalene" pyproject.toml`
returns 0.

Running it as written would produce either a JVM-shaped report about a Python
service, or a lane inventing measurements it cannot take.

**What Menhir's performance questions actually are**, and where they are covered:

| Real concern | Coverage |
|---|---|
| Synchronous I/O on the asyncio event loop | probe `--type a2`, direct and one hop |
| Sequential Cypher calls where one query would do | **uncovered** |
| Unbounded result sets from graph queries | **uncovered** |
| Unbounded in-memory structures (rate limiters, caches, queues) | **uncovered** |
| Embedding cache behavior and dimension churn | **uncovered** |
| Per-request work on the auth path | probe `--type a2` |

Two confirmed findings already came from this space (a ~501-sequential-call
handler, and blocking SQLite on the auth path), so the concerns are real even
though the audit's lane structure does not address them.

**Recommendation:** do not run performance-audit as written. Either add a
Python/asyncio variant to the workspace library, or fold the uncovered rows into
the compound prompt's A5 section, which already describes them in stack-correct
terms.

### test-coverage-audit - partial, and currently misleading

Two blockers:

1. **The coverage data is stale.** `coverage.xml` and `.coverage` are dated
   2026-07-10; HEAD is 2026-08-13. Auditing against a 34-day-old snapshot would
   attribute coverage to code that has changed. Regenerate before running.
2. **Lane B requires CI history.** It asks for "every test that [flaked] from CI
   history (last 30 days)". The repo has one workflow (`.github/workflows/tests.yml`)
   and that history is not reachable from a local audit lane.

Lane A (coverage gaps) and Lane C (assertion quality) are both viable once
coverage is regenerated. Lane C is arguably the highest-value lane in the whole
library for this codebase: the suite passes at `410 passed, 16 skipped` while 27
findings sit in the tree, and `grep -ic "succeeded\|antonym\|contradict"
tests/domain/test_admission_gate.py` returns **0** -- no test ever fed the
admission gate a contradiction pair.

**Recommendation:** regenerate coverage, run Lanes A and C, skip Lane B with the
reason stated.

### architecture-sql-storage-audit and spring-best-practices-audit - not applicable

Both are explicitly JVM + Postgres. Menhir is Python + Neo4j. The workspace
README already scopes them that way.

## The gap the library does not cover at all

There is an `architecture-sql-storage-audit` for JVM+Postgres services and
nothing equivalent for **Python + graph database**. Menhir's storage-layer risks
have no home in the current library:

- **Cypher correctness** -- parameter binding versus string interpolation,
  `MERGE` versus `CREATE` semantics, missing `WHERE` on destructive operations,
  unbounded matches. `DETACH DELETE` on a degree-zero predicate is a confirmed
  finding here.
- **Transaction boundaries** -- whether a multi-statement graph mutation is
  atomic, and what a partial failure leaves behind.
- **Namespace isolation** -- the multi-tenancy boundary. Every recall and write
  path is namespace-scoped; a leak is a cross-tenant disclosure.
- **Graphiti soft-fork drift** -- four patch modules (~2,800 lines) monkey-patch
  a pinned dependency. A confirmed finding is a patch with no fallback whose
  guard cannot cover its own deferred import.
- **SQLite sidecar discipline** -- 15 modules use SQLite for operational state.
  WAL mode, busy_timeout, connection-per-call versus shared handles, and whether
  any sidecar has drifted into holding semantic data.

These have been audited ad hoc inside A1 correctness lanes, which is why they
were found, but nothing guarantees the next lane looks for them.

**Recommendation:** add `graph-storage-audit` to the workspace library, shaped
like the SQL-storage variant but for Cypher/Neo4j, and reference it from the
Menhir compound prompt.

## Coverage status by module

`--type a1` has been run against all 11 modules. Everything else is sparse:

| | A1 | A2 | A3 | A4 | A5 | A6 | A7 |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| coverage | 11/11 | 1/11 | 0/11 | 2/11 | 0/11 | 0/11 | 1/11 |

Compliance (whole-project) has never been run despite every artifact being
present: `sbom.json`, `LICENSE`, `NOTICE`, `THIRD-PARTY-LICENSES.txt`. It is the
cheapest untouched audit with a fully actionable Lane A.

## Recommended next passes, in order

1. **Compliance** -- whole project, all inputs present, never run, cheap.
2. **A3 architecture** -- never run on any module; probe support now exists.
   Start with M9 infrastructure (`mcp.tools.base` is imported by 54 modules,
   the largest coupling point in the codebase).
3. **A6 test-coverage Lane C** on M1 domain -- assertion quality, after
   regenerating coverage. The admission-gate finding shows the suite asserts
   mechanics rather than properties.
4. **A5** only after a Python-shaped variant exists, or via the compound
   prompt's stack-correct A5 section.
