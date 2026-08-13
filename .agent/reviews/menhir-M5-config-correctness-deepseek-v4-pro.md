# Menhir M5 Config Module — Functional Correctness Audit

**Agent:** deepseek-v4-pro (model ID `deepseek/deepseek-v4-flash`)
**Audit type:** read-only code correctness audit (task `TASK-ds4pro-m5-config-audit`)
**Date:** 2026-08-12
**Scope:** exactly the 7 files under `src/menhir/config/`

---

## 1. Executive Summary

The config layer is well-structured: bool parsing is centralized (`parse_bool_env`), bind safety is enforced through a single `resolve_auth_mode` → `assert_bind_safe` path, auth precedence is deterministic and matches its docstring, and critical numerics (port, OAuth TTLs/rates, reconcile threshold) are validated eagerly at construction, so startup aborts on bad values rather than limping on.

No **High-severity** defect that produces an effectively-unauthenticated remote service was found *through config alone*: the one way to reach `AuthMode.NONE` on a remote bind is the explicit, warning-gated `MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH` opt-in, and OAuth enabled-but-misconfigured **fails closed** (503), it does not fall open.

The most significant findings are:

- **`explorer_enabled: bool = True`** (`settings_model.py:351`) ships a developer UI enabled by default — confirmed finding, the task already flagged it.
- **The SSOT claim for bool parsing is false.** `parse_bool_env` deliberately rejects `"on"` and its docstring (`settings_helpers.py:91-98`) asserts `"on"` counts "everywhere or nowhere", but `oauth._as_bool` (`oauth.py:59`) accepts `"on"`. Executed: `parse_bool_env("on") == False`, `_as_bool("on") == True`. Same env value parses differently depending on which code path and settings-object type is used.
- **A typo in an env var name silently yields the default** with no error or warning; for default-`True` flags the default is *enabled*, so a misspelled `MENHIR_EXPLORER_ENABBLED=false` keeps the explorer on. Executed and confirmed.
- **One dead setting**: `revision_retention_days` is parsed from env and stored but never read anywhere in `src/menhir/`.
- Several numeric/URL/path fields are unvalidated; a handful of consolidation ints use bare `int(...)` with no helpful parse error.

Confidence: **86/100**.

---

## 2. Scope Reconciliation

