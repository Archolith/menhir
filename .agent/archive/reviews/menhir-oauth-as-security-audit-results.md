# Menhir Embedded OAuth Authorization Server — Security Audit Results

- **Audit type:** Security (`.agent/audit/security-audit.md`), scoped to the **embedded
  authorization-server** surface (Phases 1-9), not the whole repo. This is the Phase 10
  deliverable of `menhir-embedded-oauth-as-plan.md`.
- **Date:** 2026-07-09
- **Reviewer:** Claude Code (Sonnet), end-to-end read-only pass, single-orchestrator
  (bounded ~9-file surface, no subagent fan-out).
- **Scope (files):** `api/oauth_authorize.py`, `api/oauth_token.py`, `api/oauth_as_register.py`,
  `api/oauth_as_metadata.py`, `api/auth_code_store.py`, `api/oauth_client_store.py`,
  `api/oauth_keys.py`, `api/jose_provider.py`, and the Phase 9 self-wiring in
  `api/oauth.py` (`build_oauth_config`). Cross-checked against `api/auth.py`
  (`BearerAuthMiddleware` path gating).
- **Method:** Traced each unauthenticated entry point end to end — DCR `/oauth/register`
  → `/oauth/authorize` (GET consent + POST approve, incl. Phase 8 one-click cookie) →
  `/oauth/token` (redeem + PKCE + mint) → resource-server verification. Verified the eight
  handoff focus areas: redirect_uri exact-match, PKCE, single-use codes, consent CSRF,
  admin-secret handling, open redirect, token-claim correctness, jose_provider crypto.
- **Out of scope (covered elsewhere):** the resource-server verifier hardening (S-001..S-009)
  in `.agent/reviews/` root `menhir-oauth-security-audit-results.md`; this audit does not
  re-litigate those.

## Remediation status (2026-07-09)

All seven findings are **REMEDIATED** per
`.agent/plans/menhir-oauth-as-security-remediation-plan.md`:

| Finding | Sev | Commit | Summary of fix |
|---|---|---|---|
| AS-001 | High | `032b46d` | Client-scoped one-click session (approved-`client_id` set) + `SameSite=Strict`. |
| AS-002 | Med | `23ac40f` | Per-IP rate limit + hard client ceiling on `/oauth/register`. |
| AS-004 | Med | `23ac40f` | Approve-POST throttle + single-use consent token (`jti` spent-set). |
| AS-003 | Med | `b91bcc1` | Consent/session HMAC secret derived from the signing-key file (stable across workers) + preflight warning. |
| AS-005 | Low | `0597792` | Metadata + DCR advertise only `authorization_code`. |
| AS-006 | Low | `0597792` | `0o600` key mode documented Linux-VPS-only; Windows AS dev-only. |
| AS-007 | Low | `0597792` | `client_name` marked untrusted display metadata; identity keys on `client_id`. |

Full OAuth suite after remediation: **299 passed / 1 skipped**. Master-plan acceptance #5
("no unremediated High/Critical") is now met.

## Review Confidence

**86 / 100.** High confidence for every finding below — each is a direct read of current
source at the cited line with a concrete trigger, and the load-bearing finding (AS-001) was
traced end to end (cookie attributes → one-click issuance path → open DCR). Deductions: the
CSRF vector (AS-001) is reasoned from the documented `SameSite=Lax` send semantics, **not**
exercised against a live browser; and multi-worker/entropy severities (AS-003, AS-004) depend
on deployment posture (Open Questions) not verifiable from code alone. No live-connector
(ChatGPT / claude.ai) exercise. Verified vs reported: **7 findings, 7 verified.**

## Executive Summary

The embedded AS is well-built and, on the classic OAuth attack surface, **holds up**:
redirect_uri matching is exact (no prefix/substring), the untrusted-target/open-redirect
dichotomy is correct (unknown client or unregistered redirect → direct 400, never a
redirect), PKCE is required and S256-only with a constant-time verify, authorization codes
are single-use via an atomic SQL UPDATE (and burned even on a wrong binding), the consent
integrity token is HMAC-signed and TTL-bound, the admin secret is compared in constant time
and an unconfigured operator key can never approve, all HTML output is escaped (XSS-safe even
for the attacker-chosen `client_name`), secrets/codes are stored only as sha256 hashes, and
the crypto path (RS256, 2048-bit RSA, thumbprint `kid`, algorithm allowlist, no private
material in the public JWKS) is sound. No injection, no secret logging, no unauthenticated
data access, no custom crypto were found.

