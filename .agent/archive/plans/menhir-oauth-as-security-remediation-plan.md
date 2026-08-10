# Menhir Embedded OAuth AS — Security Remediation Plan

Source review: `.agent/reviews/menhir-oauth-as-security-audit-results.md` (Phase 10 audit).
Project root for all paths below: `projects/archolith/menhir/` (src/menhir/ layout).

> **Distinct from** `.agent/plans/menhir-oauth-remediation-plan.md`, which remediates the
> resource-server half (S-001..S-009). *This* plan remediates the embedded
> **authorization-server** findings (AS-001..AS-007). Do not conflate the two.

## How to use this plan

- Do the phases **in order**; AS-001 (Phase 1) is the release blocker. Each task names the
  file and the exact edit or a tight design spec.
- The AS is security-critical: **write the crypto/state-machine logic yourself**, do not
  delegate it. Commit per phase with a `fix(oauth-as):` prefix + explicit paths + CHANGELOG.
- **Existing-test rule, with one sanctioned exception.** The AS-001 fix *intentionally*
  changes the Phase 8 consent-session contract (the session must now name the approved
  clients). Updating `tests/test_oauth_consent_session.py` to the new contract is therefore
  expected and allowed (behavior legitimately changed). Do **not** touch any other existing
  test to make it pass — if one fails, stop and report.
- After all code, run the full OAuth suite (Phase 5). Baseline before this plan:
  **278 passed / 1 skipped**.

Severity legend: **P1** = release blocker (High), **P2** = abuse/availability hardening
(Medium), **P3** = low cleanups.

---

## PHASE 1 — AS-001 (P1, High): bind the one-click session to approved clients + SameSite=Strict

**Root cause.** The Phase 8 session cookie records only *that* an admin approved something
(`{"kind":"session","sub":...}`), not *which client*, and is `SameSite=Lax`. So a live
session one-clicks **any** validated client, and the Lax cookie rides a cross-site top-level
GET. With open DCR, an attacker registers a client and CSRFs an operator-tier code out.

**Fix shape.** (a) The session payload carries the set of explicitly-approved `client_id`s;
one-click fires only when the request's `client_id` is in that set — otherwise fall through
to the consent page. (b) The cookie becomes `SameSite=Strict`. (c) Approvals accumulate the
approved set across the session lifetime so repeat connects of a *known* client stay
one-click.

All edits are in `src/menhir/api/oauth_authorize.py`.

### T1 — `_sign_session` carries the approved client set

FIND (near line 289):
```python
def _sign_session(sub: str) -> str:
    """Return a signed consent-session token binding *sub* + issue time."""
    payload = {"kind": "session", "sub": sub, "iat": int(time.time())}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_consent_secret(), payload_bytes, sha256).digest()
    return "{}.{}".format(_b64url_encode(payload_bytes), _b64url_encode(sig))
```

REPLACE WITH:
```python
def _sign_session(sub: str, clients: tuple[str, ...] = ()) -> str:
    """Return a signed consent-session token binding *sub*, the explicitly-approved
    ``client_id`` set, and the issue time. One-click is granted ONLY to clients in this
    set (see the GET handler), so a live session cannot silently authorize an
    attacker-registered client (AS-001)."""
    payload = {
        "kind": "session",
        "sub": sub,
        "clients": sorted(set(clients)),
        "iat": int(time.time()),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_consent_secret(), payload_bytes, sha256).digest()
    return "{}.{}".format(_b64url_encode(payload_bytes), _b64url_encode(sig))
```

### T2 — `_verify_session` returns `(sub, approved_clients)`

FIND (the tail of `_verify_session`, near line 323):
```python
    sub = payload.get("sub")
    return str(sub) if sub else None
```

REPLACE WITH:
```python
    sub = payload.get("sub")
    if not sub:
        return None
    clients_raw = payload.get("clients", [])
    if not isinstance(clients_raw, list):
        return None
    return (str(sub), tuple(str(c) for c in clients_raw))
```