All 7 files read in full. Line counts taken from the live files via `Get-Content .Count` (matching the task's stated 1,394 total).

| File | Lines |
|---|---|
| `src/menhir/config/__init__.py` | 18 |
| `src/menhir/config/auth_mode.py` | 62 |
| `src/menhir/config/feature_scope.py` | 43 |
| `src/menhir/config/oauth.py` | 250 |
| `src/menhir/config/settings_helpers.py` | 184 |
| `src/menhir/config/settings_model.py` | 807 |
| `src/menhir/config/settings.py` | 30 |
| **Sum** | **1,394** |

Sum: 18 + 62 + 43 + 250 + 184 + 807 + 30 = **1394** ✓

---

## 3. Findings

### F1 — `explorer_enabled: bool = True` ships the developer UI on by default
- **Severity:** High
- **File:line:** `settings_model.py:351` (default), parsed at `settings_model.py:702`, consumed at `api/server_support.py:208` (`if settings.explorer_enabled and startup_scope not in BACKENDLESS_SCOPES: mount_explorer(app)`).
- **Evidence:** Default is `True`; no consumer ever requires it to be explicitly set. Explorer routes are mounted on any default deployment.
- **Impact:** A developer UI is exposed by default on every server that does not explicitly set `MENHIR_EXPLORER_ENABLED=false`. It is loopback-gated for the auth-free path (`api/auth.py:335-338`: `direct_loopback`) but remains mounted and reachable on LAN binds. Confirmed finding per the task brief.
- **Fix:** Default to `False`; require an explicit opt-in (`MENHIR_EXPLORER_ENABLED=true`).

### F2 — Bool SSOT claim is false: `parse_bool_env` and `oauth._as_bool` disagree on `"on"`
- **Severity:** Medium
- **File:line:** `settings_helpers.py:85-100` (docstring lines 91-98) vs `oauth.py:56-59`.
- **Evidence:** `parse_bool_env`'s truthy set is `("true","1","yes")` (line 85) and its docstring (lines 97-98) asserts `"on"` is "intentionally not truthy: no flag in this codebase documents it as an accepted value, only `1`/`true`/`yes`" and that "a value like `on` either counts everywhere or nowhere". `oauth._as_bool` (line 59) uses `("true","1","yes","on")` — **`"on"` IS truthy there**. Executed:
  ```
  parse_bool_env('on') -> False
  parse_bool_env('ON') -> False
  _as_bool('on')       -> True
  _as_bool('ON')       -> True
  ```
- **Impact:** The documented single-source-of-truth for booleans is not actually single. `MENHIR_OAUTH_ENABLED=on` yields `False` via `MemorySettings.from_env` but `True` via `build_oauth_config` when handed a legacy settings object (or any object that lacks an `oauth_enabled` attribute, per the env fallback at `oauth.py:34-41`). This is the exact class of SSOT drift (SSOT-07) the docstring claims to have eliminated. It is a real consistency bug and a comment-claims-a-control-not-implemented case.
- **Fix:** Make `_as_bool` delegate to the canonical truthy set (remove `"on"`), or explicitly document and enforce the two sets.

### F3 — Typo in an env var name silently yields the default; for default-`True` flags the default is ON
- **Severity:** Medium
- **File:line:** `settings_helpers.py:29-35` (`_getenv` returns `default` on any unset/unknown var; no warning), default-`True` fields at `settings_model.py:102,132,145,193,351`.
- **Evidence:** Executed:
  ```
  # typo'd var name:
  MENHIR_EXPLORER_ENABBLED=false  -> explorer_enabled == True  (silently ignored)
  ```
- **Impact:** An operator who misspells a behavior/security flag (e.g. `MENHIR_EXPLORER_ENABBLED`, `MENHIR_STRUCTURE_WATCHER_ENABLED`) gets a silent no-op that leaves the **default** in force. When that default is `True` (e.g. explorer, structure watcher), the misconfiguration keeps the feature *enabled*. There is no warning, so the operator believes they disabled it. Contrast with F4: a *malformed value* silently disables; a *typo'd name* silently keeps the default — two different silent outcomes with no diagnostics.
- **Fix:** Warn (log) when a known env-var name is set but unrecognized, or emit a startup warning listing which `MENHIR_*` vars were ignored.

### F4 — Malformed boolean value silently disables the flag (never the default)
- **Severity:** Low
- **File:line:** `settings_helpers.py:88-100`.
- **Evidence:** Executed:
  ```
  parse_bool_env('on')  -> False
  parse_bool_env('ON')  -> False
  parse_bool_env('2')   -> False
  parse_bool_env('ture')-> False   (typo)
  parse_bool_env('')    -> False
  parse_bool_env(' yes ')-> True
  MENHIR_STRUCTURE_WATCHER_ENABLED=on  -> structure_watcher_enabled == False
  ```
- **Impact:** Any value outside `{1, true, yes}` (case/space-insensitive) yields `False`, **regardless of the field's default**. `MENHIR_STRUCTURE_WATCHER_ENABLED=on` — a very common truthy spelling — silently turns the structure watcher OFF. The `"on"` rejection is deliberate per the docstring, but it is documented only in a code comment and is not surfaced to operators, so a user writing `on` gets the inverse of their intent with zero feedback.
- **Fix:** Consider accepting `"on"` (and emitting a warning on unknown values), or log a warning for unrecognized boolean spellings.

### F5 — `parse_bool_env(None)` raises `AttributeError`
- **Severity:** Low
- **File:line:** `settings_helpers.py:100` (`raw.strip()` with no None guard).
- **Evidence:** Executed:
  ```
  parse_bool_env(None) -> AttributeError: 'NoneType' object has no attribute 'strip'
  ```
- **Impact:** Not reachable via `from_env` (defaults are passed as `str(cls.X)`), but the helper is exported (`settings.py:25`) and is fragile to a `None` input, unlike `oauth._as_bool` (`oauth.py:56-59`) which handles non-bool via `str(value)`. Inconsistent contract.
- **Fix:** Coerce `None`/empty to `""` first.

### F6 — Validation gaps (ports/URLs/hosts/timeouts/paths)
- **Severity:** Medium (mixed; mostly Low, but notable for OAuth URLs)
- **File:line:** validation block `settings_model.py:397-464`.
- **Evidence:**
  - `api_port` range validated (`401-402`). Good.
  - `graphiti_add_episode_timeout_seconds > 0` validated (`399-400`). But **`graphiti_request_stall_timeout_seconds`, `structure_watcher_interval_s`, `shadow_composition_timeout_s`, `personal_memory_consolidation_interval_s`, `llm_max_tokens`, `revision_retention_days`, `conflict_cooldown_days`** are not range-checked (a negative or zero value is accepted silently).
  - **URLs are not validated** except `oauth_public_base_url` when `oauth_as_enabled` (HTTPS-or-loopback-HTTP check at `451-460`). `neo4j_uri`, `local_llm_base_url`, `backend_url`, `langfuse_host`, `oauth_issuer`, `oauth_jwks_uri` are stored as opaque strings; a malformed URL is accepted and only fails (or behaves oddly) at use time.
  - **Paths** (`oauth_as_dir`) are not validated to exist (`371`, parsed `745`).
  - `personal_memory_consolidation_k`, `_call_budget`, `_max_tokens`, `_verify_retries` are parsed with bare `int(_getenv(...))` (`settings_model.py:605-609`) rather than `_parse_int`, so a malformed value raises a bare, message-less `ValueError` (no env-var context), and none are range-validated.
  - Validation runs **eagerly** in `__post_init__`, i.e. at construction/import of settings — it aborts startup rather than failing lazily. Good property; the gaps are coverage, not timing.
- **Impact:** Operators can silently ship an invalid URL (e.g. typo in `neo4j_uri`) and only discover it at runtime; out-of-range timeouts (e.g. `0`) can produce immediate-expiry or busy-loop behavior without a startup error.
- **Fix:** Add bounds checks for the listed timeouts/ints; validate `*_uri`/`*_url` with `urlparse` (scheme + hostname present) at construction; use `_parse_int` for the four consolidation ints.

### F7 — Two artifact-reconcile settings can contradict with no guard
- **Severity:** Low
- **File:line:** `settings_model.py:139-140`; read at `core/runtime.py:64-72`.
- **Evidence:** `artifact_reconcile_repo` (`MENHIR_ARTIFACT_RECONCILE_REPO`, parsed `590`) and `artifact_reconcile_repository` (`MENHIR_ARTIFACT_RECONCILE_REPOSITORY`, parsed `591-593`) are two distinct settings. `core/runtime.py:64` reads `artifact_reconcile_repo` first and `:72` reads `artifact_reconcile_repository`. If both env vars are set to different values, there is **no guard or warning**; the winner is an implicit source-order artifact (repo wins).
- **Impact:** Confusing/contradictory config; the effective value depends on unstated precedence.
- **Fix:** If both are set, raise or warn; treat one as the canonical name and the other as a deprecated alias (as done elsewhere, e.g. `GRAPHITI_PROVIDER` alias).

### F8 — Dead config: `revision_retention_days` is never read
- **Severity:** Low
- **File:line:** defined `settings_model.py:103`, parsed `settings_model.py:555-557`.
- **Evidence:** Proving grep across all of `src/menhir/` (command and output in §6). No consumer outside the config module.
- **Impact:** The setting is parsed from `MENHIR_REVISION_RETENTION_DAYS` and stored but has no effect on behavior — a maintenance hazard (operators believe they are tuning retention that isn't enforced).
- **Fix:** Wire it to the revision-pruning logic or remove the setting.

### F9 — OAuth precedence shadows static keys and can lock the service out
- **Severity:** Medium (footgun; fails closed, not a bypass)
- **File:line:** `auth_mode.py:33-34` (OAuth first), `api/auth.py:120-121` (static keys "not accepted on OAuth-protected routes"), `validate_no_auth_bind_safety` early-return at `settings_helpers.py:139-140`.
- **Evidence:** Executed precedence:
  ```
  oauth + static api_key  -> mode 'oauth'   (static key ignored)
  oauth_as only           -> mode 'oauth'   (enabled = rs OR as)
  ```
  With `oauth_enabled=True` but empty `oauth_issuer`/`oauth_jwks_uri`/`oauth_public_base_url` (and no AS), `build_oauth_config` yields `OAuthConfig(enabled=True, issuer="", jwks_uri="")`. The verifier (`api/oauth.py:152-160`) raises when `jwks_uri`/`issuer`/`audiences` are missing, and the middleware surfaces `server_error` → **503** (`api/auth.py:617-624`). Meanwhile `validate_no_auth_bind_safety` allows a remote bind whenever `oauth_enabled` (`settings_helpers.py:139-140`).
- **Impact:** An operator who enables OAuth without fully configuring it (or who sets both `MENHIR_OAUTH_ENABLED=true` and a static `MENHIR_API_KEY` expecting the key to work) gets a remotely-bindable service that rejects every request with 503 — and their static keys are silently ignored. This is a lockout/availability footgun, not an unauth bypass (it fails closed). The absence of a "static keys are ignored because OAuth is on" warning is the real gap.
- **Fix:** Emit a startup warning when `oauth_enabled` but OAuth config is incomplete, and warn when static keys are set alongside OAuth that they will be ignored.

### F10 — `allow_insecure_remote_no_auth=True` is the only route to a remotely reachable `AuthMode.NONE`
- **Severity:** Low (intended, documented, but the one combination that yields an effectively-unauthenticated reachable service)
- **File:line:** `settings_helpers.py:148-154`, mode resolved in `auth_mode.py:38-39`.
- **Evidence:** Executed:
  ```
  no keys, no oauth  -> mode 'none'
  ```
  With `allow_insecure_remote_no_auth=True`, `validate_no_auth_bind_safety` returns after a `logger.warning` (`settings_helpers.py:149-154`) and the bind proceeds; the middleware `AuthMode.NONE` branch (`api/auth.py:366-381`) forwards all `/api/*` and `/mcp*` requests with **no credential check**.
- **Impact:** This is the only config combination that produces a fully unauthenticated, network-reachable service, and its only guard is a log warning. It is an explicit opt-in per the error message, so it is not a hidden bug — but it is exactly the kind of combination an audit must surface. Consider making the warning a required-acknowledgement or gating it behind a startup prompt in interactive mode.
- **Fix:** Keep as opt-in; optionally refuse when `trusted_proxy=True` (a proxy would make every request appear loopback) or require a startup confirmation.

### F11 — Auth-mode divergence between settings-object types via legacy fallback
- **Severity:** Low
- **File:line:** `oauth.py:34-41` + `auth_mode.py:57-61`.
- **Evidence:** `resolve_auth_mode` → `build_oauth_config(settings)` uses `_get_setting` env fallback (which applies `_as_bool`, accepting `"on"`) only when the settings object lacks the `oauth_enabled` attribute; but `client_tokens_enabled` uses `bool(getattr(settings, "client_tokens_enabled", False))` which ignores env entirely for a legacy object. So for a legacy object, the same environment yields `OAUTH` via `"on"` but `CLIENT_TOKEN` is always `False` even if `MENHIR_CLIENT_TOKENS_ENABLED=on`. Server code always passes a `MemorySettings` (`api/server_support.py:44`), so production is unaffected; the inconsistency is in the public helpers' contract.
- **Impact:** Same env → different auth mode depending on settings-object type; a source of subtle drift if any caller hands a lightweight object to `resolve_auth_mode`.
- **Fix:** Unify the legacy fallback (read both env vars or neither) and share one bool parser.

---

## 4. Default-On Feature Inventory

Every `: bool = True` default in the scope (confirmed by grep across `settings_model.py` and `oauth.py`; there are exactly 5):

| Setting | Line | Read outside config? | Safety verdict |
|---|---|---|---|
| `record_detailed_revisions` | `settings_model.py:102` | yes | **Acceptable** — writes detailed revision history; storage/behavior cost, not a security boundary. |
| `structure_watcher_enabled` | `settings_model.py:132` | yes | **Acceptable** — background structural watcher; benign. |
| `experience_counter_enabled` | `settings_model.py:145` | yes | **Acceptable** — telemetry→counter maintenance job; can be paused via env. |
| `personal_memory_consolidation_sum_grounding` | `settings_model.py:193` | yes | **Acceptable (documented)** — deliberately promoted to default `True` (comment `190-192`); only affects the consolidation job, which itself is default-off. |
| `explorer_enabled` | `settings_model.py:351` | yes | **UNSAFE as default** — ships a developer UI on every deployment. See F1. |

`oauth.py` `OAuthConfig` has no `default=True` boolean fields (`enabled: bool = False`, `oauth.py:66`), so no additional defaults there.

---

## 5. Auth Mode Precedence Analysis

**Resolution function** (`auth_mode.py:25-62`): `auth_mode_from` precedence is strictly **OAuth > client-token > static-key > none**, matching its docstring (line 31). Executed confirmation:

```
no keys, no oauth          -> none
oauth only                 -> oauth
oauth + static api_key     -> oauth      (static key shadowed)
oauth_as only              -> oauth      (enabled = rs OR as)
client_token + static      -> client-token
agent key only             -> static
```

**Wiring is SSOT-consistent in production:**
- `resolve_auth_mode` (config) is the single source; the middleware is constructed with `auth_mode=resolve_auth_mode(settings)` (`api/server_support.py:238`) and the `__init__` uses it directly (`api/auth.py:169`), falling back to the identical `auth_mode_from` only for direct/test construction (`api/auth.py:169-175`).
- `configure_client_token_store` (`api/client_token_store.py:241-249`) builds a store **iff** `settings.client_tokens_enabled`, matching how `resolve_auth_mode` reads the same flag — so `CLIENT_TOKEN` mode and a non-None store always coincide.
- `build_oauth_config(settings).enabled` (`oauth.py:174`) = `rs_enabled OR as_enabled`, and both the bind guard (`settings_helpers.py:139`) and the middleware consult it through one path. This is the documented S-001 fix and it holds.

**Security verdict — can settings produce an effectively-unauthenticated service?**
- **No via OAuth.** OAuth enabled-but-incompletely-configured fails **closed** (503, `api/auth.py:617-624`), and when OAuth is `on` the static keys are ignored, so there is no silent fallback to open access. See F9.
- **Yes via explicit opt-in only.** `AuthMode.NONE` + `allow_insecure_remote_no_auth=True` is the sole combination that yields an unauthenticated, network-reachable service, and it requires the explicit unsafe flag (F10). The loopback bind guard prevents `NONE` on remote binds without it.
- **Precedence shadowing** (`OAuth > static`) is the notable subtlety: enabling OAuth silently disables static keys (F9). Not a bypass, but a real availability/lockout footgun worth a startup warning.

---

## 6. Dead Config Register

Method: extracted all 137 `MemorySettings` dataclass fields, then grepped each as a whole word across all `.py` files under `src/menhir/`, subtracting references inside `src/menhir/config/` (the definition + `from_env` assembly). Because `frontier_*` and `oauth_*` fields are consumed indirectly through the invoked builders `retrieval_tuning()` (`core/backend_runtime_data_ops.py:159`) and `build_oauth_config()` (`api/server_support.py:44`, `operator_diagnostics.py:52`), they are **not** dead. Only one field is genuinely never read anywhere:

**`revision_retention_days` — DEAD**

Proving command:
```
rg -n "revision_retention_days" -g "*.py" menhir/
```
Output:
```
menhir/config\settings_model.py:103:    revision_retention_days: int = 14
menhir/config\settings_model.py:555:            revision_retention_days=_parse_int(
menhir/config\settings_model.py:556:                _getenv("MENHIR_REVISION_RETENTION_DAYS", default=str(cls.revision_retention_days)),
```
All three hits are inside the config module (definition + `from_env`); there is **no consumer** elsewhere in `src/menhir/`. Parsed and stored, never used. See F8.

---

## 7. Open Questions

- **Is `revision_retention_days` intended to gate a revision-pruning job that was never wired?** The comment on line 103 (`record_detailed_revisions` / `revision_retention_days` under "M6 sidecar expansion") implies revision retention logic exists, but I found no reader. Possibly a planned-but-unimplemented feature; verify against M6 revision work before deleting the setting.
- **Does an incomplete-but-enabled OAuth AS ever mis-issue tokens that bypass the RS check?** The config layer's `enabled = rs OR as` (`oauth.py:174`) is sound, but whether the embedded AS's signing key (`configure_signing_key`, `api/server_support.py:54`) is ever shared/weak in a way that makes RS validation ineffective is **outside the 7 config files** and unverified here. I only confirmed the config layer does not open access.
- **Does any runtime path read a `MemorySettings` field by string name** (e.g. a generic settings dump `vars(settings)`/`asdict`) that my whole-word grep would under-count? I found `getattr(settings, "...")` call sites in `api/` (e.g. `api/routes_handlers.py`, `api/oauth_rate_limit.py:120`) but did not enumerate every `getattr` to confirm `revision_retention_days` is not reached that way; grep for the literal name found nothing, so it is dead by name.
- **Legacy-object reachability:** The `_as_bool("on")` divergence (F2/F11) is only reachable with a non-`MemorySettings` object or a direct `build_oauth_config` call. I did not prove whether any production call site passes a legacy object today; it remains a latent-contract inconsistency regardless.

---

## 8. Coverage Table

| File | Read |
|---|---|
| `src/menhir/config/__init__.py` | ✅ read |
| `src/menhir/config/auth_mode.py` | ✅ read |
| `src/menhir/config/feature_scope.py` | ✅ read |
| `src/menhir/config/oauth.py` | ✅ read |
| `src/menhir/config/settings_helpers.py` | ✅ read |
| `src/menhir/config/settings_model.py` | ✅ read |
| `src/menhir/config/settings.py` | ✅ read |

Supporting (outside scope, read only to verify impact): `api/auth.py`, `api/server_support.py`, `api/client_token_store.py`, `api/oauth.py`, `core/runtime.py`, `cli/__init__.py`.

---

## 9. Review Confidence

**Score: 86 / 100**

Reasoning:
- **+** All 7 in-scope files read in full; line count reconciled exactly to 1,394.
- **+** Key claims executed with the project venv: `parse_bool_env` odd inputs, `_as_bool` "on" divergence, `from_env` typo/malformed behavior, auth-precedence matrix, and the dead-config grep.
- **+** Cross-checked against the consuming layer (`api/auth.py`, `server_support.py`, `client_token_store.py`) to avoid mislabeling indirectly-consumed fields (frontier/OAuth) as dead.
- **–** Full dead-config proof relies on name-grep; a theoretical `getattr`-by-name read of `revision_retention_days` is ruled out only by absence of the literal token (noted in Open Questions).
- **–** OAuth RS/AS token-validation soundness and the signing-key security are outside scope and were only spot-checked for fail-closed behavior.
- **–** "Six confirmed cases where a comment claims a control the code does not implement" (task hint) — I confirmed **one** clear case in scope (the `parse_bool_env` SSOT/"on" claim, F2) and verified several claimed controls *are* implemented (`_normalize_reconcile_mode`, `assert_bind_safe` dual-call, `_get_setting` authoritative-empty). The other five mentioned cases are evidently elsewhere in the codebase and were not within this module's scope to confirm.
