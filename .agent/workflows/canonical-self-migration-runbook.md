---
description: Operating the canonical-self identity boundary, and what consolidating existing forks still requires
---

# Canonical self: operations runbook

Covers the **prevention** half, which has landed and remains default-off, and states precisely what
the **remediation** half still needs. Consolidating the existing forks is not authorized by this
document and cannot be performed with the tooling in the repository today.

Background: `.agent/plans/menhir-scanner-generic-entity-recall-pollution-rca.md` (accepted RCA) and
`.agent/plans/menhir-canonical-self-remediation-plan.md` in the workspace meta-repo.
The 2026-09-04 observation corrections are in
`.agent/plans/menhir-canonical-self-observation-correction-2026-09-04.md`; they supersede the
parent plan's node-authority, score-bound, and historical-population assumptions.

## What is live

`MENHIR_CANONICAL_SELF_BINDING_MODE` — `off` (default) | `observe` | `enforce`.

| Mode | Behavior |
|---|---|
| `off` | Pre-change behavior exactly. Binding is not evaluated. |
| `observe` | Evaluates and records the decision. Rewrites nothing. |
| `enforce` | Rewrites a declared subject before dedup and isolates canonical self from every undeclared ordinary-resolution path. |

An unrecognized value falls back to `off` with a warning — a config typo must not enable a
durable-write-semantics change.

## Risk profile

This is a single-owner, self-hosted service with no external users, one production app writer and
one Neo4j store. A short interruption is acceptable and the owner can immediately disable a bad
feature. Rollout should match that reality:

- test the exact release candidate, including every change since the deployed commit;
- use the provider and model read from the live production configuration, never a stale wrapup or
  example file;
- deploy once through the existing mechanically classified release path;
- run one disposable synthetic canary through the real public path and roll back on failure.

Do not require representative-user cohorts, multi-replica progression, a prolonged observation
window, or new shadow/canary infrastructure solely for this feature. Those controls add ceremony
without reducing meaningful risk on this installation. This does **not** relax exact release
identity, CI, backup freshness, authentication, writer-fence, or rollback requirements: those
protect the owner's durable data rather than a hypothetical user fleet.

## Activating

Treat this as a durable-write-semantics change, but use the shortest proof appropriate to the risk
profile above.

1. **Define the real release candidate first.** Compare the immutable deployed Menhir commit with
   the candidate and review/test the complete delta. Testing only the canonical-self files is not
   release evidence when unrelated changes will ship in the same image.
2. **Read the live LLM configuration before testing.** Record the active chat and Graphiti
   providers and model from the running container. Run the subject-endpoint corpus and public-path
   persistence E2E against those exact values and the exact candidate image with disposable Neo4j.
   A different OpenAI model is not production-parity evidence merely because it is a real model.
3. **Require normal repository CI and mechanical release classification.** Do not invent a lighter
   class for this feature. If the accumulated delta touches protected configuration, deployment or
   dependency surfaces, use the resulting `security-config` or `maintenance` path.
4. **Deploy the exact tested candidate with `enforce`.** There is no required observe-first stage
   for this installation. Production `observe` deliberately does not inject the endpoint, so it
   can describe legacy self-like output but cannot predict marker compliance or binding yield.
5. **Immediately run one disposable-namespace canary through the public API.** Require a `READY`
   projection, the deterministic canonical UUID, a current-episode relationship and `MENTIONS`
   provenance, zero reserved marker text in all persisted node/relationship properties, replay
   idempotence, and merge refusal in both directions.
6. **Read normal telemetry through one asynchronous processing cycle.** Component `self_binding`,
   event `canonical_self_decision`, should show the expected `bound` decision for the canary and no
   `ambiguous` decision. Also check projection failures/retries and the dedup resolution counters.
   No arbitrary entity names or memory text need to leave the server.
7. **Stop or roll back on a concrete failure.** Any false bind, marker leakage, ambiguous
   declaration, failed canary assertion, or repeated binding-attributable projection failure is a
   release failure. While no consolidation migration has run, restore the prior app/config or set
   the mode back to `off`; no database restore is required.

Use `observe` only when diagnosing legacy extractor output. Its
`self_like_without_subject_authority` and `first_person_unresolved` counts are candidate-population
signals, not identity decisions and not an activation gate. Do not build a discarded shadow
extractor unless the production-model corpus or live canary exposes a problem that requires it.

This mode governs Graphiti entity-node resolution. The typed-scalar and event-history pipelines
have separate existing first-person subject rules; they do not prove that this mode is active and
are outside this runbook's activation decision.

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
   In `enforce`, an undeclared node also cannot acquire canonical self through ordinary candidate
   resolution: pre-stamped inputs are refused, and canonical UUID/marker candidates are excluded
   from exact, similarity, LLM, and override paths. `off` and `observe` preserve legacy resolution.
5. Ambiguous evidence fails visibly and retryably, never by picking one. Proving who authored an
   episode never proves which extracted node is that author: that is a separate, node-level
   question (`proves_self_subject`), and without an answer to it the payload does not bind. No
   property of the extracted NAME can answer it — not the literal string, not its grammatical
   person — because a name is not provenance.
   A declaration must also carry the nonblank external pending-episode UUID for the active
   extraction call. The sole production declaration producer accepts only the receipt-owned
   endpoint on an atomically verified verbatim projection and requires a final current-episode edge
   plus index entry. The display name is never an identity-key fallback.
6. Runtime paths never delete or absorb forks.
7. Telemetry carries enums, counts and UUIDs — never memory text or arbitrary entity names.
8. A canonical-node read failure -- or a missing driver -- is an error, never "absent". Falling
   back on either would let graphiti's replacing save erase the stored node.
   Extracted and stored canonical nodes must match the logical namespace's physical group.
9. The producer census is structural, not sample-based. Any new context constructor, factory
   caller, declaration-helper reference (direct, qualified, rebound, or `getattr`), or executable
   `EXPLICIT_SELF_SUBJECT` reference requires an explicit contract review.
10. Observe records `would_bind`, never `bound`, and telemetry does not expose the opaque declared
    node identifier.