Also update the return annotation on the `def _verify_session(token: str) -> str | None:`
line (near 297) to `-> tuple[str, tuple[str, ...]] | None:` and its docstring.

### T3 — `_set_session_cookie` takes the client set and uses SameSite=Strict

FIND (near line 327):
```python
def _set_session_cookie(response: RedirectResponse, settings: object) -> None:
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=_sign_session(_ADMIN_SUBJECT),
        max_age=int(_session_ttl_s()),
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/oauth/authorize",
    )
```

REPLACE WITH:
```python
def _set_session_cookie(
    response: RedirectResponse, settings: object, clients: tuple[str, ...]
) -> None:
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=_sign_session(_ADMIN_SUBJECT, clients),
        max_age=int(_session_ttl_s()),
        httponly=True,
        secure=_cookie_secure(settings),
        # Strict (not Lax): the session is only ever used first-party on the authorize
        # page, and Strict blocks the cross-site top-level-GET send that AS-001 abused.
        samesite="strict",
        path="/oauth/authorize",
    )
```

### T4 — GET one-click fires only for an approved client

FIND (near line 402):
```python
    session_sub = _verify_session(request.cookies.get(_SESSION_COOKIE, ""))
    if session_sub and _operator_key(settings):
        return _issue_code_redirect(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=q.get("code_challenge", ""),
            resource=q.get("resource", ""),
            state=state,
            subject=session_sub,
        )
```

REPLACE WITH:
```python
    session = _verify_session(request.cookies.get(_SESSION_COOKIE, ""))
    if session and _operator_key(settings):
        session_sub, approved_clients = session
        # One-click ONLY for a client this admin explicitly approved before (AS-001).
        # Any other client — including an attacker-registered one — falls through to the
        # consent page, so a CSRF'd GET cannot silently mint a code.
        if client_id in approved_clients:
            return _issue_code_redirect(
                client_id=client_id,
                redirect_uri=redirect_uri,
                scope=scope,
                code_challenge=q.get("code_challenge", ""),
                resource=q.get("resource", ""),
                state=state,
                subject=session_sub,
            )
```

### T5 — POST approve accumulates the approved client into the session

FIND (near line 479):
```python
    response = _issue_code_redirect(
        client_id=submitted["client_id"],
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=submitted["code_challenge"],
        resource=submitted["resource"],
        state=state,
        subject=_ADMIN_SUBJECT,
    )
    _set_session_cookie(response, settings)
    return response
```

REPLACE WITH:
```python
    # Carry forward any clients approved earlier in this session, then add this one, so a
    # returning known client stays one-click while a brand-new client still needs consent.
    prior = _verify_session(request.cookies.get(_SESSION_COOKIE, ""))
    prior_clients = prior[1] if prior else ()
    approved_clients = tuple(sorted(set(prior_clients) | {submitted["client_id"]}))

    response = _issue_code_redirect(
        client_id=submitted["client_id"],
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=submitted["code_challenge"],
        resource=submitted["resource"],
        state=state,
        subject=_ADMIN_SUBJECT,
    )
    _set_session_cookie(response, settings, approved_clients)
    return response
```

### T6 — Update the Phase 8 consent-session tests to the new contract (sanctioned)

**File:** `tests/test_oauth_consent_session.py`. Every direct `_sign_session("menhir-admin")`
call must pass the approved client set. Edits:
- `test_valid_session_cookie_one_clicks`: `token = oauth_authorize._sign_session("menhir-admin", (cid,))`.
- `test_expired_cookie_falls_through_to_consent`: `_sign_session("menhir-admin", (cid,))`.
- `test_one_click_still_validates_unknown_client`: `_sign_session("menhir-admin", ("nonexistent",))`.
- `test_one_click_still_validates_redirect_uri`: `_sign_session("menhir-admin", (cid,))`.
- `test_no_operator_key_disables_one_click`: `_sign_session("menhir-admin", (cid,))`.
- `test_garbage_cookie_falls_through_to_consent`: unchanged (garbage token).

