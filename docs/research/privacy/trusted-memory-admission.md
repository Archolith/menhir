# Trusted Memory Admission — proving what is actually user info

## Status

parked

Parked as a future-need design note (originated as a shower thought).

The note's **actual core** (per the author's intent) is narrower than the 28-section dump:
*cryptographically establish that a user-tier write genuinely originated from the human* —
message/device signing or equivalent — so that "this is the user" is proven, not asserted.
Everything else (namespaces, OAuth scopes, cross-user authz, multi-tenant hardening) is
scaffolding around that one question.

State of play in code (`src/menhir`): the *downstream* of this is shipped —
`domain/truth/` tiers a claim by its `source` label and feeds the warden/belief assertion
gate (user 1.0 / structural 0.9 / agent 0.5). But the `source` string is **caller-declared**
and flows straight through `ingest_service` ungated. Agent-harness defaults (`claude-code`,
`codex`) auto-collapse to `agent_inference`, so an agent is down-tiered by default — but
**nothing prevents any caller from explicitly passing `source="user"` to claim the 1.0
tier.** So genuine user-identity authenticity — the thing this note is really about — is
**not built**: the user tier is established by convention/honesty, not proof.

Parked anyway, because at menhir's present local-first single-user scale the realistic
failure is non-adversarial (an agent over-eagerly self-tagging `source="user"`), not an
external impersonator. The cheap fix for that is making the `user` tier un-self-assignable
by agents — but the MCP model blocks it (the agent IS the client; the server cannot see who
truly originated the text without an input-boundary signing surface that does not exist).
Cryptographic user-signing is the clean answer but needs that signing surface at the human
input boundary first. Revisit if/when such a surface exists, or a hosted/multi-user
deployment makes impersonation a real threat.

## If revived, start here (the user-identity ladder)

The motivating question is "is the user really the user?" The key architectural facts that
shape any answer:

```text
menhir is downstream of the human. By the time a write arrives it has already passed
through the agent. menhir can only ever VERIFY a signature/credential it is handed; it
cannot PRODUCE the trust. The signing/attestation surface must live at the human-input
boundary, in the client/harness, OUTSIDE menhir.
```

Four consequences:

```text
1. Sign the endorsement, not the utterance. The stored memory is an agent's distillation of
   what the human said, so a signature over the raw user turn does not transfer to the
   derived claim. The signable event is an explicit human "yes, store this" on the proposed
   memory (the proposed_user_info -> user-approves loop, with the approval signed).
2. The key must be human-gated (passkey/WebAuthn/hardware/keychain unlock) per high-trust
   write. If the agent can sign without a human gate, a compromised/over-eager agent just
   signs everything and the guarantee is gone. The gate IS the security -- and it is a real
   per-write UX cost, so it is only worth it for a few durable user-profile facts.
3. Cross-system dependency. Third-party harnesses (Claude Code, Codex) will not add a
   signing step, so a signing-based version is only realizable in clients you build. menhir
   can publish the envelope + verification contract; it cannot ship the feature alone.
4. At single-user scale you probably want capability separation, not crypto. The real threat
   is not an impersonator but an agent self-tagging source="user". The right primitive is to
   make the user tier STRUCTURALLY un-writable over the MCP/agent path -- only a human-direct
   surface can set it. No keys, no signing, just a capability boundary.
```

The ladder:

```text
Now-ish, cheap:  capability separation -- agents cannot set the user tier; a human-direct
                 write path (local UI / privileged non-agent surface) can. Solves the real,
                 non-adversarial problem (agent self-tagging).
Later, if hosted: human-gated signing at the client boundary + menhir-as-verifier (registered
                 public keys, signature check on user-tier writes). Solves impersonation,
                 once the channel itself is untrusted (networked / multi-user).
```

## Promotion condition

Trusted Memory Admission is primarily an **integrity/provenance property**, the counterpart
to `sealed-recall.md`'s confidentiality property. Unlike Sealed Recall it has a real seam
into a north-star capability: trust tier and source type are a confidence prior and an
evidence-attribution source for `../belief-temporal/belief-layer.md` (a fact's admission
history feeds `safe_to_assert` / `mention_with_uncertainty` / `do_not_assert`). That seam is
its strongest promotion path; the security machinery (signing, OAuth, DPoP) is hardening that
must not block it.

Promote to `supported-by-spike` only when a menhir spike demonstrates **Level 1** against a
named failure mode (see "Failure modes prevented"):

```text
1. source_type and trust_tier are REQUIRED on every write,
2. a namespace admission policy table rejects/downgrades/proposes,
3. agent/tool/external claims cannot write durable user_info,
4. an admission audit row records every decision.
```

