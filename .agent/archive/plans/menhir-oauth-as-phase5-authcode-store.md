# Phase 5 — Short-Lived Authorization-Code Store

Parent: `menhir-embedded-oauth-as-plan.md`. Authored after Phase 0
(`../reviews/menhir-oauth-as-interop-findings.md`): **Rung 2a, PKCE S256 mandatory,
public clients only.** Standalone (no dependency on other phases); storage layer only —
no HTTP in this phase. `/authorize` writes to it in Phase 6; `/token` redeems from it in
Phase 7.

**Project:** `projects/archolith/menhir/`.

## Objective

A durable, single-use store for OAuth authorization codes carrying everything `/token`
needs to redeem them safely: PKCE challenge, exact redirect URI, client binding, scope,
resource, subject, and a hard expiry. The security properties (single-use, constant-time
lookup by hash, expiry) live HERE so the endpoint phases stay thin.

## Context / anchors

- **Mirror `src/menhir/api/client_token_store.py` exactly** (the audited pattern):
  `sqlite3.connect(self.db_path)` per op, `db_path.parent.mkdir(parents=True,
  exist_ok=True)`, table created on init, tokens stored **hashed** (same
  `hashlib.sha256` approach), module-level singleton accessor.
- DB location: `oauth_as_db_path() / "oauth_codes.db"`
  (`src/menhir/infrastructure/paths.py:60` — dir-shaped, `MENHIR_OAUTH_AS_DIR` override;
  convention confirmed in Phase 0 findings §6).
- Code generation: `secrets.token_urlsafe(32)`; store `sha256(code)`, return the raw
  code once (same discipline as client-token mint).

## Data model (`oauth_codes` table)

| column | type | notes |
|---|---|---|
| `code_hash` | TEXT PRIMARY KEY | sha256 hex of the raw code |
| `client_id` | TEXT NOT NULL | binding checked at redemption |
| `redirect_uri` | TEXT NOT NULL | exact-match at redemption (OAuth 2.1) |
| `scope` | TEXT NOT NULL | space-joined, already tier-validated by `/authorize` |
| `code_challenge` | TEXT NOT NULL | PKCE S256 challenge (base64url, no padding) |
| `code_challenge_method` | TEXT NOT NULL | always `"S256"` — reject anything else at insert |
| `resource` | TEXT | RFC 8707 resource from the authorize request (may be empty) |
| `subject` | TEXT NOT NULL | the approving identity (admin/operator, Phase 6 decides value) |
| `created_at` | REAL NOT NULL | `time.time()` |
| `expires_at` | REAL NOT NULL | created_at + TTL (default **120s**, env `MENHIR_OAUTH_AS_CODE_TTL_S`) |
| `redeemed_at` | REAL | NULL until redeemed; non-NULL = dead |

## API (class `AuthCodeStore`)

- `issue(*, client_id, redirect_uri, scope, code_challenge, code_challenge_method,
  resource, subject) -> str` — validates method == "S256", inserts, returns raw code.
- `redeem(*, code, client_id, redirect_uri) -> AuthCodeRecord | None` — **atomic
  single-use**: one `UPDATE ... SET redeemed_at = ? WHERE code_hash = ? AND redeemed_at
  IS NULL AND expires_at > ?` then fetch; returns None on any miss (unknown, expired,
  already-redeemed, client_id mismatch, redirect_uri mismatch). Mismatches MUST NOT
  reveal which check failed (single None path). **Replay of an already-redeemed code
  returns None** — Phase 7 maps that to `invalid_grant`.
- `purge_expired() -> int` — housekeeping delete; called opportunistically from `issue`.
- `get_auth_code_store()` singleton accessor mirroring `get_client_token_store()`.

PKCE verification itself (S256(verifier) == challenge, constant-time compare via
`hmac.compare_digest`) is **Phase 7's job at redemption time** — but implement the helper
here as a pure function `verify_pkce(verifier: str, challenge: str) -> bool` next to the
store so it is unit-tested in isolation.

## Tasks

1. `src/menhir/api/auth_code_store.py`: table init, `issue`, `redeem`, `purge_expired`,
   `verify_pkce`, singleton. Meat first: the atomic redeem UPDATE and `verify_pkce`.
2. Tests (`tests/test_auth_code_store.py`):
   - issue→redeem happy path returns the full record; second redeem of same code → None.
   - expired code → None; wrong client_id → None; wrong redirect_uri → None; all three
     indistinguishable from unknown-code.
   - `issue` with method != "S256" raises.
   - `verify_pkce`: RFC 7636 appendix-B test vector passes; wrong verifier fails;
     empty/None verifier fails.
   - two concurrent redeems of one code (threads) → exactly one wins.
3. No server wiring in this phase — the store is inert until Phase 6/7 import it.

## Acceptance criteria

- Suite green; no existing test modified; no HTTP surface added.
- Codes are hashed at rest; raw code never persisted or logged.
- Single-use enforced by the database (atomic UPDATE), not by application locking.
