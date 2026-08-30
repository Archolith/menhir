# Menhir production deployment

Operators should follow the ordered [live VPS deployment playbook](LIVE_VPS_PLAYBOOK.md).
This document is the detailed production contract behind that workflow.

Production container/deployment contract. Separate from the test stacks in this
directory (`docker-compose.test.yml`, `docker-compose.full.yml`) and from the
old source-building `Dockerfile` behavior. Production is image-immutable and
hardened; there is no source build on the VPS.

Files:

| File | Purpose |
|------|---------|
| `docker-compose.production.yml` | Compose project `menhir-prod`: exactly `menhir` + `neo4j`. |
| `Dockerfile` | Controlled-CI image (digest-pinned base, offline wheelhouse, fixed UID/GID, `/livez` healthcheck, secret-file entrypoint). |
| `backup-generation.sh` | Create one versioned, verifiable backup generation; requires the off-host/WORM upload wrapper receipt. |
| `menhir-backup-upload-contabo.sh` | Contabo S3 upload wrapper: client-side encrypt, AES256 + Object Lock COMPLIANCE >=30d, exact-version head/readback/hash verification, delete-denial check, atomic fsync receipt, then plaintext removal. |
| `durable-state-inventory.json` | Exhaustive capture/restore classification for every persistent production bind. |
| `installed-artifacts.json` | Canonical exact destination set that the release author and installed verifier both enforce. |
| `restore-generation.sh` | Rehearsal-first, guarded restore; writes/requires a rehearsal receipt. |
| `candidate-deploy.sh` | Start the isolated, restored candidate generation. |
| `candidate-accept.sh` | Candidate acceptance verifier + structured acceptance receipt. |
| `promote.sh` | Validate the release authority and exact receipts (never mtime), then start production and record the mutation marker. |
| `rollback.sh` | Pre-mutation route rollback; refuses post-mutation rollback without a verified reverse-generation receipt. |
| `release-lib.sh` | Fixed paths, root-file validation, health checks, release/receipt/mutation-marker helpers. |
| `secrets-map.sh` | Explicit secret owner/mode map + enforce/verify (GID 10001 vs 7474). |
| `release-validate.sh` | Validate the immutable `release.json`. |
| `release-author.py` | Creates a digest-bound security-review request, then authors the immutable release only from matching independent approval and clean canonical inputs. |
| `release.json.example` | Shape reference; the real artifact map contains every destination in `installed-artifacts.json`. |
| `security-review.json.example` | Mandatory independent review attestation; exact authority digest, full scope, APPROVED verdict, and zero unresolved critical/high findings. |
| `lib/menhir_schema.py` | Strict duplicate-key-rejecting schemas for manifest/release/receipts. |
| `lib/make_manifest.py` | Strict generation manifest writer (exact set equality + classification). |
| `production.env.example` | Non-secret root-owned release/image/provider configuration template. |

All host paths below assume the fixed roots (`/srv/menhir/production` and
`/srv/menhir/backups`); every one is overridable via the documented env vars.

No production command accepts a release without the mandatory security review
embedded in `release.json`. The strict validator recomputes its authority digest
before bootstrap, backup, candidate, restore, promotion, rollback, and runtime
binding checks; a missing, stale, self-authored, rejected, incomplete, or
critical/high-open review fails closed before production mutation.

## 1. Prerequisite: external network and reverse proxy

The stack publishes no host ports. A reverse proxy must already exist on a
Docker network named exactly `menhir-proxy`:

```bash
docker network create \
  --subnet 172.30.0.0/24 \
  --gateway 172.30.0.1 \
  menhir-proxy
```

`menhir` joins `menhir-proxy` under the alias `menhir-prod-app` (proxy forwards
to `menhir-prod-app:8099`). `neo4j` joins only the `internal: true` network (no
egress, so it cannot download plugins - APOC is not required by Menhir's Cypher
and is intentionally not installed). Menhir's outbound LLM/embedding calls leave
via `menhir-proxy`, which must have internet egress.

### Cloudflare Tunnel alternative

`docker-compose.cloudflared.yml` is the hardened, separate ingress project for
hosts that use a locally-managed Cloudflare Tunnel instead of the shared Caddy
origin. Copy `cloudflared.production.yml.example` to the host's root-owned
`/srv/menhir/production/ingress/cloudflared-config.yml`, replace the tunnel UUID,
and install the tunnel credential as
`/srv/menhir/production/secrets/cloudflare/credentials.json`. Both files must be
`root:65532 0440`; their parent directories must be `root:65532 0750`.