Promote to `supported-by-eval` only when archolith-bench's contamination fixture shows a
**zero false durable user_info admission rate** for external/tool/agent-inferred claims,
without dropping genuine user-asserted facts. Do not add signing/OAuth/DPoP/mTLS dependencies
before the Stage-1 policy layer exists and a transparent baseline is in place (anti-sprawl
rule 7).

## Purpose

OAuth/bearer tokens answer *authorization* ("may this caller write a memory?"). They do not
answer *provenance* ("is this fact actually user-originated, or did an agent infer it?").
An OAuth-authorized agent can still submit `User prefers X.` when the true provenance is
`Agent inferred the user might prefer X on weak evidence.` That must not silently enter
durable `user_info`.

Core thesis:

```text
OAuth proves who may speak to Menhir.
Provenance + admission policy decide whether what they said becomes user memory.
```

Menhir should remember not just facts, but **the authority by which each fact became memory**.

## Prior art in menhir (reuse map — read before building)

Much of the substrate already exists; net-new is smaller than the handoff implies.

The *consumption* of provenance is shipped; the *establishment of genuine user identity*
(this note's actual core) is not. The shipped `domain/truth/` stack tiers and gates a claim
by its `source` label, but that label is caller-declared — so it answers "how trusted is a
claim that says it came from the user" without ever answering "did it really come from the
user." The latter is the unbuilt part this note is about.

```text
EXISTS (provenance / trust tier -- ALREADY SHIPPED, recall-time):
  domain/truth/kinds.py        SSOT for source provenance. ANCHOR_KINDS
                               {user,log,test,git,file,external,manual,timestamp} vs
                               SELF_SOURCE_KINDS {agent_inference,llm_summary,retrieval_trace,
                               memory_call_hint}. "Rule zero: retrieval is attention, not truth"
                               -- self/agent sources cannot alone ground a current-truth claim.
                               Source-confidence tiers: USER 1.0 / STRUCTURAL 0.9 / AGENT 0.5.
  domain/truth/attestation.py  TruthAttestation = content + ReviewState trust tier
                               (HUMAN_REVIEWED/AGENT_REVIEWED/UNREVIEWED) + source +
                               source_confidence + assertion_policy (RecallBucket) +
                               warden_decision (ADMIT/FLAG/ATTENUATE/REFUSE). This IS the
                               "trust tier feeds assertion confidence" seam.
  domain/belief_evidence.py    maps provenance evidence_kind -> belief EvidenceSignal ->
                               BeliefScore -> CurrentnessWarden bucket.
  domain/warden.py +           the DECIDE layer; recall_service._apply_frontier runs the
  recall_service               oracle combiner + warden gate over scoring survivors
                               (shipped 2026-06-29; warden_gate default OFF because the
                               agent-written store is evidence-sparse and would over-refuse).
  namespace                    first-class param on add_memory / recall (silo scoping).
  scope tiers                  PERSISTENT / SESSION / CANDIDATE; add_candidate + conflict
                               pipeline implement propose -> review -> accept.

ARCHITECTURE NOTE: menhir enforces provenance at RECALL/ASSERTION time (store everything,
tier by source_confidence, let the warden refuse to ASSERT low-trust content as current
truth) -- NOT at WRITE/ADMISSION time as this handoff proposed. The shipped recall-time
design is better for a memory system: lossless, and it solves agent-inference laundering
without dropping data. The handoff's write-time AdmissionFirewall/namespace model is the
inferior design and is superseded by the shipped warden/belief stack.

NET-NEW that remains (and is parked, not worth building at current scale):
  write-time admission firewall + namespace admission policy (superseded by recall-time warden),
  server-assigned source_type (architecturally hard under MCP -- agent is the client),
  SignedProvenanceEnvelope + ReplayGuard, ObjectLevelAuthz, OAuth/DPoP/mTLS, AdmissionAuditLog.
```

## Two layers

```text
1. Auth/authz (who is calling, what scopes, which namespaces) - bearer now, OAuth later.
2. Provenance/admission (what kind of claim, where from, which trust tier, which namespace).
```

Layer 2 is the missing one.

## Memory namespaces

```text
user_info          high-trust durable personal facts (user-originated or user-approved)
user_observed      facts observed from user-authorized connected systems
project_memory     project / codebase / repo decisions and architecture notes
external_claims    web pages, documents, retrieved or outside claims
agent_inferences   agent guesses, summaries, hypotheses
proposed_user_info candidate user facts awaiting confirmation
system_memory      internal state, migrations, policy, configuration
session_memory     temporary per-session context
ephemeral_memory   short-lived working memory, never durable
```

Key rule:

```text
Only direct user assertion, explicit user approval, or a trusted high-confidence import
path can write durable user_info. An LLM or tool cannot silently write user profile facts.
```

## Trust tiers

```text
T0 external_untrusted    web pages, scraped docs, arbitrary tool output
T1 agent_inferred        agent interpretation, summary, guess, hypothesis
T2 connected_observed    user-authorized source (GitHub, calendar, email, repo, fs)
T3 user_asserted         direct user message / user-authored input
T4 user_approved         user explicitly approved a proposed memory
T5 user_signed           user/device-signed assertion with strong provenance
T6 system_attested       internal migration / policy import / admin-attested
```

Admission rule for user_info:

```text
accepts:  T3, T4, T5, selected T6
rejects:  T0, T1, most T2
```

Connected-observed facts are useful but are not automatically profile facts:
`GitHub says user has access to repo X` becomes `user_observed`/`project_memory`, not
`User is a maintainer of X and prefers Y`.

## Source types

`source_type` is required and, critically, **server-assigned where possible** (clients
should not be trusted to self-declare a high-trust source). Values:

```text
direct_user_message, user_clicked_remember, user_confirmed_suggestion, user_uploaded_file,
user_edited_memory, calendar_observed, email_observed, github_observed, filesystem_observed,
git_observed, repo_scan_observed, web_retrieved, document_retrieved, tool_output,
mcp_tool_output, agent_inferred, agent_summarized, agent_proposed, system_migration, admin_import
```

## Admission firewall

One central gate before any durable write:

```text
admit_memory(event) -> admitted | downgraded | quarantined | proposed | rejected
```

Checks, in order:

```text
1  auth token valid?
2  token subject matches target user? (ObjectLevelAuthz - body cannot escalate)
3  client allowed to write this namespace?
4  object-level authz (project/repo membership, memory ownership)?
5  signature valid (if required for this namespace/tier)?
6  timestamp fresh, within skew?
7  nonce/event_id unused (ReplayGuard)?
8  source_type allowed for target namespace?
9  trust_tier allowed for target namespace?
10 claim type requires confirmation?
11 policy requires redaction / downgrade / quarantine?
12 should this be a proposed memory instead of durable?
```

Worked example:

```text
in:  source_type=agent_inferred, target=user_info, claim="User prefers Rust."
out: reject direct user_info write; store as proposed_user_info (or agent_inferences);
     require user confirmation before promotion.
```

## Promotion workflow (reuse the candidate pipeline)

```text
agent_inferences    -> proposed_user_info -> user_confirmed -> user_info
connected_observed  -> user_observed      -> proposed_user_info (if personal) -> user_info
external_claims     -> proposed_project_fact -> accepted_project_fact
```

This is the existing `CANDIDATE` -> review -> accept lifecycle, scoped by source/trust. It
keeps a clean distinction between what menhir noticed, what it inferred, and what the user
confirmed.

## Signed provenance envelopes (Level 2, deferred)

Every write arrives as a structured, signed event, not a blob. Signature covers the
canonicalized payload (JSON Canonicalization Scheme, deterministic field order, normalized
timestamps, signature field excluded, versioned schema):

```text
canonical = canonicalize(event_without_signature)
event_hash = sha256(canonical)
signature  = sign(device_private_key, event_hash)
```

A signature proves *a registered client/device signed this event* and gives payload
integrity + replay resistance + accountability. It does **not** prove *the human meant this
as durable user info* — that is why source_type + trust_tier + admission policy remain
load-bearing. Require signatures for: user_info writes, user_approved promotions,
admin/system migrations, high-trust project writes. ReplayGuard rejects reused
nonce/event_id, stale timestamps, revoked keys.

## Object-level authorization

Never trust IDs in the request body. `token.sub` (or a delegated/admin grant) determines the
user; the body cannot say `user_id: user_abc` to redirect a write. For project memory, verify
the caller can write to that project and may use the requested source_type/trust_tier. Most
clients may not set arbitrary trust tiers.

## Auth hardening path (do not lead with this)

```text
Stage 1  namespaces + required source_type + required trust_tier + admission policy + audit
Stage 2  signed envelopes, client/device keys, nonce/timestamp replay protection
Stage 3  OAuth scopes (memory:read, memory:write:session/project/external/inference/
         user_proposed/user_confirmed, memory:admin), short-lived tokens, refresh rotation
Stage 4  sender-constrained tokens (DPoP for local/CLI, mTLS for service-to-service)
Stage 5  user-facing proposed-memory inbox: approve/reject/edit + provenance display
```

Normal agents must NOT receive `memory:write:user_confirmed`.

## Failure modes prevented

```text
SilentUserInfoContamination  external/tool/inferred claims become durable user profile facts
AgentInferenceLaundering     a weak agent guess is stored as a confident user assertion
ToolOutputAsAssertion        MCP/tool output is mistaken for a direct user statement
LeakedTokenHighTrustWrite    a copied bearer token pushes a high-trust user_info write
MemoryEventReplay            an old signed write event is replayed later
CrossUserWrite               valid creds for user A write into user B's namespace
UnexplainableMemoryOrigin    a memory exists with no answer to "who wrote this and why admitted?"
```

## Audit log

Every admission decision is recorded: decision (admitted/downgraded/quarantined/proposed/
rejected/requires_confirmation/promoted/revoked), reason_code, actor/client/device/session,
source_type, input vs final namespace and trust_tier, policy_version, signature_verified,
object_authz_passed, nonce, promotion_source_memory_id. This makes "why is this in user
memory?" answerable. (Pairs with Sealed Recall's decrypt audit — admission and decrypt are
logged separately.)

