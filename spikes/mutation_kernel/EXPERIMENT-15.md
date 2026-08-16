# Experiment 15 — trust-aware investigative folding

**Tested implementation:** `458b579c402b216312668a107e02795e334bdc93`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Can purpose-sensitive trust from Experiment 14 participate in actual belief formation without losing exact provenance, excluded evidence, or counterevidence?

The test is intentionally stronger than a trust-metadata lookup. The same immutable admitted assertions and TrustProfiles are folded twice for two different investigative purposes. The result must change because the evidentiary question changed, not because source records were rewritten.

## Result

Yes, in the tested model.

The successful flow is:

```text
immutable admitted Assertions
        +
immutable TrustProfiles
        ↓
PurposeTrustEngine
        ↓
purpose-specific effective authority per assertion
        ↓
extension-owned trust-aware Fold
        ↓
View or Abstention
        +
immutable TrustFoldTrace
```

The fold uses a deliberately conservative rule rather than averaging arbitrary trust into one score:

1. Resolve every current assertion's trust for the named purpose.
2. Find the highest effective authority rank present.
3. If more than one distinct claimed value exists at that highest rank, abstain.
4. If there is one winning value:
   - same-value assertions at the highest rank become required contributors;
   - lower-rank assertions agreeing with the winner are retained as excluded/redundant support;
   - lower-rank assertions claiming another value remain explicit counterevidence.

No count, majority, or recency tie-break is used to defeat equal top-trust disagreement.

## Hostile fixture

One parcel has four admitted ownership assertions:

| source | claim | admission ceiling | extension evidentiary role |
|---|---|---|---|
| deed | Alice | `trusted_tool` | `title_record` |
| operator investigative note | Bob | `trusted_tool` | `beneficial_owner_synthesis` |
| anonymous tip | Bob | `agent` | `anonymous_allegation` |
| third-party user statement | Alice | `agent` | `third_party_statement` |

Every proposal initially requests `user` authority. Experiment 13's admission boundary still clamps each one before the trust-aware fold sees it.

The same four admitted Assertions and profiles are then folded without mutation.

## Recorded-title result

For `purpose=recorded_title`:

- the deed directly addresses recorded title and resolves to `trusted_tool`;
- the investigative note, tip, and third-party statement resolve to `agent` for this purpose.

The resulting View says Alice is the recorded owner.

Its exact evidence roles are:

```text
included contributor:
  deed -> Alice

excluded lower-trust support:
  third-party user statement -> Alice

counterevidence:
  investigator note -> Bob
  anonymous tip -> Bob
```

Only the deed is a contributor to the View's effective authority. Lower-trust agreement does not silently inherit the deed's authority by being on the winning side.

## Beneficial-owner result

For `purpose=beneficial_owner`:

- the operator synthesis directly addresses beneficial ownership and requests `manual`, but its admission ceiling was `trusted_tool`, so the generic PurposeTrustEngine clamps it to `trusted_tool`;
- deed, anonymous tip, and third-party statement resolve to `agent` for this purpose.

The resulting View says Bob is the beneficial owner.

Its exact roles are:

```text
included contributor:
  operator investigative note -> Bob

excluded lower-trust support:
  anonymous tip -> Bob

counterevidence:
  deed -> Alice
  third-party user statement -> Alice
```

Thus the same immutable evidence set forms two distinct, auditable beliefs because the question being asked differs.

## Fold trace

`TrustFoldTrace` is an immutable audit sidecar separate from the normal kernel `View`.

For every current assertion it stores:

- assertion ID;
- TrustProfile ID;
- claimed value;
- role (`included`, `excluded`, `counterevidence`, or `contested`);
- requested purpose authority;
- effective purpose authority after the admission clamp; and
- trust-resolution reason.

The View remains compact and generic: contributor IDs and counterevidence IDs stay on the View, while the trace preserves why each assertion was or was not used.

The trace ID is content-addressed from purpose, resolver ID/version, output status/value, role sets, and participation records. Replay of the same trace is idempotent.

## Equal top-trust conflict

A separate fixture creates:

```text
deed          -> Alice -> trusted_tool for recorded_title
court record  -> Bob   -> trusted_tool for recorded_title
```

