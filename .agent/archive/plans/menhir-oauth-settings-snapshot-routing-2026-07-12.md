# OAuth/client-token MemorySettings-snapshot routing (SSOT-07 follow-up)

> **ARCHIVED 2026-08-10.** This SSOT-07 follow-up was implemented on 2026-07-12 with the approved
> security revisions and passed focused, unit, and full-suite validation. The body is retained as
> the implementation and security-review record.

Parent: `.agent/archive/plans/ssot-remediation-2026-07-11.md`, SSOT-07 — this sub-item was
explicitly scoped out of that fix ("bigger than mechanical") and parked as its own
follow-up. This is that follow-up plan.

## Approved revision after security review (2026-07-12)

The original mechanical call-site design below is retained as investigation history,
but is superseded where it conflicts with this approved implementation:

- The actual settings inventory is 32 fields: the 16 `OAuthConfig` inputs, the 15
  HTTP/embedded-AS fields in the original table, and `MENHIR_OAUTH_AS_DIR`.
- `MemorySettings` attributes are authoritative even when empty. `_get_setting()` may
  consult the environment only for legacy/test doubles that do not define the attribute.
- `menhir serve` constructs one settings snapshot and passes it to both the HTTP surface
  and backend runtime.
- Embedded-AS stores, signing key, client-token registry, and rate limiters are configured
  from that snapshot before serving requests. Compatibility accessors remain for direct
  unit tests and non-server callers.
- Startup validates required URLs, HTTPS outside loopback, algorithms, TTLs, limits,
  windows, proxy peers, and startup scope.
- Consent HTML receives no-store, anti-framing, no-referrer, nosniff, and restrictive CSP
  headers. Forwarded client IPs are accepted only from configured direct proxy peers.
- The Starlette dependency is pinned above the fix for GHSA-86qp-5c8j-p5mr.

Validation includes environment-mutation immutability, typed parsing, TLS failure,
trusted-proxy peer enforcement, consent response headers, the focused OAuth suite, and
the broader unit suite.

## Actual scope (bigger than originally estimated)

Investigating this turned up a more significant finding than "8 stray `os.getenv`
calls scattered across OAuth modules": `api/oauth.py`'s `_get_setting()` helper
already *looks* settings-first —

```python
def _get_setting(settings: object, attr: str, env_var: str, default: object, *aliases: str) -> object:
    value = getattr(settings, attr, None)
    if value not in (None, "", ()):
        return value
    for key in (env_var, *aliases):
        raw = os.getenv(key)
        ...
```

— and `build_oauth_config()` calls it for 16 different attributes
(`oauth_public_base_url`, `oauth_resource`, `oauth_audiences`, `oauth_enabled`,
`oauth_issuer`, `oauth_jwks_uri`, `oauth_authorization_servers`, `oauth_as_enabled`,
`oauth_scopes_supported`, `oauth_read_scopes`, `oauth_write_scopes`,
`oauth_admin_scopes`, `oauth_jwks_cache_ttl_s`, `oauth_http_timeout_s`,
`oauth_clock_skew_s`, `oauth_allowed_algorithms`). Verified directly
(`dataclasses.fields(MemorySettings)`): **none of these 16 attributes exist on
`MemorySettings`**. `getattr(settings, attr, None)` therefore always returns `None`,
so every single call silently falls through to `os.getenv` unconditionally today —
the "settings-first" design is aspirational dead code, not a working snapshot path.
This is the real gap, not a cosmetic one: fixing it is adding the fields, not just
routing call sites that already had somewhere correct to route to.

Separately, 8 more OAuth/client-token call sites (listed below) read `os.getenv`
directly with no settings indirection at all, some duplicating each other
(`_int_env` is defined near-identically in both `oauth_rate_limit.py` and
`oauth_as_register.py`).

## Why this was scoped out of SSOT-07 rather than rushed

- Needs new `MemorySettings` fields (~26 total: 16 already-referenced-but-missing
  `oauth_*` fields, plus ~10 more for the stray call sites below) threaded through
  `MemorySettings.from_env()`.
- Touches 8 files, several of them the authentication/authorization-critical path
  (token minting, consent, rate limiting, DCR).
- Every field has its own type-coercion and default-preservation risk (float TTLs,
  bool parsing, CSV-to-tuple parsing) — the kind of thing that's easy to get subtly
  wrong under time pressure and hard to catch without deliberately testing each one.

## Inventory: stray `os.getenv` call sites needing new settings fields

