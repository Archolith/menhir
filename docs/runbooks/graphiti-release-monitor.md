# Graphiti release monitor

Menhir checks `getzep/graphiti` releases once per day and compares them with the exact
`graphiti-core` pin in `pyproject.toml`. The monitor is a review trigger, not a dependency updater:
it never changes the pin, opens an upgrade pull request, merges, deploys, or connects to production.

## Why this shape

The repository has ordinary GitHub Actions but no standing repository-agent runtime. Dependabot
would lead with a mechanical pin-changing pull request and would open one for releases that may not
matter to Menhir. A small scheduled Action is therefore the lower-complexity fit: deterministic,
auditable, free of model credentials, and able to stay completely quiet when no action is warranted.

The workflow is split into three reviewed parts:

- `.github/graphiti-release-monitor.json` owns the Menhir-specific coupling policy.
- `scripts/maintenance/graphiti_release_monitor.py` reads the pin and public release metadata and
  produces a deterministic assessment plus issue body.
- `.github/workflows/graphiti-release-monitor.yml` schedules the assessment and owns the only write:
  creating or refreshing a GitHub issue.

## Trigger and materiality policy

The Action runs daily at 14:17 UTC and can be run manually. Drafts, prereleases, and the repository's
separately versioned `mcp-vX.Y.Z` releases are ignored; only plain `vX.Y.Z`/`X.Y.Z` core tags are
considered. A stable release newer than Menhir's pin is material when either:

1. it crosses a Graphiti major or minor release line, which is always review-worthy because Menhir
   imports private APIs and monkey-patches Graphiti internals; or
2. release notes for any stable release between the pin and latest stable target match a policy
   category covering security/dependencies, API/schema changes, entities/edges/episodes,
   dedup/resolution, Neo4j/query behavior, retrieval/performance/token cost, or extraction/prompts.

An unmatched patch release records a quiet successful workflow run and performs no GitHub write. If
an earlier release is material but a later stable patch is available, the issue targets the latest
stable version so a review does not deliberately plan an already-obsolete target.

## Issue lifecycle

For a material target, the workflow creates `Graphiti upgrade plan: X.Y.Z` with a hidden,
version-specific marker and the `graphiti-release-monitor` label. The body contains the current pin,
upstream releases considered, every known coupling area and its files/tests, highlighted release-note
matches, migration guardrails, and a staged validation plan.

The same open issue is updated only when its generated content changes. A closed issue is treated as
an owner decision and is not reopened. A later target version gets its own issue. Nothing in the
workflow approves the plan or changes repository or production state.

## Permissions and credentials

The workflow declares only:

- `contents: read` to check out and inspect the repository;
- `issues: write` to create the monitor label and create/update the plan issue.

It uses GitHub's short-lived built-in `GITHUB_TOKEN` for authenticated GitHub API rate limits. No
OpenAI key, Neo4j credential, deployment secret, package-index credential, or production access is
needed. `persist-credentials: false` prevents checkout from leaving a write-capable Git credential in
the worktree.

Repositories that restrict Actions from creating issues must allow the workflow token read/write
access under **Settings -> Actions -> General -> Workflow permissions**. No additional secret is
required.

## Testing

Run the monitor's offline policy tests:

```bash
pytest tests/test_graphiti_release_monitor.py -q
```

Run a live, read-only assessment against public upstream metadata:

```bash
python scripts/maintenance/graphiti_release_monitor.py --repository-root . --output graphiti-monitor.json
```

The generated `graphiti-monitor.json` is local and untracked. Review `actionable`, `reasons`,
`categories`, and the generated issue before deleting it.

In GitHub Actions, use **Run workflow** with `dry_run` left enabled. The run summary shows the pin,
target, and decision without touching issues. Disable `dry_run` only when intentionally testing the
issue upsert. Closing that test issue prevents the same target from being reopened.

Before approving a real upgrade, follow the generated plan: inspect every Graphiti patch/private
import, run focused Graphiti tests, run the offline suite, and then run opted-in integration tests
against a disposable Neo4j. Production is outside the monitor's boundary.

## Relationship to the ChatGPT Graphiti Pin Watch

Keep the existing daily ChatGPT watch during the initial observation period. The repo-native monitor
becomes the durable source of truth for the actual pin, deterministic release detection, and the
version-specific issue. The ChatGPT watch remains useful as a semantic second opinion on sparse or
ambiguous upstream notes, where a keyword policy can miss relevance.

After several releases show acceptable coverage, the ChatGPT watch can be reduced to auditing new
monitor issues or checking quiet patch releases rather than duplicating daily notifications. Do not
remove it until that evidence exists; the two mechanisms currently complement each other.
