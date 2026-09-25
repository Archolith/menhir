"""Advisory subject identity, correction classification, and assertion construction.

The deterministic unbindable-claim sentinel uuid, span-local correction-cue classification, and
the committed-proposal -> durable ``TypedAssertion`` builder. Moved verbatim from
``typed_scalar_rules``; the facade module re-exports every name so existing import sites keep
working unchanged.
"""

from __future__ import annotations

import re

from menhir.domain.typed_assertion import TypedAssertion
from menhir.services.typed_scalar_rules_proposal import TypedScalarProposal

# ------------------------------------------------------- (3) binding + persistence + rebuild (C.4.3)
#
# A committed `TypedScalarDecision` is still UNBOUND: its representative proposal carries the raw
# `subject_text` the model extracted, not a resolved entity. Binding happens POST-FINALIZATION —
# after Graphiti has correlated/merged the episode's entities — so we bind against the SURVIVING
# entities linked to the episode, never a pre-merge guess. The rule is fail-closed: a UNIQUE
# name match is authority (persist a fully-bound assertion that can materialize a View); ZERO or
# MULTIPLE matches abstain from authority (persist an ADVISORY event-log entry with a sentinel
# subject the store flags `binding_pending`, so NO display-text-keyed View is ever built — that
# would recreate exactly the lexical sidecar this design rejected).

#: advisory (unbindable) assertions carry a deterministic, per-source-claim sentinel subject_uuid.
#: It (a) satisfies TypedAssertion's non-blank identity rule, (b) can never collide with a real
#: Graphiti Entity uuid, so the repository's write-time `Entity IS NULL` check sets
#: `binding_pending=true` and the fold (materializable_only) never turns it into a UUID-keyed View,
#: and (c) is source_key-stable, so re-perceiving the same unbindable claim lands on the SAME head
#: rather than forking a duplicate. A later orphan-rebind pass (C.4.4) resolves it to a survivor.
_ADVISORY_UUID_PREFIX = "unbound:"


def advisory_subject_uuid(source_key: str) -> str:
    """Deterministic sentinel subject_uuid for an assertion that could not be uniquely bound."""
    return f"{_ADVISORY_UUID_PREFIX}{source_key}"




#: correction cues (lowercase), matched span-locally against the grounded stated_span ONLY (never the
#: wider episode), so a conversational "actually" elsewhere never grants correction authority. Kept to
#: UNAMBIGUOUS phrases -- a false correction grants unearned authority, whereas a missed one merely
#: leaves the tie AMBIGUOUS_ANCHOR (safe), so precision beats recall here. `_CORRECTION_NOT_RE` matches
#: the "not X, Y" shape (e.g. "not 180, 182") without the false positives of a bare "not" substring.
_CORRECTION_CUES: tuple[str, ...] = (
    "actually,", "actually ", "correction:", "i meant", "meant to say", "i was wrong",
    "let me correct", "to correct that", "i misspoke",
)
_CORRECTION_NOT_RE = re.compile(r"\bnot\s+\S+\s*,")


def classify_absolute_semantics(stated_span: str, operation: str = "absolute") -> str:
    """DETERMINISTICALLY classify an absolute's framing from its grounded source span -- 'correction'
    when the span itself carries an explicit correction cue, else 'ordinary'. Pure code (NOT an LLM
    output), a function of the span alone, so all k samples of one claim classify identically and the
    modifier can stay OUT of assertion identity. Only `absolute` operations can be corrections; deltas
    and expires are always 'ordinary'. Conservative on purpose: a missed correction merely leaves the
    tie AMBIGUOUS_ANCHOR (safe), whereas a false correction would grant unearned authority."""
    if operation != "absolute":
        return "ordinary"
    span = (stated_span or "").strip().lower()
    if not span:
        return "ordinary"
    if any(cue in span for cue in _CORRECTION_CUES) or _CORRECTION_NOT_RE.search(span):
        return "correction"
    return "ordinary"


def _assertion_from_proposal(
    p: TypedScalarProposal, *, subject_uuid: str, subject_display: str, valid_at: str,
    learned_at: str, time_basis: str, evidence_tier: str, namespace: str | None,
    perceiver_version: str, name_embedding: "list[float] | None" = None,
    embed_version: str | None = None,
) -> TypedAssertion:
    """Build the durable `TypedAssertion` from a committed proposal + its resolved (or sentinel)
    subject. The proposal's span/ordinal/episode flow through unchanged, so the assertion's
    binding-stable `source_key` is IDENTICAL to the one the proposal predicted (shared
    `build_source_key`) — the whole point of C.4.1's identity contract."""
    return TypedAssertion(
        subject_uuid=subject_uuid, subject_display=subject_display,
        attribute=p.attribute, scope=p.scope, value_kind=p.value_kind, unit=p.unit,
        operation=p.operation, value=p.value, stated_span=p.stated_span,
        episode_uuid=p.episode_uuid, valid_at=valid_at, learned_at=learned_at,
        span_start=p.span_start, span_end=p.span_end, claim_ordinal=p.claim_ordinal,
        time_basis=time_basis, evidence_tier=evidence_tier,
        perceiver_version=perceiver_version, namespace=namespace,
        # deterministic, span-local correction classification (NOT an LLM field; out of identity keys)
        absolute_semantics=classify_absolute_semantics(p.stated_span, p.operation),
        name_embedding=name_embedding, embed_version=embed_version,
    )
