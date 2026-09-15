## 2026-09-15 - smoke-test operator key, corrected

- Cold-start run #5 (first run able to execute `menhir up --compose-neo4j`: a nested Docker
  daemon, so the documented one-liner was finally reachable). It worked end to end, and
  disproved a README line added the day before: `menhir setup` writes no `MENHIR_OPERATOR_KEY`
  line at all -- `.env.example` names it only in a comment -- so "set the empty line" sent the
  reader looking for something that does not exist. The smoke test now gives the exact append
  command and reads the key back with `grep | cut`.

## 2026-09-14 - cold-start run #4 doc fixes

- Findings from cold-start run #4 (end to end in 8.8 min, smoke test matched the documented
  output; no code changes needed). README: `--compose-neo4j` now says it needs the Docker
  daemon where `menhir` runs and what to do when Menhir is itself in a container; the smoke
  test says to set `MENHIR_OPERATOR_KEY` in `.env` first and how. `deploy/README.md` no longer
  claims the release image builds from a plain clone -- `deploy/Dockerfile` installs only
  from the pre-built wheelhouse, as the root README already said.

## 2026-09-14 - one path to a running server

- `menhir setup` now ends with the same tier report `menhir up --check` prints and exactly one
  next command (`menhir up`); three cold-start evaluators read its old "configure .env, check,
  serve" hint and never tried `up`.
- Bare `menhir` (no subcommand) inside a checkout runs the readiness check instead of printing
  help; outside a checkout it still prints help.
- README quick start is a single path: prerequisites, install and start with `menhir up`,
  connect a client, REST examples, smoke test. The manual Install / Configure / Start Neo4j /
  Check-and-run steps move to a "Step by step (what `menhir up` does)" appendix.

## 2026-09-14 - smoke test that actually cleans up; minimum extraction model

- Findings from cold-start run #3. `DELETE /api/memory/{id}` removes the episode and its
  projections but keeps the entities it produced (shared knowledge, by design); the README's
  smoke test wrongly claimed a cascade. It now writes into a throwaway namespace and tears it
  down with `DELETE /api/namespace/{ns}` (operator tier, dry-run first).
- `POST /api/memory?wait=true` reports `entities_linked` on a `ready` write, counted on the
  Graphiti episode the anchor resolved to (counting on the anchor always read 0). `ready` with
  `entities_linked=0` is the "model extracted nothing" case that was invisible before.
- Minimum recommended extraction model is `gpt-4o-mini` class. Measured on OpenRouter:
  `openai/gpt-4o-mini` extracted 5/5 test sentences (one prefixed "SMOKE TEST:");
  `openai/gpt-4.1-nano` returned zero entities 6/9 on ordinary sentences. `.env.example`
  defaults and the OpenRouter example now name `gpt-4o-mini`; README examples use a
  person-plus-action sentence.

## 2026-09-14 - cold-start evaluation #2 fixes

- `POST /api/memory?wait=true` now reports the terminal processing state -- `ready`, or
  `failed` with `error` and `retry` (`retryable` / `manual_review` / `terminal`) -- and
  `timed_out` when the wait elapsed. It previously answered `queued` even when enrichment had
  already failed for good; the only trace was in the server log.
- The README's REST example is a two-entity sentence with an explicit relation, and the text
  explains that a relationless fragment is refused by design (`relationless_extraction`).
- README's `uv` route uses `uv venv --seed` so the venv has `pip`.
- README gains a "Smoke test, then clean up" sequence (write a marked memory with `wait=true`,
  recall it, delete it by `episode_id`), and `menhir up` points at it before handing off to serve.
- Graphiti's `EquivalentSchemaRuleAlreadyExists` downgrade is installed process-wide by
  `configure_logging`; the previous filter was scoped to `build_indices_and_constraints`, but
  Graphiti's constructor fires the same errors from a background task before that call.

## 2026-09-14 - point newcomers at `menhir up`

