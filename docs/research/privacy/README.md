# privacy & trust — encrypted memory and provenance-gated admission

Cluster 6 of the research corpus. Trust properties of the memory layer, on two axes:
**confidentiality** (encryption at rest, local embeddings, selective decrypt) and
**integrity/provenance** (what is allowed to become durable user memory, and by what
authority). These are trust properties, mostly orthogonal to recall quality. **Both docs are parked** —
future-need shower thoughts whose threat models presume a hosted/multi-user scale menhir does
not have at its present local-first, single-user scale. Kept as design pointers, not active
work. The two compose into one pipeline if ever revived: admission decides integrity, then
Sealed Recall provides confidentiality. The single piece worth reviving first is provenance
tiers feeding belief-layer assertion confidence (see `trusted-memory-admission.md`).

| Doc | Status | Owns |
|---|---|---|
| `sealed-recall.md` | parked | Confidentiality: MemoryIndex/MemoryBlob/KeyMap/AuditLog layering, envelope encryption, privacy levels L0-L4, local-embedding + top-k selective decrypt, the SemanticShadowLeak threat-model caveat, Git/temporal decrypt-set narrowing. |
| `trusted-memory-admission.md` | parked | Integrity/provenance: AdmissionFirewall (admit_memory), memory namespaces, required source_type + trust_tier (T0-T6), signed provenance envelopes + ReplayGuard, object-level authz, promotion workflow over the existing CANDIDATE/conflict pipeline, admission audit. Single owner for memory-admission/provenance concepts. |

Master index: [`../README.md`](../README.md).
