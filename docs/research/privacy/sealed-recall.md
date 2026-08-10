# Sealed Recall — local embeddings over encrypted memory content

## Status

parked

Parked as a future-need design note (originated as a shower thought). No current
implementation path: encryption-at-rest solves little at menhir's present local-first,
single-user scale, and the one load-bearing dependency (local embeddings) carries an
unmeasured retrieval-quality cost. Revisit only if a hosted/team/multi-user deployment
creates a real confidentiality requirement. The genuinely separable sub-idea — local
embeddings, evaluated on offline/cost/quality grounds rather than privacy — should be
assessed on its own, decoupled from encryption.

## Promotion condition

Sealed Recall is a **trust/privacy property**, not a recall-quality mechanism. It does not
improve any research north-star capability (temporal recall, belief drift, contradiction,
structure-aware recall, temporal blast radius, evidence attribution, calibration, agent
debugging continuity). Per `process/research-process.md`, it therefore stays a research
note and **must not touch the product roadmap** until it earns a code surface.

Promote to `supported-by-spike` only when a menhir spike demonstrates **Level 1** end to
end against a named failure mode (see "Failure modes prevented"):

```text
1. local embedder produces vectors without sending plaintext off-machine,
2. memory content is encrypted at rest with authenticated encryption,
3. vector search returns candidates WITHOUT decrypting the whole store,
4. only top-k selected memories are decrypted,
5. every decrypt is recorded in an audit sidecar.
```

Promote to `supported-by-eval` only when archolith-bench shows the **decrypt-minimization**
metric (below) holds without a measurable recall loss versus plaintext recall on a shared
fixture. Do not adopt any heavy crypto/PIR/TEE/HE dependency before a transparent
local-abstraction baseline exists (anti-sprawl rule 7).

## Purpose

Menhir does not just store chat history. It stores agent memories, code structure,
temporal facts, Git-aware debugging context, decisions, contradictions, and belief drift.
That makes the memory layer powerful and sensitive. The default vector-memory assumption is:

```text
plaintext memory -> embedding -> vector store -> retrieval -> LLM context
```

The problem is that plaintext memory stays readable in the same store or adjacent tables.
Sealed Recall narrows the window in which plaintext exists:

```text
plaintext memory exists only during local ingest and selected decrypt
```

The honest framing — not "the memory system is private because content is encrypted" but:

```text
Raw content is encrypted at rest; semantic retrieval stays available through a local
vector index. The vector layer may still leak approximate semantic information.
```

Short version: **search the shadow; unseal only the necessary memory.**

## Working thesis

Encrypted-content-plus-vectors is not novel on its own (see Related work). The
differentiated-for-our-use-case contribution is the *combination* menhir is already built
to support:

```text
encrypted temporal agent memory
+ local embeddings
+ selective top-k decrypt
+ Git / code-structure-aware retrieval narrowing the decrypt set
+ supersession-aware memory versions
+ a decrypt audit trail
```

Do not claim novelty in the doc until the prior-art lane below has been worked.

## Architecture

Three storage layers plus an audit sidecar. The mapping flow:

```text
vector_id -> memory_id -> blob_ref -> key_id -> decrypt on demand
```

### MemoryIndex (retrieval + filtering; avoid plaintext where possible)

```text
memory_id, vector_id, embedding, embedding_model, embedding_version,
created_at, learned_time, valid_time_start, valid_time_end,
memory_type, source_type, project_id, repo_id, scope, access_policy,
content_hash, embedding_hash, supersedes_memory_id, superseded_by_memory_id,
blob_ref, key_id
```

### MemoryBlob (encrypted content)

```text
memory_id, encrypted_content, encrypted_metadata, nonce/iv, auth_tag,
encryption_algorithm, encryption_version, key_id, created_at, content_hash
```

Blob may hold: raw memory text, expanded context, agent notes, source excerpts,
summaries, sensitive metadata, and symbol/file references when configured sensitive.

### KeyMap (envelope encryption — never a single master key per memory)

```text
key_id, owner_id, project_id, repo_id, scope, encrypted_data_key,
parent_key_id, key_version, created_at, rotated_at, revoked_at, status
```

Key hierarchy:

