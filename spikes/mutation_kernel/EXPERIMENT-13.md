# Experiment 13 — authority is granted at admission, not trusted from the producer

**Tested implementation:** `44e7a1c3e293b91d5334855750fdf83db9ff3bf0`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Can Menhir keep authority/provenance generic while allowing each extension to decide which evidence is admissible for its own domain, without letting a model/extractor self-promote its output into `user` or `manual` trust?

## Result

Yes, for the tested slice, with an important rule: **the authority label requested by an Assertion producer is untrusted input.**

The admission boundary uses two independent ceilings:

```text
core-issued source grant
  exact source ID + verified source kind + admission mechanism
  authority ceiling
             \
              -> generic AdmissionEngine -> sealed admitted Assertion
             /
extension-owned admission policy
  admit/reject
  domain authority ceiling
```

The effective authority is the weakest of:

1. the producer's requested authority;
2. the core-issued source/ingress ceiling; and
3. the extension policy ceiling.

The extension can reject or lower authority. It cannot raise evidence above the core-issued source grant.

## Core-issued source grants

`SourceAuthorityGrant` is bound to:

- `source_id`;
- verified `source_kind`;
- a trusted `provenance_class` describing the admission mechanism; and
- an `authority_ceiling`.

Reference provenance classes include concepts such as:

- `model_inference`;
- `grounded_user_statement`;
- `trusted_record`;
- `untrusted_record`; and
- `operator_manual`.

These are capabilities issued by the trusted ingestion/admission path, not strings the extractor gets to mint merely by describing its own output.

Every evidence source on the proposed Assertion must have exactly one matching source grant. A proposal claiming its evidence is a `deed` while the core grant says the same source is an `anonymous_tip` is rejected as `evidence_grant_mismatch` before the investigation policy can benefit from the forged label.

For multi-source Assertions, the generic ingress ceiling is the weakest source grant.

## Model self-promotion test

The hostile reference policy deliberately says every test claim may have `user` authority.

The producer also requests `user`.

But the exact evidence grant is:

```text
provenance_class = model_inference
authority_ceiling = agent
```

The generic engine admits the semantic claim at **agent** authority, marks `attempted_promotion=true`, and records the requested/ingress/policy/effective authorities separately.

This proves extension code cannot promote a model-inference grant above its core ceiling even when the extension policy itself is maximally permissive.

## Personality policy

The reference personality policy distinguishes explicit configuration from interpretation.

`personality.explicit_preference` is admitted only when the source grant proves an explicit admission mechanism:

- `grounded_user_statement`; or
- `operator_manual`.

The same user-message source presented through a `model_inference` grant is rejected as `explicit_preference_requires_verified_explicit_source`.

A genuinely grounded user preference can retain `user` authority.

By contrast, inferred/learned personality traits, values, and behavior policies are capped by the extension at `agent` authority even if their evidence originated in a user message. The source can be highly authentic while the *interpretation* remains an inference.

This gives the desired separation:

```text
"user explicitly prefers concise replies"      -> may be user authority
"model infers user is generally impatient"     -> agent authority
```

## Investigation policy

The reference investigation policy demonstrates that source authority is domain-specific and may be lower than the ingress channel permits.

Examples tested:

- recorded `deed` -> at most `trusted_tool`;
- `official_filing` / `court_record` -> at most `trusted_tool`;
- `anonymous_tip` -> `agent`;
- ordinary `user_statement` about an investigative fact -> `agent`;
- `investigator_note` -> `manual` only through an `operator_manual` admission path.

A direct user channel therefore does not automatically make a factual investigative assertion the strongest evidence in the domain. The extension lowers it before the Assertion enters durable history.

A mixed deed + anonymous-tip Assertion receives `agent` authority because both the generic source-grant fold and the investigation policy preserve the weakest required contributor.

## Admission-policy version sealing

The original generic Assertion identity does not include its authority field. That is useful for separating semantic source interpretation from trust metadata, but it would allow a later admission-policy version to produce a different immutable authority envelope under the same Assertion ID.

The admission engine therefore seals accepted Assertions by extending the interpreter version:

```text
extractor-v1|admission:personality.admission@1
```

and recomputes the durable assertion interpretation ID from that sealed interpreter version.

The source key remains unchanged.

The test admits the same source/semantic proposal under personality admission v1 and v2 and verifies:

- same source key;
- distinct admitted assertion IDs.

This prevents a policy change from silently colliding with a prior immutable assertion envelope. How old/new admission versions supersede one another remains a separate lifecycle question.

## Durable admission receipts

`Neo4jAdmissionStore` persists governance receipts separately from semantic Assertions.

A receipt records:

- admitted/rejected status;
- reason;
- proposal assertion ID;
- sealed admitted assertion ID, when any;
- policy ID/version;
- requested authority;
- ingress ceiling;
- policy ceiling;
- effective authority;
- attempted-promotion flag; and
- exact source grant IDs.

Rejected attempts remain auditable even though they never enter Assertion history.

The real-Neo4j test verifies:

- a model claim requesting `user` is persisted only as a sealed `agent` Assertion;
- a model-inferred explicit personality preference is rejected and never persisted as an Assertion;
- a genuinely grounded explicit preference is persisted at `user` authority;
- a forged source-kind attempt is rejected and never persisted;
- admission receipt replay is idempotent; and
- only engine-produced sealed Assertion IDs enter the wrapper's Assertion store.

## Real-Neo4j coverage

Workflow run: `31917395446`  
Job: `95091487012`

The complete branch workflow finished successfully against the throwaway `neo4j:5-community` service. The previous suite contained 46 passing tests and Experiment 13 added 8 admission tests; all passed. The only pytest warning remains Graphiti 0.29.2's Pydantic-v2 deprecation.

The Experiment 13 diff from the Experiment 12 documentation head contains only:

- `spikes/mutation_kernel/admission.py`
- `spikes/mutation_kernel/admission_policies.py`
- `spikes/mutation_kernel/admission_store.py`
- `spikes/mutation_kernel/test_admission_neo4j.py`

No `src/menhir` files changed.

## Boundary learned

The generic authority contract is now more precise:

```text
producer requested authority    untrusted request
source authority grant          core-issued upper bound
extension policy ceiling        domain-owned upper bound
sealed Assertion authority      weakest admitted result
admission receipt               durable explanation of the decision
```

Source authenticity and interpretation authority are not the same thing. A user-authentic source may support only an agent-level inference; a trusted official record may support strong authority for one investigative claim type but not every conclusion drawn from it.

## Limitations / next pressure point

1. **The kernel still has one global total authority order** (`agent < trusted_tool < manual < user`). Investigation already hints that authority is purpose-specific: a deed is strong evidence of recorded title, but not necessarily of beneficial ownership; a user statement may be authoritative for personal preference but weak for external factual truth.
2. **Admission persistence is a wrapper, not an unbypassable core API.** The underlying spike `Neo4jEnvelopeStore` can still be called directly.
3. **Admission receipt + Assertion persistence are not one transaction.** A durable decision receipt may exist even if later Assertion persistence fails; the receipt records whether assertion persistence completed.
4. **Policy upgrades need lifecycle reconciliation.** Version sealing prevents collision, but the spike does not yet automatically supersede/refold Assertions admitted by an older policy version.
5. **Grant issuance itself is assumed trusted.** The experiment defines the capability boundary but does not yet build authentication/authorization for who may issue `grounded_user_statement`, `operator_manual`, etc.

The strongest next mutation is authority shape: test whether a single global scalar authority is actually sufficient across personality and investigation, or whether core should preserve opaque trust dimensions while extensions define how those dimensions rank for a particular claim/fold.
