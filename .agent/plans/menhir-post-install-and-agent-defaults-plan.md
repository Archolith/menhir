# Menhir Post-Install and Agent Defaults

**Last verified:** 2026-08-18 — NOT MERGED — work exists on an unmerged branch. `agent/menhir-post-install-agent-defaults` @ `0328629` (2026-08-11) is NOT an ancestor of `main`; `post_install` is 0 hits in `main`, while `install_hooks`/`def install` (4/2) are the pre-existing CLI installer. Plan carries no Status line. Decide: merge the branch or close it.


## Why

- Installation currently ends after `pip install`, while environment creation, repository hook wiring,
  service persistence, MCP registration, and optional client hooks live in separate documents or commands.
- The root agent instruction files tell agents to read all of `.agent/`, contradicting the routed,
  narrow-read workflow in `.agent/README.md`.
- Operators need one idempotent command that completes safe local setup and names every conditional or
  privacy-sensitive step it intentionally did not perform.

## Scope

- Add an idempotent `menhir setup` command for a source checkout.
- Create `.env` from `.env.example` without overwriting existing configuration.
- Install the repository-managed Git hooks path without replacing an undisclosed custom hooks path.
- Optionally install the existing Claude-compatible recall/checkpoint hooks and Windows watchdog task.
- Add a post-install runbook and a reusable default agent-use contract.
- Replace the duplicated root agent instructions with a concise routed default.

Out of scope: provisioning secrets, changing Neo4j or LLM infrastructure, registering every MCP client
format automatically, silently enabling prompt capture, and deploying Menhir.

## Proposed Design

- Put setup orchestration in `menhir.cli.setup`; keep its checks independently testable.
- Treat `.env` and `.githooks` as safe defaults. Refuse to overwrite a different `core.hooksPath` unless
  the operator explicitly passes a force flag.
- Keep agent-client hooks and the Windows watchdog opt-in because they write outside the checkout or
  change login-time behavior. TurnEvidence and file-event capture remain separately consented integrations.
- Make `docs/agent-usage.md` the canonical consumer-facing contract and keep root model-specific files as
  short routers to `AGENTS.md` and the project-local `.agent/README.md`.

## Alternatives Considered

- A PowerShell-only bootstrap would match the maintainer workstation but not the public package contract.
- A documentation-only checklist would leave the same drift-prone manual actions in place.
- Enabling all capture hooks by default would hide a privacy-relevant behavior change inside installation.

## Risks

- Mitigable: an existing custom Git hook path could be lost; setup detects it and requires explicit force.
- Mitigable: project hook installation could target the wrong directory; setup resolves and validates the
  Menhir checkout before writing.
- Acceptable: client-specific MCP registration remains manual because client schemas and credential tiers
  differ.
- Acceptable: wheel-only installs cannot manage checkout-local scripts; setup reports that it requires a
  source checkout.

## Invariants

- Existing `.env`, hook configuration, unrelated client hooks, and user worktree changes are preserved.
- Hook failures remain fail-open.
- No secret value is printed or generated.
- Optional prompt/file capture is never enabled merely by installing the package.

## Validation

- Unit tests for checkout detection, dry checks, idempotent environment creation, Git hook wiring,
  custom-hook refusal, and setup CLI output.
- Existing CLI hook tests after extracting a reusable installer.
- Package metadata test and CLI help smoke.
- Placeholder/link checks for new documentation and final diff review.

## Docs To Update

- `README.md`
- `docs/post-install.md`
- `docs/agent-usage.md`
- `AGENTS.md` and model-specific instruction routers
- `CHANGELOG.md`
