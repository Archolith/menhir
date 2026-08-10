---
description: Local runbook and verification steps
---

## Environment Setup

From the repository root:

1. Install dependencies

```bash
python -m pip install -r requirements.txt
```

2. Ensure env is present

```bash
copy .env.example .env
```

3. Neo4j connectivity

Neo4j is a remote systemd service (`menhir-neo4j.service` on a remote host), not local Docker. Verify connectivity via `smoke_test.py` or the main entry point (see below).

## Temp Directory Handling

Use the project script when possible:

```bash
.\scripts\menhir.ps1 <task>
```

It now forces `TEMP`, `TMP`, and `TMPDIR` to `.agent/test_tmp` and sets a pytest `--basetemp`
under `.agent/test_tmp/pytest`, which avoids failures on systems where global temp paths are locked
or unavailable.

## Local Verification

### Quick connectivity check (recommended first)

```bash
python smoke_test.py
```

### Unit tests

```bash
pytest -m unit
```

### Full tests

Excludes live `online` tests unless explicitly enabled:

```bash
pytest
```

### Fast/background full run (large suite, ~2800 tests)

The full unit suite takes minutes serially, and has a known history of an
unresolved single-test hang (see the 2026-07-11 SSOT review: `pytest -m unit`
made no progress after 68 tests and hit 120s/300s timeouts twice).

`pytest-timeout` is configured in `pytest.ini` (`timeout = 60`,
`timeout_method = thread` — works on Windows, unlike the signal method) so any
single hanging test fails after 60s instead of stalling the whole run.

**`pytest-xdist` is a dev dependency, but do not run `-n auto` on this
machine.** A live attempt (2026-07-11) spawned one worker per core (20 on this
box); the workers OOM-crashed partway through the run (`node down: Not
properly terminated`) and thrashed the machine's RAM badly enough that the run
had to be killed. `-n auto` is unproven and NOT recommended here until
someone deliberately re-tests with a small, capped worker count (e.g. `-n 4`)
and confirms memory stays bounded — do not casually retry `-n auto` "to see if
it was a fluke."

Until that capped-worker number is proven safe, prefer:

```bash
pytest tests/ -m unit -q
```

serial, timeout-guarded, and safe. If parallelizing later, start low
(`-n 2`, `-n 4`) and watch memory before increasing, and always launch
full-suite runs in the background rather than blocking a session on them.

### Online pytest suite

Requires explicit opt-in:

```bash
pytest --run-online -m online
```

### Full extraction/integration run

Requires live Neo4j and LLM endpoint:

```bash
python integration_test.py
```

## Running the Service Entry Point

```bash
# Performs dependency checks for both Neo4j and llama.cpp
python -m menhir.main
```

Expected behavior:

- loads `.env`
- validates Neo4j connectivity
- validates the configured Graphiti extraction backend and Graphiti embed backend separately
- for hybrid mode, this means direct OpenAI extraction can be checked independently from the local OpenAI-compatible embed endpoint
- exits `0` when both are healthy, otherwise exits `1`