## Data model sketch

```text
memory_events  event_id, event_type, schema_version, actor_type/id, client_id, device_id,
               session_id, source_type, source_ref, source_hash, claim_type, claim_hash,
               target_namespace, requested_trust_tier, observed_at, signature_key_id,
               signature, event_hash, verification_status
memories       memory_id, current_event_id, namespace, trust_tier, claim_type, content_ref,
               valid/learned-time, source_type, source_ref, created_by_*, policy_version, status
admission_audit  audit_event_id, event_id, memory_id, decision, reason_code, policy_version,
               input/final namespace, input/final trust_tier, requires_confirmation,
               signature_verified, object_authz_passed
memory_promotions  promotion_id, from/to_memory_id, approved_by, approval_event_id,
               original/promoted trust_tier + namespace
trusted_keys   key_id, owner/client/device, public_key, algorithm, expires/revoked_at,
               status, allowed_scopes, allowed_namespaces
```

## Integration with Sealed Recall

The two compose into one trust pipeline (admission decides integrity, Sealed Recall provides
confidentiality):

```text
event -> auth/token -> signature/provenance verify -> AdmissionFirewall # gitleaks:allow — architecture labels
      -> trust tier + namespace -> local embedding -> encrypted blob -> vector index -> audit
```

Provenance happens **before** encryption; trust_tier is stored alongside the encrypted memory;
admission and decrypt are audited separately.