**Add** these regression tests (new behavior):
- `test_one_click_denied_for_unapproved_client`: register `cid_a` and `cid_b`; sign a session
  bound to `(cid_a,)`; GET authorize for `cid_b` (valid client + redirect) → **200 consent
  page** (`name="consent_token"` present), NOT a 302 code. This is the AS-001 proof.
- `test_session_cookie_is_samesite_strict`: after `_approve(...)`, assert
  `"samesite=strict" in resp.headers["set-cookie"].lower()`.
- `test_approve_then_reconnect_same_client_one_clicks`: approve `cid` via POST (captures the
  Set-Cookie), then GET authorize for the same `cid` carrying that cookie → 302 with a code.
- `test_approve_accumulates_two_clients`: approve `cid_a`, carry cookie, approve `cid_b`;
  the resulting cookie one-clicks **both** `cid_a` and `cid_b`.

**Verify:** `pytest tests/test_oauth_consent_session.py` all green; the new
`_denied_for_unapproved_client` test fails against the pre-fix code and passes after.

---

## PHASE 2 — AS-002 + AS-004 (P2, Medium): rate-limit unauthenticated AS endpoints + single-use consent token

Do these together — they share one small limiter.

### T7 — Minimal in-process rate limiter

**New file:** `src/menhir/api/oauth_rate_limit.py`. A dependency-free fixed-window counter
keyed by a caller key (client IP), safe under the ASGI event loop + threads:
- `class FixedWindowLimiter:` holds `dict[str, tuple[int_window, int_count]]` under a
  `threading.Lock`; `allow(key: str) -> bool` increments the current window's count and
  returns False once `max_per_window` is exceeded. Window + max are constructor args.
- Helper `client_ip(request) -> str` derived from `request.client.host` (do **not** trust
  `X-Forwarded-For` unless a known proxy is configured — note this in a comment).
- Env knobs with safe defaults: register `MENHIR_OAUTH_AS_REGISTER_RATE` (default e.g. 20 per
  10 min per IP); authorize-POST `MENHIR_OAUTH_AS_APPROVE_RATE` (default e.g. 10 per 5 min).

Keep it minimal and self-contained; no external dependency.

### T8 — AS-002: throttle `/oauth/register`

**File:** `src/menhir/api/oauth_as_register.py`. After the `_as_enabled` gate and before
parsing the body, reject over-limit callers:
```python
if not _register_limiter.allow(client_ip(request)):
    return JSONResponse(status_code=429,
        content={"error": "temporarily_unavailable",
                 "error_description": "Registration rate limit exceeded"})
```
Instantiate one module-level `_register_limiter` from the env knob. Also add a hard ceiling:
if `get_client_store()` count `>= MENHIR_OAUTH_AS_MAX_CLIENTS` (default e.g. 1000), reject
new registrations (add a cheap `count()` to `OAuthClientStore`, or reuse `all()` length).
**Verify:** N+1 rapid registrations → the (N+1)th returns 429; at the ceiling, further
registrations are refused.

### T9 — AS-004a: throttle failed `/oauth/authorize` POSTs

