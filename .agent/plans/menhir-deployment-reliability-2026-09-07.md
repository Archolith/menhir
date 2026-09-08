---
artifact_schema: 1
artifact_uuid: f4a95baf-e708-4571-ad6f-fef7ae4b072e
artifact_type: plan
artifact_status: IMPLEMENTING
---

# Menhir deployment reliability

## Why

Release `menhir-prod-0.2.0-13` took three product versions and several hours. The safety gates
eventually protected the data and proved the final image, but they discovered workstation, image-transfer,
release-authority, and live-ingress mismatches only after release preparation or after the expensive
maintenance transaction had started. The release also mixed application changes with deployment-tooling
and sibling-repository changes, mechanically forcing the slowest deployment class.

The next routine application release must be a boring, resumable operation whose identity and host
prerequisites are checked before approval or production mutation.

## Scope

In scope for this pass:

- reconstruct the release 11-13 incident from Git and durable receipts;
- make released change fragments leave the `unreleased` queue mechanically;
- eliminate copy/paste of release and staging identities between normal commands;
- add a read-only, machine-readable production-readiness check and require staging to prove it;
- reconcile the handbook with the live Cloudflared ingress topology;
- rehearse the resulting path without changing production;
- define the follow-up infrastructure-as-code boundary.

Out of scope:

- another production release or database migration;
- replacing the proven release authority, isolated staging, owner approval, or rollback contracts;
- adopting Kubernetes, a second orchestrator, or a hosted deployment platform;
- moving production secrets into a new provider during this remediation.

## Acceptance criteria

1. A finalized product release has one explicit publication step that archives exactly the fragments
   bound into its notes and prevents them from reclassifying the next release.
2. The personal deployment coordinator can select and run isolated staging in one resumable command,
   and can derive its approval/promotion confirmations from the immutable state it already validates.
3. Staging refuses to emit a passing receipt unless a deployment-class-specific, read-only production
   preflight passes. Maintenance preflight checks the actual ingress implementation and all candidate
   route prerequisites before backup or writer fencing.
4. App-only promotion touches only the Menhir application container and completes within its five-minute
   foreground budget; security configuration has a distinct implementation plan instead of silently
   becoming maintenance.
5. The retained clean 0.2.0-13 isolated rehearsal proves exact image identity, OAuth PKCE, MCP
   read/write/deny behavior, restart persistence, rollback behavior, and production non-interference;
   the new class-specific live preflight is then exercised independently against the unchanged host.
6. Documentation names the current Cloudflared topology, the release-class decision, the expected
   operator commands, timing budgets, refusal states, and recovery handoff.
7. Release authoring accepts only scanner and image evidence that is transitively bound to one
   immutable CI publication identity. A digest, SBOM, or scan from another publication must fail
   before release authority is written.
8. The canonical digest-qualified image reference emitted by publication enters isolated staging
   without operator reconstruction. Export is anchored to the resolved local image ID, and the VPS
   verifies the archive's CI-bound configuration and layers rather than labels alone.
9. Maintenance creates its root-owned transaction and acquires the same cross-lane admission fence
   before the installer can mutate production. The fence remains held through installation, backup,
   cutover, and acceptance, and every production lane refuses conflicting ownership.
10. Maintenance receipts preserve immutable root start/completion timestamps and initiating
    approval/promotion-attempt identity. Adoption never synthesizes chronology and rejects work that
    predates approval or belongs to another attempt.
11. Closure requires focused local tests/static checks for each changed module and a green required
    CI suite on the exact pushed commit. A full-system audit is exceptional and runs only when the
    owner explicitly requests one; it is not an automatic or repeating release gate.
12. Every privileged production mutator, including app-only and security-config, validates the
    initiating approval, staging receipt, execution digests, promotion attempt, and chronology at the
    root boundary. No directly callable sudo path can bypass that authority.
13. Retry/adoption validates the currently live release, environment, images, approval, and attempt
    while holding the shared locks. An older completed receipt can never report success after another
    release has advanced production.
14. Release and scaffold installation acquire the system admission fence before mutation and retain a
    durable, crash-recoverable transaction. Retirement and unit changes are snapshotted before the first
    write and are either restored exactly or rolled forward explicitly.