- `menhir setup` now ends by recommending `menhir up --check` / `menhir up`; its previous hint
  (configure .env, check, serve) steered a cold-start evaluator past the one-command path
  entirely. README shows the `menhir up` variant for an existing Neo4j / no Docker.

## 2026-09-14 - cold-start evaluation fixes

Findings from a fresh Sonnet agent installing Menhir on Debian from the README alone.

- `menhir check` and `menhir diagnostics` now read `.env` like `serve`; the documented
  diagnostics -> check -> serve sequence reported stale defaults after `.env` was configured.
- `POST /api/recall` defaults `include_session` to true, matching every MCP recall tool. The
  first-run check -- write one memory, read it back over REST -- returned nothing until the
  evaluator found the flag in a source comment.
- Neo4j `01N52` "property key does not exist" notifications are filtered at the
  `neo4j.notifications` logger, so Graphiti's own driver is covered on a first write, not only
  Menhir's drivers.
- README: minimal images ship no `python3`; the deploy Docker stack needs a release-built
  wheelhouse and is not a from-clone path; REST write/read curl examples; configured tier keys
  must be distinct (also in `.env.example`); `post-install.md` notes which commands read `.env`.

## 2026-09-14 - hosted OpenAI-compatible gateways (OpenRouter) work as the `local` provider

- `GET /models` is treated as authoritative only for a loopback server. Hosted gateways route
  models they do not enumerate (OpenRouter serves `openai/text-embedding-3-small` without
  listing it), so a missing name there is now a warning verified on first call instead of a
  preflight failure that forced `degraded_queue_only`. Preflight wording no longer assumes
  llama.cpp. `.env.example` shows the OpenRouter block.

## 2026-09-14 - quieter first boot, honest MCP serverInfo

- Menhir's Neo4j drivers disable the UNRECOGNIZED notification classification (Neo4j's
  ``01N52`` "property key does not exist"), which fired dozens of WARNING lines on an empty
  graph and never meant anything; every other classification stays visible.
- Graphiti's ``EquivalentSchemaRuleAlreadyExists`` errors during ``build_indices_and_constraints``
  -- its index shapes collide by (label, property) with Menhir's under different names, so
  ``IF NOT EXISTS`` cannot help -- are downgraded to DEBUG for the duration of that call only.
- MCP ``serverInfo.version`` now reports Menhir's package version; the SDK's ``FastMCP`` has no
  version parameter and was advertising the ``mcp`` package's own (``1.30.0`` for a ``0.2.0`` build).

## 2026-09-14 - Menhir reads one .env, and a refused Neo4j password is named as such

- `python-dotenv`'s path-less `load_dotenv()` walks up from the *calling file* whenever the
  entry point has a `__file__` (`python -m menhir.cli`, the installed `menhir` script), so
  `graphiti_core.helpers`' import-time call loaded whatever `.env` sat above `site-packages`
  and Menhir's own `load_dotenv(ENV_FILE or None)` loaded the one above `menhir/cli`. Keys the
  operator's `.env` left unset were silently filled from a bystander file (seen as
  `GRAPHITI_EMBED_PROVIDER=openai` in a deployment configured `local`). Menhir now resolves
  exactly one file -- `ENV_FILE`, else `./.env` -- via `menhir.env_file`, and installs a guard
  at package import that turns a dependency's bare `load_dotenv()` into a no-op.
- Preflight distinguishes a Neo4j that refused `NEO4J_USER`/`NEO4J_PASSWORD` from one that
  did not answer, with one connection instead of two; `menhir up` stops its wait immediately
  on a refused password and the tier report says which credential to fix.

## 2026-09-14 - a rejected LLM credential is reported, not just logged

- Every OpenAI-compatible chat/embedding failure already passes through one seam; it now
  classifies 401/403/`invalid_api_key` and keeps the most recent rejection process-wide until a
  call succeeds. `add_memory` appends a WARNING naming it (and `OPENAI_API_KEY`), `/api/health`
  gains `services.llm_auth` and `provider_auth_failure`, and `/api/ready` reports `degraded` with
  the same text while capabilities stay truthful about configuration.