## Benchmark ideas (archolith-bench)

```text
ContaminationRate    corpus of {user facts, agent guesses, web claims, tool output, repo obs,
                     connected obs, malicious writes, replays, cross-user writes} ->
                     PRIMARY metric: false durable user_info admission rate (target 0 without
                     confirmation); secondary: genuine-user-fact admit rate, weak->proposed rate
ProvenanceExplain    % of memories that can fully answer who/what/confirmed/source/policy
ReplayTest           replayed signed events rejected on nonce/event_id/timestamp reuse
CrossUserWriteTest   user A creds writing to user B namespace -> rejected by object authz
AgentOverreachTest   weak evidence -> stored as agent_inference/proposed, never user_info
```

## Open design decisions

```text
source_type assigned only by server-side adapters?   clients ever allowed to request trust_tier?
sign all writes or only high-trust?                  user_info always require direct text/approval?
can repeated observed behavior become user_info without confirmation?
connected-source facts: personal or observed memory? project admins define custom policy?
proposed memories expire?                            store rejected writes for audit or discard?
device revocation invalidate old signed events?
```

## What this does not prove

```text
that a compromised trusted device / runtime is contained (it is not)
that a user cannot intentionally approve a false memory (they can)
that the recall-quality seam (trust tier -> belief scoring) actually improves a metric
  (that requires the belief-layer eval, not asserted here)
that signing/OAuth choices are sound (no formal security claim is made here)
```

## Next steps

```text
1. map the proposal onto existing namespace + scope=CANDIDATE + conflict pipeline; identify
   the exact net-new fields (source_type, trust_tier) on the current memory write path
2. scope a Stage-1 spike: required source_type/trust_tier + a small admission policy table +
   "agent/tool/external cannot write user_info" + an audit row, against
   SilentUserInfoContamination + AgentInferenceLaundering only
3. design the ContaminationRate fixture in archolith-bench before any signing/OAuth work
4. only after Stage 1 lands, evaluate whether trust_tier improves belief-layer assertions
```

## Source

Trusted Memory Admission handoff (chat brainstorm), distilled into a single owner doc per the
anti-sprawl rules. Confidentiality counterpart: `sealed-recall.md`.