```text
root/master key -> project/user key -> per-memory data key -> memory content
```

Benefits: project-level revocation, rotation without rewriting blobs, per-memory
isolation, safer backups, cleaner multi-user future. Requirement is **authenticated**
encryption (AES-256-GCM / XChaCha20-Poly1305 / libsodium secretbox-style); do not
overcommit to one algorithm in the note. Local-first key storage: OS keychain, local
encrypted key file, age/sops-style project secret; KMS/Vault later.

## Retrieval flow

Ingest:

```text
receive text -> classify -> local embed -> mint memory_id -> store vector
-> mint per-memory data key -> encrypt content -> store blob -> store key map
-> discard plaintext from process memory asap
```

The local embedder is load-bearing: a remote embedding API before encryption weakens the
model because plaintext leaves the machine.

Query:

```text
embed query locally -> vector search -> candidate memory_ids
-> metadata filter (project/repo/time/source/scope)
-> decrypt only top-k -> optional rerank on decrypted content
-> approved LLM context pack -> audit log records what was unsealed
```

## Threat model

This is the part that keeps the note honest.

```text
Protects against:
  database dumps exposing raw memory text
  casual plaintext inspection
  accidental LLM ingestion of the whole memory store
  backups containing readable memories

Does NOT fully protect against:
  semantic inference from vectors (nearest-neighbor probing infers topics)
  malicious local process access
  compromised runtime during decrypt
  accidental plaintext logging
  embeddings generated by a remote API
```

