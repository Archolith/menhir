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