| File | Function | Env var | New `MemorySettings` field | Type | Default |
|---|---|---|---|---|---|
| `auth_code_store.py:70` | `AuthCodeStore.__init__` | `MENHIR_OAUTH_AS_CODE_TTL_S` | `oauth_as_code_ttl_s` | float | `120.0` (`_DEFAULT_TTL_S`) |
| `oauth_token.py:43` | `_access_ttl_s()` | `MENHIR_OAUTH_AS_ACCESS_TTL_S` | `oauth_as_access_ttl_s` | int | `3600` (`_ACCESS_TTL_DEFAULT_S`) |
| `oauth_authorize.py:120` | `_consent_secret()` | `MENHIR_OAUTH_AS_CONSENT_SECRET` | `oauth_as_consent_secret` | str | `""` (falls back to `_persistent_consent_secret()`) |
| `oauth_authorize.py:127` | `_consent_ttl_s()` | `MENHIR_OAUTH_AS_CONSENT_TTL_S` | `oauth_as_consent_ttl_s` | float | `300.0` (`_CONSENT_TTL_DEFAULT_S`) |
| `oauth_authorize.py:353` | `_session_ttl_s()` | `MENHIR_OAUTH_AS_SESSION_TTL_S` | `oauth_as_session_ttl_s` | float | `600.0` (`_SESSION_TTL_DEFAULT_S`) |
| `oauth_rate_limit.py:70` | `_trusted_proxy_enabled()` | `MENHIR_TRUSTED_PROXY` | `trusted_proxy` | bool | `False` — **also drop `"on"` from this function's ad hoc truthy set**, same `parse_bool_env` drift class as SSOT-07's `client_tokens_enabled` bug |
| `oauth_rate_limit.py:125+` | `build_register_limiter()`/`build_approve_limiter()` (via `_int_env`) | `MENHIR_OAUTH_AS_REGISTER_RATE`, `MENHIR_OAUTH_AS_REGISTER_WINDOW_S`, `MENHIR_OAUTH_AS_APPROVE_RATE`, `MENHIR_OAUTH_AS_APPROVE_WINDOW_S` | `oauth_as_register_rate`, `oauth_as_register_window_s`, `oauth_as_approve_rate`, `oauth_as_approve_window_s` | int | `20`, `600`, `10`, `300` |
| `oauth_as_register.py:61` | `_max_clients()` | `MENHIR_OAUTH_AS_MAX_CLIENTS` | `oauth_as_max_clients` | int | `1000` |
| `oauth_as_register.py:65` | `_stale_client_max_age_s()` | `MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S` | `oauth_as_stale_client_max_age_s` | int | `86400` |
| `server.py:58` | `create_app()` | `MENHIR_STARTUP_SCOPE` | `startup_scope` | str | `"full"` |
| `server.py:157` | `create_app()` | `MENHIR_CORS_ORIGINS` | `cors_origins` | str (CSV) or `tuple[str,...]` | `""` / `()` |
| `routes.py:389` | `health()` | `MENHIR_INSTANCE_ID` | `instance_id` | str \| None | `None` |

`server.py:206`'s `os.getenv("ENV_FILE")` (used to select which `.env` file to load
*before* settings can be constructed) and `oauth.py:53`'s read inside `_get_setting`
itself (the designed fallback mechanism) are correctly excluded — neither can or
should route through a settings snapshot.

## Concrete per-file fix shape (already verified against current code, not guessed)

1. **`config/settings.py`**: add the ~26 fields above (16 already-referenced
   `oauth_*` ones `_get_setting` expects, plus the 10 in the inventory table) to the
   `MemorySettings` dataclass, each read in `from_env()` via the existing `_getenv`
   helper (and `parse_bool_env` for the one boolean, per SSOT-07's canonical
   parser — no `"on"`).