Named risk: **SemanticShadowLeak** — the vector index leaks approximate topic
information even when content is sealed (e.g. "this memory is probably about billing /
auth / a medical issue"). The privacy claim must always be scoped to "raw content at
rest," never "the system is private."

## Privacy levels

Sealed Recall is a setting, not a single fixed mode:

```text
L0 Plaintext Dev      plaintext content + metadata + vectors (debug only, not the trust story)
L1 Encrypted Content  plaintext vectors + routing metadata, encrypted content  <- MVP target
L2 + Sensitive Meta   encrypt summaries, tags, file paths/symbols when configured sensitive
L3 Local-Only Vault   vectors local-only, strict key isolation, limited/no sync
L4 Research Mode      secure kNN / TEE / HE / differential-privacy embeddings (NOT MVP)
```

## Menhir-specific angle: code metadata is the real leak

For ordinary chat memory, dates and tags are low-risk. For menhir, code metadata is
revealing on its own. `src/auth/billing/FraudReviewService.ts` tells an observer the
project has billing, has fraud review, and couples auth+billing — without any file
contents. Treat these as potentially sensitive: file paths, symbol names, commit messages,
branch names, ticket IDs, repo names, human names, agent notes, test names, stack traces,
error messages, deployment/environment names.

Boundary that keeps retrieval working:

```text
public routing metadata: memory_id, hashed project_id, memory_type, coarse created_at, vector
encrypted sensitive metadata: exact file path, exact symbol, raw commit message, raw stack trace
```

## Temporal interaction

Sealed Recall must compose with Chronostratum/belief-temporal, not fight it. The rule:

```text
Any plaintext content mutation invalidates the old embedding -> new sealed version.
```

Track `content_hash`, `embedding_hash`, `embedding_model/version`, `encryption_version`,
`supersedes_memory_id`/`superseded_by_memory_id`, valid/learned-time, `expired_at`,
`revoked_at`. Superseded memories stay decryptable unless revoked, so "what did the agent
believe at the time?" still works. Open question: should expired memories stay searchable
but undecryptable, and should revoking a memory also delete its vector?

## Git-aware debugging use case (the strongest menhir fit)

```text
Test C failed. Which memories should be unsealed to explain why?
1. Git graph -> changed commits/files/symbols
2. structure graph -> dependency cone
3. temporal memory -> relevant time window
4. vector search -> semantically relevant candidates
5. decrypt only the survivors
6. LLM gets a small, justified, audited bundle
```

This is the same blast-radius x time query owned by
`../belief-temporal/connected-data-substrates.md`, with selective decryption layered on
top. It is much stronger than generic encrypted vector search because structure+time
shrink the decrypt set before any unsealing happens.

## AuditLog

Every decrypt is auditable:

```text
event_id, memory_id, session_id, agent_id, user_id, project_id, query_hash,
reason, retrieval_rank, decrypted_at, key_id, sent_to_llm, llm_provider,
redaction_applied, retention_policy
```

Trust feature: menhir should know not only what it remembered, but **when a memory was
unsealed, why, and whether it was sent to a model**. `sent_to_llm` is tracked separately
from `decrypted_locally`.

## Failure modes prevented

Per the corpus promotion rule, the concrete failure modes this prevents (the qualifying
criterion for a research note to exist):

```text
PlaintextMemoryDump      a DB/backup dump exposes readable agent memories
WholeStoreLLMIngestion   the entire memory store is accidentally fed to a model
CodeMetadataLeak         file/symbol/commit strings reveal architecture from metadata alone
UnaccountedDecrypt       a memory reaches a model with no record of why it was unsealed
```

## MVP

Build Level 1, boring and buildable:

```text
components:  local embedder, vector table, encrypted blob table, key metadata table,
             retrieval decrypt path, audit log, privacy-level setting
non-goals:   HE vector search, federated sync, multi-org KMS, policy engine,
             differential privacy, formal cryptographic claims
storage:     reuse the existing menhir stack (do not pick a new vector store for this);
             SQLite+sqlite-vec / Postgres+pgvector only if it matches what menhir already runs
acceptance:  content never plaintext after ingest; local embeddings; vector search without
             full decrypt; top-k-only decrypt; decrypt events logged; primitive rotation
             path; superseded memories remain decryptable unless revoked; results carry
             provenance explaining why a memory was unsealed
```

## Benchmark ideas (archolith-bench)

```text
DecryptMinimization   given corpus+query+known-relevant: top-k recall before decrypt,
                      number of decrypts required, irrelevant-decrypt rate, answer quality
MetadataSealing       recall/precision/latency as metadata is progressively encrypted
                      (plaintext -> hashed project/repo -> encrypted paths -> encrypted
                      symbols -> vector-only)
TemporalSupersession  correct memory version chosen for time-scoped questions while sealed
GitBlastRadiusSealed  combined: relevant-memory recall, irrelevant decrypts, time-window
                      and dependency-cone accuracy on a debugging trace
```

## Related work (prior-art lane — work before any novelty claim)

Search terms: encrypted vector database; privacy-preserving vector search; secure nearest
neighbor / secure kNN; searchable encryption semantic search; embedding inversion attack;
text embedding privacy leakage; model/membership inference on embeddings; homomorphic
encryption vector search; confidential computing / TEE vector database; RAG over encrypted
documents; local-first encrypted memory; end-to-end encrypted semantic search; differential
privacy embeddings. Adjacent categories: encrypted document search, zero-knowledge storage,
password managers with encrypted notes, secure enclaves, private information retrieval,
client-side encrypted search, confidential RAG. Each load-bearing source gets a source
card (`process/research-process.md` Step 2) before it informs the design.

## Open design decisions

```text
delete vectors on revoke?              expired memories still searchable?
decryptable after expiration?          top-k decrypt require policy approval?
exact timestamps plaintext or bucketed?  encrypt file paths by default?
store summaries plaintext / encrypted / not at all?
remote embedding only in non-private mode?
temporary per-session decrypt tokens?  cache decrypted memory, and for how long?
```

## What this does not prove

```text
that the vector index is private (it is not — SemanticShadowLeak stands)
that any recall-quality north-star metric improves (Sealed Recall is orthogonal to recall quality)
that the crypto choices are sound (no formal cryptographic claim is made here)
```

## Next steps

```text
1. work the prior-art lane; produce source cards; replace novelty language with "differentiated for our use case"
2. confirm menhir's current embedding path can run a local embedder (today: text-embedding-3-small, remote)
3. scope a Level-1 spike against PlaintextMemoryDump + UnaccountedDecrypt only
4. only then design the DecryptMinimization fixture in archolith-bench
```

The MVP stays boring and buildable; the research frontier (L4) explores stronger privacy
but must not block implementation.

## Source

Sealed Recall handoff (chat brainstorm), distilled into a single owner doc per the
anti-sprawl rules.