15. Image provenance is anchored in verified CI attestations and canonical repository identity;
    untracked working-tree inputs, unhashed dependency acquisition, and mutable evidence reads cannot
    enter a release authority.
16. A reviewed clean checkout exposes one executable, resumable infrastructure convergence path from
    bootstrap privilege through installed runner verification, backup/rehearsal evidence, desktop
    archive verification, and app-only admission. Check mode must describe real drift without requiring
    the unapplied post-state.

## Proposed design

Extend the existing two state machines instead of adding a parallel deployer:

- Product release remains `prepare -> independent review -> finalize -> publish`. `publish` validates
  the frozen release state, writes a publication receipt, and moves only the exact reviewed fragments
  into a release-named archive.
- Personal deployment remains `select -> stage -> approve -> promote`, with a convenience `rehearse`
  entry point and state-derived confirmations. Approval remains explicit and promotion remains a
  separate action.
- The VPS staging runner captures a sanitized readiness report before creating disposable staging.
  Checks are selected by deployment class. The report is digest-bound into the staging receipt.
- The production topology contract describes roles (ingress peer, application, database), not a stale
  assumption that the ingress peer must be Caddy. Maintenance route mutation is allowed only when the
  bundle and live host expose a compatible route transaction; otherwise it refuses before mutation.

## Modular acceptance model

The deployment system is reviewed and rehearsed as five bounded modules plus one integration gate.
No module may infer or reconstruct authority owned by another module. Each module must validate its
input, emit a versioned immutable output, and refuse before mutation when that contract is absent or
inconsistent.

| Module | Owns | Required input | Authoritative output |
|---|---|---|---|
| 1. Build and provenance | Clean-checkout image build, SBOM, vulnerability scan, registry publication | Reviewed source commit and pinned build inputs | CI publication document binding image ID, config, layers, scanners, archive, and registry digest |
| 2. Publication and staging | Release authoring, install bundle, release-note publication, disposable VPS rehearsal | Complete Module 1 evidence and reviewed release inputs | Published release workspace, exact install bundle, and staging receipt bound to the release/bundle/image/preflight |
| 3. Approval and promotion | Owner approval, immutable promotion attempt, desktop-to-root handoff | Published Module 2 state and passing staging receipt | Approval document plus promotion receipt importing the exact root transaction receipt |
| 4. VPS transactions | Cross-lane admission, mutation serialization, backup, cutover, rollback, recovery | Module 3 approval/attempt binding and exact trusted bundle | Root-owned terminal transaction receipt with preserved chronology and runtime identities |
| 5. Infrastructure convergence | Scaffold executables, sudoers, host directories, systemd policy, drift verification | Exact reviewed infrastructure checkout and explicit infrastructure operation | Root-owned scaffold receipt and read-only host verification |
| 6. Integration gate | Interface compatibility and operator rehearsal across Modules 1-5 | Exact commits and all focused module test results | Green required CI on the exact pushed SHA and one clean-checkout, non-production rehearsal report |

The shared production-admission fence is an interface owned jointly by Modules 4 and 5. Product
transactions and infrastructure convergence must acquire the same exclusive fence before their first
mutation and retain it until their terminal receipt or rollback is durable. A module-local lock is not
sufficient evidence of system-wide serialization.

Module reviews may run in parallel. Remediation and local tests remain scoped to the affected module.
After push, required CI runs the complete suite on the integrated commit. CI failure is reproduced
with the failing test and affected neighbors locally, then fixed and pushed as a new SHA; the full
suite is not repeatedly run on the maintainer machine. A passing module test never overrides a
failing integration gate.

## Alternatives considered

- **Replace the scripts with Ansible now.** Ansible check/diff mode is useful for host convergence, but
  migrating the release transaction during incident remediation would create a second unproven path.
- **Move all deployment into GitHub Actions now.** This can later provide attestations and protected
  environment approval, but the private single-host SSH and desktop-held archive contract should first
  become deterministic locally.
- **Keep documentation-only checklists.** Rejected because every major release 13 delay was a condition
  software could have detected earlier.

## Risks

- Archiving fragments mutates the source checkout, so publication must be explicit, atomic, and refuse
  dirty or mismatched inputs.