2. **`oauth_token.py`**: this file *already* has a `_settings_for(request)` helper
   (line 38) that isn't used by `_access_ttl_s()`. Change `_access_ttl_s()` to
   `_access_ttl_s(settings)` reading `settings.oauth_as_access_ttl_s` (mirror
   `_get_setting`'s pattern, or call it directly now that the field exists); update
   the one call site at line 109 (`ttl = _access_ttl_s()` → `_access_ttl_s(settings)`,
   `settings` is already in scope there from `config = build_oauth_config(settings)`).
3. **`oauth_authorize.py`**: same file already has `_settings_for(request)` (line 84)
   and uses `_get_setting` correctly elsewhere (e.g. `_cookie_secure(settings)` at
   line 356, right next to the broken `_session_ttl_s()`). Fix `_consent_secret()`,
   `_consent_ttl_s()`, and `_session_ttl_s()` to accept `settings` and route through
   `_get_setting`, matching `_cookie_secure`'s existing pattern exactly. Thread
   `settings` through their call sites (need to trace each; not done in this
   investigation pass).
4. **`auth_code_store.py`**: `AuthCodeStore.__init__` already accepts an explicit
   `ttl_s: float | None = None` constructor param — the class itself doesn't need to
   change. Fix the single call site, `get_auth_code_store()` (line 221), to build
   `MemorySettings.from_env()` and pass `ttl_s=settings.oauth_as_code_ttl_s`
   explicitly, mirroring exactly how SSOT-07 fixed `client_token_store.client_tokens_enabled()`
   to delegate to `MemorySettings.from_env()` instead of re-reading env itself.
5. **`oauth_rate_limit.py`**: confirmed both limiter factories are called exactly
   once each, at *module import time*, as top-level singletons:
   `oauth_as_register.py:40` (`_register_limiter = build_register_limiter()`) and
   `oauth_authorize.py:69` (`_approve_limiter = build_approve_limiter()`). Neither
   has a `settings`/`request` object available at that point — module import
   happens before any `MemorySettings.from_env()` call in the app's lifespan, so
   routing these through a snapshot means either (a) accepting
   `MemorySettings.from_env()` read once at import time (simplest, matches today's
   `os.getenv` timing exactly, but still not request-scoped/injectable for tests),
   or (b) moving both singletons to lazy construction on first request (bigger
   change, enables real settings injection/override in tests). `_trusted_proxy_enabled()`
   is different: it's called per-request from `client_ip(request)`, which already
   receives `request` and could resolve settings the same way `oauth_token.py`'s
   `_settings_for(request)` does. **This file needs a real design decision (module-
   import-time snapshot vs. lazy construction) before editing — flag for explicit
   discussion, not a blind mechanical swap.**
6. **`oauth_as_register.py`**: `_max_clients()`/`_stale_client_max_age_s()` -- same
   shape as `oauth_rate_limit.py`'s `_int_env` duplication. Fold both files' copies
   of `_int_env` into one shared helper while fixing this (small, in-scope
   dedup — same SSOT spirit as SSOT-12's `symbol_structure_path` extraction).
7. **`server.py`**: `create_app(settings)` already has `settings` in scope at both
   call sites (line 58 `startup_scope`, line 157 `cors_origins_raw`) — trivial
   `os.getenv(...)` → `settings.startup_scope`/`settings.cors_origins` swap, no
   settings-snapshot plumbing needed since `settings` is a function parameter
   already.
8. **`routes.py`**: `health()` has `request` in scope — add
   `getattr(request.app.state, "settings", None) or MemorySettings.from_env()`
   (the exact pattern already used in `oauth_token.py`/`oauth_authorize.py`/
   `oauth_as_register.py`/`oauth_as_metadata.py`/`oauth_as_metadata.py`) and read
   `settings.instance_id`.

## Suggested execution order

1. `config/settings.py` field additions (mechanical, no behavior change by itself —
   `_get_setting`'s `getattr` fallback means adding fields with correct defaults is
   safe even before call sites are updated, since matching the current env-read
   defaults exactly preserves current behavior).
2. `server.py` + `routes.py` (steps 7–8): trivial, settings already in scope, no
   design questions.
3. `oauth_token.py` + `auth_code_store.py` (steps 2, 4): call sites already have
   settings in scope or an established singleton-getter pattern to mirror from
   SSOT-07.
4. `oauth_authorize.py` (step 3): mirror the file's own already-correct
   `_cookie_secure` pattern for the three broken functions.
5. `oauth_rate_limit.py` + `oauth_as_register.py` (steps 5–6) last, and
   deliberately — these need a real look at whether the rate limiters can even
   accept settings injection given their current module-level construction, not a
   blind swap. Do not rush this pair.

## Regression tests

For each new `MemorySettings` field: a parametrized `from_env()` test (mirror
`tests/test_settings.py::test_client_tokens_enabled_from_env`'s shape). For the
`oauth_rate_limit.py` `"on"` boolean-parser fix specifically: a test proving
`MENHIR_TRUSTED_PROXY=on` is `False` (same regression shape as SSOT-07's
`test_client_tokens_enabled_from_env`). For each call-site fix: a test constructing
`MemorySettings` with an explicit non-default value and asserting the consuming
function/class picks it up without needing the env var set at all (proves the
settings path is load-bearing, not just present).

## Status

Implemented on 2026-07-12 with the approved security-review revisions above.
Verification completed with 197 focused OAuth/security tests, 1,525 unit tests,
and the full 2,834-test suite passing (expected skips excluded from pass counts).
