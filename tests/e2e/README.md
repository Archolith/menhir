# Black-box stdio E2E campaign

Harness for the local-stdio MVP release plan's **Phase C** (pre-freeze E2Es) and
**Phase F** (the same eight lanes rerun on the frozen RC).

Authority: `.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md` §6 owns the
acceptance criteria. Tracking roll-up: issue #123. This directory implements those
criteria; it does not redefine them.

## Status

| Lane | Subject | State |
| --- | --- | --- |
| E2E-1 | cold install, `initialize`, `tools/list`, schemas, shutdown | **implemented** |
| E2E-2 | memory lifecycle (carries #118's remaining acceptance) | scaffolded |
| E2E-3 | integrated coding workflow | scaffolded, fixture repo builds |
| E2E-4 | WorkArtifact lifecycle | scaffolded, needs fixture corpus |
| E2E-5 | TODO lifecycle | scaffolded |
| E2E-6 | Beacon generation and consumption | scaffolded, policy pinned |
| E2E-7 | restart and interrupted work | scaffolded |
| E2E-8 | isolation and adversarial (carries #88's regression pin) | scaffolded |

A scaffolded lane is **not** a silent skip. `_harness/pending.py` writes a full evidence
directory recording every acceptance criterion as unproven, then skips — so
`result.json` reads `status: PENDING, criteria_passed: 0` with each criterion named.
The plan says a skip is not a pass; the evidence tree says so too.

## Running

```bash
# Nothing runs without this. The campaign builds a wheel and a venv.
MENHIR_E2E=1 pytest tests/e2e -v

# One lane
MENHIR_E2E=1 pytest tests/e2e/test_e2e_01_cold_install.py -v
```

Prerequisites: Docker, a disposable Neo4j, and `build` installed. Each is checked, and a
missing one produces a skip that names it rather than a confusing failure.

### Opt-in is an env var, not `--run-e2e`

The repo convention for expensive lanes is a CLI flag (`--run-online`,
`--run-remote-sim`). pytest only honours `pytest_addoption` in the *initial* conftest, so
a `--run-e2e` flag has to be declared in `tests/conftest.py`. That is a two-line change
and the right end state — it is deliberately not made here so this scaffold touches no
existing file.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MENHIR_E2E` | unset | `1` enables the campaign |
| `MENHIR_E2E_STRICT` | unset | `1` aborts on a dirty tree — **required for the RC run** |
| `MENHIR_E2E_NEO4J_URI` | `bolt://127.0.0.1:7689` | Disposable graph |
| `MENHIR_E2E_BACKEND_PORT` | `8199` | Loopback `menhir serve` |
| `MENHIR_E2E_WORK_ROOT` | pytest tmp dir | Venv, fixtures, evidence |
| `MENHIR_E2E_BEACON_PYTHON` | unset | Interpreter with Beacon 0.1.0, for E2E-6 |

Port `7689` is deliberately **not** `7688`: that is the unit suite's instance, and an E2E
lane resetting it mid-run would corrupt a parallel `pytest --run-online` into failures
that look like product defects. Port `8100` is refused outright — it is the documented
default for the operator's own `menhir serve`.

## Feature matrix

Any registered feature can be toggled on or off in any combination, and the active
combination is recorded in every lane's evidence.

```bash
# A named preset
MENHIR_E2E=1 MENHIR_E2E_FEATURE_PRESET=all_off pytest tests/e2e

# Explicit overrides, applied on top of the preset
MENHIR_E2E=1 MENHIR_E2E_FEATURES="explorer=on,structure_watcher=off" pytest tests/e2e

# Full powerset: every lane runs 2**n times
MENHIR_E2E=1 MENHIR_E2E_FEATURE_POWERSET="frontier_bm25,frontier_fact_edges,scalar_state" \
  pytest tests/e2e
```

Presets: `mvp_default` (shipping defaults, the configuration the release actually
claims), `all_off`, `all_on`, `mvp_surface_only`, `research_on`.

Three design points worth knowing before extending this:

1. **The registry is validated against `settings_model.py` at session start.**
   `validate_registry` fails the session if a declared flag names a variable the settings
   model no longer reads. Without that check a renamed flag would leave the campaign
   exporting a dead variable and reporting it had tested the feature disabled.
2. **The combo reaches both child processes.** `feature_env` is one fixture consumed by
   the backend *and* the stdio bridge. They are separate processes with separate settings
   resolution; applying the combo to one would report a configuration that was never
   fully in effect.
3. **The backend starts per lane, not per session.** Flags are read at startup, so a
   session-scoped backend could not honour a per-combo matrix — it would report results
   for whichever combination started first.

Powerset sweeps above 8 flags are refused. Eight is already 256 runs of every lane.

## The production-graph fence

`tests/conftest.py` forces the *pytest process* onto the test graph. That guard does not
reach this harness, because every process here is a child running an installed Menhir,
and `menhir.env_file.resolve_env_file` falls back to `./.env` relative to the child's
cwd. A child launched with an inherited environment inside the checkout would load the
developer's real `.env` and connect to production, with the parent's guard reporting
nothing.

So the fence is re-established at the process boundary, in `_harness/config.py`:

- children get an **allow-listed** environment, never `os.environ`;
- `ENV_FILE` points at a harness-written file, and cwd is the harness state directory,
  so neither env-file discovery path can reach the checkout;
- `assert_not_production` refuses the recorded prod URI, the ambient `NEO4J_URI`, and
  loopback `:7687` — compared by host:port with scheme dropped, so swapping `bolt://`
  for `neo4j://` or `localhost` for `127.0.0.1` does not slip past it;
- the check runs again inside `reset_graph`, because that is the function that destroys
  data, and again after a lane's `extra` env is applied.

`PYTHONPATH` is scrubbed for a related reason: `pytest.ini` sets `pythonpath = src`, and
an inherited `PYTHONPATH` would put the checkout ahead of the installed wheel, quietly
turning E2E-1's "non-editable install" into a source-tree run. `install_into_venv`
additionally asserts the venv imports `menhir` from `site-packages` and not from the
checkout.

## Evidence

One directory per lane per combination per run:

```
evidence/<run_id>/<lane>[<combo>]/
    manifest.json     commit, tree cleanliness, wheel hash, versions, feature combo
    transcript.jsonl  every MCP request and response, in order
    backend.log       `menhir serve` stdout+stderr
    stdio.log         the bridge's stderr
    result.json       verdict plus per-criterion outcomes
```

`result.json` records each acceptance criterion by its plan identifier, because "the lane
passed" is not what Gate C asks for — it asks which checklist items were proven. A
criterion recorded twice keeps the worse outcome, so a lane cannot overwrite a failure it
already saw by re-asserting more loosely after a retry.

Responses are never truncated in the transcript. A shortened response is not evidence of
what the server returned.

## Known gaps

- **Lanes 2–8 are scaffolds.** Criteria are enumerated; assertions are not written.
- **E2E-2 needs a provider decision.** Enrichment needs a real LLM/embedder to reach
  READY. `child_environment` withholds provider credentials by default so an accidental
  live call fails loudly instead of spending budget. The lane must either receive them
  deliberately or assert only the states reachable without one — and record which.
- **E2E-4 needs a committed fixture artifact corpus.**
- **E2E-6 needs a Beacon interpreter** (`MENHIR_E2E_BEACON_PYTHON`).
- **E2E-3 may be reading the wrong path.** The snapshot read path landing in the working
  tree makes a published canonical view win over local structure for the same project.
  The lane carries a `no_published_snapshot_view_in_effect` criterion for exactly this,
  and it must be asserted, not assumed.
