---
artifact_schema: 1
artifact_uuid: 53226b14-e147-4485-b821-f207be853561
artifact_type: plan
artifact_status: PROPOSED
---

# menhir -- #168 Phase 0: pinned baseline and evidence contract

Status: **PROPOSED.** Offline pins and configuration evidence recorded 2026-09-28. No paid or
live model call was made to produce this document.

Owner decisions 2026-09-28: section 6 limits **ratified as written**; campaign model
**GPT-6 Luna** (`openai/gpt-6-luna`); B1 cleared (`uv sync --frozen`); D1 filed as #193, but its
fix was deferred at that baseline; the 2026-10-05 follow-up below repairs it. **No dollar ceiling is set and no paid run is authorized**: the owner
questioned whether paid calls are needed at all, so every live run needs a fresh go-ahead.

Scalar decisions (#170): agreement is **lenient** (threshold `2/3`, attribute/scope/subject
reconciliation on), and scalar input is the user's statement **quoted verbatim by the agent** on
`add_memory`, trusted for perception only. Both still have to pass the section 6 limits. Plan:
`menhir-scalar-default-enablement-plan.md`.

Owner issue: [#168](https://github.com/Archolith/menhir/issues/168). Children #169-#173 consume
this baseline; nothing here enables a feature.

## 1. Pins

| Item | Pin | How checked |
|---|---|---|
| Menhir | `de370260102f5dc7782742510588723f57f05f42` (`main`, 2026-09-28, PR #192) | `git fetch`; local `HEAD` == `origin/main` |
| Bench | `8a8d629cf3a45bc1a6804b55702ecfc6ccbc00cb` (`origin/master`, PR #9) | `git fetch`. Replaces `2331fc47` from the handoff: 67 commits newer, including LME ingest changes (`bab736e` pronoun-fragment skip, `dffffab` IT-acronym guard, `d95d533` fail-closed ingest acceptance, `da1f4b9` declared Oracle fixture) |
| Package | `archolith-menhir 0.2.3` (pyproject). Main is 227 commits past tag `v0.2.3` (`17f8b97b`); the PyPI `0.2.3` wheel is **not** this baseline | `git rev-list --count v0.2.3..HEAD` |
| Graphiti | `archolith-graphiti-core==0.30.2.post1` from PyPI (uv.lock registry source; no Git dependency) | `uv.lock`, `pip index versions` |
| Local wheel | `archolith_menhir-0.2.3-py3-none-any.whl`, sha256 `ff9d9f7e50cbead72b30acd278b8af36096b46e7ddeb43cec7181c225c42b6c7`, built by `uv build --wheel` from `git archive de370260` | Local build; not reproducible byte-for-byte across builders. The campaign must record its own hash |
| Runtime deps | `uv export --frozen --no-dev` (296 lines); installed and imported in a clean Python 3.12 venv: neo4j 6.2.0, openai 2.45.0, archolith-oauth 0.3.1, archolith-mcp-framework 0.2.0 | scratch venv |

The #174 text in #168 (draft PR, Git-dependency blocker) is stale: the fork ships from PyPI.

**Blocker B1 -- stale local interpreter (CLEARED 2026-09-28).** `projects/archolith/menhir/.venv`
had upstream `graphiti-core 0.29.3` and no `archolith-graphiti-core`. Bench's `config.sh` defaults
`MENHIR_MAIN_PY` / `MENHIR_MAIN_BIN` to that venv, so Bench runs on the defaults would have
measured the wrong Graphiti. The symptom: `ModuleNotFoundError: graphiti_core.extraction_routing`
in `tests/test_artifact_shape.py`. Fixed with `uv sync --frozen`, which swapped in
`archolith-graphiti-core 0.30.2.post1` and dropped the unlocked `build`/`coverage`/`pyproject-hooks`.
Record `importlib.metadata.version("archolith-graphiti-core")` in every run manifest so this
can't recur silently.

## 2. Supported fresh-install paths

The tables below retain the 2026-09-28 baseline; the follow-up records candidate changes.

### Package configuration follow-up (2026-10-05, #193)

Audit the installed candidate wheel, source template and offline release-image configuration checks
before the held ingest/qualification campaign. Reproduce blank generated OpenAI models in a clean
non-editable install. Repair setup by filling missing/blank provider defaults from MemorySettings
while preserving non-empty operator choices, secrets and explicit feature false values on rerun.
Keep local embedding configuration manual where the product has no default. Do not normalize all
empty settings globally or overwrite custom models by rerunning provider setup. Test fresh state,
legacy blanks, custom settings and opt-outs; repeat the wheel probe after rebuilding. Record wheel
hash/import origin and fork identity, and distinguish offline Dockerfile/builder checks from a real
sealed-image build. No feature defaults, deployment, ingest, paid calls or RC freeze are authorized.

Evidence: Python 3.12 non-editable wheel installed into a disposable clean venv; baseline
wheel reproduced both blank OpenAI models. Rebuilt candidate wheel SHA256
`1311f05b66b734a3b87e9c24e4b60a97ac2e5c0c6a6b9dd50481fead89e1da1b`
imported from `package-config-venv/Lib/site-packages/menhir`, with
`archolith-graphiti-core==0.30.2.post2`, upstream `graphiti-core` absent, and `pip check` clean.
Candidate reinstall held those runtime dependencies fixed. Isolated installed-wheel probes
passed fresh OpenAI/local defaults, preserved custom URL/models and a test secret, and byte-identical
reruns. Explicit false values for Warden, belief, evidence-anchor, contradiction-interrupt and
verifier-sync remained false in loaded settings. Focused setup/up/hook/readiness/package/image
workflow/controller checks: **169 passed, 1 skipped**. The Dockerfile keeps feature defaults
unchanged and installs verified wheelhouse artifacts without network resolution. This audit did
not build or run a sealed release image: named-RC cold-container E2Es and frozen dependency/image
provenance remain under #123; paid quality and ingest remain held until the end.

| Path | Commands (README "Quick start") | Config it produces |
|---|---|---|
| W: wheel | `pip install archolith-menhir`; `menhir up --compose-neo4j --provider openai` | `~/.menhir/.env` (or `MENHIR_STATE_DIR`): short generated header + Neo4j compose keys + provider block. No feature flags written |
| C: checkout | `pip install .`; `menhir setup` (copies `.env.example`) | `.env.example` activates only host/port, provider selection, provider/model keys, Langfuse keys and two Graphiti token caps. No `MENHIR_*` feature flag is active in it |

Result: on both paths every feature flag takes its `MemorySettings` code default.

Effective differences from code defaults (clean environment, `MemorySettings.from_env()`, 153 fields):

| Setting | Code default | W (`--provider openai`) | C (`.env.example`) |
|---|---|---|---|
| `chat_provider` / `graphiti_provider` | `local` | `openai` | `local` |
| `graphiti_embed_provider` | `''` | `openai` | `''` |
| `openai_chat_model` | `gpt-4o-mini` | **`''`** | `gpt-4o-mini` |
| `openai_embed_model` | `text-embedding-3-small` | **`''`** | `text-embedding-3-small` |
| `local_llm_chat_model` | `qwen3.5-35b-a3b` | same | same |
| `langfuse_host` | `''` | `''` | `http://localhost:3001` |

**Defect D1 -- wheel path writes empty OpenAI model keys.** `cli/setup.py` `_PROVIDER_KEYS["openai"]`
ensures `OPENAI_CHAT_MODEL` and `OPENAI_EMBED_MODEL` exist with an empty value. `_getenv` returns
a present-but-empty value as-is, and `ProviderConfig._for_openai` passes it through. So the
documented wheel + OpenAI install runs with an empty chat and embed model until the operator
fills them in. `menhir up --check` names only the key, not the models. Reproduced in a scratch
state dir; filed as #193. Any default-promotion E2E on path W must either fix this or state the
operator step explicitly. Also seen: `up --check` reports an empty `OPENAI_API_KEY` as "key
REJECTED (401/403)" rather than "missing" (cosmetic).

## 3. Provider and model contract

- Product defaults: provider `local` (`http://127.0.0.1:8081/v1`, `qwen3.5-35b-a3b`, embed model
  blank). Consolidation uses the global chat model unless
  `personal_memory_consolidation_chat_model` is set.
- Bench LME defaults (`scripts/longmemeval/config.sh` @ `8a8d629`): extract `openai/gpt-4o-mini`,
  answer `gpt-4o`, judge `gpt-4o-mini`, embed `text-embedding-3-small`.
- Campaign rule: baseline and candidate arms use one identical provider/model tuple, recorded in
  the manifest.
- **Owner decision: GPT-6 Luna** (`openai/gpt-6-luna`, the id Bench's beacon-eval already uses;
  $0.10 / $0.50 per M tokens in `beacon_eval/judge.py`) for extract, answer and judge. Embeddings
  stay `text-embedding-3-small` (Luna is not an embedding model). Two gaps to close before a run:
  (a) the LME cost summarizer (`scripts/longmemeval/lib/summarize_llm_usage.py` @ `8a8d629`) prices
  `gpt-5.6-luna` but not `gpt-6-luna`, so cost attribution would be missing; (b) Luna emits
  reasoning tokens, which count against `personal_memory_consolidation_max_tokens` (8192), and
  `..._disable_reasoning` cannot be used on api.openai.com (it rejects the parameter).

## 4. Shipped baseline behavior (flags at default)

Boolean settings: 44. ON by default: `record_detailed_revisions`, `structure_watcher_enabled`,
`experience_counter_enabled`, `personal_memory_consolidation_sum_grounding`, `explorer_enabled`.
All other 39 are OFF, including every #169-#173 candidate.

Modes: `canonical_self_binding_mode=off` (unrecognized value falls back to `off` with a warning),
`startup_scope=full`, `runtime_mode=production`, `artifact_reconcile_mode=audit`,
`saga_reconcile_startup_mode=observe`, `frontier_fact_edge_mode=standalone`,
`frontier_similarity_scale=rrf`, `frontier_fusion_admission_policy=attributed`.

Background jobs registered on a fresh install (`maintenance_scheduler.py`): lease recovery, failed-
enrichment retry, queue health, three conflict jobs, `refresh_structure_graphs`,
`sync_experience_counters`, telemetry pruning. **Not** registered: `consolidate_personal_memory`
(needs consolidation, event history or scalar state on, plus a sync chat provider) and
`sync_verifiers`.

Recall: all Frontier portions off, so the ScoringService path is unchanged. The per-recall stale-anchor
scan and per-candidate staleness evidence run in the baseline (see #84 below).

Budgets: enrichment per-job cap 100 and session cap 5000/900 s, both **report-only**
(`providers.py`). Consolidation, when enabled, is bounded by `personal_memory_consolidation_call_budget=300`
calls per cycle every 300 s. Its calls are metered to `llm_usage_events` via the default callback, but
the enrichment budgets do not cover them.

### Flag dependencies relevant to #169-#173 (from code and comments)

| Flag | Requires / interaction |
|---|---|
| `personal_memory_scalar_view_authority_enabled` | Views exist only when `personal_memory_scalar_state_enabled` ran |
| `personal_memory_scalar_history_enabled` | Built by the projection coordinator alongside scalar_state; does not by itself register the consolidation job |
| `personal_memory_event_history_authority_enabled` | `personal_memory_event_history_enabled` |
| `scalar_reconcile_*`, `scalar_threshold` | Only act inside scalar_state. Flipping on an existing namespace needs a `perceiver_version` bump |
| `frontier_belief_gate`, `frontier_evidence_anchor` | `frontier_warden_gate` |
| `personal_memory_scalar_deterministic_shadow` | Audit output needs `personal_memory_consolidation_audit_enabled` |
| `personal_memory_recall_audit_enabled`, `..._consolidation_audit_enabled`, `frontier_shadow`, `shadow_context_composition` | Observation switches. Diagnostic only, never counted as defaults |

### Parsing of absent, false and invalid values

`parse_bool_env` accepts `1/true/yes` (case-insensitive) as true; everything else is false.
Observed: `false`, `0`, `''`, `maybe`, `on` all parse to False. Consequence for promotion: an empty
or mistyped value **silently disables** a default-ON flag (checked on `experience_counter_enabled`).
`menhir setup`/`up` write no feature-flag keys at all, so a rerun leaves explicit `false` values
alone. A future default-promotion that writes flags must keep that property. Any flag promoted to default-ON inherits this silent-off behavior unless the
registry work adds invalid-value rejection.

## 5. Bench profile deviations from shipped behavior

Every LME script at `8a8d629` exports `MENHIR_BENCHMARK_MODE=1`, which disables the scheduler,
consolidation/decay and orphan recovery. Its `config.sh` also defaults to consolidation audit 1,
recall audit 1, scalar threshold `2/3`, reconcile attribute/scope/subject 1, scalar call budget 50
(product 300) and canonical-self 0. These apply only once scalar state is on (default 0).

So a Bench baseline is **not** the shipped fresh-install behavior. The campaign manifest must list
every exported `MENHIR_*` value per arm. The baseline arm uses product defaults for every flag under
test. `benchmark_mode` is declared as a harness condition. Background-job features (#170) need an
explicit consolidation drive, since the scheduler is off.

## 6. Preregistered limits (RATIFIED by owner 2026-09-28; no candidate result viewed yet)

Applies per option (one flag or one tightly coupled flag group), against the same-input pinned
baseline, on held-out data separate from development fixtures:

1. **Quality.** The target metric must improve with a paired 95% interval excluding zero on the
   held-out target corpus. On every non-target corpus/category: no regression greater than 2 pp
   absolute, or more than 1 item when N < 50. Lift on one corpus does not qualify another.
2. **Wrong-owner / false-current.** Zero tolerance on held-out controls: 0 wrong-owner current
   Views, 0 stale-as-current answers attributable to the option, and 0 authority suppressions of a
   correct current fact (read from recall audit). One occurrence blocks promotion pending root cause.
3. **Latency.** Recall p50 at most +15% and p95 at most +25% vs baseline on the same host and graph.
   Ingest-to-ready p95 at most +20%. No scheduler job cycle may exceed its interval (consolidation
   under 300 s at the measured load).
4. **Cost.** Recall-side options (#171-#173) add zero model calls per recall unless the option's
   purpose is a model call; if so, the per-recall call count is stated in advance. Background
   options (#170) state their calls per dirty user turn in advance and stay at or below it at
   p95. Per-cycle cap stays at `call_budget` (300). All calls are attributed in `llm_usage_events`.
   **Owner sets the dollar ceiling per run**; stop immediately on 429 or at the ceiling.
5. **Abstention.** Configured-but-inert (flag on, no effect) is reported as its own outcome, with
   the refusal/abstention reason from the audits. It is not scored as neutral lift.

## 7. #84 and #106 against current code (de370260)

| Item | Current code | Needed for which default |
|---|---|---|
| #84.2 per-recall stale-anchor scan | Still per recall, `limit=200` (`recall_pipeline.py:1526`); no `structure_dirty` index in schema | Baseline latency term for #171-#173. Measure dbHits before comparing latency; fix only if the latency limit fails |
| #84 RLF4 `git log` per candidate | Still in a loop (`recall_pipeline.py:1439`) | Same. Also matters to #173 if currentness wardens consume staleness evidence |
| #84 RLF1 sequential pending-episode wait | Still present (`recall_pipeline.py:137`) | Recall-after-ingest latency for all arms; measure |
| #84.1 shared default executor | No dedicated executor anywhere in `src/` | #170: consolidation adds background offload load. Measure pool saturation under recall + consolidation |
| #84.4 unbounded enrichment queue | `asyncio.Queue()` no maxsize (`ingest_queue.py:141`) | Not required by any proposed default |
| #84 ARCH-01 sequential scheduler | Still sequential | #170: a long consolidation cycle delays other jobs. Covered by latency limit 3 |
| #84 SVCC3-PRF-1 shadow composition scan | Unchanged; flag default OFF | Only if `shadow_context_composition` were proposed as a default (it is an observation switch, so no) |
| #106 enforcement off | Still `report_only=True` (`providers.py:299`) | Enrichment path only. It does not govern #170 consolidation spend |
| #106 session-cap calibration | Issue text (50/900 s) is **stale**: default is 5000/900 s since `92808ff1` | None for this campaign |

Net: no #84/#106 fix is a prerequisite for any Phase-0 deliverable. #84.2, RLF1, RLF4 and #84.1
must be **measured** in the baseline run, because the latency limits are relative to it. #106's
open items do not bound the background spend #170 would add; the #170 cost limit (6.4) does.

## 8. Phase 0 checklist status

| #168 item | Status |
|---|---|
| Record SHAs, install paths, providers/models, flags/deps, baseline behavior | Done offline (sections 1-5). Provider/model tuple for the campaign: owner decision |
| Reconcile with the feature-flag registry plan | Done. See the reconciliation section appended to `menhir-feature-flag-registry.md` |
| Bounded inspectable evidence (audits, shadow paths, active vs inert, latency, cost attribution) | **Not run.** Needs Neo4j + a model provider. Instrumentation exists (consolidation/recall audits, `llm_usage_events`, recall `_t_phases`). The first live step is a baseline-only capture on the pinned wheel after B1 is cleared |
| Preregister limits | Done: ratified 2026-09-28 (section 6) |
| Reconcile #84/#106 | Done (section 7) |

Open before any live run: owner go-ahead and a dollar ceiling (none authorized), Luna pricing
in the LME cost summarizer, D1 (#193; fix deferred, so path-W E2Es must set the models by
hand). Cleared: B1, section 6 ratification, and the model choice.
