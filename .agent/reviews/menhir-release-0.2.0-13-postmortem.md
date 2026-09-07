---
artifact_schema: 1
artifact_uuid: 72dafb59-8b35-40ba-ab4c-ed8b5e88d1f1
artifact_type: review
artifact_status: COMPLETE
reviews: 7e14721a-2902-4eab-85ce-32381dd4d522
---

# Postmortem: Menhir release 0.2.0-13

## Executive finding

The application did not require three versions because it failed three times. The deployment system
discovered several previously untested release and host assumptions late, after expensive work or owner
approval. Each retry fixed a real contract gap, but release engineering and application delivery were
combined into one train. That forced every deployment-tool fix back through image build, release
authority, review, staging, and the maintenance path.

The final release is healthy and its data protections held. The process failure was allowing first-use
integration discovery in the release path instead of a disposable rehearsal and read-only host preflight.

## What happened

| Release / interval | Failure discovered | Root cause | Earliest correct detection point |
|---|---|---|---|
| 0.2.0-11 preparation | A second release workspace and review were required. | Python runtime evidence used `sha256:<hex>` where the schema required bare hex; fragment commit binding also needed correction. | Input-schema validation before review. |
| 0.2.0-11 acceptance | Candidate authentication, Cloudflare request identity, and a fixed-IP collision failed. | Acceptance depended on an external token file, authenticated POSTs omitted the declared User-Agent, and a one-shot digest container competed for the candidate address. | Disposable Linux candidate rehearsal. |
| 0.2.0-11 personal staging | The new image was absent from the VPS and private GHCR pull was unavailable. | The host intentionally had no standing registry credential; Docker save/load does not recreate registry `tag@digest` metadata. | Cold-cache, credentialless staging rehearsal. |
| 0.2.0-12 promotion | Authority and workstation transport mismatches appeared. | Neo4j index usage counters were hashed as authority, the digest worker was undersized, and PowerShell 5/7 and credential-helper behavior differed. | Production-sized read-only fixture plus both supported PowerShell versions. |
| 0.2.0-13 maintenance route | Menhir acceptance passed, but route promotion failed. | The candidate route directory was initially absent, TLS source paths were directories, and validation attempted an occupied fixed IP. More fundamentally, the route transaction assumed Caddy owned the Menhir proxy role while the live proxy network assigns `.2` to Cloudflared. | Deployment-class-specific live preflight before backup/fence and a topology fixture matching production. |
| Post-deploy audit | Two operational units were failed. | The Caddy reconcile watcher retried while the release lock was held; the app-only audit recognizes only the older scaffold restore receipt, not the successful current maintenance-release rehearsal. | Completion cleanup and receipt-schema compatibility test. |

## Evidence that the safety model worked

- Isolated staging for 0.2.0-13 passed all 17 identity, resource, OAuth PKCE, MCP, restart,
  production-isolation, and rollback checks.
- The final image is
  `ghcr.io/archolith/menhir:0.2.0-13@sha256:c228117f6b9a0e65badd4eac3c54072a3d47b6bae0a60ae3a8d41e38749fa115`.
- The completed maintenance journal binds release authority
  `3712124dd91de810963741f09fa2d43bab7dcda993d412e9874cf9daf5810f7b`.
- The route override retained the already-serving route only after candidate acceptance, exact image
  verification, container health, public health, OAuth discovery, and OAuth/MCP acceptance passed.
- Production currently has one healthy Menhir application container and one healthy Neo4j container.
  Menhir has a 2 GiB limit; Neo4j has a 4 GiB limit. The host has 11 GiB RAM, 4 GiB swap, and about
  98 GiB free disk.

## Process failures

1. **Release scope was not isolated.** Application changes, release automation, and a sibling `yawn.vps`
   tip refresh shipped together. The mechanical classifier correctly selected `maintenance`; this was
   not a routine application deployment.
2. **Publication is incomplete.** The coordinator describes `prepare -> review -> finalize -> publish`,
   but has no publication state. Released fragments remain in `deploy/changes/unreleased`, so old
   maintenance fragments can contaminate the next release unless moved manually.
3. **Identity is still copied between commands.** The state machine already knows the release ID and
   staging digest, but the operator must paste them into approval and promotion commands.
4. **Production prerequisites are checked too late.** Exact-image staging proves the application but does
   not prove every host-side promotion prerequisite before owner approval.
5. **Deployment class is not fully immutable.** It is stored in `release-flow.json`, not in the signed
   release authority and receipt chain.