The tunnel is fixed at `172.30.0.2`, the sole trusted proxy peer, and forwards to
`menhir-prod-app:8099`. Its path expression is an authoritative public allowlist:
MCP, OAuth, metadata, and health routes pass; all other paths return a tunnel-side
404 and never reach Menhir. Validate the rendered rules before starting it:

```bash
cloudflared tunnel --config cloudflared-config.yml ingress validate
docker compose -p menhir-ingress -f deploy/docker-compose.cloudflared.yml config --quiet
docker compose -p menhir-ingress -f deploy/docker-compose.cloudflared.yml up -d
```

When managing DNS from a workstation that also runs another tunnel, pass a
management config that does not contain a default `tunnel:` field. Otherwise
`cloudflared` can silently target that default instead of the tunnel named on
the command line. Verify the selected UUID with `cloudflared tunnel info` before
changing the CNAME.

## 2. Secret ownership (fixed-path, read-only, never in env or Git)

Secrets are files under `/srv/menhir/production/secrets/`, grouped by consumer
and mounted read-only into `/run/secrets` (and referenced via the compose
`secrets:` block for Neo4j). No credential is ever required in the host
environment, written into the compose file, or rendered by `docker compose
config` or `ps` argv.

| Path (under `secrets/`) | Exposed as | Required | Consumed by |
|-------------------------|------------|----------|-------------|
| `neo4j/neo4j-auth` | `NEO4J_AUTH_FILE` (`neo4j/<password>`) | yes | Neo4j server auth |
| `menhir/neo4j-password` | `NEO4J_PASSWORD` | yes | menhir app connection to Neo4j |
| `oauth/oauth_signing_key.json` | `MENHIR_OAUTH_SIGNING_KEY_PATH` | yes | OAuth signing key |
| `oauth/retry-response-keyring.json` | `MENHIR_OAUTH_REFRESH_RETRY_KEYRING_PATH` | yes | refresh retry keyring |
| `oauth/oauth-consent-secret` | `MENHIR_OAUTH_AS_CONSENT_SECRET` | yes | consent HMAC |
| `menhir/openai-api-key` / `gemini-api-key` / `local-llm-api-key` | `OPENAI_API_KEY` / `GEMINI_API_KEY` / `LOCAL_LLM_API_KEY` | as configured | LLM providers |
| `menhir/operator-key` | `MENHIR_OPERATOR_KEY` | yes | OAuth approval / operator tier |
| `menhir/source-fence-token` | `MENHIR_SOURCE_FENCE_TOKEN` | yes | authenticated old-source cutover proof; dedicated to this purpose |
| `menhir/api-key` / `agent-key` / `readonly-key` | `MENHIR_API_KEY` / `MENHIR_AGENT_KEY` / `MENHIR_READONLY_KEY` | optional | static bearer keys |

The OAuth signing key and retry keyring live under `secrets/oauth/` and are
never part of the writable state tree; provisioning/rotation is an operator
action there (the mount is read-only, so the app never rewrites them).

Permissions: `root:10001 0440` (the menhir runtime user is UID/GID 10001). The
Neo4j `secrets:` file is read by the Neo4j entrypoint/healthcheck. Create the
tree and files, then:

```bash
chown -R root:10001 /srv/menhir/production/secrets
chmod -R u=rwX,g=rX,o= /srv/menhir/production/secrets
```

The menhir image entrypoint maps the `menhir/` files to env without logging
them; Neo4j reads `neo4j/neo4j-auth` through its native `NEO4J_AUTH_FILE`
mechanism (no credential in argv or env).

State dirs under `/srv/menhir/production/state/` must be owned by the matching
container users:

| Path | Owner |
|------|-------|
| `state/oauth/` and `state/telemetry/` | `10001:10001` |
| `state/neo4j/{data,logs}/` | `7474:7474` |

## 3. Immutable client policy (read-only policy mount)

`MENHIR_STARTUP_SCOPE=production` requires an immutable OAuth client policy: a
version-1 JSON file at `/srv/menhir/production/policy/client-policy.json` whose
SHA-256 (of the canonical, `canonical_digest`-stripped payload) must equal both
its embedded `canonical_digest` and `MENHIR_CLIENT_POLICY_DIGEST` (64 lowercase
hex chars). Format is defined in `src/menhir/api/client_policy.py`
(`load_client_policy`). The file is mounted read-only and its directory is
`root:root 0444`.