One finding is **release-blocking for public exposure**: **AS-001** — the Phase 8 "true
one-click" consent session is a *blanket* admin session (it records only that *some* approval
happened, not *which client* was approved) carried on a `SameSite=Lax` cookie. Combined with
open Dynamic Client Registration, a cross-site top-level GET can silently mint an
authorization code to an attacker-registered client during the session window, which the
attacker exchanges (with their own PKCE verifier) for an **operator-tier** token. Fix before
turning the AS on for any internet-reachable deployment.

The remaining findings are availability/abuse and operability hardening (open DCR growth,
per-process HMAC secret under multi-worker, admin-secret brute-force window) plus two low
correctness/portability nits (advertised-but-unimplemented refresh_token grant; Windows key
file mode).

---

## Findings

### AS-001 (High): One-click consent session is client-agnostic + SameSite=Lax → CSRF to operator-tier token
- **Severity:** High (privilege escalation to operator via CSRF; conditional on an active admin session window)
- **CWE:** CWE-352 (Cross-Site Request Forgery) + CWE-613 (Insufficient Session Expiration scope) / CWE-863 (Incorrect Authorization).
- **Files:**
  - `api/oauth_authorize.py:327-336` (`_set_session_cookie` — `samesite="lax"`, `path="/oauth/authorize"`).
  - `api/oauth_authorize.py:289-294` (`_sign_session` — payload is `{"kind":"session","sub":<admin>,"iat":...}`; **no client_id binding**).
  - `api/oauth_authorize.py:399-412` (GET one-click branch: a valid session cookie + configured operator key issues a code for **any** validated client with no re-consent).
- **Status:** CONFIRMED (end-to-end trace).
- **Detail:** After the first admin approval, `_set_session_cookie` stores a signed cookie
  whose payload binds only the admin subject — not the approved `client_id`(s). On any later
  `GET /oauth/authorize`, once params validate, lines 402-412 issue a code and 302 it to the
  request's `redirect_uri` **without showing consent**, for *any* client. Because the cookie
  is `SameSite=Lax`, it is sent on cross-site **top-level GET navigations**. Dynamic Client
  Registration (`/oauth/register`) is open and unauthenticated (by design), so an attacker
  can register their own public client with their own `redirect_uri` and known PKCE
  `code_verifier`.
- **Attack scenario:** (1) Attacker registers client `C_evil` via `/oauth/register` with
  `redirect_uri=https://evil.tld/cb`. (2) Within the session TTL (`MENHIR_OAUTH_AS_SESSION_TTL_S`,
  default 600s) after any legitimate admin approval, the attacker lures the admin's browser to
  a page that top-level-navigates to
  `https://<menhir>/oauth/authorize?response_type=code&client_id=C_evil&redirect_uri=https://evil.tld/cb&code_challenge=<attacker>&code_challenge_method=S256&scope=menhir:admin`.
  (3) The Lax session cookie rides the navigation; the one-click branch issues a code and
  302s the admin's browser to `https://evil.tld/cb?code=…`, delivering the code to the
  attacker's server. (4) The attacker exchanges it at `/oauth/token` with their own verifier
  → an **operator-tier** JWT (full admin scope). No admin secret is ever re-entered.
- **Remediation (any one closes it; do the first two together):**
  1. **Bind the session to approved clients.** Store the set of explicitly-approved
     `client_id`s in the session payload; the one-click branch must issue silently *only* for
     a `client_id` in that set. A never-before-approved client always renders the consent page
     (matching how mainstream IdPs scope "remember this app"). This removes the "approve one →
     auto-approve all" flaw regardless of cookie policy.
  2. **`SameSite=Strict`** for `menhir_as_session` (it is only ever used first-party on the
     authorize page; Strict does not harm the legitimate flow and blocks the cross-site send).
  3. Optionally add a per-request CSRF nonce to the one-click GET, or require the consent page
     (never one-click) when the `Sec-Fetch-Site` header is not `same-origin`.
- **Verification:** With (1), a `GET /oauth/authorize` carrying a valid session cookie for a
  `client_id` not in the session's approved set renders consent (HTTP 200 form) instead of
  302-with-code. With (2), the cookie is absent on a cross-site top-level GET.

### AS-002 (Medium): Unauthenticated open DCR has no rate limit or cap → client-table resource exhaustion
- **Severity:** Medium (availability / abuse)
- **CWE:** CWE-770 (Allocation of Resources Without Limits or Throttling).
- **File:** `api/oauth_as_register.py:51-118` (`register_client` — no throttle, no per-IP or
  global cap; each call `INSERT`s a row into `oauth_clients`).
