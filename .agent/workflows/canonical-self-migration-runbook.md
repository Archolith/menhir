---
description: Operating the canonical-self identity boundary, and what consolidating existing forks still requires
---

# Canonical self: operations runbook

Covers the default-off **prevention** candidate and states precisely what the **remediation** half
still needs. This branch is implementation evidence, not proof of deployment or activation.
Consolidating the existing forks is not authorized by this document and cannot be performed with
the tooling in the repository today.

Background: `.agent/plans/menhir-scanner-generic-entity-recall-pollution-rca.md` (accepted RCA) and
`.agent/plans/menhir-canonical-self-remediation-plan.md` in the workspace meta-repo.
The 2026-09-04 observation corrections are in
`.agent/plans/menhir-canonical-self-observation-correction-2026-09-04.md`; they supersede the
parent plan's node-authority, score-bound, and historical-population assumptions.

## Candidate code contract (not deployed by this branch)

`MENHIR_CANONICAL_SELF_BINDING_MODE` — `off` (default) | `observe` | `enforce`.

| Mode | Behavior |
|---|---|
| `off` | Pre-change behavior exactly. Binding is not evaluated. |
| `observe` | Preserves legacy writes and recall while evaluating/recording legacy decisions. Rewrites nothing. |
| `enforce` | Constructs canonical self as a structural endpoint, admits only exact owner-confirmed Graphiti assertions, retains unconfirmed proposals on their episode, blocks alternate self writers, and excludes legacy unconfirmed self material from default authoritative recall. |

Runtime startup rejects an explicitly configured unrecognized value. This is deliberate: a typo in
an intended `enforce` activation must not silently run the service in unprotected `off` mode. The
default remains `off` when the variable is absent.

`enforce` startup also requires Graphiti-backed reads and the combined-extraction, exact-edge,
candidate-isolation, and canonical-dedupe patches. A missing patch or failed Graphiti construction
aborts startup; the degraded-client fallback remains available only to `off` and `observe`.

This repository change does not activate any mode in production. `enforce` is fail-closed: if any
confirmation setting is blank, unreadable or inconsistent, semantic self assertions remain
proposal-only.

## Owner-confirmation contract

Menhir verifies but never creates owner authority. There is no signing endpoint, agent tool,
private-key setting or signature field in the graph. Configure all three values:

```text
MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_PATH=/run/secrets/menhir/canonical-self/owner-public.pem
MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_SHA256=<sha256 of raw 32-byte Ed25519 public key>
MENHIR_CANONICAL_SELF_CONFIRMATION_DIRECTORY=/run/secrets/menhir/canonical-self/confirmations
```

The fingerprint is 64 lowercase hexadecimal characters; an optional `sha256:` prefix is accepted.
The public key must be PEM-encoded Ed25519. Mount both the key and confirmation directory read-only.

For episode UUID `E`, the confirmation filename is
`sha256(UTF-8(E)).hexdigest() + ".json"`. The recommended document shape is:

```json
{
  "confirmations": [
    {
      "payload": {
        "assertion": {
          "counterpart": {"labels": ["Entity"], "name": "Chicago", "uuid": "<persistent-entity-uuid>"},
          "fact": "user lives in Chicago",
          "predicate": "LIVES_IN",
          "subject": {"kind": "canonical_self"}
        },
        "claim_digest": "...",
        "claim_revision": 1,
        "direction": "self_to_entity",
        "episode_uuid": "...",
        "evidence_sha256": "...",
        "lane": "graphiti_edge",
        "namespace": "default",
        "policy_version": "menhir-canonical-self-authority-v2",
        "polarity": "affirmed",
        "principal_id": "...",
        "schema_version": 2,
        "temporal_scope": {
          "expired_at": null,
          "invalid_at": null,
          "valid_at": null
        },
        "turn_evidence_uuid": "..."
      },
      "signature": "<base64 Ed25519 signature>"
    }
  ]
}
```

Sign the compact, key-sorted, UTF-8 JSON bytes of `payload` with no ASCII escaping and no NaN
values. The payload is exact: principal, namespace, episode and turn lineage, evidence SHA-256,
lane, endpoint direction, polarity, structured assertion, temporal scope, claim revision, schema,
policy and derived claim digest must all match. `assertion.counterpart.uuid` is the persistent UUID
selected before confirmation; do not sign Graphiti's temporary extraction UUID or infer this value
from a name. Extra, omitted, retyped, stale or replayed fields do not authorize the edge. One file
may contain at most 256 records and one MiB. Schema-v1 confirmations are intentionally rejected and
must be recreated from a newly emitted v2 proposal; never backfill the UUID by name.