Tool authority is resolved from the exact OAuth `client_id`. Hosted ChatGPT and
Claude clients are operator-tier and receive 51 of 54 tools, including document
and project ingestion. Only namespace-wide deletion and client credential
administration remain outside their connector authority. Agent Smith clients receive the smaller
managed-workspace set named by their shared instructions, including the read-only
`list_todos`; Codex alone additionally receives `add_memory_and_track`, which its
generated MCP configuration explicitly pins.
Consent is client-scoped: no client inherits another client's approval, even when
their current tool sets overlap. `ingest_project` remains denied to the narrower
Agent Smith clients and available to the hosted operator clients.
The release invalidates pre-change consent-session cookies. Existing hosted-client
access and refresh tokens lack the newly required `menhir:admin` scope and fail the
exact policy check, forcing a fresh authorization for the operator grant.

## 4. Bringing up production (authoritative, read-write)

Build `deploy/python-base.Dockerfile` in the controlled release environment,
scan and publish that image, and pass its immutable registry digest as
`PYTHON_BASE` when building `deploy/Dockerfile`. The base recipe deliberately
pins both the upstream image and upgraded OpenSSL package versions; the final
base-image digest and its scan are part of the release authority.

Build, scan, and publish `deploy/neo4j-base.Dockerfile` the same way and use
that output digest for `NEO4J_IMAGE`. It preserves the reviewed Neo4j release
while applying every OS-package security fix available at release time.

```bash
export MENHIR_IMAGE="<registry>/menhir:<version>@sha256:<digest>"
export NEO4J_IMAGE="neo4j:5-community@sha256:<digest>"
export MENHIR_RUNTIME_MODE=production
export MENHIR_PUBLIC_BASE_URL=https://memory.ctharvey.me
export MENHIR_CLIENT_POLICY_DIGEST=<64-hex-sha256>
export LLM_CHAT_PROVIDER=openai
export GRAPHITI_LLM_PROVIDER=openai
export GRAPHITI_EMBED_PROVIDER=openai
docker compose -f deploy/docker-compose.production.yml up -d
```

No `NEO4J_PASSWORD` in the environment: it is read from the mounted secret file.
Health: `/livez` (liveness) and `/readyz` (readiness) via the proxy.

For Cloudflare Tunnel ingress, start from
`deploy/cloudflared-config.production.yml.example`. Its allowlist includes the
Agent Smith CIMD document as well as the MCP, OAuth, discovery, and health
surfaces; keep the catch-all 404 rules last.

## 5. Candidate-readonly (isolated proof boundary)

`candidate-readonly` serves existing-token reads while never admitting authority
mutations. The candidate graph/OAuth/telemetry writes are ISOLATED into a
disposable candidate state root; the authoritative OAuth/policy/key inputs are
mounted read-only. The app also raises its mutation fence (OAuth-authority
mutation routes return 503; scheduler/enrichment disabled) regardless of mounts.

```bash
export MENHIR_RUNTIME_MODE=candidate-readonly
export MENHIR_STATE_ROOT=/srv/menhir/candidate/state   # isolated clone / disposable boundary
export MENHIR_AUTHORITIES_READ_ONLY=true
# (plus MENHIR_IMAGE, NEO4J_IMAGE, MENHIR_PUBLIC_BASE_URL,
#  MENHIR_CLIENT_POLICY_DIGEST, and provider settings as in production)

# Prepare the isolated candidate Neo4j clone before first candidate up:
mkdir -p /srv/menhir/candidate/state/neo4j/{data,logs}
chown -R 7474:7474 /srv/menhir/candidate/state/neo4j
# (clone authoritative data, or start empty as a disposable generation boundary)

docker compose -f deploy/docker-compose.production.yml up -d
```

In this mode: Neo4j data/logs bind to `MENHIR_STATE_ROOT` (candidate clone,
never production); telemetry writes bind to `MENHIR_STATE_ROOT/telemetry` (a
disposable sink, never the authoritative telemetry); OAuth (`state/oauth`) is
mounted read-only and `MENHIR_OAUTH_AS_DIR` still points at the authoritative
signing key; policy and secrets are read-only. To reset: `docker compose down`
and `rm -rf /srv/menhir/candidate`.