- A preflight can become another stale policy surface. It must report observed and expected values in a
  versioned schema and have fixture-based tests.
- Convenience commands must not weaken the owner gate. They may derive already-bound identifiers, but
  may not auto-approve or auto-promote.
- Existing 0.2.0-13 evidence predates the new schemas and must remain readable.

## Validation

- focused unit tests for release publication, fragment selection, resumability, confirmation derivation,
  and deployment-class preflight;
- integrated negative tests for wrong-image scanner evidence, digest-only publication-to-staging,
  mutable-tag substitution, maintenance mutation before admission, pre-approval adoption, and
  cross-lane concurrency;
- focused deployment contract tests for every changed module, followed by the complete required CI
  suite on the exact pushed commit;
- artifact validation and changelog checks;
- the retained successful isolated staging receipt for exact image 0.2.0-13 plus a fresh execution of
  the new read-only preflight against that unchanged production host;
- read-only live infrastructure audit before and after the rehearsal.
- an independent full-system audit only if the owner explicitly requests one for exceptional risk.

## Full-system audit disposition (2026-09-08)

The independent audit of Menhir `1487247` and shared operator commit `2aded37e` returned `FAIL`.
The findings are the remediation ledger; none may be waived by a passing component test.

| Canonical blocker | Owning modules | Closure evidence required |
|---|---|---|
| P1-1 Fast-lane privileged boundaries do not enforce owner approval | 3, 4 | Direct invocation before approval and cross-attempt replay both refuse before mutation; root receipt binds approval and attempt |
| P1-2 Caddy immutable image authority is lost before staging | 1 | `release.json` retains the digest-qualified reference; clean-host staging pulls and verifies the exact proxy image/container |
| P1-3 Completed fast-lane receipts can be adopted after live state advances | 3, 4 | Adoption runs under both locks and rejects a receipt whose live release, environment, app, database, or ingress state is no longer current |
| P1-4 Not every privileged mutator participates in the shared admission protocol | 4, 5 | Release install, scaffold install, Ansible convergence, app-only, security-config, and maintenance pass adversarial interleaving tests |
| P1-5 Caddy retirement occurs outside release-install rollback | 4 | All retired files/routes/unit state are snapshotted and rollback is durably armed before the first disable/delete |

The same audit recorded two P2s (unverified dependency-lock hashes and incomplete scaffold unit-state
rollback) and one P3 (mtime-based bundle discovery contradicts the handbook). These historical
findings define the remediation ledger; closure now depends on their focused regression evidence and
required CI, not a repeating independent-audit loop.

| ID | Priority | Owning module | Required remediation |
|---|---:|---|---|
| F1 | P1 | 3 / 4 | Make app-only and security-config root boundaries verify and persist owner approval, staging receipt, wrapper/runner identities, promotion attempt, and immutable chronology. |
| F2 | P1 | 1 / 2 | Preserve the immutable Caddy repository digest reference through release authority and prove the staging proxy runs that exact image. |
| F3 | P1 | 3 / 4 | Adopt a completed fast-lane receipt only under both locks and only while its exact release/environment/container state remains live. |
| F4 | P1 | 4 / 5 | Require admission-before-mutation for release installation, scaffold installation, and Ansible convergence; a maintenance helper must prove the active binding. |
| F5 | P1 | 4 | Snapshot and arm rollback for retired Caddy units/files/routes before the first retirement mutation, then verify restoration on failure. |
| F6 | P2 | 1 | Retain package hashes from the lock through wheel download/build, including build-isolation dependencies and index authority. |
| F7 | P2 | 5 | Either exactly restore every accepted systemd unit state or reject states the scaffold rollback cannot reproduce. |
| F8 | P3 | 5 | Remove modification-time bundle selection; require the immutable bundle path explicitly. |

Closure sequence: implement the disjoint module fixes, run focused adversarial tests and static checks
per module, push the exact integrated commits, require green CI on those SHAs, and run one
clean-checkout non-production rehearsal. Production, merge, and release remain prohibited until CI,
rehearsal evidence, and the external infrastructure prerequisites are satisfied.

## Rehearsal result (2026-09-07)