6. **The fast security lane is only documentation.** `security-config` dispatches to `Maintenance`, so an
   OAuth policy-only change still invokes database backup/restore and route machinery.
7. **Promotion evidence is too thin.** The personal receipt is synthesized after a zero exit code and does
   not bind the underlying transaction receipt, wrapper digest, duration, unchanged database/ingress
   identity, or rollback result.
8. **The ingress source of truth is ambiguous.** Cloudflared directly proxies the public Menhir allowlist
   to the app, while the shared Caddy configuration also contains a Menhir route. The handbook describes
   only the Caddy topology. A release must declare which ingress mode is authoritative before attempting
   route mutation.
9. **Operational failures did not close the release.** The Caddy reconciliation path remained failed after
   the lock collision, and the scheduled readiness audit remained failed after a successful maintenance
   rehearsal because its receipt reader was too narrow.

## Operator error assessment

The malformed 0.2.0-11 runtime digest and a few manual path choices were operator-adjacent mistakes.
They were not acceptable causes for a multi-hour incident: deterministic input validation should have
rejected them immediately. The 0.2.0-13 override was a controlled exception with durable evidence, not
an unsafe improvisation.

## Required changes before the next release

- Complete and test product publication, including exact fragment archival.
- Bind deployment class, changelog digests, source revision, image identity, and runner identity through
  release, staging, approval, transaction, and promotion receipts.
- Add a single resumable rehearsal command and derive already-validated confirmations from state.
- Add class-specific host preflight before staging can pass.
- Implement the focused `SecurityConfig` runner before claiming the ten-minute lane.
- Make app-only and security-config receipts prove Neo4j and ingress were unchanged and enforce one
  end-to-end deadline including rollback.
- Declare one primary ingress mode in release authority. A Cloudflared release must not execute a Caddy
  route transaction; a Caddy release must validate regular TLS files and collision-free candidate
  networking before production mutation.
- Accept strict current maintenance-release rehearsal receipts in scaffold readiness and verify all
  required watchers/audits are healthy at completion.

## Remediation status on 2026-09-07

| Finding | Status | Evidence / remaining condition |
|---|---|---|
| Manual fragment lifecycle | FIXED IN BRANCH | Explicit nonce-bound `publish` transaction; drift, unowned archives, interruption, and idempotence tests pass. |
| Repeated release/digest copy-paste | FIXED IN BRANCH | One resumable `rehearse` command; approval and promotion derive the validated identifiers. Owner identity and explicit promotion execution remain intentional. |
| Late host prerequisite discovery | FIXED IN BRANCH | A sanitized class-specific production preflight is required before image load or staging resource creation and is bound into the staging receipt. |
| Deployment class/changelog outside authority | FIX IN PROGRESS | Bind mechanically derived class and both generated notes digests into `release.json` and require state/authority agreement. |
| Restore evidence incompatibility | FIXED IN BRANCH AND LIVE | Current maintenance rehearsal receipts are accepted strictly; the live drill receipt was rebound to the current backup generation and the audit now passes. |
| Failed operational units | FIXED LIVE | Caddy reconcile path and scaffold audit timer are active; audit result is successful. |
| Resource gate brittleness | FIXED IN BRANCH | Sidecar memory is explicitly limited and the preflight uses the resulting 6.5 GiB envelope; the live app-only preflight passes. |
| Thin promotion transaction evidence | OPEN | Bind fixed wrapper identity, underlying transaction result, elapsed time, and unchanged component identities before calling the receipt chain complete. |
| No dedicated security-config runner | OPEN | Implement and rehearse bounded config/app replacement before advertising the ten-minute lane. |
| Ambiguous ingress authority | BLOCKS MAINTENANCE | Select Cloudflared or Caddy in release authority, remove the inactive duplicate path, and prove rollback for the selected mode. |

## Rehearsal gate

No production deployment should begin until one clean rehearsal demonstrates:

1. release inputs are generated and validated without hand-edited digests;
2. the image can reach a cold VPS without standing registry credentials;
3. the exact image and source revision remain identical through staging;
4. all OAuth PKCE and MCP read/write/deny checks pass;
5. restart persistence and simulated rollback pass;
6. a class-specific live preflight passes before approval;
7. the preview names the only production components that will change;
8. the promotion receipt schema can prove elapsed time, transaction result, and untouched components;
9. operational audit services finish healthy.

The rehearsal is a go/no-go gate for the machinery. It is not another owner approval loop and does not
authorize a production mutation.