The commands above explain the Compose mode; they are not the release cutover
procedure. For a real release, use the fixed root-owned operation wrappers.
`candidate-deploy.sh` starts candidate Neo4j first, computes and fsyncs a
release/generation-bound digest over restored OAuth, telemetry, secrets,
policy, and complete Neo4j content, and only then starts Menhir. After the
acceptance probes, `candidate-accept.sh` recomputes the same authority and
refuses any change. This prevents app startup from occurring before the
acceptance baseline exists.

## 6. Resource budget

| Service | CPU | Memory | Notes |
|---------|-----|--------|-------|
| `neo4j` | 2 | 4 GiB | ~1 GiB heap + ~1.5 GiB page cache + off-heap/OS |
| `menhir` | 1 | 2 GiB | API + enrichment worker budget |

Menhir runs with a read-only root; both services use `tmpfs` scratch,
`cap_drop: ALL`,
`no-new-privileges`, fixed users, `pids_limit`, `nofile` ulimit, and capped JSON
log rotation. Documented exception: the official Neo4j image keeps its
ephemeral container overlay writable because its entrypoint materializes
environment-backed config. Only `/data` and `/logs` persist to the host.

## 7. Backup generation (encrypted off-host WORM is REQUIRED)

Run as root, with the same environment as `docker compose up`. Requires all
secrets/policy to be present (`secrets/neo4j/neo4j-auth`,
`secrets/menhir/neo4j-password`, `secrets/menhir/operator-key`,
`secrets/menhir/source-fence-token`, `secrets/oauth/oauth_signing_key.json`,
`secrets/oauth/retry-response-keyring.json`,
`secrets/oauth/oauth-consent-secret`, `policy/client-policy.json`; provider
keys are required according to the configured providers). It quiesces the stack,
dumps Neo4j `neo4j` + `system` as UID/GID 7474, snapshots OAuth/telemetry with
WAL-safe SQLite checkpoint + integrity proof, copies secrets (hashed only),
policy, and release manifests, then writes `SHA256SUMS`, a strict-JSON
`MANIFEST.json`, and a `COMPLETE` marker.

```bash
export MENHIR_IMAGE=... NEO4J_IMAGE=... MENHIR_RUNTIME_MODE=production \
       MENHIR_PUBLIC_BASE_URL=... MENHIR_CLIENT_POLICY_DIGEST=... \
       LLM_CHAT_PROVIDER=... GRAPHITI_LLM_PROVIDER=... GRAPHITI_EMBED_PROVIDER=...
deploy/backup-generation.sh
```

Failure leaves the stack **stopped**. It is restarted only after a fully
successful, wrapper-uploaded generation, and a restart failure is fatal. The
script **fails closed** unless a root-owned wrapper is executable at
`/usr/local/sbin/menhir-backup-upload` (override `MENHIR_BACKUP_WRAPPER`). The
wrapper contract, invoked by the fixed operations worker with its exact
`MENHIR_OPERATION_JOB_ID` as
`menhir-backup-upload <generation-absolute-path>`:
create a tar archive -> encrypt it locally to an age X25519 recipient -> remove
the unencrypted archive -> upload the `.tar.gz.age` object -> verify exact
object identity/immutability/WORM retention -> durably journal plaintext
generation cleanup -> finalize the receipt, exiting 0 only on success. A kill
during cleanup is resumed with `menhir-backup-upload --resume-cleanup`;
promotion remains blocked while `plaintext_removed=false`.

For Contabo S3, use `deploy/menhir-backup-upload-contabo.sh` directly as the
wrapper. All settings come from root-owned mode-0600
`/etc/menhir/backup-upload.conf`; ambient `AWS_*` variables are scrubbed. The
config selects fixed AWS profile/config files and contains the public
`age_recipient=age1...`. The age private identity remains off the VPS and is
required for clean-host restore. It enforces:
AES256 server-side encryption, Object Lock COMPLIANCE mode with retention
>=30 days, exact `VersionId` head/readback hash-size-encryption-lock-retention
verification, exact-version delete denial plus re-head confirmation, durable
atomic `fsync` receipt, and plaintext deletion only after all verification
passes and the receipt is validated against `menhir_schema.py`.

