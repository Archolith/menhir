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
| E2E-2 | memory lifecycle (carries #118's remaining acceptance) | **implemented** |
| E2E-3 | integrated coding workflow | **implemented** |
| E2E-4 | WorkArtifact lifecycle | **implemented** |
| E2E-5 | TODO lifecycle | **implemented** |
| E2E-6 | Beacon generation and consumption | scaffolded, policy pinned |
| E2E-7 | restart and interrupted work | **implemented** |
| E2E-8 | isolation and adversarial (carries #88's regression pin) | **7 of 8 criteria** |

E2E-8 is four tests rather than one. A lane declares a single provider, and its criteria
need opposite ones: the #88 isolation pin needs a provider that succeeds (entities must be
written before they can land in the wrong silo), while "provider failure does not silently
pass" needs one that reliably fails. Its one remaining criterion is the adversarial variant of E2E-6's happy path, and is
declared pending against that prerequisite.

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

### Per release

This is the suite to run against a release candidate. The RC run differs from a development
run in one way that matters:

```bash
MENHIR_E2E=1 MENHIR_E2E_STRICT=1 pytest tests/e2e -v
```

`MENHIR_E2E_STRICT=1` aborts on a dirty working tree. Without it the campaign will build a
wheel from uncommitted changes and record a commit hash that does not describe what was
tested -- which makes the evidence worse than useless, because it still looks authoritative.

Afterwards, read `result.json` rather than the pytest summary. The gate asks which
acceptance criteria were proven, and a green run with pending lanes is not the same answer
as a green run without them:

```bash
cat evidence/<run_id>/*/result.json   | jq -r '.lane + ": " + .status + " — " + (.criteria_unproven | join(", "))'
```

No lane currently asks for a live model, so a full campaign run spends nothing and needs no
API key.

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
| `MENHIR_E2E_BEACON_PYTHON` | unset | Interpreter whose Beacon supports `build`+`validate` (the SHA pinned in `.github/workflows/tests.yml`; PyPI 0.1.0 does not), for E2E-6 |

Port `7689` is deliberately **not** `7688`: that is the unit suite's instance, and an E2E
lane resetting it mid-run would corrupt a parallel `pytest --run-online` into failures
that look like product defects. Port `8100` is refused outright — it is the documented
default for the operator's own `menhir serve`.

## Providers

Enrichment lanes need a model to reach READY. Rather than a real one, the campaign
carries two fake OpenAI-compatible servers in `_harness/providers.py`, ported from
`mvp-118-stdio-e2e` where they were built and proved for #118:

```python
@pytest.mark.provider("deterministic")   # answers every Graphiti extraction schema
@pytest.mark.provider("failing")         # refuses every completion, for E2E-8
@pytest.mark.provider("real")            # a live model, skips when no key is configured
```

The default is **no provider**: a lane that does not declare one gets no credentials and
no endpoint, so an unintended live call fails loudly instead of quietly spending budget.

The deterministic handler is why E2E-2 can assert on real enrichment output — READY
status, recall by wording and paraphrase, correction currentness — rather than only the
states reachable without a model. `failing` exists because E2E-8 requires that provider
failure does not silently pass, and that is only testable against something that
reliably fails.

## The operator CLI

E2E-4 is the one lane that drives both surfaces. `audit_artifact_corpus` is read-only by
design -- its own docstring sends the caller to `menhir artifacts reconcile --apply` to
write anything -- so a lane asserting only the MCP half would leave the registration path,
the half that writes, untested. `_harness/stack.py:run_menhir_cli` runs the installed
`menhir` under the same allow-listed environment and state directory as every other child,
so the CLI cannot reach a graph the campaign did not choose either.

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

- **E2E-6 needs a Beacon interpreter** (`MENHIR_E2E_BEACON_PYTHON`) that satisfies the
  build contract: install the Beacon SHA pinned in `.github/workflows/tests.yml` into a
  separate venv (the `stdio-e2e` CI job installs none, so this lane stays pending there).
  The overwrite and CAS refresh policy is already pinned in the lane's docstring, so what is
  missing is the second venv, not the decision. The graph-backed E2E-6 in
  `tests/test_beacon_e2e6.py` (online job) already runs the full flow against that pin.
- **No lane has been run end to end.** Every lane collects, skips cleanly without the
  opt-in, and asserts against tool signatures and output formats read from the source.
  That is not the same as having passed. Expect the first real run to surface format
  mismatches; treat an early failure as the harness finding its footing rather than as a
  product defect, until the transcript says otherwise.
