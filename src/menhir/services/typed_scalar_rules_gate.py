"""SOURCE-CLAIM-FIRST k-sample consistency gate for typed-scalar proposals.

Vote sentinels, threshold validation, the ``TypedScalarDecision`` verdict record, interpretation
labels, claim grouping/identity reconciliation, and ``gate_typed_scalars``. Moved verbatim from
``typed_scalar_rules``; the facade module re-exports every name so existing import sites keep
working unchanged.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

from menhir.services.typed_scalar_rules_bind import SELF_SUBJECT_DISPLAY, _is_self_reference
from menhir.services.typed_scalar_rules_proposal import TypedScalarProposal

# ---------------------------------------------------------------------------- (2) k-sample gate

#: perception proposals are LLM-derived, so a committed typed-scalar decision is 'agent' tier — the
#: lowest, advisory tier. Higher tiers (user/manual/trusted_tool) come from other paths, NEVER from
#: probabilistic extraction. C.4.3 stamps this onto the persisted :TypedAssertion; the fold's
#: effective-tier is still the weakest required contributor at read time (C.2), never trusted here.
PERCEPTION_EVIDENCE_TIER = "agent"

#: structured veto labels (telemetry), mirroring `perception.VETO_*`: which guard produced a decision.
TS_VETO_COMMIT = "commit"
TS_VETO_SELF_CONSISTENCY = "self_consistency"   # no single interpretation reached the threshold
TS_VETO_TIE = "tie"                             # two interpretations share the modal count (no unique winner)
TS_VETO_UNGROUNDED = "ungrounded"               # defense-in-depth: winner lacks a located span


def _validate_threshold(threshold: float) -> None:
    """A commit threshold must be a real fraction in (0, 1]. Reject bool, non-finite (NaN/inf), and
    out-of-range values EXPLICITLY rather than letting them fail open: `agreement < NaN` is always
    False, so an unchecked NaN/zero/negative threshold would commit a scattered claim. Fail closed."""
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError(f"threshold must be a real number in (0, 1], got {threshold!r}")
    if not math.isfinite(threshold) or not (0.0 < threshold <= 1.0):
        raise ValueError(f"threshold must be finite and in (0, 1], got {threshold!r}")

#: vote sentinels — a sample that did NOT cast a single clean vote for a source claim. Both stay in
#: the denominator (k) but can never WIN, so they can only push a claim toward abstention. They are
#: control-prefixed so a real interpretation label can never collide with them.
_VOTE_ABSENT = "\x00absent"          # the sample did not perceive this source claim at all
_VOTE_CONFLICTED = "\x00conflicted"  # the sample emitted >=2 DIFFERENT interpretations of it


@dataclass(frozen=True)
class TypedScalarDecision:
    """Committed-or-abstained verdict for ONE source claim (`source_key`), with the full
    deterministic vote trail so abstention is observable (never a silent skip). `proposal` is the
    representative interpretation when committed — it carries the typed value, located span, episode
    provenance, and raw subject_text that C.4.3 binding + persistence consume; None on an abstention.
    No persistence happens here."""

    source_key: str
    committed: bool
    reason: str
    veto: str
    agreement: float
    k: int
    distribution: dict[str, int]
    proposal: TypedScalarProposal | None = None
    evidence_tier: str = PERCEPTION_EVIDENCE_TIER


def _interpretation_label(
    p: TypedScalarProposal, *, include_attribute: bool = True,
    include_scope: bool = True, include_subject: bool = True,
    canonical_self: bool = False,
) -> str:
    """The vote key WITHIN a source claim: the fully-interpreted reading, independent of source
    LOCATION (that is the `source_key`) but INCLUDING subject and effective world-time. Two samples
    agree only if they read the SAME subject, slot, operation, value, AND when â€” agreement on a value
    with a different date is NOT agreement on the same assertion. subject_text is normalized here
    (strip+lower) purely for the vote; the representative proposal keeps its raw subject_text for
    C.4.3 binding. Joined with a control separator so free-text subject_text cannot forge a field
    boundary.

    The three IDENTITY fields -- subject, attribute, scope -- can each be dropped from the vote and
    reconciled after grouping (see `gate_typed_scalars`). They are separable switches because they
    carry different risk, but they describe ONE defect: the model distributes a fact's identity
    across them in whatever order it likes. Read the residual gate losses and the same span comes
    back decomposed three ways --

        attr='watched'            scope='last_3_months'
        attr='watched'            scope='mcu_films'
        attr='mcus_watched_count' scope=''

    -- with subject, value, unit, operation, `when` and the span itself identical. Exact-matching
    each field independently turns one agreed fact into three single-vote claims. Measured on the
    LME panel: the attribute name ALONE vetoed ~11% of trials where all k samples emitted the asked
    value against the same episode; scope accounts for most of what survives that.

    `canonical_self` folds first-person subjects to `SELF_SUBJECT_DISPLAY` via the same
    `_is_self_reference` predicate the BINDER uses. Without it the vote key contradicts the binder:
    two samples that both correctly identify the self, one writing "I" and one writing "user", are
    counted as disagreeing about the subject even though binding resolves them to one entity.

    value_kind / unit / operation / when are never dropped -- they come from constrained
    vocabularies where disagreement is a genuine difference of reading, and dropping them measured
    worth ~0-2% each. `operation` in particular MUST stay: "I've added 25 postcards" (delta) and "I
    have 25 postcards" (absolute) fold into completely different Views."""
    subject = p.subject_text.strip().lower()
    if canonical_self and _is_self_reference(subject):
        subject = SELF_SUBJECT_DISPLAY
    fields = []
    if include_subject:
        fields.append(subject)
    if include_attribute:
        fields.append(p.attribute)
    if include_scope:
        fields.append(p.scope)
    fields.extend((
        p.value_kind, p.unit, p.operation,
        p.normalized_value,
        p.when or "",
    ))
    return "\x1f".join(fields)