- **Status:** CONFIRMED.
- **Detail:** DCR is unauthenticated by spec (required for ChatGPT/claude.ai one-click), but
  the handler applies only per-request shape limits (≤5 redirect_uris, ≤255-char name). There
  is no rate limit and no ceiling on total registered clients, so an unauthenticated attacker
  can register unbounded clients, growing `menhir_oauth_as.db` without limit and enlarging the
  set of attacker-controlled clients usable in AS-001.
- **Attack scenario:** A script POSTs valid registration bodies in a loop; the client table
  grows without bound (disk exhaustion / DB bloat) and never expires (there is no unused-client
  GC — `purge_expired` exists only for `oauth_codes`, not `oauth_clients`).
- **Remediation:** Rate-limit `/oauth/register` (per-IP token bucket) and/or cap total
  clients with an LRU/TTL reaper for clients that never complete an authorize+token exchange.
  Reject once a configurable ceiling is hit. Pairs with the AS-001 fix (fewer stale
  attacker clients to abuse).
- **Verification:** After the limit, further registrations return 429/400; the client count
  is bounded.

### AS-003 (Medium): Per-process random HMAC secret breaks consent + one-click under multi-worker deployment
- **Severity:** Medium (availability / correctness under horizontal scaling)
- **CWE:** CWE-757 (Selection of Less-Secure Algorithm During Negotiation) / operational
  correctness (non-deterministic failure).
- **File:** `api/oauth_authorize.py:63` (`_PROCESS_CONSENT_SECRET = secrets.token_bytes(32)`)
  consumed by `_consent_secret()` (`:79-83`), `_sign_consent`/`_verify_consent`,
  `_sign_session`/`_verify_session`.
- **Status:** CONFIRMED.
- **Detail:** When `MENHIR_OAUTH_AS_CONSENT_SECRET` is **unset**, the HMAC key for both the
  consent-integrity token and the Phase 8 session cookie is a per-process random value. Under
  a multi-worker server (e.g. `uvicorn --workers N`, gunicorn), the GET that renders the
  consent token and the POST that verifies it may land on **different workers with different
  secrets**, so `_verify_consent` fails and the flow dies with "Consent request is invalid or
  has expired" non-deterministically. Session cookies (one-click) fail across workers the same
  way. The signing *key* is fine (loaded from disk, stable across workers); only this HMAC
  secret is per-process. Single-worker deployments are unaffected, which is why the test suite
  and local smoke pass.
- **Remediation:** When the AS is enabled, require (or derive-and-persist) a stable
  `MENHIR_OAUTH_AS_CONSENT_SECRET` — e.g. read it from the same on-disk secret material as the
  signing key, or fail preflight if multi-worker is configured without it. Add an operator
  preflight check.
- **Verification:** Two verifier instances constructed with distinct process secrets reject
  each other's consent tokens; with a shared env secret they interoperate.

### AS-004 (Medium): No brute-force throttle on the admin secret; one consent token is replayable for its full TTL
- **Severity:** Medium (authorization brute-force; bounded by operator-key entropy)
- **CWE:** CWE-307 (Improper Restriction of Excessive Authentication Attempts).
- **File:** `api/oauth_authorize.py:466-477` (admin-secret check) + `:116-144` (`_verify_consent`
  is **not** single-use — the same `consent_token` validates repeatedly until its
  `MENHIR_OAUTH_AS_CONSENT_TTL_S` (default 300s) expires).
- **Status:** CONFIRMED.
- **Detail:** The admin-secret compare is constant-time (`hmac.compare_digest`, good), but the
  POST handler applies no attempt throttle/lockout, and a single valid `consent_token`
  (obtainable by any GET with a registered `client_id`) can be reused for its 300s lifetime.
  An attacker can therefore submit unlimited `admin_secret` guesses within the window. Risk is
  bounded by `MENHIR_OPERATOR_KEY` entropy (meant to be high), so this is only exploitable
  against a weak operator key — but there is no defense in depth.
- **Remediation:** Rate-limit failed `/oauth/authorize` POSTs (per-IP and/or per-consent-token
  attempt counter), make the consent token single-use (bind a nonce recorded server-side, or
  fold in a short counter), and document a minimum operator-key entropy.
- **Verification:** After K failed admin-secret attempts against one consent token, further
  attempts are rejected (429) regardless of guess.

