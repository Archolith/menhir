# Release-note fragments

Every file in `deploy/changes/unreleased` must be a regular, non-symlink `.json`
file containing exactly these keys:

```json
{
  "schema": 1,
  "id": "safe-lowercase-slug",
  "category": "changed",
  "deployment_class": "app-only",
  "summary": "A short, single-line summary.",
  "details": "A complete description of the change.",
  "operator_impact": "What an operator must do, or that no action is required.",
  "repositories": {
    "menhir": ["0123456789abcdef0123456789abcdef01234567"]
  },
  "security_scopes": ["runtime-hardening-and-observability"],
  "breaking": false
}
```

Allowed categories are `added`, `changed`, `fixed`, `security`, and
`operations`. Allowed deployment classes are `app-only`, `security-config`,
and `maintenance`. Repository names are limited to `menhir`,
`archolith_oauth`, `yawn_deploy`, and `yawn_vps`; every listed commit must be a
full 40-character lowercase hexadecimal commit ID. A commit may appear only
once in a fragment.

`security_scopes` must be a nonempty, sorted, duplicate-free subset of:

- `authentication-and-oauth-authority`
- `authorization-and-client-tool-policy`
- `backup-restore-and-rollback`
- `host-privilege-and-command-wrappers`
- `network-and-ingress-boundaries`
- `runtime-hardening-and-observability`
- `secret-handling`
- `supply-chain-and-build-evidence`

Fragment IDs must be unique across the directory. JSON duplicate keys, unknown
or missing keys, unknown directory entries, empty values, and non-regular
inputs are rejected.

Limits are 64 characters for `id`, 160 for `summary`, 4,000 for `details`,
1,000 for `operator_impact`, 100 commits per repository, eight security scopes,
and 64 KiB per fragment.

Validate fragments:

```text
python deploy/release_notes.py validate deploy/changes/unreleased
```

Render deterministic Markdown or JSON:

```text
python deploy/release_notes.py render deploy/changes/unreleased release-notes.md --format markdown --release-id menhir-prod-0.2.0-11
python deploy/release_notes.py render deploy/changes/unreleased release-notes.json --format json --release-id menhir-prod-0.2.0-11
```

Output paths must be relative to the current working directory and must not
contain traversal or point inside the fragment directory. Writes are atomic.
Existing output is refused unless `--overwrite` is supplied explicitly.