def _reconciled_identity(
    members: "list[TypedScalarProposal]", fields: "tuple[str, ...]",
) -> "tuple[str, ...]":
    """Pick ONE value for each of `fields` for a group of proposals that agreed on everything else.

    Votes on the field COMBINATION rather than each field independently, because the fields are not
    independent: `attr='watched' scope='mcu_films'` and `attr='mcus_watched_count' scope=''` are two
    spellings of one slot, and choosing the modal attribute and the modal scope separately can
    synthesize a pair (`mcus_watched_count` + `mcu_films`) that NO sample proposed. Voting on the
    tuple also guarantees a representative proposal carrying the winning combination exists, which
    keeps `slot_key` internally coherent.

    Modal first â€” if two of three samples said `postcard_count`, that is the reading. Ties break to
    the LONGEST combination, then lexicographically. Length is a deliberate bias toward specificity:
    when samples split 1-1-1 between `count`, `postcard_count`, and `zebra_count`, all three are
    truthful but `count` is a strictly worse ScalarStateView slot -- it collides with every other
    tally the subject owns. Both tie-breaks are pure functions of the candidate SET, never of sample
    order, so the chosen slot cannot change when the same samples arrive in a different sequence."""
    votes = Counter(tuple(str(getattr(p, f)) for f in fields) for p in members)
    top = max(votes.values())
    return sorted(
        (combo for combo, n in votes.items() if n == top),
        key=lambda c: (-sum(len(v) for v in c), c),
    )[0]


def _reconciled_attribute(members: "list[TypedScalarProposal]") -> str:
    """Attribute-only reconciliation. Retained as the published single-field entry point; the
    behaviour is `_reconciled_identity` restricted to one field."""
    return _reconciled_identity(members, ("attribute",))[0]