| Gate | Result | Evidence / disposition |
|---|---|---|
| Existing isolated 0.2.0-13 rehearsal | PASS | All 17 image, resource, OAuth PKCE, MCP, restart, production-isolation, and rollback checks passed in the retained staging receipt. |
| New app-only production preflight | PASS | Exact 0.2.0-13 app and Neo4j digests healthy; completed release journal; expected network roles; 105,022,050,304 bytes free disk and 7,496,204,288 bytes available memory. Sanitized report digest: `be492ac57a33650251586580429992c05fb299eec8f015ea20e5be1928a287d3`. |
| Preflight resource envelope | PASS after correction | The first 7 GiB threshold failed on a normal 32 MiB fluctuation. Staging sidecars now have explicit 256 MiB limits and the gate requires the resulting 6.5 GiB total hard-limit envelope. |
| Operational units | PASS | Cloudflared is authoritative; the duplicate Caddy route and both reconcile units are retired; `menhir-scaffold-audit.timer` remains active. |
| Scoped acceptance audit | PASS after correction | One independent audit found three release-gate gaps. Generated label sequence, direct-runner refusal, authority-derived promotion mode, and preflight-seal recomputation were corrected; only the affected suite was rerun, with 60 tests passing. |
| Maintenance route preflight | PASS | Current authorities declare Cloudflared; preflight proves the `.2` peer is the running `cloudflared` Compose service and maintenance retains ingress without a route mutation. |

This is a valid rehearsal of the next routine app-only path because the application behavior was
already exercised against the exact live image and the newly added host gate is read-only. It is not
a successful maintenance rehearsal, and the plan does not claim one.

## Release lanes and remaining gates

| Lane | Current readiness | Next required work |
|---|---|---|
| Packaged product | Ready in branch | Use `next-id -> prepare -> review -> finalize -> publish`; prepare enforces the generated label and publication archives exact prepared fragments. |
| Personal app-only | Ready in branch | Use `rehearse -> approve -> promote`; one human approval remains intentional and the direct gate independently verifies authority and preflight evidence. |
| Security configuration | Implemented and tested | It is mechanically classified and uses the dedicated config/application root transaction. The receipt proves unchanged Neo4j and Cloudflared identities and the complete prior config set is the rollback anchor. |
| Maintenance | Cloudflared contract aligned | Release authority and the maintenance journal retain Cloudflared; the inactive duplicate Caddy path and writers are removed. The root receipt binds the unchanged ingress identity. |
| Recovery | Existing contract retained | Continue scheduled encrypted backups and current-generation restore evidence; do not exercise during routine app-only release. |

## Infrastructure follow-up

Persistent host prerequisites are expressed in the bounded Ansible role and verified with
pytest-testinfra over SSH. Keep release transactions in the digest-bound coordinators. Use Ansible
check/diff before convergence. Image validation/publication is defined in the reusable GitHub
workflow with provenance attestations; production promotion remains a separate owner-approved step.

Recommended adoption order:

1. **Routine code change:** keep classification tests current whenever a new protected source or
   authority surface is added.
2. **Infrastructure change:** preview the bounded Ansible role in check/diff mode, then converge only
   reviewed root-owned directories, systemd, tmpfiles, and retired ingress writers.
3. **Verification:** run pytest-testinfra over SSH after Ansible convergence; use Molecule only for
   disposable role tests.
4. **Publication:** move image build and package publication into a reusable GitHub workflow with
   artifact attestations and a protected production environment. Keep owner approval separate from
   build publication.

Official references evaluated:

- Ansible check/diff mode: <https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_checkmode.html>
- pytest-testinfra SSH/Docker backends: <https://testinfra.readthedocs.io/en/latest/backends.html>
- Molecule Docker scenarios: <https://docs.ansible.com/projects/molecule/examples/docker/>
- Docker Compose health waiting: <https://docs.docker.com/reference/cli/docker/compose/up/>
- GitHub artifact attestations: <https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations>
- GitHub protected deployment environments: <https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments>

## Docs to update

- `deploy/RELEASE_AUTOMATION.md`
- `deploy/LIVE_VPS_PLAYBOOK.md`
- `deploy/PRODUCTION.md`
- `.agent/scripts-index.md`
- `CHANGELOG.md`
