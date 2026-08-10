# Config/OAuth Cycle Break

## Why

- Authentication-mode selection and OAuth environment parsing currently live under `api` even
  though settings validation imports them, producing a six-module config/API import cycle.
- Startup safety, middleware wiring, and operator diagnostics must continue resolving one identical
  authentication mode from the immutable settings snapshot.

## Scope

- Move `AuthMode`, its precedence resolver, `OAuthConfig`, and OAuth config construction into
  `menhir.config`.
- Keep the existing `menhir.api.auth_mode` and `menhir.api.oauth` imports as compatibility surfaces.
- Make OAuth preflight depend on the config-owned OAuth contract.
- Do not change token verification, OAuth routes, scope policy, environment-variable names, or
  precedence.

## Proposed Design

- `config/oauth.py` owns the OAuth settings contract, legacy-object environment fallback, and
  immutable config builder.
- `config/auth_mode.py` owns auth-mode values and the single precedence decision.
- API modules consume those config contracts; the old API modules re-export compatibility names.
- `config` never imports `menhir.api`.

## Alternatives Considered

- Keep lazy cross-package imports: rejected because they hide rather than remove the cycle.
- Move all OAuth runtime code into config: rejected because token verification and HTTP behavior are
  API responsibilities, not configuration.

## Risks

- Legacy test/settings objects rely on environment fallback when an OAuth attribute is absent.
- Existing callers import private OAuth parsing helpers from `menhir.api.oauth`.
- Snapshot precedence must continue treating explicit empty `MemorySettings` values as authoritative.

## Invariants

- OAuth > client token > static key > no-auth precedence is unchanged.
- Existing API import paths and OAuth challenge/config shapes remain compatible.
- No config module imports `menhir.api`; no import cycle spans the two packages.

## Validation

- Auth-mode, settings snapshot, OAuth self-wiring/preflight, middleware, and diagnostics tests.
- AST boundary and cross-package cycle tests.
- Full offline suite, with known baseline failures recorded separately.

## Docs To Update

- `.agent/architecture.md`
- `CHANGELOG.md`

## Result

- Auth-mode values, precedence, OAuth config contracts, and environment/snapshot construction are
  config-owned; `menhir.config` has no API imports.
- Existing API imports remain aliases to the config-owned objects and helpers.
- Focused auth/OAuth/config validation: 336 passed, 1 skipped.
- Full offline validation: 3,942 passed, 2 skipped, 2 pre-existing failures. The failures are the
  exact worktree-name assertion and stale scalar contract expectation already present in #1; neither
  failing file is changed here.
