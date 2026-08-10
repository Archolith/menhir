# Loopback Multi-Client Provenance (no-auth mode)

> **ARCHIVED 2026-07-11 (ctharvey-approved).** Shipped and live on the no-auth path (no flag):
> Task 1 `client_name`->`client_id` sha256 fallback (`api/auth.py:221`, only when no id/api_key);
> Task 2 provenance binding in the `AuthMode.NONE` branch (`auth.py:280-295`, `bind_request_session`
> with self-declared identity, no tier bound); Task 3 loopback auth-mode label correctly omitted
> (the plan's collision-safe path); tests `tests/test_loopback_multiclient_provenance.py` present.
> Implemented via an `AuthMode` enum dispatch; behavior matches the plan. Archived per owner rule (a).

Parent: `menhir-embedded-oauth-as-plan.md` (the **local-loopback tier** of the deployment-
tiered auth spine). **Independent of the OAuth AS ladder — near-term, higher priority than
the AS work** since it is what most local users hit. Bite-sized; one middleware branch + a
small header helper + tests.

**Project:** `projects/archolith/menhir/`.

## Objective

In loopback **no-auth** mode (no static keys, OAuth disabled — Menhir bound to 127.0.0.1),
capture **per-client identity from self-declared labels** so multiple local clients (Claude
Code, Claude Desktop, local agents) are attributed distinctly in session/client telemetry —
**without tokens and without changing access/tier behavior**.

Trust model: labels are **cooperative, not enforced** (any local process can send any
`x-yawn-client-name`; safe because startup guarantees loopback bind via
`validate_no_auth_bind_safety`, and no-auth mode grants no differential access). Enforced
per-client identity is out of scope — that is the per-client-token tier.

## Context / anchors (verified)

- The gap: `src/menhir/api/auth.py`, `BearerAuthMiddleware.__call__`, the no-key branch:
  ```python
  # Static API key auth — only used when OAuth is disabled.
  if not (self._operator_key or self._agent_key or self._readonly_key):
      await self.app(scope, receive, send)
      return
  ```
  This returns **before** any `_request_session_headers` / `bind_request_session`, so no
  provenance is bound.
- Identity derivation: `_request_session_headers(...)` (`api/auth.py:95`) already reads
  `x-yawn-client-id`, `x-yawn-client-name`, and `?client_name=` (for MCP paths) when
  `trust_identity_headers=True`, and derives a `client_id` from the api_key when absent.
- Binding + telemetry: `bind_request_session` / `reset_request_session` and
  `touch_session` / `touch_client` in `src/menhir/mcp/service_access.py` (`touch_client`
  only fires when `client_id` is non-empty).

## Tasks

1. **Stable `client_id` fallback from `client_name`.** In `_request_session_headers`
   (`api/auth.py`), after the existing `if not client_id and api_key:` block, add a lowest-
   priority fallback so a client that sends only a name (no `x-yawn-client-id`, no api_key —
   the loopback case) still gets a **stable** id for grouping:
   ```python
   if not client_id and client_name:
       client_id = hashlib.sha256(client_name.encode("utf-8")).hexdigest()[:16]
   ```
   Place it so it does NOT override an api_key-derived id (the static-key path is unchanged;
   there `api_key` is set, so this branch is skipped). Use the derived `client_name` variable
   as it exists at that point (i.e. after the default-name resolution — confirm the derived
   name, not a bare header, so unlabeled clients hash a default rather than "").
   Guard against hashing the generic default (`remote-mcp`/`remote-api`) if you want unlabeled
   clients to stay in one anonymous bucket — decide and document which (recommended: only
   hash when a real, non-default label was supplied, so unlabeled clients share the default
   id).

2. **Bind provenance in the no-key branch.** Replace the bare passthrough with an identity-
   binding passthrough. Do **not** bind a tier (preserve current no-auth access exactly):
   ```python
   if not (self._operator_key or self._agent_key or self._readonly_key):
       # Loopback no-auth: no tokens, but still capture self-declared per-client
       # identity for provenance/telemetry. Safe because startup guarantees a
       # loopback bind (validate_no_auth_bind_safety). Labels are cooperative,
       # not an enforced security boundary. Access/tier behavior is unchanged.
       user_id, session_id, client_id, client_name = self._request_session_headers(
           headers, path=path, api_key="", qs=qs, trust_identity_headers=True
       )
       session_token = bind_request_session(
           user_id, session_id, client_id=client_id, client_name=client_name
       )
       try:
           await self.app(scope, receive, send)
       finally:
           reset_request_session(session_token)
       return
   ```
   (Only `/api/*` and `/mcp*` requests reach here; non-matching paths and `_EXEMPT_PATHS`
   already returned earlier, so health/ready are untouched.)

3. **Auth-mode label (optional, check for test collision first).** Optionally also
   `bind_request_auth_mode("loopback")` + reset, so telemetry distinguishes this tier.
   **Before adding it,** grep the test suite for assertions that no-auth requests have
   auth_mode `"none"` (default). If any exist, adding `"loopback"` changes their expectation —
   in that case **stop and report** (do not edit those tests); ship Task 2 without the
   auth-mode label. If none exist, include it.

## Tests (new file `tests/test_loopback_multiclient_provenance.py`)

- `test_two_named_clients_get_distinct_identity`: two no-auth requests with
  `x-yawn-client-name: alpha` and `beta` bind two different `client_id`s; both reach the app
  (200, no 401).
- `test_client_name_via_query_on_mcp_path`: `/mcp-http?client_name=gamma` binds `gamma`.
- `test_client_id_stable_across_calls`: same `client_name` on two calls yields the same
  derived `client_id`.
- `test_unlabeled_client_uses_default_bucket`: a no-auth request with no client label reaches
  the app and binds the default identity (behavior unchanged from today).
- `test_no_auth_access_unchanged`: a no-key loopback request to a protected route still
  succeeds (no tier enforcement regression) — this is the guardrail that access did not change.
- `test_telemetry_records_per_client` (if a telemetry inspection seam exists): after two named
  requests, `touch_client` recorded two clients.

## Acceptance criteria

- Multiple labelled local clients are attributed distinctly in session/client telemetry in
  loopback no-auth mode.
- No change to access/authorization: no-auth requests still reach protected routes exactly as
  before (verified by `test_no_auth_access_unchanged`).
- Static-key and OAuth paths are byte-for-byte unchanged (the `client_name`->`client_id`
  fallback only fires when both `client_id` and `api_key` are empty).
- `pytest -p no:cacheprovider -q tests/test_loopback_multiclient_provenance.py tests/test_api_auth.py tests/test_loopback_auth_safety.py` passes; no existing test modified.

## Out of scope

- Enforced (tamper-proof) per-client identity — that is the per-client-token tier (each client
  gets a secret; identity bound to the token). Separate plan:
  `menhir-per-client-token-tier.md` (FUTURE; the stated end goal).
- Per-client tiering/authorization in loopback mode (provenance only, not access control).

## Forward compatibility (do not block the enforced tier)

The enforced tier (`menhir-per-client-token-tier.md`) is the north star: it binds provenance
through the **same** `bind_request_session` surface used here, differing only in that identity
is sourced from a **verified token registry** (with `trust_identity_headers=False`) instead of
a self-declared header. Therefore: keep the provenance binding routed through
`bind_request_session` (do not inline a bespoke telemetry write), and do not add logic that
assumes headers are the *only* possible identity source. No extra abstraction is needed now —
just don't foreclose the swap.

## Verify

`pytest -p no:cacheprovider -q tests/test_loopback_multiclient_provenance.py tests/test_api_auth.py tests/test_loopback_auth_safety.py`
Commit: `feat(auth): per-client provenance in loopback no-auth mode`