The same root-owned config fixes `receipt_root`, `local_archive_root`, and
`local_retention_generations` (minimum `2`). Each successful generation keeps
its own client-encrypted local archive and immutable per-generation receipt in
addition to the WORM copy. Plaintext deletion, singleton receipt finalization,
and publication of that immutable receipt are one restartable transaction.
The uploader does not automatically prune local encrypted generations; prune
only after preserving the configured floor and independently verifying a newer
off-host readback plus restore rehearsal.

`deploy/durable-state-inventory.json` is the exhaustive authority census. Both
backup and restore validate it against the exact persistent bind set in the
production Compose file; any new bind fails closed until its capture, restore,
and writer service are classified. Immediately before backup, the validator
also inspects the two live production containers and writes a root-only census
report. The exact bind source/destination/read-write set must match the
contract. Open authority files are recorded when present and must remain under
declared mounts; idle SQLite connections are allowed to be closed.

The wrapper is installed as a regular root-owned executable at
`/usr/local/sbin/menhir-backup-upload`; it deliberately loads
`menhir_schema.py` and `backup_cleanup_txn.py` from the fixed immutable
`/srv/menhir/production/bin` release, never relative to `/usr/local/sbin`.

## 8. Restore (rehearsal-first, guarded production)

Run as root, same environment as `up`. Only a strictly validated generation id
(shape `generation.<alnum>`) under `/srv/menhir/backups/decrypted/<id>` is
accepted; symlinks and special files are refused. The manifest is parsed as
strict JSON and the full chain (`COMPLETE -> MANIFEST.json -> SHA256SUMS ->
files`) is verified, together with image digest identity, schema identity, and
pinned-image offline consistency (digest-pinned refs, present locally, matching
the manifest). Mixed-generation state is refused.

Rehearsal (default, never touches production) restores to the deterministic
candidate root `/srv/menhir/backups/candidate/<generation-id>` and writes
`REHEARSAL-PASSED` only after Neo4j consistency checks and SQLite integrity:

```bash
deploy/restore-generation.sh <generation-id>
# verifies graph load/check (as UID/GID 7474) + SQLite integrity.
```

Production restore (guarded). Incoming authority is built and checked in
same-filesystem sibling staging directories before downtime. After the stack
is stopped, an fsynced transaction journal hashes the current and incoming
trees and swaps each directory with atomic renames. The displaced OAuth,
telemetry, Neo4j, secrets, and policy trees remain as digest-bound rollback
anchors; incoming state is never merge-copied:

```bash
MENHIR_RESTORE_CONFIRM=<generation-id> \
    deploy/restore-generation.sh <generation-id> --yes --production
```

An apply failure is automatically reconciled from
`/var/lib/menhir-production/restore-journal.json`. A successful swap commits
the journal unchanged under
`/var/lib/menhir-production/pre-restore-anchors/`. If post-swap validation,
restart, or generation-marker commit fails, the script restores the displaced
authority and preserves the failed restored trees separately. For a later
manual rollback, stop the stack and run the fixed helper with the selected
root-owned anchor and a separate result receipt; the anchor itself is not
rewritten:

```bash
python3 /srv/menhir/production/bin/restore_authority_txn.py rollback \
  /var/lib/menhir-production/pre-restore-anchors/<restore-id>.json \
  /var/lib/menhir-production/pre-restore-anchors/<restore-id>.rollback.json
```

Restore `current-generation` to the anchor's `prior_generation`, verify the
five prior tree digests, and start the stack. An unfinished restore journal is
fail-closed and must be applied or rolled back before another mutation.

## 9. Candidate, acceptance, promotion, and rollback

The root-owned `/var/lib/menhir-production/restore-selection` contains exactly
one `generation.<alnum>` value. After rehearsal and guarded production restore,
`candidate-deploy.sh` starts a separate `menhir-candidate` Compose project on
the exact generation already restored into the production authority while the
production project remains stopped. OAuth authority is read-only, application
mutations are fenced, and only probe telemetry is redirected to the isolated
candidate directory. It
uses the eventual production proxy identity (`172.30.0.3`, alias
`menhir-prod-app`), so the same transactionally validated Caddy route exercises
candidate and production; the scripts enforce that they are never attached
simultaneously. After acceptance, root writes the same
generation to `/var/lib/menhir-production/candidate-accepted` mode `0400`.

