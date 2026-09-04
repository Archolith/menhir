---
description: Operating the canonical-self identity boundary, and what consolidating existing forks still requires
---

# Canonical self: operations runbook

Covers the **prevention** half, which has landed and ships dormant, and states precisely what the
**remediation** half still needs. Consolidating the existing forks is not authorized by this
document and cannot be performed with the tooling in the repository today.

Background: `.agent/plans/menhir-scanner-generic-entity-recall-pollution-rca.md` (accepted RCA) and
`.agent/plans/menhir-canonical-self-remediation-plan.md` in the workspace meta-repo.

## What is live

`MENHIR_CANONICAL_SELF_BINDING_MODE` — `off` (default) | `observe` | `enforce`.

| Mode | Behavior |
|---|---|
| `off` | Pre-change behavior exactly. Binding is not evaluated. |
| `observe` | Evaluates and records the decision. Rewrites nothing. |
| `enforce` | Rewrites a proven human to the deterministic UUID before dedup. |

An unrecognized value falls back to `off` with a warning — a config typo must not enable a
durable-write-semantics change.

## Activating

Treat this as a durable-write-semantics change, not an app-only config flip.

1. **Run `observe` first, long enough to cover representative traffic.** Its verdict is designed to
   match what `enforce` would do, so the window is only meaningful if you actually read it.
2. **Read the decision records.** Component `self_binding`, event `canonical_self_decision`:

   | Field | What it tells you |
   |---|---|
   | `outcome` | `bound` / `not_eligible` / `no_self_candidate` / `ordinary_user_entity` / `ambiguous` |
   | `ordinary_user_entity` | self-alias entities in a trusted turn that binding declined for lack of a declared subject. **Expect this to be every self-bearing episode**, since nothing emits `EXPLICIT_SELF_SUBJECT` today. Its volume is the size of the prize for building per-node subject provenance. |
   | `first_person_without_authority` | the subset of those that were first-person. The sharper number: how many binds a span/attribution signal from extraction would enable. |
   | `bound` (non-zero) | a producer has started declaring self subjects. Find out which, and what it actually proves, BEFORE trusting the count. |
   | `ambiguous` | two declared aliases in one payload; binding refused and wrote nothing. |
   | `self_like_without_evidence` | self-alias entities that did NOT bind, on any outcome |
   | `evidence_kind`, `speaker_role`, `source_kind` | why the decision went that way |

   Ambiguous refusals are recorded before they raise, and in `observe` they do not fail the
   episode -- observing must never change ingest success.

3. **The gate is `self_like_without_evidence`.** A persistently non-zero count means self-like
   entities are still being minted outside the trusted path and are still fragmenting recall.
   Understand them by producer and source before enforcing. Zero known false-positive binds is the
   activation requirement.
4. **Watch the dedup branch counters** (component `graphiti_dedup`, events
   `deterministic_resolution_branches` and `resolution_outcomes`). `multiple_exact_llm` staying
   high for a name means that name's candidate window is saturated — the exact condition that
   fragmented `user` — and `candidate_count_max` pinned at the window limit is the same signal.
   Per-candidate cosine scores are deliberately absent: graphiti's search ranks by score and then
   drops it from the returned record, so any reported bound would have been measured from
   embeddings that are `None` in production. `llm_prompt_*` sizes the dedupe prompt by section
   (entities, candidates with attributes, episode, previous episodes).
5. **Do not set `enforce` expecting it to prevent forks today.** With no `EXPLICIT_SELF_SUBJECT`
   producer, `enforce` and `off` are behaviorally identical — the flag is ready for a mechanism
   whose evidence source does not exist yet. Build per-node subject provenance first; until then
   `observe` is the only mode worth running, and only to size that work.

### Rollback asymmetry

Turning `enforce` back to `off` is safe **while no migration has run**. After a consolidation
migration commits, do not reopen the old probabilistic writer against the migrated graph: fix
forward, or restore the pre-cutover database and app release together as one pair.

## Detecting forks

`ensure_self_entity` is non-destructive. When it finds same-named forks it logs

```
SELF_FORKS_REQUIRE_MIGRATION namespace=<ns> canonical=<uuid> forks=<n>
```

and leaves them untouched. `MemoryGraphAdapter.detect_self_forks(namespace)` returns the same
inventory read-only.

Detection deliberately reads **both** physical spellings (`""` and the logical namespace name),
because the pre-fix writer stamped `group_id = <logical name>`. A single-spelling read reports
zero forks on exactly the population that has them.

## Consolidation: NOT available

There is no migration tool in this repository, and the old absorber has been removed. Do not
reconstruct one from git history.

`_absorb_self_entity_forks` rewired every incident relationship and ended in `DETACH DELETE`, as a
side effect of an ordinary write. Its `m.uuid <> $self_uuid` predicates deliberately dropped
fork-to-canonical relationships as "split artifacts" — relationships the migration contract
requires be preserved. **Its Cypher must not be revived, adapted, or used as a template.** A test
parses the module AST and fails if that predicate or `DETACH DELETE` returns to a query literal.

Consolidation requires, in order, none of which exist yet:

1. A read-only census of the candidate population, hash-bound to the production release, Graphiti
   artifact and database identity.
2. A disposition manifest classifying every UUID as `PROVEN_SELF`, `PROVEN_GENERIC_USER`,
   `AMBIGUOUS` or `EXCLUDED_DERIVED_OR_STRUCTURAL` — with relationship-level decisions for any
   proposed deletion. **Never select nodes by name.** Ambiguous nodes are quarantined, never
   auto-folded into the human.
3. Owner approval of that manifest.
4. A journaled, resumable, reversible migration with an encrypted inverse bundle and an explicit
   delete barrier.
5. Apply / rollback / re-apply proven on a restored production copy.
6. Independent review with zero unresolved critical or high findings.
7. A fenced production cutover with a fresh verified backup.

### Known cutover trap

Production's compose configuration includes an overlay that exists only in `/tmp`
(`/tmp/menhir-operator-scope-hotfix.yml`), which sets the OAuth scope variables including
`menhir:admin`. Any cutover that restarts the stack without promoting it to a durable, root-owned
location silently loses operator scope. The failure surfaces as `unauthorized_client — OAuth
authorization scopes do not match production client policy` immediately after a graph migration,
where it will look like the migration broke authentication.

Promote and hash the overlay before fencing, and assert the running container's `config_files`
list contains no `/tmp` path.

## Invariants a change here must not break

1. `normalize(name) == "user"` is never, by itself, proof of the human.
2. One UUID formula: `self_uuid_for_namespace()`. A static test fails on a second copy.
3. One logical→physical mapping: `namespace_to_group_id()`.
4. A proven self causes zero candidate searches and zero dedup-LLM calls.
5. Ambiguous evidence fails visibly and retryably, never by picking one. Proving who authored an
   episode never proves which extracted node is that author: that is a separate, node-level
   question (`proves_self_subject`), and without an answer to it the payload does not bind. No
   property of the extracted NAME can answer it — not the literal string, not its grammatical
   person — because a name is not provenance.
6. Runtime paths never delete or absorb forks.
7. Telemetry carries enums, counts and UUIDs — never memory text or arbitrary entity names.
8. A canonical-node read failure -- or a missing driver -- is an error, never "absent". Falling
   back on either would let graphiti's replacing save erase the stored node.