def _claim_groups(
    samples: "list[list[TypedScalarProposal]]", *, align_spans: bool,
) -> "list[list[tuple[int, TypedScalarProposal]]]":
    """Partition the k samples' proposals into CLAIM groups, in deterministic first-seen order.

    Default (`align_spans=False`): group by exact `source_key`, the durable claim locator. Two
    samples that quoted the same fact with spans differing by one character are then two claims, each
    seen by one sample, and each vetoed for want of k votes.

    `align_spans=True`: group by same episode + a COMMON span intersection, at most one proposal per
    sample. Common intersection rather than chained pairwise overlap -- chaining drags
    non-overlapping A and C together through a long B that touches both. Ambiguous groups (two
    proposals from ONE sample in the same component) are left ungrouped, so the worst case is the
    status quo, never a wrong merge. Singletons pass through unchanged."""
    flat = [(si, p) for si, sample in enumerate(samples) for p in sample]
    if not align_spans:
        order: list[str] = []
        by_key: dict[str, list[tuple[int, TypedScalarProposal]]] = {}
        for si, p in flat:
            if p.source_key not in by_key:
                by_key[p.source_key] = []
                order.append(p.source_key)
            by_key[p.source_key].append((si, p))
        return [by_key[k] for k in order]

    by_episode: dict[str, list[int]] = defaultdict(list)
    for i, (_si, p) in enumerate(flat):
        by_episode[p.episode_uuid].append(i)

    groups: list[list[tuple[int, TypedScalarProposal]]] = []
    grouped: set[int] = set()
    for _episode, idxs in by_episode.items():
        edges: dict[int, set[int]] = defaultdict(set)
        for a in idxs:
            sa, pa = flat[a]
            for b in idxs:
                if b <= a:
                    continue
                sb, pb = flat[b]
                if sa == sb:  # a sample never overlaps itself into one claim
                    continue
                if pa.span_start < pb.span_end and pb.span_start < pa.span_end:
                    edges[a].add(b)
                    edges[b].add(a)
        seen: set[int] = set()
        for start in idxs:
            if start in seen:
                continue
            stack, component = [start], []
            seen.add(start)
            while stack:
                cur = stack.pop()
                component.append(cur)
                for nb in edges.get(cur, ()):
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            if len(component) < 2:
                continue
            spans = [(flat[i][1].span_start, flat[i][1].span_end) for i in component]
            sample_ids = [flat[i][0] for i in component]
            if max(s for s, _ in spans) >= min(e for _, e in spans):
                continue  # no span common to ALL members
            if len(set(sample_ids)) != len(sample_ids):
                continue  # a sample appears twice -- ambiguous, leave it alone
            common_start = max(s for s, _ in spans)
            common_end = min(e for _, e in spans)
            verifiable_text: str | None = None
            for i in component:
                proposal = flat[i][1]
                if len(proposal.stated_span) != proposal.span_end - proposal.span_start:
                    continue
                offset = common_start - proposal.span_start
                verifiable_text = proposal.stated_span[offset:offset + (common_end - common_start)]
                break
            if verifiable_text is not None and not re.search(r"\w", verifiable_text, re.UNICODE):
                continue  # punctuation/whitespace overlap is not a grounded claim anchor
            groups.append([(flat[i][0], flat[i][1]) for i in sorted(component)])
            grouped.update(component)
    for i, (si, p) in enumerate(flat):
        if i not in grouped:
            groups.append([(si, p)])
    return groups


def _canonical_aligned_proposal(
    proposal: TypedScalarProposal,
    candidates: list[TypedScalarProposal],
) -> TypedScalarProposal:
    """Return the winner grounded to the deterministic intersection shared by its quote variants.

    The intersection is independent of sample order and is still an exact source substring, so one
    k-sample decision cannot pick a different durable ``source_key`` merely because sample order
    changed. Directly-constructed test proposals may carry synthetic offsets that do not match their
    quote length; those use the shortest candidate's location while preserving the already-selected
    winning interpretation.
    """
    if len(candidates) < 2:
        return proposal
    start = max(candidate.span_start for candidate in candidates)
    end = min(candidate.span_end for candidate in candidates)
    fallback = min(
        candidates,
        key=lambda candidate: (
            candidate.span_end - candidate.span_start,
            candidate.span_start,
            candidate.span_end,
            candidate.source_key,
        ),
    )
    if start >= end:
        return replace(
            proposal,
            stated_span=fallback.stated_span,
            span_start=fallback.span_start,
            span_end=fallback.span_end,
        )
    for candidate in sorted(candidates, key=lambda item: item.source_key):
        if len(candidate.stated_span) != candidate.span_end - candidate.span_start:
            continue
        offset = start - candidate.span_start
        quote = candidate.stated_span[offset:offset + (end - start)]
        if quote and re.search(r"\w", quote, re.UNICODE):
            return replace(proposal, stated_span=quote, span_start=start, span_end=end)
    return replace(
        proposal,
        stated_span=fallback.stated_span,
        span_start=fallback.span_start,
        span_end=fallback.span_end,
    )


def _readable_vote(label: str) -> str:
    """Render one vote key for the audit trail. Pure; used only for observability.

    `_interpretation_label` joins its 8 fields with \\x1f, and non-votes are the \\x00-prefixed
    sentinels. Both are control characters that survive JSON but are unreadable in a details blob,
    so votes become `subject|attribute|scope|kind|unit|op|value|when` and sentinels become plain
    words. Unknown shapes are returned unchanged rather than mangled.
    """
    if label == _VOTE_ABSENT:
        return "(absent)"
    if label == _VOTE_CONFLICTED:
        return "(conflicted)"
    return label.replace("\x1f", "|")