### AS-005 (Low): Metadata + DCR response advertise a `refresh_token` grant the token endpoint does not implement
- **Severity:** Low (interop/correctness; no security impact)
- **CWE:** CWE-684 (Incorrect Provision of Specified Functionality).
- **Files:** `api/oauth_as_metadata.py:43` (`"grant_types_supported": ["authorization_code","refresh_token"]`)
  and `api/oauth_as_register.py:127` (DCR response `"grant_types": ["authorization_code","refresh_token"]`),
  vs. `api/oauth_token.py:67-69` (only `authorization_code`; anything else → `unsupported_grant_type`).
- **Status:** CONFIRMED. Refresh tokens are explicitly out of scope per the master plan, yet
  both advertisements claim support. A conformant client may attempt a refresh and get a
  400 `unsupported_grant_type`.
- **Remediation:** Drop `refresh_token` from both advertisements until it is implemented (or
  implement short-rotation refresh). One-line change in each site.
- **Verification:** Metadata and DCR responses list only `authorization_code`.

### AS-006 (Low): Signing-key file mode `0o600` is not enforced on Windows
- **Severity:** Low (local-only; Linux/VPS unaffected)
- **CWE:** CWE-276 (Incorrect Default Permissions).
- **File:** `api/oauth_keys.py:37-44` (`os.open(..., 0o600)` + best-effort `os.chmod`, whose
  `OSError` is swallowed). The existing skipped test confirms the `0o600` assertion is a
  Windows no-op.
- **Status:** CONFIRMED. On Windows, POSIX mode bits are effectively ignored, so
  `oauth_signing_key.json` (the RSA **private** JWK) is created with default ACLs. Any local
  user/process able to read the file can forge tokens. On the Linux VPS the `0o600` holds.
- **Remediation:** If Windows is ever a production target for the AS, set an explicit
  restrictive ACL via `icacls`/Win32 on key creation; otherwise document that the embedded AS
  is Linux-only for production and keep local Windows use dev-only.
- **Verification:** On Windows, the key file's ACL grants only the owning user.

### AS-007 (Low / informational): Minted `client_name` claim is attacker-chosen at DCR time
- **Severity:** Low (attribution integrity; not a privilege boundary)
- **CWE:** CWE-345 (Insufficient Verification of Data Authenticity — display identity).
- **Files:** `api/oauth_as_register.py:101-104` (client_name taken from the DCR body, trimmed
  to 255 chars) → stored → `api/oauth_token.py:102-113` copies it into the token's
  `client_name` claim → surfaces in session/client telemetry.
- **Status:** CONFIRMED. Tier is derived from **scope**, not `client_name`, so this is not an
  escalation — but any audit/telemetry view keyed on `client_name` shows an attacker-chosen,
  possibly-colliding label (e.g. registering a client named "Claude Code"). It is the DCR-side
  analogue of the RS audit's S-004 header-spoofing note. The value is HTML-escaped at every
  render site (no XSS).
- **Remediation:** Treat `client_name` as untrusted display metadata everywhere (already
  escaped for HTML); where provenance must be trustworthy, key on the AS-issued `client_id`
  (unguessable `secrets.token_hex(8)`), not the name. Optionally de-duplicate or tag
  DCR-supplied names in operator views.
- **Verification:** Telemetry/audit joins use `client_id`, not `client_name`, as the identity key.

---

## Focus-area verdicts (handoff checklist)

| Focus area | Verdict | Evidence |
|---|---|---|
| redirect_uri exact-match | **PASS** | `oauth_authorize.py:203` — `redirect_uri not in client.redirect_uris` (exact tuple membership, no prefix/substring); DCR restricts to https or loopback-http (`oauth_as_register.py:31-43`). |
| PKCE required / correct | **PASS** | authorize requires `code_challenge` + `S256` only (`:221-229`); token verifies via `verify_pkce` constant-time (`auth_code_store.py:33-43`); `issue` rejects non-S256 (`:124-125`). |
| Single-use codes | **PASS** | atomic `UPDATE … WHERE redeemed_at IS NULL AND expires_at > ?` + `rowcount==1` (`auth_code_store.py:178-198`); burned even on wrong binding. |
| Consent CSRF | **PARTIAL → AS-001** | Stateless HMAC integrity token is sound for the *password* path (`:116-144`), but the Phase 8 one-click session is client-agnostic + Lax → CSRF (**AS-001**). |
| Admin-secret handling | **PASS (w/ AS-004)** | constant-time compare, unconfigured key cannot approve (`:466-477`); no brute-force throttle (**AS-004**). |
| Open redirect | **PASS** | untrusted target → direct 400 (`:169-176,382-386`); only a proven redirect_uri gets a 302 (`:388-397`). |
| Token-claim correctness | **PASS** | `iss`=base, `aud`=`{base}/mcp-http`, `sub`=admin, scope from code, `exp` (`oauth_token.py:105-118`); matches verifier's iss/exp/aud checks (`oauth.py:394-400`). |
| jose_provider crypto path | **PASS** | RS256, 2048-bit RSA, RFC-7638 `kid`, algorithm allowlist enforced in `verify_jwt(algorithms=…)` (`jose_provider.py:59-76`); `public_jwks` asserts no `d` (`oauth_keys.py:57`). |

