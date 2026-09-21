---
description: Risk-based local verification, CI ownership, and test reporting
---

# Run and test

## Policy

Every contributor or agent finishing a change must run the smallest test set that credibly covers
the affected behavior, broaden it when risk or failures indicate wider impact, and report exactly
what ran. The complete repository suite is an integration/release control run by CI on the reviewed
commit, not a routine after-every-edit local command.

Local targeted testing and full CI are complementary:

- local tests provide fast, change-specific evidence and make failures diagnosable;
- CI runs every offline test plus the supported graph-backed lane on one immutable SHA;
- neither may be reported as evidence for the other.

## Default workflow

### 1. Identify the changed surfaces

- List changed and newly added files.
- For each changed source file, use
  `query_structure(query_type="affected_tests", project="menhir", path="...")` when the structural
  index is available.
- Treat that result as a starting point, not an authority. The index can be stale, capped, or lack a
  test edge. Empty output never means "no tests needed."
- Confirm the selection from imports, callers, public contracts, existing test names, and the
  behavior changed. Include tests for every changed API/MCP/storage/deployment boundary.
- Add or modify a direct regression test when behavior changes or a bug is fixed.

### 2. Run the narrowest credible tests during implementation

Prefer explicit files and node ids:

```bash
python -m pytest tests/test_changed_behavior.py -q
python -m pytest tests/test_changed_behavior.py::TestCase::test_regression -q
```

Do not substitute `pytest -m unit` for selection. Many valid offline tests are intentionally
unmarked, so the unit marker is useful for a broad local lane but is not the complete offline suite.

### 3. Broaden once the narrow tests pass

At closeout, run the union of:

- direct tests for changed behavior;
- affected neighboring tests for callers and contracts;
- a subsystem-level selection when shared behavior changed; and
- matching static checks for changed Python files.

Example static checks, scoped to changed paths:

```bash
ruff check --select F811,F821,ASYNC src/menhir/changed.py tests/test_changed.py
```

The repository has deliberately scoped lint gates and historical baseline findings. Do not turn an
unrelated whole-repository cleanup into part of an ordinary change.

### 4. Push the reviewed commit and use CI for integration

`.github/workflows/tests.yml` owns the full integration check:

- selective Ruff correctness gates;
- the complete offline suite with no marker filter; and
- graph-backed online tests against a disposable Neo4j service, excluding tests requiring an LLM.

Required CI must be green on the exact SHA being merged or released. A later local edit invalidates
an earlier CI result. PyPI publication already enforces this exact-SHA test gate.

## Risk tiers

| Change | Minimum local verification | When to broaden |
|---|---|---|
| Documentation/comments only | Link/path/example validation relevant to the document; artifact validation when its workflow requires it | Run code tests only when the document drives generation, executable examples, packaging, or a runtime contract |
| Pure local logic | New/direct unit tests plus affected callers | Add the containing subsystem when a public helper, parser, normalization rule, or shared model changes |
| API, MCP, CLI, provider, or backend contract | Direct contract tests, auth/error cases, and at least one adjacent round-trip test | Add all implementations/consumers of the changed contract |
| Graph query, schema, identity, lifecycle, or destructive write | Focused unit tests plus disposable-Neo4j online tests for positive, refusal, absent/legacy, and rollback paths | Add neighboring writers/readers and fresh-schema/bootstrap coverage |
| Auth, tenancy, secrets, permissions, or release controls | Focused positive and negative security tests; exercise the final side-effect boundary | Add every alternate protocol/writer and exact deployment/build validation |
| Scheduler, concurrency, leases, retries, or timing | Direct deterministic tests and stale/interleaving cases; timing-marked tests serially | Add worker/recovery subsystem tests and relevant disposable-service tests |
| Shared fixtures, `conftest.py`, collection hooks, markers, `pytest.ini`, test dependencies, or test runner scripts | Collection check plus representative tests from every affected class | Full offline CI is mandatory; a justified local full run may also be useful |
| Packaging, migrations, broad refactor, or release candidate | Focused behavior tests plus build/install/migration smoke | Required full offline and graph-backed CI on the exact candidate SHA |

## Escalation rules

Broaden beyond the initially selected tests when any of these occurs:

- a public signature, serialized shape, schema, shared fixture, or environment default changes;
- a test fails outside the directly edited behavior;
- the change affects multiple writers/readers or has destructive, security, concurrency, or
  migration consequences;
- the affected-test mapping is empty, stale, or inconsistent with source inspection;
- a previously passing focused test becomes order-dependent or flaky; or
- the diff grows beyond the scope used to choose the original tests.

After fixing a failure, rerun the failure, the original focused set, and one reasonable neighboring
ring. Do not repeatedly launch the entire suite to discover the next failure.

## When a local full run is justified

A complete local offline run is exceptional, not forbidden. State the reason before starting it.
Reasonable cases include:

- changing test collection, markers, global fixtures, test isolation, or runner configuration;
- changing a foundational utility with no reliable impact boundary;
- reproducing a CI-only interaction after focused attempts failed; or
- preparing a release when required CI is unavailable and the release process explicitly accepts
  local evidence.

Run it serially and timeout-guarded:

```bash
python -m pytest -v
```

Do not use `-n auto`. A previous 20-worker run exhausted memory and crashed workers. Timing tests
must remain serial. If a long run is warranted, use a background/CI job so an interactive agent is
not blocked waiting on routine integration evidence.

## Online and live-service tests

Online tests are opt-in and may run destructive, unscoped graph queries. Use only a disposable test
database—never an operator or production graph.

```bash
python -m pytest --run-online -m online -q
python -m pytest --run-online -m "online and not needs_llm" -q
```

Tests marked `timing` must run serially. Tests requiring an LLM need the documented test endpoint
and must not be inferred green from the Neo4j-only CI lane.

## Environment and temp handling

From the repository root, install development dependencies with:

```bash
python -m pip install -e . --group dev
```

The dependency-group form requires pip 25.1 or newer. This repository does not use a
`requirements.txt` file.

On Windows, prefer the project wrapper when it supports the task:

```powershell
.\scripts\menhir.ps1 <task>
```

It routes temporary files under `.agent/test_tmp`. `pytest.ini` also disables the cache provider and
sets a 60-second per-test timeout so one hang cannot stall a run indefinitely.

## Failure and unavailable-environment handling

- Do not rerun a flaky or hanging test until it passes and call that proof. Isolate it, record the
  attempts, and report the instability.
- If an external dependency is unavailable, run the strongest offline substitute and say which
  online behavior remains unverified.
- A skipped test is not evidence that its dependency-backed behavior passed.
- Never claim "tests pass" without naming the command and outcome.

## Verification receipt

Every implementation closeout must report:

```text
Changed surfaces: <modules/contracts/data paths>
Selection basis: <direct tests + affected_tests + caller/contract inspection>
Commands run: <exact commands and pass/fail/skip totals>
Not run: <full offline / online / LLM / platform lanes and why>
CI status: <exact SHA and required checks, or pending/not pushed>
Residual risk: <untested boundary, flaky test, unavailable dependency, or none known>
```

For documentation-only work, explicitly say why pytest was unnecessary and report the document or
artifact checks that replaced it.

## Service checks

`python smoke_test.py` checks configured connectivity. `python -m menhir.main` performs dependency
checks for Neo4j plus the configured Graphiti extraction and embedding backends. These are operator
or integration checks, not substitutes for focused regression tests.