- Preflight now asks the provider whether the key is accepted using the free `GET /v1/models`
  call (no tokens billed), once per startup. Only a definite 401/403 fails the check and names
  `OPENAI_API_KEY`; a probe that cannot reach the provider leaves startup as permissive as
  before. `menhir up` reports the key as verified, REJECTED, or unverifiable, never a bare ok.

## 2026-09-14 - open loopback mode binds the operator tier

- With no credential configured on a loopback bind, the auth middleware bound a session but no
  request tier, and every tool contract refuses to run without one: `tools/list` worked, every
  `tools/call` failed with "No request tier is bound". The mode now binds `operator` (clamped
  to `readonly` under the candidate fence), matching the loopback bootstrap mint. Found during a
  fresh-install walkthrough on Debian.

## 2026-09-14 - README says which distros actually ship Python 3.12

- Prerequisites now list where 3.12+ is the default, where it is an extra package (RHEL 9, Leap),
  where it is absent from the repositories entirely (Debian 12, Ubuntu 22.04), and the `uv`
  route that works everywhere. Surveyed in fresh containers on 2026-09-14.

## 2026-09-14 - `menhir up`: one command from checkout to running server

- Added `menhir up`: ensures `.env` (with `--provider` / `--compose-neo4j` passthrough), starts
  the root compose Neo4j when asked, waits for Bolt with a bounded timeout, prints a tier report
  that names the `.env` key behind each missing capability and the startup mode you would get,
  then runs `serve`. `--check` reports and exits without launching anything.
- `menhir.infrastructure` and `menhir.services` now resolve their package attributes lazily.
  Their eager imports reached `graphiti_core`, whose import-time `load_dotenv()` read the current
  directory's `.env` before the CLI loaded the checkout's own -- so running any command from a
  different directory silently took that directory's Neo4j and provider settings. `menhir up`
  loads the checkout `.env` first; shell environment still wins over `.env`.

## 2026-09-14 - remove the Gemini chat provider

- Removed `GeminiChatBackend`, `ProviderKind.GEMINI`, and the `GEMINI_*` settings. Gemini could
  only back the auxiliary `LLMAdapter` calls -- never Graphiti extraction, embeddings, or the
  reranker -- so `LLM_CHAT_PROVIDER=gemini` produced a server that could not ingest memories
  while the docs presented it as a peer of `local` and `openai`. Supported providers are now
  exactly the two that work end to end. `gemini` as an MCP client name and as a memory source
  label is unaffected.

## 2026-09-14 - tiered .env.example and `menhir setup --provider`

- `.env.example` is now ordered by what a feature needs: Tier 0 (works with the root compose and
  `menhir serve`), Tier 1 (needs a credential, URL, or tuning value), Tier 2 (needs a public
  origin, secret files, a proxy, or an operator ceremony). Block text is unchanged.
- `menhir setup --provider local|openai` writes a consistent chat / Graphiti LLM / Graphiti embed
  provider block and ensures the provider's URL, model, and key lines exist; `--compose-neo4j`
  writes the root compose credentials. Both upsert in place and never overwrite a filled-in
  secret, so re-running is safe.

## 2026-09-14 - remove operator-specific literals from runtime code

- `access_contract` no longer hardcodes one deployment's public origin. The policy's
  `primary_endpoint` must be `https://<origin>/mcp-http`; production startup still binds it to
  `MENHIR_OAUTH_RESOURCE`, which is where the concrete origin lives.
- `todo_location.parse_code_ref` no longer trims absolute Windows paths against a built-in
  `/IdeaProjects/` marker. The domain default is "no marker"; the todo and artifact
  repositories pass `paths.default_workspace_marker()`, derived from `WORKSPACE_ROOT`, so
  existing workspaces behave as before and other installs reject absolute paths as before.