def gate_typed_scalars(
    samples: list[list[TypedScalarProposal]], *, threshold: float = 1.0,
    reconcile_attribute: bool = False, reconcile_scope: bool = False,
    reconcile_subject: bool = False, canonical_self: bool = False,
    align_spans: bool = False,
) -> list[TypedScalarDecision]:
    """SOURCE-CLAIM-FIRST k-sample consistency gate. For each distinct `source_key` seen across the k
    proposal samples, commit ONE interpretation only if it is concentrated:

      1. GROUP by `source_key` (the binding-stable claim locator), so unrelated unbound proposals â€”
         e.g. two episodes each stating a different subject owns 2 cats â€” stay separate claims and are
         never collapsed before C.4.3 resolves identity.
      2. Within a source claim, each sample casts AT MOST ONE vote for its `interpretation_label`
         (subject + slot + operation + value + when). A sample that read the claim TWO different ways
         is internally conflicted and casts NO vote (`_VOTE_CONFLICTED`); a sample that did not
         perceive the claim at all is `_VOTE_ABSENT`. Neither can win, but both remain in the
         denominator â€” the denominator is always the configured `k`, so omissions and conflicts count
         AGAINST agreement rather than vanishing from it.
      3. `agreement` = (votes for the modal real interpretation) / k. Commit iff `agreement >=
         threshold` (default 1.0 = unanimous). Sentinels are excluded from being the winner but not
         from `k`.
      4. Defense-in-depth: never commit a winner without a located source span (C.4.1 already
         guarantees this, so it should not fire â€” but it fails closed if a caller injects proposals
         directly).

    Returns one `TypedScalarDecision` per source claim (first-seen order, deterministic), each with
    its full vote distribution so abstention is observable. Committed decisions carry the
    representative `proposal` for C.4.3; nothing is persisted or bound here. Pure and deterministic.

    Commits ONLY a UNIQUE modal interpretation: if two interpretations share the top real count, the
    winner is ambiguous and the claim abstains (`TS_VETO_TIE`) even if that count meets `threshold` â€”
    a 2-2 split must never resolve to whichever the sort happened to place first. `threshold` is
    validated to (0, 1] up front so a NaN/zero/negative value cannot fail open.

    TWO EXPLICIT RELAXATIONS default off at this pure boundary. The production perception service
    always requests conservative span grounding; the free-text identity reconciliations remain
    configured separately. Both address the same measured defect: on the LME panel the model emits
    the asked value in ~100% of namespace-trials and all k samples emit it in ~81%, but only 23%
    commit. Much of that loss is DISAGREEMENT ABOUT THE KEY rather than about the value.

    CORRECTION -- an earlier revision of this docstring claimed lowering `threshold` recovered
    nothing. That was a measurement error: the sweep used 0.67, and 2/3 = 0.6666... is strictly less
    than 0.67, so a 2-of-3 majority could never clear it and every configuration scored identically.
    Re-measured at exactly 2/3 on the same frozen panel, recovery of the asked value is:

                        threshold 1.0   threshold 2/3
        both off                   23              51
        reconcile_attribute        34              72
        align_spans                33              54
        both                       45              74

    So the unanimity requirement is the LARGEST single lever, larger than either relaxation, and the
    two are complementary rather than alternatives. It is not free: commits rise 64 -> 298 while the
    share matching the benchmark's labelled current value falls 41% -> 32%, and slots holding two
    contradictory values in one pass rise 22 -> 68. `threshold` is a validated caller argument,
    supplied by `personal_memory_scalar_threshold` (default 1.0).

      * `reconcile_attribute` â€” drop the free-text attribute NAME from the vote and choose it after
        grouping (`_reconciled_attribute`). Samples routinely agree on subject/slot/value/when and
        disagree only on what to call the attribute. Replayed on the frozen panel: 23 -> 34 of 100.
      * `align_spans` â€” group claims by common span intersection instead of exact `source_key`, so a
        one-character difference in the quoted span is no longer two separate single-vote claims.
        Replayed: 23 -> 33 of 100; with both, 45 of 100.
        The committed span is the exact common source substring, independent of sample order.
        Re-perception with a materially different set of quote boundaries can still select a
        different substring, so callers must retain normal namespace reset/idempotency discipline."""
    _validate_threshold(threshold)
    k = len(samples)
    if k == 0:
        return []

    # Partition into claim groups, then index each group as a per-sample SET of distinct
    # interpretation labels. rep maps (group index, label) -> every proposal that read it, first-seen
    # first, so the committed representative is deterministic regardless of value type (37 vs 37.0
    # normalize equal) and attribute reconciliation has the full candidate set to choose from.
    groups = _claim_groups(samples, align_spans=align_spans)
    # The identity fields dropped from the vote, in label order, reconciled together afterwards.
    reconciled_fields = tuple(field for field, enabled in (
        ("subject_text", reconcile_subject),
        ("attribute", reconcile_attribute),
        ("scope", reconcile_scope),
    ) if enabled)
    per_source: list[list[set[str]]] = [[set() for _ in range(k)] for _ in groups]
    rep: dict[tuple[int, str], list[TypedScalarProposal]] = defaultdict(list)
    for gi, members in enumerate(groups):
        for si, p in members:
            label = _interpretation_label(
                p,
                include_attribute=not reconcile_attribute,
                include_scope=not reconcile_scope,
                include_subject=not reconcile_subject,
                canonical_self=canonical_self,
            )
            per_source[gi][si].add(label)
            rep[(gi, label)].append(p)

    decisions: list[TypedScalarDecision] = []
    for gi, members in enumerate(groups):
        sk = members[0][1].source_key
        votes: list[str] = []
        for interps in per_source[gi]:
            if len(interps) == 0:
                votes.append(_VOTE_ABSENT)
            elif len(interps) == 1:
                votes.append(next(iter(interps)))
            else:
                votes.append(_VOTE_CONFLICTED)  # >=2 readings in one sample -> no clean vote
        dist = Counter(votes)

        # the winner is the top REAL interpretation (sentinels can dilute but never win).
        real = [(lbl, c) for lbl, c in dist.most_common() if lbl not in (_VOTE_ABSENT, _VOTE_CONFLICTED)]
        top_label, top_n = (real[0] if real else (None, 0))
        agreement = top_n / k

        if top_label is None or agreement < threshold:
            decisions.append(TypedScalarDecision(
                source_key=sk, committed=False,
                reason=f"scattered: modal interpretation holds {top_n}/{k} (threshold {threshold})",
                veto=TS_VETO_SELF_CONSISTENCY, agreement=agreement, k=k, distribution=dict(dist),
            ))
            continue

        # unique-winner guard: a tie at the modal count has no unambiguous interpretation, so abstain
        # even when that count meets threshold (a 2-2 split at threshold=0.5 must NOT commit whichever
        # the sort ordered first). Only reached when agreement >= threshold, so it never masks scatter.
        if len(real) >= 2 and real[1][1] == top_n:
            decisions.append(TypedScalarDecision(
                source_key=sk, committed=False,
                reason=f"tie: {len(real)} interpretations share the modal {top_n}/{k}",
                veto=TS_VETO_TIE, agreement=agreement, k=k, distribution=dict(dist),
            ))
            continue

        # All winners read the claim identically on every voted field; when `attribute` was excluded
        # from that vote they may still differ on the NAME, so reconcile it and commit the proposal
        # that actually carries the winning name -- its slot_key must stay internally coherent.
        candidates = rep[(gi, top_label)]
        proposal = candidates[0]
        if reconciled_fields:
            winning = _reconciled_identity(candidates, reconciled_fields)
            proposal = next(
                p for p in candidates
                if tuple(str(getattr(p, f)) for f in reconciled_fields) == winning
            )
        if align_spans:
            proposal = _canonical_aligned_proposal(proposal, candidates)
        # Under span alignment the proposal is grounded to the deterministic common source span, so
        # the durable identity is independent of which sample happened to be seen first.
        sk = proposal.source_key

        if proposal.span_start < 0 or proposal.span_end < 0:  # defense-in-depth (C.4.1 guarantees >=0)
            decisions.append(TypedScalarDecision(
                source_key=sk, committed=False,
                reason="ungrounded: winning interpretation has no located source span",
                veto=TS_VETO_UNGROUNDED, agreement=agreement, k=k, distribution=dict(dist),
            ))
            continue

        decisions.append(TypedScalarDecision(
            source_key=sk, committed=True,
            reason="unanimous" if agreement >= 1.0 else f"concentrated {agreement:.2f}",
            veto=TS_VETO_COMMIT, agreement=agreement, k=k, distribution=dict(dist),
            proposal=proposal,
        ))
    return decisions
