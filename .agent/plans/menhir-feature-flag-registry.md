# menhir -- FeatureFlag registry: one declarative inventory, machine-enforced

Status: **PROPOSED. No code written.**

Date: 2026-09-11. Derived from the 2026-09-11 full feature-flag scan (44 boolean settings +
modes + ~20 env vars read outside `MemorySettings`, cross-referenced against `.env` and
`.env.example`), which was performed entirely by hand and surfaced:

- **B6** (`MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED`): existed, default-off,
  documented nowhere. Fixed manually 2026-09-11 (`e8a3f002`) -- but nothing prevents the next one.
- **~12 env vars** read by code but absent from `.env.example`
  (`MENHIR_INGEST_ALLOWED_ROOTS`, `MENHIR_ALLOW_INSECURE_BACKEND_URL`,
  `MENHIR_HOST_PID_NAMESPACE_VERIFIABLE`, `MENHIR_GRAPHITI_DISABLE_REASONING`,
  `MENHIR_FRONTIER_TRACE`, `MENHIR_FRONTIER_ORACLE_SUBSET`, `MENHIR_RECALL_COMPACT`,
  `MENHIR_LOG_LEVEL`, `MENHIR_LOG_DIR`, `MENHIR_ALLOW_SYSTEM_PYTHON`,
  `MENHIR_BENCH_RESULTS_ROOT`/`_ACTIVE_RUN_ID`, `MENHIR_TELEMETRY_BUSY_TIMEOUT_S`).
- **The personal-memory runtime block** (`SCALAR_STATE_ENABLED`, `SCALAR_HISTORY_ENABLED`,
  `EVENT_HISTORY_ENABLED`, `RECALL_AUDIT_ENABLED`, `SHADOW_CONTEXT_COMPOSITION`,
  `VERIFIER_SYNC_ENABLED`, `EXPERIENCE_COUNTER_ENABLED`, `STARTUP_SCOPE`,
  `SAGA_RECONCILE_STARTUP_MODE`, `CLIENT_TOKENS_ENABLED`, scheduler intervals) -- likewise
  invisible in `.env.example`.
- Interaction knowledge that lives only in comment prose and can rot invisibly:
  `frontier_belief_gate` REQUIRES `frontier_warden_gate`; the scalar reconcile flags require a
  `perceiver_version` bump when flipped on an existing namespace.

Owner-approved direction (2026-09-11): a registry of feature-flag descriptor objects plus a
test suite that keeps the registry, `MemorySettings`, and `.env.example` consistent in both
directions -- so a feature cannot be added silently, removed silently, or lost to documentation
rot.

## Problem

Menhir's feature surface is large (44 boolean settings, mode/enum settings, ~20 additional env
vars read directly) and spread across three artifacts that must agree:

1. `MemorySettings` (code truth: defaults, parsing, validation)
2. `.env.example` (human catalog)
3. the deployment `.env` (operator's overrides)

Nothing connects them. A flag can be added to the code and never reach `.env.example` (B6 --
real, happened, cost a sweep to discover). A flag can be consulted by code that no longer
implements the feature. Interaction rules (requires / version-bump) live in prose only. The
2026-09-11 scan took hours of manual cross-referencing and is already stale the moment a PR
merges.

The codebase already established the right pattern twice:

- `ViewKind` / `view_write_repository.KINDS` -- "add a memory type by adding a ViewKind here,
  nothing else."
- `ToolScope` / `assert_tool_scopes_declared` -- a tool with no declared scope refuses to
  START: the failure mode for a new tool is a loud startup error, not a silently unscoped tool.

This plan applies the same idea to feature flags. The mechanism closest in spirit to
CF-127's ratchet is the *enforcement*; but where CF-127 must regex-scan Cypher because no
registry exists, here we create the registry and get exact enforcement instead of a census.

## Design

### The descriptor object

Registry, not inheritance: flags have no behavior to share, only metadata. A frozen dataclass.

`src/menhir/config/feature_flags.py` (new):

```python
@dataclass(frozen=True)
class FeatureFlag:
    setting: str                    # MemorySettings field name, or "" for env-only vars
    env_var: str                    # canonical env var; aliases recorded in env_var_aliases
    default: bool | str | int | float
    category: str                   # "neo4j" | "llm" | "scheduler" | "scalar" | "frontier"
                                    # | "auth" | "oauth" | "telemetry" | "saga" | "misc"
    description: str                # ONE line; long prose stays in .env.example / settings_model
    env_var_aliases: tuple[str, ...] = ()     # deprecated spellings still honored by _getenv
    requires: tuple[str, ...] = ()           # settings this flag needs to be effective
    recall_affecting: bool = False           # MUST be stated, ToolScope-style, no silent default
    version_bump: bool = False               # flipping requires a perceiver/cursor version bump
    documented: bool = True                  # False only for deliberate exemptions (see T4)

FEATURES: dict[str, FeatureFlag] = { ... }
```

Keying: by `setting` name where one exists, by env var for env-only entries. One entry per
feature, `MemorySettings` untouched.

### Why metadata lives in the registry and prose does not

- `description` is the one-liner a `config show` table prints. The load-bearing prose -- CF-234's
  measured percentiles, the threshold `2/3`-not-`0.67` trap, the production-compose section --
  stays in `.env.example` and `settings_model.py` where it already reads well. Generation would
  flatten exactly the context that makes a flag understandable; the registry enforces PRESENCE,
  humans keep MEANING.
- `MemorySettings` stays exactly as-is. No rewrite of `from_env`'s per-field parsers, no drift
  risk in the parsers themselves. The registry is metadata ABOUT settings, and test T1 below
  kills the dual-source-of-truth risk on defaults.
- `RETIRED: dict[str, str]` (flag name -> "date: reason") records removals, matching the
  repo's provenance culture, at one line per retired flag.

### Scoping: progressive, ratchet-style

Full inventory is ~130 entries. Phase 1 registers the surfaces that actually bit:

- all boolean `MemorySettings` fields (44)
- mode/enum settings (`canonical_self_binding_mode`, `startup_scope`, `runtime_mode`,
  `artifact_reconcile_mode`, `saga_reconcile_startup_mode`, `frontier_fact_edge_mode`,
  `frontier_similarity_scale`, `frontier_fusion_admission_policy`)
- all env vars read OUTSIDE `settings_model.py` (the ~20 found by the scan)

Numeric tunings (intervals, budgets, ks, TTLs, ports) and secrets are Phase 2 -- they are
configuration, not features, but they belong in the same catalog eventually. A shrink-only
`EXEMPT` baseline (CF-127 style) covers anything intentionally out of scope so the registry can
land without the full 130 in one sitting; entries migrate out of `EXEMPT` as they are added.

## The tests (the actual deliverable)

`tests/test_feature_flag_registry.py` (new). Every test is exact, not heuristic:

- **T1 -- bijection with the settings model.** Every bool + mode field in
  `dataclasses.fields(MemorySettings)` maps 1:1 to a registry entry; every registry entry with a
  `setting` maps to a real field; the recorded `default` equals the dataclass field default.
  Catches: added-without-entry (the B6 class), removed-without-cleanup, entry typos, and default
  drift between registry and model.
- **T2 -- env-only var census.** Scan `src/menhir/**/*.py` for `os.getenv` / `os.environ`
  reads of `MENHIR_*` constants and literals (the simple cousin of CF-127's census -- no Cypher
  clause-tracking needed). Every hit must be a registry entry (env-only or alias); every
  registry entry with no `setting` must still be read somewhere. Catches: env vars appearing
  outside the model without documentation, and registry entries for vars that no longer exist.
- **T3 -- documentation presence.** Every `documented=True` entry's `env_var` (and each alias)
  appears in `.env.example` -- active or commented, either counts. B6 becomes structurally
  impossible rather than sweep-discoverable.
- **T4 -- exemption ratchet.** `EXEMPT: dict[str, str]` (name -> reason) for entries
  deliberately undocumented (expected to be empty or near-empty). A test may only shrink it.
  Same discipline as CF-127's `BASELINE`.
- **T5 -- liveness.** Every entry whose `setting` is bool: the field is actually consulted
  somewhere in `src/` beyond `settings_model.py` itself (a `getattr(settings, ...)` /
  `settings.<name>` read). A removed feature whose flag nobody consults fails here instead of
  rotting silently -- the "don't lose features" property from the consumption side.
- **T6 -- interaction honesty.** For each `requires` pair, assert the code path that consumes
  the flag also checks the requirement (initially: spot-assert the two known pairs --
  `frontier_belief_gate` requires `frontier_warden_gate` at
  `services/assertion_pipeline.py`'s construction; `frontier_evidence_anchor` applies only
  under `frontier_warden_gate` per its own comment -- plus a vacuity check that at least the
  known pairs are present, mirroring CF-127's `test_the_detector_is_not_vacuous`).
- **T7 -- retired registry.** `RETIRED` keys must NOT appear as live settings or live env reads;
  entries must carry a date and reason. Removal archaeology with teeth.

T5 needs a consumption scanner; a `getattr(settings, ...)`/`settings.<field>` grep over
`src/menhir` with a per-flag `consumed_in` allowlist for flags read dynamically (if any) is
sufficient and matches the CF-127 over-count bias.

## Runtime surface (optional Phase 3)

`menhir config show` CLI subcommand: joins `FEATURES` with `MemorySettings.from_env()` and
prints `flag / default / effective / source (default|env) / category / interactions`, secrets
blanked by name pattern (`key|secret|password|token`). No HTTP surface, no auth design, no
redaction review. Answers "what's on THIS box" -- the question the 2026-09-11 hand-scan
answered in an hour -- in one command. An admin `/api/admin/settings` endpoint + explorer page
is a later option only if the CLI proves insufficient; it adds surface for no new information.

## Approaches considered and rejected

- **Hand-maintained `docs/feature-flags.md`.** The B6 failure mode with extra steps: nothing
  mechanically ties it to code, so it drifts exactly like the prose it would duplicate.
- **Generated `.env.example`.** Flattens the load-bearing comment blocks; the file's value is
  the prose, and prose is not generated.
- **Source-scan-only ratchet (the literal CF-127 transplant) with no registry.** Feasible for
  T2/T3 but cannot express defaults, categories, requires-edges, or version-bump rules -- the
  metadata that makes "don't lose features" checkable rather than greppable. The registry is the
  added structure that upgrades enforcement from heuristic to exact.
- **Class hierarchy with flag subclasses inheriting a base.** Registry-not-inheritance, per the
  design section: flags share metadata, not behavior; inheritance would add ceremony without
  adding a single check the dataclass cannot.

## Phases

| Phase | Work | Output |
|---|---|---|
| P1 | `feature_flags.py`: `FeatureFlag`, `FEATURES`, `EXEMPT`, `RETIRED` for the Phase-1 scope (44 bools, 8 modes, ~20 env-only vars) | Registry module |
| P2 | `tests/test_feature_flag_registry.py`: T1-T7 | CI-enforced consistency |
| P3 | `.env.example` backfill for everything T3 immediately fails on (the ~12 env-only vars, personal-memory runtime block, scheduler intervals) | Catalog complete |
| P4 | `menhir config show` | Operator surface |
| P5 (later) | Migrate numeric tunings + remaining settings out of `EXEMPT` | Full inventory |

P2 lands before P3 on purpose: the test failing on the real gaps IS the backfill worklist.
Nothing in P1-P3 touches runtime behavior; P4 adds a read-only CLI.

## Mechanics that will bite

- **`_getenv` aliases.** Several settings honor deprecated spellings (`LLAMA_*`,
  `MEMORY_CHAT_PROVIDER`, `GRAPHITI_PROVIDER`). Registry records the canonical in `env_var` and
  the rest in `env_var_aliases`; T3 checks only the canonical (`.env.example` documents one
  spelling); T2 must accept any alias at a read site.
- **Env vars read via module constants** (`INGEST_ALLOWED_ROOTS_ENV`, `SAGA_WRITERS_GATE_AWARE_ENV`,
  `_SCHEME_ENV`, `_ALLOW_INSECURE_BACKEND_ENV`, `HOST_PID_NAMESPACE_ENV`): T2's census must
  resolve constants to their string values in the defining module, or the scan misses them --
  this exact class of indirection is how #83's var stayed invisible.
- **`parse_bool_env` is the single bool parser** (SSOT-07); the registry asserts nothing about
  parsing -- it catalogs. No change to parsing semantics anywhere in this plan.
- **Dynamically-read settings** (if any exist): T5's scanner will flag them; the per-flag
  `consumed_in` allowlist is the honest escape hatch, and it shrinks only.
- **Test-suite placement**: unit-marked, runs in the routine serial suite; no live Neo4j/LLM
  needed -- T1-T7 are pure-Python/static-text checks.

## Open decisions

1. **Scope of P1**: full ~130 in one pass, or the progressive subset above? Recommend
   progressive; `EXEMPT` makes the choice non-binding either way.
2. **`config show` output shape**: table vs JSON flag (`--json`) vs both. Recommend both; JSON
   is one `dataclasses.asdict` away.
3. **Whether the scheduler-interval family** (the #81 issue surface: unvalidated env ints)
   joins the registry at P1 or waits for #81's fix. Recommend waiting -- #81 may remove or
   rename those knobs as part of validation, and the registry should register the post-fix
   shape.
4. **Explorer exposure** of the registry (a "features" page next to `_feature_report`, which
   shows usage while the registry shows config). Defer to after P4; not part of this plan.

## Verification

- P2's own suite is the primary verification: it must initially FAIL on the known gaps (B6's
  absence pre-`e8a3f002`, the ~12 env-only vars) -- a registry test that passes on day one
  without forcing the backfill is vacuous, and its vacuity must itself be tested (T6's
  spot-assertions and T4's ratchet).
- Routine suite: `pytest tests/ -m unit -q` (serial; maintainer machine rule).
- P4 verification: run `menhir config show` against the live `.env` and diff its "effective"
  column against the 2026-09-11 scan table (the one in this plan's Origin block) -- they must
  agree on every row, or the scan had an error the join just exposed.