## 2026-09-14 - root local state in MENHIR_STATE_DIR instead of the install layout

- Telemetry sidecar and embedded OAuth AS files now default to `~/.menhir` (override with
  `MENHIR_STATE_DIR`, or per file with `MENHIR_MCP_TELEMETRY_DB` / `MENHIR_OAUTH_AS_DIR`).
  The old discovery walked up from the installed package looking for a `CLAUDE.md` + `projects/`
  workspace and fell back to a fixed ancestor, which pointed into site-packages on a pip install.
- `WORKSPACE_ROOT` remains an explicit legacy alias (`$WORKSPACE_ROOT/.agent`) so existing
  operator workspaces keep their state without editing `.env`; without it,
  `repo_root_for_project` returns `None` and git staleness evidence is simply unavailable.
- The shared telemetry connect seam creates the state directory on first use.

## 2026-09-14 - remove the external LLM scheduler client

- Removed the `cth.mcp.scheduler` integration: Menhir no longer acquires model endpoints from,
  auto-starts, pings, traces to, or reads stall status from an external scheduler. Local model
  endpoints are plain OpenAI-compatible URLs that the operator's own serving layer keeps up.
- Dropped every `SCHEDULER_*` environment variable and `GRAPHITI_REQUEST_STALL_TIMEOUT_SECONDS`
  (its only consumer was the scheduler-status stall watchdog); `GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS`
  remains the request bound. `/api/ready` and `/api/health` no longer report `scheduler_ready` /
  `services.scheduler`, and `/api/stats` no longer carries `scheduler_url`.
- The context-window probe now reads llama.cpp's native `GET /props` instead of a scheduler proxy;
  other servers simply yield "cannot derive". The per-episode telemetry task id moved to
  `infrastructure/telemetry/task_ids.build_episode_task_id`.

## 2026-09-14 - auto-scope the project .venv interpreter guard

- Enforced the `.venv` interpreter guard only when a source checkout actually carries a project
  `.venv`; pip, pipx, and container installs no longer fail preflight on interpreter path, and
  `check_graphiti_dependency` remains the importability check for those installs.
- Made `menhir check` and `menhir serve` share that decision instead of `check` hardcoding the
  guard on; `MENHIR_ALLOW_SYSTEM_PYTHON=1` stays as the explicit opt-out.

## 2026-09-14 - complete relation extraction and repair checks after the release merge

- Limited canonical-self relation guidance to first-person text, leaving third-person extraction on
  Graphiti's native prompt and preserving named people as their own typed-scalar subjects.
- Excluded derived Menhir Views from Graphiti's semantic identity candidates so later turns cannot
  attach ordinary relationships to projection nodes and corrupt their evidence provenance.
- Made unchanged-FACT provenance refusals report the exact failed gate, including stale MENTIONS
  parity and contributor scope, lifecycle, and fence-generation details.

- Kept host-only Testinfra assertions out of the ordinary product suite when their external
  operator toolchain is absent, while preserving their documented explicit invocation.
- Updated stale View-provenance, namespace-fence, reasoning-control, and Linux runner contracts to
  exercise the newly published behavior and arguments.
- Registered the feature-flag plan with stable artifact metadata and the active plan index.

## 2026-09-08 - transact first-backup bootstrap before release installation

- Moved first encrypted-backup bootstrap out of the desktop wrapper and into the root release
  installer's already-bound, locked, snapshotted, durably journaled transaction.
- Added cleanup-resume-before-recount behavior, fixed verified helper overlays through the same
  atomic install primitive as the full release, and fail-closed inherited FD 9 lock validation.
- Added bootstrap-phase rollback/recovery and ordering contracts so candidate helpers cannot become
  a retry baseline and general release authority remains unchanged until the backup is complete.

## 2026-09-07 - define the staged promotion deployment model