Promotion requires the candidate, restored, and accepted generation markers to
match and requires fresh complete backup metadata newer than the restore. It
also requires a short-lived Ed25519-signed source-fence receipt bound to the
release and an mTLS-authenticated live challenge against the old source over a
direct, non-public path. The signing private key exists only on the old source;
the release pins its public key and source CA digest. A timeout or connection
failure is not proof: the old
source must identify itself while in `candidate-readonly` mode, then return the
explicit mutation-fenced 503 response. The challenge endpoint is intentionally
absent from the public Caddy allowlist. Only after repeating those checks does
promotion stop candidate, start `menhir-prod` (`172.30.0.3`), and wait for both
health checks. On failed startup it attempts to restore candidate. `rollback.sh`
stops VPS production and starts the mutation-fenced exact restored authority;
the separate
Caddy transaction must restore the prior local route. None of these scripts
accept caller paths or commands, and each holds
`/run/lock/menhir-production.lock` for its complete transaction.

Before the first transactional Caddy release on an already-running VPS, root
runs `caddy-release.sh adopt-current`. This one-time command does not reload or
restart Caddy. It freezes the canonical checkout's Caddyfile, Compose,
registry, and env; proves that the rendered server graph equals the live admin
API; captures the exact image ID/ref, network aliases, and vhost status matrix;
and atomically activates an `adopted-current-*` immutable bundle. This bundle
is explicitly rollback authority, not evidence that the legacy runtime matches
the incoming Menhir release. The subsequent release snapshots it and restores
it exactly if the first immutable-bundle release fails.

### Independent external prerequisite evidence

Two release-pinned Ed25519 workers on distinct external networks must each run
`external-evidence-worker.py` against the same release and route version. Each
worker performs all seven checks, signs its observation locally, and transfers
only the JSON observation to the release host. Worker private keys never go to
the VPS; two observations from the VPS or the same network do not count.

On the release host, aggregate the observations into the fixed receipt consumed
by `candidate-accept.sh`:

```bash
python3 deploy/external-evidence-aggregate.py \
  /srv/menhir/production/release/release.json <route-version> \
  /var/lib/menhir-production/external-prerequisite.json \
  worker-a.json worker-b.json
```

Aggregation refuses duplicate workers or networks, stale or unpinned evidence,
route drift, symlinks, malformed JSON, and an existing output. It atomically
writes mode `0600` and validates every signature against the release authority.

### Source-side writer-fence receipt

Run `source-fence-author.py` **only on the old source**, as root, immediately
before promotion. The Ed25519 private key is source-only custody: generate and
retain it on that source, pin only its unpadded base64url public key and key ID
in `release.json`, and never copy the private key to the production VPS,
operator workstation, backup generation, shell history, logs, or evidence
files. Its path must be absolute and name a root-owned regular non-symlink file
with mode `0400` (a stricter POSIX mode is also accepted). The producer verifies
that the private key's public key is exactly the release-pinned key before it
makes a request or signs a receipt.

Prepare the release-pinned CA and a dedicated mTLS client certificate/key for
the direct old-source listener. The probe base is a canonical origin such as
`https://old-source.internal.example:8443`: lowercase host, no trailing slash,
credentials, path, query, fragment, or explicit default `:443` port. Both the
challenge and mutation requests are constructed from that one origin,
redirects are refused, and the CA file's SHA-256 must equal
`source_fence_tls_ca_sha256` in the exact release file. Put the dedicated bearer
secret in a root-owned, non-group/other-writable file containing exactly one
nonempty printable ASCII line terminated by LF. Do not pass the token itself on
the command line.

Immediately before running the producer, collect the source host's
service-manager proof that the legacy writer service is disabled and its
persistent-firewall proof that the source mutation path remains fenced. Keep
the fenced probe listener available: a timeout, DNS failure, refused
connection, or missing route is not proof of a stopped writer. Record those two
independent local observations as root-owned, non-group/other-writable, regular
non-symlink JSON files. They have these exact schemas; replace placeholders
with the release ID, the SHA-256 of the exact `release.json` bytes, the old
source's configured instance ID, and a timezone-aware observation time:

```json
{"schema":1,"kind":"source-service-disabled","release_id":"menhir-prod-0.2.0-11","release_manifest_sha256":"<64-lowercase-hex>","source_id":"old-source-01","observed_utc":"2026-08-27T15:00:00+00:00","source_service_disabled":true}
```

```json
{"schema":1,"kind":"source-firewall-persistent","release_id":"menhir-prod-0.2.0-11","release_manifest_sha256":"<same-64-lowercase-hex>","source_id":"old-source-01","observed_utc":"2026-08-27T15:00:00+00:00","source_firewall_persistent":true}
```

