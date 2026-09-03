## 2026-09-03 - add Utopia prior-art comparison

- Added a revision-pinned comparison of Utopia's governed bitemporal knowledge application against
  Menhir's code-linked evidence, repository structure, agent authority, and change-impact model.
- Recorded the novelty and category boundary, the ideas worth borrowing, the ideas to keep outside
  Menhir core, and a dependency-aware follow-up order.
- Updated the prior-art index to classify Utopia as the strongest adjacent comparison for enterprise
  world models rather than a direct replacement for Menhir's software-understanding center.

## 2026-08-30 - position Menhir around provenance and governance

- Reframed the public README, runtime descriptions, CLI help, and agent template around
  inspectable evidence, code impact, lifecycle authority, artifact governance, and
  release provenance, with MCP described as an access surface rather than the product
  category.
- Added an evaluation posture for the LongMemEval-derived temporal subset that records
  its diagnostic limits, publication requirements, and effect on default-off decisions.
- Updated governance and model records to match current source defaults and the checked-in
  SBOM without presenting a historical `.env`, coverage snapshot, or benchmark as live
  deployment evidence.
- Updated package metadata to use the same provenance and governed-context description.

## 2026-08-30 - require an independent security review for every production release

- Added a two-phase release-authoring flow that emits the exact candidate
  authority digest for review and refuses final authoring without a matching
  independent `APPROVED` attestation.
- Bound the review to every release claim, including all four commits, evidence,
  rendered artifacts, image digests, policies, rollback anchors, secret versions,
  and installed artifacts; any drift invalidates approval.
- Made zero unresolved critical/high findings and complete security scope strict
  release-schema requirements inherited by bootstrap, backup, candidate,
  promotion, rollback, and runtime validation paths.
- Documented the permanent release gate and recorded the follow-up to replace
  opaque MCP internal errors with actionable subsystem-specific diagnostics.

## 2026-08-29 - close the live operations gateway source blockers

- Updated the live VPS playbook to the implemented fixed topology: local
  no-shell root-wrapper dispatch, a Docker-bridge-only gateway listener, exact
  Caddy peer admission, `/ops` TLS routing, and OAuth protected-resource
  discovery bound to the operations audience.
- Pinned the external `menhir-proxy` bootstrap command to subnet
  `172.30.0.0/24` and gateway `172.30.0.1`, matching the fixed host listener.
- Kept live activation explicitly unproven until the immutable four-repository
  release is installed and passes backup, candidate, route, authorization, and
  public negative-path acceptance on the VPS.

## 2026-08-29 - add a guarded live VPS deployment playbook

- Added the canonical ordered workflow for immutable four-repository Menhir
  releases, one-time host bootstrap, backup/rehearsal/candidate/route/promotion,
  acceptance, and phase-aware rollback.
- Documented two real first-bootstrap stop gates in the current operations
  gateway: its Linux service still dispatches through a Windows-only runner,
  and no reviewed TLS transport currently reaches its loopback listener.
- Updated the production environment example to the current digest-bound hosted
  operator policy and added contract tests to prevent workflow or digest drift.
- Removed the stale blanket rejection of admin-scoped production authorization.
  Exact client policy still controls the full scope set, so ChatGPT and Claude
  can complete their reviewed operator grant while narrower clients remain
  unable to request admin authority.

## 2026-08-29 - allow hosted operators to ingest documents and projects

- Added `ingest_document` and `ingest_project` to the exact ChatGPT and Claude operator policies.
  Hosted operators now receive 51 of 54 MCP tools; only `delete_namespace`, `mint_client`, and
  `revoke_client` remain denied.

## 2026-08-29 - promote hosted web clients to operator authority

- Promoted the separate ChatGPT and Claude OAuth clients to exact operator-tier grants with
  read, write, and admin scopes. Each receives 49 of the 54 MCP tools, including artifact,
  todo, conflict, scheduler, and scoped memory-administration operations.
- Kept namespace-wide deletion, host-filesystem ingestion, and client credential administration
  outside hosted connector authority. Bumped the consent-session schema so the scope elevation
  requires fresh operator authorization; old access and refresh tokens fail the exact scope check.

## 2026-08-29 - bind production rights and consent to each OAuth client

- Removed shared Agent Smith consent groups so every hosted and managed client requires an explicit
  approval and can carry an independent digest-bound tool policy.
- Kept hosted web clients on the reviewed memory, diagnostics, and structure surface. Narrowed
  Agent Smith clients to their documented workspace tools, including read-only `list_todos`, while
  retaining `add_memory_and_track` only for Codex because its generated config explicitly pins it.
- Rejected legacy or unknown client-policy fields, documented the authority boundary, and added
  regression coverage for different client rights and non-transitive consent. Versioned consent
  cookies invalidate the old group-capable format at deployment.

## 2026-08-29 - expose read-only project structure to production agents

- Added `query_structure` to the digest-bound production tool surface for hosted web and
  Agent Smith OAuth clients so connector sessions can inspect already-ingested repositories.
- Kept `ingest_project` denied; expanding the production connector does not grant a new graph-write
  path. Added policy assertions and documented the resulting authority boundary.

## 2026-08-29 - retire Reasonix OAuth authority

- Removed the archived Reasonix client from Agent Smith's published OAuth metadata and
  digest-bound production policy.
- Added regression coverage proving the retired client is neither published nor admitted by
  production policy while the remaining managed-client suite stays intact.