- Bound every privileged lane to the approved release, bundle, root runner, and staged Cloudflared
  identity before mutation; added durable root-receipt adoption and no-replay completed recovery.
- Made image evidence executable policy: Syft and Grype now inspect the exact sealed archive, critical
  findings fail publication, clean CI builds the frozen wheelhouse, and publication emits a
  digest-only reference through a collision-resistant candidate tag.
- Made scaffold convergence preflighted and transactional, including an independent trusted-copy
  digest check, sudoers validation, prior file/unit snapshots, and automatic rollback.
- Made production a promotion-only target: the exact finalized image must first pass one complete
  production-equivalent staging workflow with isolated data, OAuth/MCP behavior, restart, and
  automatic rollback evidence.
- Limited routine app and non-migrating security-configuration cutovers to one approval, bounded
  replacement, read-only public canary, and automatic prior-image rollback.
- Reserved fresh backups, restore rehearsals, full authority comparisons, and writer replacement for
  mechanically classified maintenance or recovery releases, and blocked further production releases
  until the staging receipt is enforced end to end.
- Separated product publication from personal deployment: publication produces immutable consumable
  artifacts without production access, while personal deployment selects one published digest and
  cannot rebuild or republish it.

## 2026-09-06 - harden the stacked core projection promotion

- Added instance-local View and evidence registries plus source-bound admission and projection
  definition contracts while preserving the existing default vocabularies.
- Added durable projection lifecycle, coverage, realization, materialization, and reconciliation
  components with transaction-scoped fencing and fail-closed stale or corrupt state handling.
- Kept the new lifecycle opt-in: existing scalar rebuilding remains available, physical default-
  namespace storage is unchanged, and only typed-assertion reads canonicalize logical aliases.

## 2026-09-06 - tighten typed scalar identity before voting

- Canonicalized elapsed durations to seconds, explicit USD money to exact decimals, supported
  measurement units to closed lexical forms, and grounded clock times to 24-hour values before
  proposal identity and k-sample voting are computed.
- Made counts source-authoritative and integer-only, forced intrinsically unitless scalar kinds to
  blank units, normalized weekday/status casing, and rejected boolean values that contradict an
  unambiguous grounded source polarity.
- Preserved exact money values through durable JSON storage, hydration, folding, and View identity,
  with fail-closed behavior for absent, unknown, fractional, or ambiguous source constraints.

## 2026-09-06 - automate reviewed production release staging

- Added committed change fragments and deterministic Markdown/JSON release-note rendering so
  release history is staged alongside each production-impacting fix.
- Added strict four-repository release-spec generation and deterministic install-bundle creation,
  replacing the previous one-off release workspace scripts.
- Added a resumable `prepare -> review -> finalize -> deploy` coordinator that preserves the
  independent security-review gate, previews deployment by default, and requires the exact release
  ID plus an explicit execution flag before invoking the existing production transaction.

## 2026-09-06 - admit ChatGPT's stable CIMD identity

- Added `https://chatgpt.com/oauth/client.json` to the digest-bound ChatGPT
  operator policy while retaining the restored DCR identity during migration.
- Added an authorization regression test that combines ChatGPT's current CIMD
  metadata shape, stable callback, public-client method negotiation, and the
  real production policy.
- Updated the hosted-client access documentation and production policy digest
  to `a6c7cd4f061010415c9f68b66bb79b808eca49b8ed5df51495ff18de312a865c`.

## 2026-09-04 - add verified subject endpoints for canonical self

- Made the lease-acquiring episode claim atomically certify exact evidence-projection lineage,
  cardinality, role/declarant, content, namespace, and no-diff requirements.
- Added an enforce-only, episode-scoped author endpoint carried through Graphiti extraction and
  relationless repair, with deterministic envelope validation plus final current-episode edge/index
  validation before the sole production `declare_self_subject` call.