**File:** `src/menhir/api/oauth_authorize.py`. Before the admin-secret compare (step 5), gate
on `_approve_limiter.allow(client_ip(request))`; on over-limit return the consent page (or a
429) without evaluating the secret. Count only reaching the secret check (so denials/invalid
params don't consume budget unfairly is optional). **Verify:** repeated bad-secret POSTs from
one IP get 429 after the threshold.

### T10 — AS-004b: make the consent token single-use

**File:** `src/menhir/api/oauth_authorize.py`. Add a random `jti` to the signed consent
payload (`_sign_consent`), and a small in-memory spent-set with TTL cleanup
(`dict[jti] -> expiry`, pruned on access). In `authorize_post`, after `_verify_consent`
passes, atomically check-and-record the `jti`; reject replays with the same
`_bad_request("... restart the authorization.")`. This closes the "reuse one consent_token to
brute-force the secret for 300s" window. **Verify:** a second POST reusing the same
`consent_token` (even with the correct secret) is rejected as replay.

> Note: `_verify_consent`/`_sign_consent` are covered by `tests/test_oauth_authorize.py`;
> adding a `jti` changes the signed payload shape. If those tests assert the exact payload,
> treat this like T6 (sanctioned contract change) — otherwise leave them untouched.

---

## PHASE 3 — AS-003 (P2, Medium): stable consent/session HMAC secret across workers

**File:** `src/menhir/api/oauth_authorize.py`, `_consent_secret()` (near line 79).

Today an unset `MENHIR_OAUTH_AS_CONSENT_SECRET` falls back to a **per-process** random
`_PROCESS_CONSENT_SECRET`, which breaks consent + one-click under multi-worker deployments.

### T11 — Derive a persistent secret from the signing-key file

Replace the per-process fallback with a value derived from the already-persisted signing-key
file (stable across workers/restarts, and no weaker than the signing key itself, which those
workers all load):
```python
def _consent_secret() -> bytes:
    raw = os.getenv("MENHIR_OAUTH_AS_CONSENT_SECRET")
    if raw:
        return raw.encode("utf-8")
    return _persistent_consent_secret()  # stable, disk-derived; falls back to per-process
```
`_persistent_consent_secret()` reads the bytes of `oauth_as_db_path()/oauth_signing_key.json`
and returns `sha256(b"menhir-as-consent-v1\0" + key_bytes)` (domain-separated), cached in a
module global. If the file cannot be read, fall back to `_PROCESS_CONSENT_SECRET` (single
-worker dev). Do **not** introspect the jose key handle — read the file bytes directly.

### T12 — Operator preflight warning

**File:** `src/menhir/operator_diagnostics.py`. Add a check: when the AS is enabled and
`MENHIR_OAUTH_AS_CONSENT_SECRET` is unset, emit an informational/warn note that multi-worker
deployments must set it explicitly (the disk-derived default covers same-host workers, but an
explicit secret is required for multi-host horizontal scaling). **Verify:** diagnostics
surface the note only when AS-enabled and the env is unset.

---

## PHASE 4 — Low cleanups (P3)

### T13 — AS-005: stop advertising an unimplemented `refresh_token` grant
- **File:** `src/menhir/api/oauth_as_metadata.py:43` — change
  `"grant_types_supported": ["authorization_code", "refresh_token"]` to
  `"grant_types_supported": ["authorization_code"]`.
- **File:** `src/menhir/api/oauth_as_register.py:127` — DCR response
  `"grant_types": ["authorization_code", "refresh_token"]` → `["authorization_code"]`.
  Leave request-side `_SUPPORTED_GRANT_TYPES` tolerant (accepting `refresh_token` in a
  registration request does no harm), or drop it too — but do not start advertising refresh
  until it is implemented.
- **Verify:** metadata + DCR response list only `authorization_code`;
  `tests/test_oauth_as_metadata.py` / `test_oauth_as_register.py` updated only if they
  assert the old list (sanctioned, same rule as T6).

### T14 — AS-006: Windows signing-key file permissions
- **File:** `src/menhir/api/oauth_keys.py` (`load_or_create_signing_key`, near 37-44). Either
  (a) document that the embedded AS is **Linux-only for production** (the `0o600` holds on the
  VPS; Windows use is dev-only) in the module docstring + `.agent/` notes, or (b) on Windows
  set a restrictive ACL via `icacls`/Win32 granting only the owning user. Prefer (a) unless
  Windows becomes a production target.
- **Verify:** documented, or on Windows the key file ACL grants only the owner.

### T15 — AS-007: treat `client_name` as untrusted display metadata
- Confirm operator/telemetry views key identity on the AS-issued `client_id`
  (`secrets.token_hex(8)`), never on the DCR-supplied `client_name`. Every current render
  site already `html.escape`s it (no XSS). This is largely a verification/doc task; add a
  one-line comment at the `client_name` claim assembly in `oauth_token.py` noting it is
  attacker-chosen display metadata, and adjust any diagnostics that group by name (if found).
- **Verify:** no security-relevant decision keys on `client_name`.

---

## PHASE 5 — Verify, document, commit

### T16
1. Run the full OAuth suite from `projects/archolith/menhir/` with
   `-p no:cacheprovider`:
   ```
   pytest -p no:cacheprovider -q \
     tests/test_jose_provider.py tests/test_oauth_keys.py tests/test_oauth_client_store.py \
     tests/test_auth_code_store.py tests/test_oauth_metadata.py tests/test_oauth_as_metadata.py \
     tests/test_oauth_as_register.py tests/test_oauth_jwt_verifier.py tests/test_api_auth.py \
     tests/test_loopback_auth_safety.py tests/test_operator_diagnostics.py \
     tests/test_oauth_operator_preflight.py tests/test_oauth_local_smoke.py \
     tests/test_oauth_authorize.py tests/test_oauth_token.py tests/test_oauth_consent_session.py \
     tests/test_oauth_as_e2e.py tests/test_oauth_as_self_wiring.py
   ```
   Plus the new `tests/test_oauth_rate_limit.py` (T7) if added. All green; only the sanctioned
   files (T6, and T10/T13 if applicable) changed among existing tests.
2. Update `CHANGELOG.md` with a dated entry summarizing the AS security remediation
   (AS-001..AS-007), noting AS-001 as the release-blocker fix.
3. Update the Phase 10 row / acceptance note in
   `.agent/plans/menhir-embedded-oauth-as-plan.md`: once AS-001 lands, acceptance #5
   ("no unremediated High/Critical") is met.
4. Re-run or update the audit doc's status in
   `.agent/reviews/menhir-oauth-as-security-audit-results.md` (mark AS-001 remediated with the
   commit hash).
5. Commit per phase, explicit paths, e.g.
   `fix(oauth-as): AS-001 client-scoped one-click session + SameSite=Strict (CSRF)`.

---

## Acceptance criteria (this plan)

1. A live admin session one-clicks **only** clients the admin explicitly approved; a
   validated-but-unapproved client (incl. attacker-registered) always gets the consent page.
   The session cookie is `SameSite=Strict`.
2. `/oauth/register` and failed `/oauth/authorize` POSTs are rate-limited; the client table is
   bounded; a consent token cannot be replayed.
3. Consent + one-click work deterministically under a multi-worker deployment without an
   explicit `MENHIR_OAUTH_AS_CONSENT_SECRET` (disk-derived default), and preflight warns for
   multi-host scaling.
4. Metadata/DCR advertise only implemented grants; key-file permissions are enforced or
   documented Linux-only; `client_name` is treated as untrusted display metadata.
5. Full OAuth suite green; master-plan acceptance #5 met (no unremediated High/Critical).

## Task dependency summary

- **Phase 1 (T1-T6)** is self-contained and the release blocker; ship it first, even alone.
- T2 depends on T1; T4/T5 depend on T2/T3; T6 depends on T1-T5.
- Phase 2: T8/T9 depend on T7; T10 is independent.
- Phase 3: T12 depends on T11.
- Phase 4 tasks are independent of each other and of Phases 1-3.

## Out of scope

- Refresh-token rotation (still deferred; AS-005 only stops *advertising* it).
- A distributed/shared rate-limiter or client store (in-process limiter is sufficient for the
  single-host VPS target; revisit if multi-host).
- Trusting `X-Forwarded-For` for the rate-limit key (only if a known reverse proxy is put in
  front and explicitly configured).
