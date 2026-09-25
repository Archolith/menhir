"""Deterministic typed-scalar extraction, gating, temporal, and binding rules.

Facade module: the rule set is decomposed into ``typed_scalar_rules_*`` sibling modules, each of
which owns one cohesive unit moved verbatim from this file. Every moved symbol is re-exported
here, so every existing import site keeps working unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from menhir.clock import utc_now_iso as _utc_now_iso
from menhir.domain.typed_assertion import OPERATIONS, VALUE_KINDS, validate_value
from menhir.services.seam_types import LlmComplete
from menhir.services.typed_scalar_rules_activity import (
    _AMBIG_APPROX_RE,
    _AMBIG_DISCRETE_RE,
    _AMBIG_RANGE_RE,
    _ACTIVITY_COUNT_ATTRIBUTE_RE,
    _ACTIVITY_EVENT_BOUND_RE,
    _ACTIVITY_EXACT_COUNT_CUE_RE,
    _ACTIVITY_MODAL_RE,
    _ACTIVITY_PRESENT_PERFECT_RE,
    _ACTIVITY_TOTAL_TOKEN_RE,
    _NON_QUANTIFYING_AROUND_RE,
    _activity_clause_context,
    _activity_scalar_admission_reason,
    _activity_source_context,
    is_ambiguous_exact,
)
from menhir.services.typed_scalar_rules_bind import (
    LookupNamespaceEntities,
    ResolveSelfSubject,
    SELF_SUBJECT_DISPLAY,
    SELF_TOKENS,
    _POSSESSIVE_DETERMINERS,
    _SUBJECT_DETERMINERS,
    _bind_from_candidates,
    _bind_subject,
    _is_self_reference,
    _resolve_subject,
    _subject_spellings,
    _subject_variants,
)
from menhir.services.typed_scalar_rules_canon import (
    _FREQUENCY_INTERVAL_RE,
    _FREQUENCY_NUMBER_PATTERN,
    _FREQUENCY_NUMBER_WORDS,
    _MODIFIER_SEP_RE,
    _SNAKE_RE,
    _TEMPORAL_ATTRIBUTE_PREFIXES,
    _canon_attribute,
    _canon_modifier,
    _expand_episode_envelopes,
    _frequency_number,
    _ground_span,
    _normalize_interval_frequency,
    _normalize_when,
    _opt_str,
    _parse_iso_strict,
    _parse_json_array,
    _req_episode_index,
    _req_str,
)
from menhir.services.typed_scalar_rules_gate import (
    PERCEPTION_EVIDENCE_TIER,
    TS_VETO_COMMIT,
    TS_VETO_SELF_CONSISTENCY,
    TS_VETO_TIE,
    TS_VETO_UNGROUNDED,
    TypedScalarDecision,
    _VOTE_ABSENT,
    _VOTE_CONFLICTED,
    _canonical_aligned_proposal,
    _claim_groups,
    _interpretation_label,
    _readable_vote,
    _reconciled_attribute,
    _reconciled_identity,
    _validate_threshold,
    gate_typed_scalars,
)
from menhir.services.typed_scalar_rules_persist import (
    _ADVISORY_UUID_PREFIX,
    _CORRECTION_CUES,
    _CORRECTION_NOT_RE,
    _assertion_from_proposal,
    advisory_subject_uuid,
    classify_absolute_semantics,
)
from menhir.services.typed_scalar_rules_prompt import TYPED_SCALAR_SYSTEM_PROMPT
from menhir.services.typed_scalar_rules_proposal import TypedScalarProposal
from menhir.services.typed_scalar_rules_temporal import (
    _BOUNDED_RE,
    _CURRENT_RE,
    _FUTURE_REL_RE,
    _MONTHS,
    _PAST_RE,
    _SRC_ISO_RE,
    _SRC_MONTH_DAY_RE,
    _SRC_MONTH_DAY_YEAR_RE,
    _parse_source_date,
    _resolve_valid_time,
    _same_calendar_day,
    _to_dt,
    resolve_temporal_disposition,
)
from menhir.services.typed_scalar_rules_values import (
    _BOOLEAN_NEGATIVE_RE,
    _BOOLEAN_POSITIVE_RE,
    _BOOLEAN_UNCERTAIN_RE,
    _COUNT_NEGATIVE_DELTA_RE,
    _COUNT_POSITIVE_DELTA_RE,
    _COUNT_RANGE_RE,
    _COUNT_TOKEN_RE,
    _COUNT_WORDS,
    _CLOCK_SOURCE_RE,
    _DURATION_H_MM_SS_RE,
    _DURATION_M_SS_RE,
    _DURATION_UNIT_SECONDS,
    _MEASUREMENT_AMOUNT_UNIT_RE,
    _MEASUREMENT_UNIT_ALIASES,
    _SOURCE_NUMBER,
    _USD_SOURCE_RE,
    _as_number,
    _boolean_source_polarity,
    _clock_time_from_source,
    _count_token_value,
    _count_value_from_source,
    _coerce_value,
    _decimal_number,
    _duration_colon_seconds,
    _duration_value,
    _money_currency_from_source,
    _money_value,
    _measurement_unit_from_source,
    _normalize_colon_duration_value,
)

logger = logging.getLogger(__name__)





def extract_typed_scalars_once(
    episodes: list[Any], llm_complete: LlmComplete,
    *, on_drop: "Callable[[str], None] | None" = None,
) -> list[TypedScalarProposal]:
    """One typed-scalar extraction pass: prose -> validated, grounded `TypedScalarProposal`s.

    `episodes` are objects with `.uuid` and `.content` (e.g. `perception.Episode`). A row survives
    ONLY if every required field is an actual non-blank string (`subject`, `attribute`, `operation`,
    `stated_span`), `operation` is explicitly present, `episode` is a real in-range integer,
    `attribute` is canonical snake_case, the value passes the domain `validate_value`, any supplied
    `when` parses as ISO, and `stated_span` occurs EXACTLY ONCE in the episode text (unique grounding
    -> located offsets; zero/multiple -> dropped as ungrounded/ambiguous). Everything else fails
    closed to omission â€” malformed model output never acquires a semantic default. `claim_ordinal`
    is always 0: identity comes from the located span, not model output order, so re-ordering rows
    (across k samples or a retry) cannot change a claim's durable `source_key`. Pure given the
    injected `llm_complete`; writes nothing.

    `on_drop` is an OPTIONAL observability seam: when supplied it is called once per DISCARDED row
    with a short reason, and ONCE for a whole-response parse failure (`malformed_json` /
    `not_a_json_array`, both of which yield zero rows). A discarded row is indistinguishable
    downstream from a claim the model never emitted, which is what made the measured 72% "absence"
    band uninterpretable -- this makes the two separable. The response-level case matters more than
    the row-level one: a truncated pass is not an abstention but a lost sample, and at threshold=1.0
    a lost sample vetoes every claim in the batch. It is injected rather than emitted from here so
    the function stays pure by default (25 call sites, almost all tests, are unaffected) and so the
    audit emit lives with the other emits in the caller."""
    if not episodes:
        return []

    def _drop(reason: str) -> None:
        if on_drop is not None:
            on_drop(reason)
    log = "\n".join(f"[{i}] {getattr(e, 'content', '')}" for i, e in enumerate(episodes))
    raw_rows, parse_failure = _parse_json_array(llm_complete(TYPED_SCALAR_SYSTEM_PROMPT, log))
    if parse_failure is not None:
        _drop(parse_failure)
    else:
        raw_rows = _expand_episode_envelopes(raw_rows, len(episodes), _drop)

    out: list[TypedScalarProposal] = []
    for row in raw_rows:
        proposal = parse_scalar_row(row, episodes, _drop)
        if proposal is not None:
            out.append(proposal)
    return out


def parse_scalar_row(
    row: Any, episodes: list[Any], drop: "Callable[[str], None]",
) -> "TypedScalarProposal | None":
    """Validate ONE raw model row into a grounded proposal, or None with a `drop` reason.

    Lifted verbatim out of `extract_typed_scalars_once` so experimental extraction protocols (see
    .agent/plans/menhir-proposer-reviewer-vocabulary-experiment-plan.md) admit rows through the
    IDENTICAL rules -- required fields, canonical attribute, kind-typed value, unique span grounding,
    hedged-value abstention, temporal disposition. A second implementation of these rules would drift
    from this one and silently make experiment arms incomparable to the baseline they are scored
    against.

    Pure. `drop` is called at most once, immediately before returning None.
    """
    if not isinstance(row, dict):
        drop("not_an_object")
        return None
    # required non-blank strings â€” no defaults, no coercion from non-string types.
    subject_text = _req_str(row, "subject")
    attribute = _req_str(row, "attribute")
    operation = _req_str(row, "operation")
    stated_span = _req_str(row, "stated_span")
    if subject_text is None or attribute is None or operation is None or stated_span is None:
        drop("missing_required_field")
        return None
    attribute = attribute.lower()
    operation = operation.lower()
    if not _SNAKE_RE.match(attribute) or operation not in OPERATIONS:
        drop("bad_attribute_or_operation")
        return None
    attribute = _canon_attribute(attribute, stated_span)
    value_kind = _req_str(row, "value_kind")
    if value_kind is None or value_kind.lower() not in VALUE_KINDS:
        drop("bad_value_kind")
        return None
    value_kind = value_kind.lower()

    # optional textual fields: '' when absent, dropped when present-but-non-string.
    scope = _opt_str(row, "scope")
    unit = _opt_str(row, "unit")
    display = _opt_str(row, "display")
    if scope is None or unit is None or display is None:
        drop("bad_optional_field")
        return None

    idx = _req_episode_index(row, len(episodes))
    if idx is None:
        drop("bad_episode_index")
        return None
    episode_uuid = str(getattr(episodes[idx], "uuid", "") or "").strip()
    if not episode_uuid:
        drop("unresolvable_episode")
        return None  # unresolvable provenance -> cannot ground

    count_value_unresolved = False
    normalized_frequency = (
        _normalize_interval_frequency(stated_span)
        if value_kind == "frequency" and operation != "delta"
        else None
    )
    if normalized_frequency is not None:
        value, unit = normalized_frequency
    elif value_kind == "duration":
        normalized_duration = _duration_value(row.get("value"), unit, operation)
        if normalized_duration is None:
            drop("duration_unit_unresolved")
            return None
        value, unit = normalized_duration
    elif value_kind == "money":
        unit = _money_currency_from_source(stated_span)
        if unit is None:
            drop("money_currency_unresolved")
            return None
        value = _money_value(row.get("value"), operation)
    elif value_kind == "measurement":
        unit = _measurement_unit_from_source(stated_span)
        if unit is None:
            drop("measurement_unit_unresolved")
            return None
        value = _coerce_value(value_kind, operation, row.get("value"))
    elif value_kind == "clock_time":
        value = _clock_time_from_source(stated_span)
        unit = ""
        if value is None:
            drop("clock_time_unresolved")
            return None
    elif value_kind == "count":
        model_value = _coerce_value(value_kind, operation, row.get("value"))
        try:
            validate_value(value_kind, operation, model_value)
        except (ValueError, OverflowError):
            # Source authority may correct a plausible numeric estimate, but it must not launder a
            # malformed or fractional model field into a valid count.
            drop("value_failed_validation")
            return None
        value = _count_value_from_source(stated_span, operation, model_value)
        unit = ""
        if value is None:
            # Preserve the parser's provenance precedence: an ambiguous/unlocatable span is first
            # an ungrounded claim, even when its text also lacks a usable source count.
            count_value_unresolved = True
            value = model_value
    else:
        value = _coerce_value(value_kind, operation, row.get("value"))
        if value_kind in {"boolean", "status", "weekday"}:
            unit = ""
        if value_kind in {"status", "weekday"} and isinstance(value, str):
            value = value.lower()
    try:
        validate_value(value_kind, operation, value)
    except (ValueError, OverflowError):
        drop("value_failed_validation")
        # not well-typed for its kind -> drop. OverflowError is defense-in-depth: the domain
        # validator already fails an oversized int closed with ValueError, but catching it here
        # too guarantees one malformed proposal can never abort the whole extraction pass.
        return None
    if value_kind == "boolean":
        source_polarity = _boolean_source_polarity(stated_span)
        if source_polarity is not None and source_polarity is not value:
            drop("boolean_source_mismatch")
            return None

    when_ok, when = _normalize_when(row)
    if not when_ok:
        drop("malformed_when")
        return None  # a supplied-but-malformed date must not reach valid_at

    content = str(getattr(episodes[idx], "content", "") or "")
    span = _ground_span(content, stated_span)
    if span is None:
        drop("span_not_uniquely_located")
        return None  # not uniquely located in the source -> ungrounded/ambiguous -> does not exist
    span_start, span_end = span
    if count_value_unresolved:
        drop("count_value_unresolved")
        return None

    # Activity-count rows need one additional source-shape guard.  The model may correctly fill the
    # typed schema for a hypothetical total or a single event, but those are not durable cumulative
    # scalars.  Use only the punctuation-delimited clause prefix ending at the grounded span so a
    # modal in trailing text cannot veto an otherwise valid observation.
    activity_rejection = _activity_scalar_admission_reason(
        attribute=attribute,
        value_kind=value_kind,
        operation=operation,
        source_context=_activity_source_context(content, span_start, span_end),
        event_context=_activity_clause_context(content, span_start, span_end),
    )
    if activity_rejection is not None:
        drop(activity_rejection)
        return None

    # Hedged-value abstention: an underdetermined statement ("about 30 or 40", "around 7, sometimes
    # 7:30") must not become an exact scalar. Drop it here (span-local + deterministic) so the gate
    # sees no vote for it and the claim abstains, rather than admitting an over-confident value the k
    # samples happened to agree on. A genuine [lo, hi] range value is exempt.
    if is_ambiguous_exact(stated_span, value, operation):
        drop("hedged_value")
        return None

    # When-discipline: deterministically resolve temporal grounding from the span. A past-only,
    # unresolvable, bounded, or model-conflicting claim is DROPPED (never votes / persists). When an
    # explicit source date resolves, `when` becomes that SOURCE-derived timestamp (never the model's);
    # otherwise `when` is None and bind falls to the episode reference -- so a hallucinated model date
    # can never reach valid_at, on any operation (incl expire). All k samples decide identically.
    _ep_ref = getattr(episodes[idx], "reference_time", None)
    _disp, _keep, when = resolve_temporal_disposition(stated_span, when, operation, _ep_ref)
    if not _keep:
        drop(f"temporal_{_disp}")
        return None

    return TypedScalarProposal(
        subject_text=subject_text,
        attribute=attribute,
        scope=_canon_modifier(scope),
        value_kind=value_kind,
        unit=_canon_modifier(unit),
        operation=operation,
        value=value,
        stated_span=stated_span,
        episode_uuid=episode_uuid,
        span_start=span_start,
        span_end=span_end,
        when=when,
        display=display,
        claim_ordinal=0,
    )