Do not create either file from an operator-supplied boolean. Create each only
after its named host control has been inspected successfully. The producer
rejects duplicate or extra JSON keys, false claims, a release or digest
mismatch, different source IDs, future observations, and observations older
than five minutes. It then performs this exact procedure:

1. Load and validate the strict root-controlled release authority and bind the
   SHA-256 of its exact bytes.
2. Generate a new random challenge and POST it over HTTPS with mTLS and the
   dedicated bearer token to `/internal/source-fence`. Require status 200,
   exact response keys, the same challenge/release/key/source identity,
   `candidate-readonly` mode, an active mutation fence, and a valid
   release-pinned Ed25519 signature.
3. POST the invalid `client_credentials` client ID to `/oauth/token` on the
   same origin. Require exact status 503, the existing two-field structured
   `temporarily_unavailable` refusal, `Retry-After: 60`, and
   `Cache-Control: no-store`.
4. Recheck evidence freshness, derive `source_writer_stopped` and
   `source_mutation_probe_denied` only from those two successful live checks,
   sign `menhir_schema.source_fence_payload`, and atomically replace the output
   after file and directory `fsync`. The output path must be absolute and its
   real parent directory must be root-owned and non-group/other-writable on
   POSIX. The visible receipt is mode `0400` and expires no more than five
   minutes after its check time.

Example invocation on the old source:

```bash
python3 deploy/source-fence-author.py \
  --release /srv/menhir/source-fence/release.json \
  --private-key /srv/menhir/source-fence/source-fence-private.pem \
  --token-file /srv/menhir/source-fence/source-fence-token \
  --service-disabled-evidence /run/menhir/source-service-disabled.json \
  --firewall-evidence /run/menhir/source-firewall-persistent.json \
  --probe-base https://old-source.internal.example:8443 \
  --tls-ca /srv/menhir/source-fence/source-ca.pem \
  --tls-cert /srv/menhir/source-fence/source-client.pem \
  --tls-key /srv/menhir/source-fence/source-client-key.pem \
  --output /run/menhir/source-writer-fence.json
```

Transfer the completed receipt to the production release host's fixed
source-fence receipt path, preserve root ownership and mode `0400`, and transfer
the one-line bearer-token file separately through the approved secret channel
because `promote.sh` repeats the live checks. Run promotion before the receipt
expires. If any input changes or the five-minute window is missed, discard the
receipt, refresh both local observations, and rerun the entire producer. Remove
the temporary target-side token copy after the cutover window; retain the
receipt according to the release evidence policy and retain the source-only
private key only according to the key-rotation and incident-retention policy.

## 10. Source retirement policy

**Local source retirement and any production target mutation are forbidden until
an encrypted off-host WORM backup exists and a clean-volume restore rehearsal has
passed.** Concretely, before decommissioning the local source path or mutating
the production graph/state: (1) run `backup-generation.sh` and confirm the
off-host WORM object and that the stack restarted cleanly, then (2) run
`restore-generation.sh <id>` rehearsal to completion on a clean scratch volume.
Only after both pass may the operator proceed with retirement or target
mutation. The image is immutable and shipped from the registry; the wheelhouse
is built in CI, never on the VPS.

## Notes / gaps

- Mark the helpers executable: `chmod +x deploy/backup-generation.sh deploy/restore-generation.sh`.
- `neo4j-admin database dump/load` runs with a minimal injected `neo4j.conf`
  (`server.directories.data=/data`) as UID/GID 7474; the `system`-database dump
  requires the pinned `NEO4J_IMAGE` digest to support it. Pin a version that does.
- The signing key/keyring is provisioned out-of-band under `secrets/oauth/` and
  mounted read-only; key rotation is an operator action in `secrets/oauth/`,
  not performed by the running app.
- Provision `menhir/source-fence-token` as a unique random secret and pin both
  its version and its non-secret key ID in the release. Transfer its temporary
  one-line target copy through the same out-of-band secret channel as the other
  credentials; never put it in `production.env`, shell history, Git, or a
  public URL. Remove the temporary target copy after the cutover window closes.
- `read_only: ${MENHIR_AUTHORITIES_READ_ONLY:-false}` uses the boolean coercion
  of Compose interpolation; set the value to exactly `true` or `false`.
- Rehearse restore on a throwaway host against a scratch state root before any
  real cutover.
