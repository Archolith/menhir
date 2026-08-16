# Experiment 14 — purpose-sensitive trust profiles

**Tested implementation:** `be6b98464b9e4cf7b09ee0bdfa638b301bf28474`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Is the global total authority order (`agent < trusted_tool < manual < user`) expressive enough across radically different extensions, or does Menhir need to preserve richer trust dimensions and let extensions interpret them for a particular claim/fold purpose?

## Result

The single ladder is useful as an admission hard ceiling and backward-compatibility path, but it is too lossy as the complete trust model for the tested personality/investigation cases.

The successful shape is:

```text
AdmissionDecision + core-issued source grants
        ↓
immutable TrustProfile
  protected core facets
  opaque extension facets
        ↓
extension-owned purpose resolver
        ↓
requested purpose authority
        ↓
generic PurposeTrustEngine
        ↓ clamp to admission ceiling
purpose-specific TrustResolution
```

Core still owns the safety boundary: the resolver can never raise an Assertion above the authority granted at admission. The extension owns the meaning of the richer profile for a named purpose.

This is intentionally not a universal numeric confidence score.

## Trust profile

`TrustProfile` is an immutable governance sidecar for one admitted Assertion interpretation. It contains protected core facets including:

- admission authority ceiling;
- admission policy ID/version;
- exact grant IDs;
- provenance/admission classes; and
- verified source kinds.

Extensions may add arbitrary namespaced facets, for example:

```text
investigation.document_role = title_record
investigation.corroboration = two_independent_indexes
```

The generic profile builder refuses extension attempts to write the `core.*` namespace.

The profile is fingerprinted independently of the semantic Assertion and can be persisted/reloaded without the generic store understanding the extension facets.

## Same deed, different investigative purpose

The reference deed is admitted at `trusted_tool` authority through a `trusted_record` grant.

The same immutable profile resolves as:

```text
purpose = recorded_title
  -> trusted_tool

purpose = beneficial_owner
  -> agent
```

The source and admission result did not change. The distinction is that a recorded deed directly addresses recorded legal title but is only indirect evidence of ultimate beneficial ownership.

This is exactly the information the single authority scalar loses.

## Same user statement, different purpose

A grounded user statement about the user's own preference is admitted at `user` authority.

The same profile resolves as:

```text
purpose = explicit_preference
  -> user

purpose = external_fact
  -> agent
```

Authenticity as the user's own statement does not make the user an authoritative source about arbitrary third-party/external reality.

This supports the personality/investigation split without teaching core what either domain means.

## Purpose resolver still cannot self-promote

The investigation resolver intentionally proposes `trusted_tool` for an anonymous tip when the purpose is `lead_priority`: a weak source can be actionable as a lead even though it is weak evidence of truth.

The tip was admitted under an `agent` ceiling.

The generic `PurposeTrustEngine` therefore returns:

```text
resolver request = trusted_tool
admission ceiling = agent
effective = agent
clamped = true
```

Purpose-sensitive interpretation cannot bypass the Experiment 13 admission boundary.

## Same scalar ceiling, different trust shape

Two profiles can share the same admission authority while carrying materially different evidentiary shape.

The fixture gives both a deed and an operator-authored investigative note a `trusted_tool` admission ceiling. For `beneficial_owner`:

- the deed resolver asks for `agent` because it is indirect;
- the operator note resolver asks for `manual` because it represents operator-endorsed synthesis;
- core clamps the latter back to the profile's `trusted_tool` admission ceiling.

So the richer model can distinguish evidence types without converting admission authority into an unrestricted extension-controlled score.

## Legacy/scalar compatibility

`LegacyAuthorityResolver` deliberately ignores purpose and returns the profile's admission authority unchanged.

The test round-trips every existing authority value:

- `agent`
- `trusted_tool`
- `manual`
- `user`

across multiple arbitrary purposes and gets the exact same authority back.

This means production typed-scalar semantics do not need to adopt purpose-sensitive trust merely because richer extensions do. The current total order can remain the default/legacy trust resolver while investigation/personality opt into richer semantics.

## Neo4j persistence

`Neo4jTrustProfileStore` stores immutable `MutationTrustProfile` sidecars and fingerprint-checks replay.

The real-Neo4j fixture persists a deed profile containing opaque investigative facets, reloads it, and verifies:

- profile equality after round-trip;
- extension facets are preserved exactly;
- protected source/provenance facets remain intact;
- replay is idempotent; and
- the restored profile still resolves differently for `recorded_title` vs `beneficial_owner`.

The persistence layer never imports investigation/personality resolver semantics.

## Real-Neo4j coverage

Workflow run: `31917636783`  
Job: `95092111529`

The branch workflow completed successfully against the throwaway `neo4j:5-community` service. The entire pre-existing mutation-kernel suite remained green and all **7 new trust-profile tests** passed. The pytest warning remains the existing Graphiti 0.29.2 Pydantic-v2 deprecation.

The Experiment 14 diff from the Experiment 13 documentation head contains exactly:

- `spikes/mutation_kernel/trust.py`
- `spikes/mutation_kernel/trust_resolvers.py`
- `spikes/mutation_kernel/trust_store.py`
- `spikes/mutation_kernel/test_trust_profiles_neo4j.py`

No `src/menhir` files changed.

## Boundary learned

The authority model now separates four concepts:

```text
admission authority     hard safety ceiling granted at ingestion
trust profile           immutable evidence/governance dimensions
purpose resolver        extension-owned interpretation for a question
trust resolution        purpose-specific result, clamped by admission
```

The total authority order remains useful, but it is no longer required to carry every evidentiary distinction itself.

Core only needs to protect its own facets and the admission ceiling. It can preserve extension facets opaquely rather than learning what `recorded_title`, `beneficial_owner`, `explicit_preference`, etc. mean.

## Limitations / next pressure point

1. **Purpose vocabulary is extension-owned but not yet registered/version-fenced.** A resolver changing the meaning of `beneficial_owner` needs a definition/version lifecycle similar to projection/materializer definitions.
2. **Trust resolution is not yet integrated into fold contributor selection.** The test resolves profiles independently; a real investigation fold may need to rank/filter contributors by purpose before deriving a conclusion.
3. **Profile lifecycle is immutable but not automatically regenerated on admission-policy changes.** Admission v2 creates a new admitted Assertion ID, but trust-sidecar backfill/supersession is not wired yet.
4. **Legacy authority remains the clamp.** That is useful for safety/backward compatibility, but a future design may eventually need a richer core capability ceiling than a single scalar tier.
5. **No cryptographic protection of core facets in the spike.** The boundary is enforced by constructors/APIs, not signatures.

The strongest next experiment is **trust-aware folding**: give an investigative conclusion several immutable contributors (e.g. deed, anonymous tip, operator note) and prove the extension can select/weight them differently for `recorded_title` and `beneficial_owner` while preserving exact included, excluded, and counterevidence contributor IDs. That would test whether richer trust actually composes with the Fold/View machinery rather than merely existing as metadata.