## What was checked and found clean

- **AS endpoints correctly unauthenticated by spec:** `BearerAuthMiddleware` protects only
  `/api/*` and `/mcp`,`/mcp/*`,`/mcp-http` (`auth.py:40,208`); `/oauth/*` and `/.well-known/*`
  pass through, as required for discovery/DCR/authorize/token. No sensitive data is emitted by
  the metadata endpoints (issuer/endpoint URLs only).
- **No secret/code/token logging** anywhere in `oauth_authorize.py` or `oauth_token.py`
  (grep clean); codes and client secrets are persisted only as sha256 hashes
  (`auth_code_store.py:28-30`, `oauth_client_store.py:24-26`).
- **XSS-safe consent page:** every rendered value (`client_name`, `client_id`, scope,
  `redirect_uri`, hidden fields) is `html.escape(..., quote=True)` (`oauth_authorize.py:237-272`);
  the attacker-controlled DCR `client_name` cannot break out.
- **Domain separation** between the consent-integrity token and the session cookie via a
  `kind:"session"` tag prevents cross-replay (`:316`).
- **Scope confinement:** DCR grants only scopes ⊆ `scopes_supported` (`oauth_as_register.py:92-99`);
  authorize rejects a requested scope outside the client's grant (`:208-218`).
- **Phase 9 self-wiring fails closed:** with the AS flag on but no `MENHIR_PUBLIC_BASE_URL`,
  issuer/JWKS default to empty and the verifier rejects on its missing-issuer check rather
  than trusting a guess (`oauth.py` build + `test_oauth_as_self_wiring.py`).
- **Signing-key hygiene:** private material is never serialized into the public JWKS
  (defensive assert), key generated with a stable thumbprint `kid`.

## Dependency / crypto register

- **joserfc** — sole JOSE library, confined to `api/jose_provider.py`; RS256 only in the AS
  path. No `alg=none`, no HS/RS confusion (allowlist passed explicitly to `decode`). No CVE
  scan run in this audit (see Open Questions).
- **sqlite3 / hashlib / hmac / secrets** — stdlib; correct primitives (sha256 hashes,
  `compare_digest`, `token_urlsafe`/`token_hex` CSPRNG).

## Open questions / not independently verified

- **Deployment worker model** — is Menhir ever run with more than one worker/process? Sets the
  real severity of AS-003 (harmless single-worker, breaking multi-worker).
- **Operator-key entropy policy** — determines the practical exploitability of AS-004.
- **Windows-as-production** — determines whether AS-006 matters (assumed Linux VPS only).
- **Live CSRF PoC** — AS-001 is reasoned from `SameSite=Lax` send semantics, not exercised in
  a real browser; recommend a live PoC before/after the fix.
- **Dependency CVE scan** (OSV/Snyk on joserfc/httpx/fastapi/starlette) not run here.
- **No live connector run** (ChatGPT / claude.ai one-click) against a hosted instance.

## Recommended remediation order

1. **AS-001** (client-scoped one-click session + `SameSite=Strict`) — release blocker before
   any internet-reachable AS deployment. Feeds a plan in `.agent/plans/`.
2. **AS-002 / AS-004** (rate-limit open DCR and the consent POST; single-use consent token) —
   abuse/brute-force hardening; do together (shared rate-limit machinery).
3. **AS-003** (stable `MENHIR_OAUTH_AS_CONSENT_SECRET` + preflight check) — before any
   multi-worker rollout.
4. **AS-005 / AS-006 / AS-007** (drop unadvertised refresh grant; Windows key ACL or
   Linux-only note; client_name-as-untrusted) — low-severity cleanups.

Acceptance criterion #5 of the master plan ("Phase 10 audit finds no unremediated
High/Critical") is **now met (2026-07-09)**: AS-001 (High) is remediated in commit `032b46d`
(client-scoped one-click session + `SameSite=Strict`), and all Medium/Low findings
(AS-002..AS-007) are remediated as well — see the Remediation status table at the top of this
document. The AS is launch-ready for public exposure from the security-audit standpoint.