Menhir keeps the exact authorized payload on the Graphiti relationship and re-verifies the current
read-only confirmation during final edge resolution and fact-edge recall. It also requires the
actual persisted endpoint direction, stable counterpart UUID, resolved counterpart name and labels,
predicate, fact and three temporal fields to equal the signed payload. Graphiti cannot infer a
timestamp, choose a merely similar duplicate, invalidate another
edge or discard the server-owned payload after approval. Deleting or changing the confirmation
therefore blocks an in-flight final write and revokes an existing relationship from default
authoritative recall. The relationship remains available to explicit operator graph inspection
until a separately approved reconciliation removes it.
Unconfirmed proposals are bounded, lease-guarded JSON receipts on their `:Episodic` evidence node;
they have no authoritative entity edge and do not enter ordinary recall. The rejected self-proposal
episode is not the only risk: later ordinary turns can reuse its text as context. Therefore every
`enforce` receipt skips free-form Graphiti node-summary and attribute hydration. Ordinary nodes also
lose NEW model-generated summaries/attributes in this mode; existing stored state and name
embeddings are preserved. Historical summaries are not repaired or certified. Restoring hydration
requires verified per-input provenance, not inspection of language shape. Confirmed self facts use
a dedicated verified recall lane in `enforce`; general fact-edge feature flags and history-query
classification do not disable it. `off` and `observe` retain their existing hydration behavior.

The test-image launcher binds only explicitly configured public-key and confirmation fixtures at
`/run/menhir-self-authority/owner-public.pem` and `/run/menhir-self-authority/confirmations`, read-only.
It rewrites their environment paths for the container and rejects nonexistent/wrong-type fixtures
before launching Docker. The confirmation DIRECTORY is live-mounted so host additions, atomic file
replacement and revocation can be seen by a running image. Do not place private signing material in
that directory; the E2E signing key stays on the host and its parent directory is not mounted.
Launcher unit tests verify the mount contract, not actual Docker visibility or release acceptance.

In `enforce`, post-extraction maintenance is fenced too. Correlation and lifecycle bridge writers
cannot create unsigned `RELATES_TO` edges involving structural self; merge, unmerge, decay and direct
delete paths refuse operations that would consume or recreate an incident self relationship; and
synthetic fact repair cannot rewrite a signed or self-incident edge. These refusals preserve the
assertion boundary rather than treating maintenance credentials as owner confirmation.

The offline promotion loop is deliberately two-pass: let the first enforce pass finish `READY`,
export and inspect its proposal payload, sign that exact payload outside Menhir, place the record in
the read-only confirmation directory, then use the operator-only `force_reenrich` tool for the same
episode UUID. That tool may reopen a `READY` episode only when its last receipt contains more
proposals than verified confirmations; it cannot reopen arbitrary completed work. The second pass
still performs full extraction, evidence binding and signature verification and fails closed if the
payload changes. Operator access to reprocess is not authority to sign.

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
4. **Deploy the exact tested candidate with `enforce` only after provisioning owner authority.**
   Hash and record the exact read-only Ed25519 public key, create the per-episode confirmation
   directory outside Menhir, verify all three settings in the immutable candidate, and prove that
   missing/mismatched configuration remains proposal-only. There is no required observe-first stage
   for this installation; `observe` preserves legacy behavior and cannot predict confirmed yield.
5. **Immediately run one disposable-namespace canary through the public API.** Require a `READY`
   projection, the deterministic canonical UUID, one current-episode relationship carrying the
   exact verified payload and `MENTIONS` provenance, zero reserved marker text in all persisted
   node/relationship properties, replay idempotence, revocation on confirmation removal, and merge
   refusal in both directions. An unsigned twin must remain proposal-only.
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

This mode now governs the complete canonical-self semantic boundary, not only Graphiti resolution.
In `enforce`, typed-scalar and event-history lanes have no signed promotion path and remain
proposal/advisory-only for canonical self; direct typed repositories reject canonical-self writes;
self-anchored and UUID-less self-alias recall Views are rejected; rebind/restore paths cannot attach
assertions to canonical self; and default recall plus recent/flagged bootstrap context exclude
legacy canonical-self nodes, including historical UUID-less self-alias Views, scalar states/history,
counterpart summaries and event authority. Ordinary non-View entities named `user` remain eligible.
`off` and `observe` preserve those legacy paths for compatibility.

### Residuals before activation

- Confirmation files are external to Neo4j, so their read and the relationship save cannot share a
  transaction. Menhir re-verifies at final resolution and again on every authoritative recall. A
  confirmation revoked in the remaining narrow interval can leave a stored edge, but that edge
  fails the next recall and is not authoritative.
- A stale ordinary-node summary left behind after a historical self edge was already deleted or
  transformed has no current relationship to identify its origin. Current-edge filtering cannot
  discover that text. Do not claim the historical graph is clean until the separately approved
  read-only census and journaled remediation classify it.
- This branch contains no production database evidence. Docker, live-provider, deployment,
  activation and historical remediation remain separate approval gates.

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
5. Ambiguous evidence fails visibly and retryably, never by picking one. Trusted turn authorship
   permits Menhir to construct the structural endpoint; it never authorizes a semantic edge. Each
   semantic attachment requires an exact owner signature over its principal, namespace, episode,
   turn evidence, evidence digest, lane, direction, polarity, assertion, temporal scope, revision,
   schema, policy and claim digest. A classifier or model-selected marker may solicit that proposal
   but can never grant authority. The display name is never an identity-key fallback.
   Graphiti's final resolver rechecks the external confirmation and exact persisted semantic tuple;
   losing the in-memory authorization capability or changing any signed field fails the episode.
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