- Made canonical binding atomically rename the endpoint to `user` while preserving UUID, edge,
  index-map, and display-name rollback. Post-tool projections are queued immediately, and retries
  recover a durable pending projection left behind by an earlier queue exception.
- Added fail-closed unit coverage for malformed authority, retries, mixed RBAC `user` entities,
  prompt isolation, queue failures, legacy blank/default namespace equivalence, and the
  declaration-producer census.

## 2026-09-04 - align production OAuth and agent todo authority

- Restored `menhir:admin` to the production compose authorization-server scope surface so the
  digest-bound Codex, Claude, and ChatGPT operator grants can be issued.
- Granted every agent-tier client the complete todo workflow: list, read, add, close, and stale
  close, while retaining the existing agent OAuth tier and all non-todo denials.
- Made production startup fail closed when runtime scope/tier configuration cannot satisfy the
  canonical access contract, and added compose plus startup regression coverage.

## 2026-09-04 - close six review findings on the canonical-self prevention path

- **Binding now requires a DECLARED node-level subject, and nothing else qualifies.** Trusted
  evidence proves who AUTHORED an episode; it never proves which extracted entity that author is.
  Three successive rules tried to answer the second question from the entity's name -- the literal
  string `user`, then an arity guard, then first-person grammar -- and each has a counterexample
  inside a valid human turn, the last being reported speech (`She told me, "I will handle it"`
  extracts an `I` who is someone else). All three made the same mistake: treating a property of
  the extracted STRING as a fact about its PROVENANCE. Only `EXPLICIT_SELF_SUBJECT`, a trusted
  internal caller declaring the episode's subject to be the owner, now binds; two declared aliases
  in one payload raise `AmbiguousSelfBindingError` and write nothing.
  **No production producer emits that declaration, so the prevention path is inert**: `enforce`
  and `off` are currently behaviorally identical. That is deliberate -- correct and doing nothing
  beats plausible and occasionally catastrophic -- but it means preventing forks needs per-node
  subject provenance from extraction (each node's source span, and whether it is quoted speech),
  which does not exist. The `self_like_unresolved` outcome,
  `self_like_without_subject_authority`, and `first_person_unresolved` counters describe the
  unclassified population in observe mode; they deliberately do not predict which nodes are safe
  to bind. A structural census now fails on any new context constructor, factory call site, or
  executable `EXPLICIT_SELF_SUBJECT` reference.
- **A missing driver or a failed canonical-node read is no longer treated as "absent".** Graphiti saves with
  `SET n = $entity_data`, which replaces the property map, so falling back to the sparse extracted
  node on a transient driver error would let a later write erase the stored node's markers,
  provenance, flags and summary. Only `NodeNotFoundError` falls back now.
- **The first canonical node in a namespace is stamped** with `is_self`, `entity_role` and the
  logical namespace. It was previously created without them, and the generic ingest metadata stamp
  supplies neither, so no structural reader would have recognized the node just created.
- **Resolution telemetry now covers the LLM outcomes**, not only deterministic similarity:
  `llm_selected_candidate`, `llm_selected_new`, `no_candidates_new`, unresolved count,
  candidate-count bounds, embedding model and dimension. Per-candidate cosine scores are NOT
  recorded: graphiti's search ranks by score and then drops it, omitting `name_embedding` from the
  returned record and popping it from `attributes`, so a measurement taken here would silently
  measure nothing in production while looking like a metric. The saturation signature the RCA
  depends on stays visible in `candidate_count_max` and `multiple_exact_llm`. Dedupe-prompt sizes
  are recorded per batch and per section -- entities, candidates including their attributes, the
  episode, and previous episodes including their serialized timestamps.
- **An ambiguous refusal is now recorded before it raises**, and observe mode no longer fails
  the episode: the outcome an operator most needs during an observation window was the only one
  producing no telemetry.
- **`detect_self_forks` no longer writes.** It obtained its uuid by calling `ensure_self_entity`,
  which MERGEs, so a census mutated the graph it was inspecting.