The court record is later in time, but the fold does not use recency to override an equally trusted contradictory source.

Result:

```text
Abstention(reason="contested_top_trust")
```

Both top-trust assertion IDs are preserved as the contested evidence set in the outcome/trace.

## Fail-closed profile binding

The fold requires one TrustProfile for every current Assertion and verifies both:

- `profile.assertion_id == assertion.assertion_id`; and
- `profile.source_key == assertion.source_key`.

A missing profile or a profile copied from another Assertion fails before belief formation.

This prevents a stronger TrustProfile from being attached to an unrelated assertion by caller error.

## Real Neo4j end-to-end test

The real-Neo4j fixture persists and reloads:

1. all four admitted Assertions through the generic `Neo4jEnvelopeStore`;
2. all four TrustProfiles through `Neo4jTrustProfileStore`;
3. two purpose-specific conclusion Views through the same generic projection store; and
4. two immutable TrustFoldTrace records through `Neo4jTrustFoldTraceStore`.

After reload:

- exactly two current conclusion Views exist because `purpose` is an opaque View dimension;
- the recorded-title View is byte/structure-equivalent to the pre-persistence result;
- the beneficial-owner View is equivalent to its pre-persistence result;
- both fold traces reconstruct exactly;
- included/excluded/counterevidence assertion IDs remain exact; and
- trace replay does not create a duplicate.

The generic Assertion/View persistence layer does not learn what `recorded_title`, `beneficial_owner`, deed, operator synthesis, Alice, Bob, or ownership means.

## Real-Neo4j coverage

Workflow run: `31918024708`  
Job: `95093095183`

The complete branch workflow finished successfully against the throwaway `neo4j:5-community` service.

Measured result: **67 passed, 1 warning in 20.79s**.

The warning remains Graphiti 0.29.2's Pydantic-v2 class-config deprecation at `graphiti_core/driver/search_interface/search_interface.py:22`.

The Experiment 15 diff from the Experiment 14 documentation head contains exactly:

- `spikes/mutation_kernel/trust_fold.py`
- `spikes/mutation_kernel/test_trust_fold_neo4j.py`

No `src/menhir` files changed.

## Boundary learned

Purpose-sensitive trust now composes with actual mutation-kernel belief formation:

```text
admission authority      hard ingestion ceiling
TrustProfile             immutable evidence/governance shape
purpose resolver         domain interpretation for one question
trust-aware Fold         domain belief-forming algebra
View                     rebuildable current answer
TrustFoldTrace           immutable explanation of contributor selection
```

A major consequence is that resolver semantics are no longer presentation metadata. Changing a purpose resolver can change which assertion becomes a contributor, which value wins, whether a View exists at all, and what counterevidence is attached.

Therefore a purpose resolver has become a versioned projection dependency.

## Limitations / next pressure point

1. **Resolver deployment is not fenced yet.** A stale process holding resolver v1 could currently fold after resolver v2 has been deployed unless a registry fence prevents it.
2. **Resolver changes do not automatically dirty dependent Views.** Existing purpose-specific Views need rebuild scheduling when the resolver contract/version changes.
3. **Trace history is immutable, but current-View lineage does not yet point to a specific resolver deployment record.** The trace records resolver ID/version, but there is no shared resolver-definition registry yet.
4. **The fold rule is intentionally categorical.** Highest-trust-wins is a pressure-test algebra, not a claim that every investigative domain should use that rule.
5. **No trust-profile migration/backfill yet.** Admission-policy upgrades may require new profiles and refolds separately from resolver upgrades.

## Next experiment

Version and deploy purpose-trust resolvers as first-class extension definitions.

A resolver v1 -> v2 change should:

- advance a shared resolver definition/version;
- invalidate the purpose/fold targets depending on that resolver;
- prevent a stale v1 worker from committing a View after v2 publication;
- rebuild from the same immutable Assertions and TrustProfiles under v2;
- replace the current View if the answer changes;
- preserve the old TrustFoldTrace as historical derivation evidence; and
- persist a new trace naming resolver v2.

That is the point where Experiments 12 (deployment fencing), 14 (purpose trust), and 15 (belief formation) converge.