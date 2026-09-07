# Canonical-self production observation — 2026-09-04

**Status:** read-only aggregate evidence; **not** a Phase 7 census or disposition manifest

**Observed at:** 2026-09-04 13:29:30 UTC

**Production host:** `vmi3131796`

**Access path:** workspace `scripts/vps-ssh.ps1`

**Mutations:** none — no deploy, restart, configuration change, graph write, or migration

## Deployed identity and rollout state

| Item | Observed value |
|---|---|
| App container | `menhir-prod-app`, healthy |
| Release | `0.2.0-9` / `menhir-prod-hotfix-agent-todos-20260904` |
| OCI revision | `613ff7866c75b44cb3703233f301ce4f90336fbc` |
| Image digest | `sha256:c406c6214789d07a38d14763b63a7f8fd0cd166f03477497619bdb06fa1bb5b3` |
| Canonical-self branch present | no — neither the initial binding commit nor the reviewed tip is an ancestor of the deployed revision |
| Binding mode | environment variable unset; deployed code therefore has no canonical-self rollout active |

The branch's new decision counters cannot be observed from this release. Producing them would
require a deployment and configuration change, which this observation did not authorize.

## Aggregate graph observations

The queries used Neo4j read access and returned counts and timestamps only. No episode text,
facts, arbitrary entity names beyond the explicit self-like search terms, or per-node UUIDs were
returned.

### Self-like names by group

| Physical group | Raw name | Nodes | `is_self` marker | `entity_role=self` |
|---|---:|---:|---:|---:|
| default (`""`) | `user` | 73 | 0 | 0 |
| default (`""`) | `i` | 1 | 0 | 0 |
| default (`""`) | `mine` | 1 | 0 | 0 |
| `yawn` | `user`, `i`, `me`, `my` | 1 each | 0 | 0 |
| `ctharvey` | `user` | 1 | 0 | 0 |
| canary group | `user` | 1 | 0 | 0 |
| `archolith` | `user` | 1 | 0 | 0 |
| `menhir` | `user` | 1 | 0 | 0 |

The remainder of this report concerns the 73 exact-name `user` nodes in physical default group
`""`, because that is the population previously described as “66 existing forks.”

### Creation range

- Earliest: `2026-03-07T04:27:33.179637Z`
- Latest: `2026-09-04T02:32:04.800452Z`
- Missing `created_at`: 0
- Twelve were created on September 3–4; every one of those twelve is mentioned only by episodes
  whose persisted source is `claude-code`.

### Incoming episode-source evidence

Source groups overlap because one entity may be mentioned by episodes from several sources.

| Episode source | Distinct nodes | `MENTIONS` relationships |
|---|---:|---:|
| `claude-code` | 40 | 136 |
| `project-scan` | 49 | 113 |
| `user` | 7 | 19 |
| `opencode` | 7 | 16 |
| `menhir_review` | 1 | 14 |
| `codex` | 2 | 8 |
| `zcode` | 3 | 6 |
| `agent_inference` | 1 | 2 |

Bucketed without overlap:

| Linked-source bucket | Nodes |
|---|---:|
| Non-human sources only | 65 |
| Both human and non-human sources | 7 |
| No incoming `MENTIONS` evidence | 1 |
| Human sources only | 0 |

For only the twelve nodes created September 3–4, non-`MENTIONS` relationships were 49 outgoing
`ANCHORED_TO`, 19 outgoing `RELATES_TO`, and 3 incoming `RELATES_TO`. These counts do not describe
the older 61 nodes.

## What this evidence permits

It does **not** establish that 66—or any other count—are human-self forks. A non-human episode can
mention the owner, and a human episode can discuss an RBAC/application user. Source provenance is
therefore useful routing evidence but is not node identity authority.

No node is eligible for an automatic migration disposition from these aggregates:

| Required disposition | Established count | Reason |
|---|---:|---|
| `PROVEN_SELF` | 0 | no node-level subject provenance or approved per-UUID evidence was observed |
| `PROVEN_GENERIC_USER` | 0 | a non-human source alone does not prove generic semantics |
| `AMBIGUOUS` | not enumerated | aggregate queries intentionally did not produce a UUID manifest |
| `EXCLUDED_DERIVED_OR_STRUCTURAL` | not enumerated | requires per-node classification |

The safe conclusion is narrower: all 73 exact-name `user` nodes remain outside any approved
migration manifest. The 65 non-human-only nodes must not be canonicalized merely because their
name is `user`; the 7 mixed-source nodes and 1 unmentioned node likewise need per-node evidence.

## Limits and next boundary

This observation did not capture a database backup identity, database-store hash, per-node UUIDs,
properties, facts, or relationship instances. It therefore does not satisfy Phase 7 and cannot be
used as migration input.

The next useful evidence would be either a privacy-safe per-node metadata census or an observe-mode
deployment of the corrected telemetry. The former remains read-only but needs a separately reviewed
query/output contract; the latter changes production deployment/configuration. Neither is performed
or authorized by this report.
