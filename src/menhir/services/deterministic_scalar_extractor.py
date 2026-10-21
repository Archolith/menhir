"""Deterministic v0.1 typed-scalar extraction (pure, shadow-safe, no LLM, no persistence).

This is the deterministic-first candidate extractor designed in
`.agent/plans/menhir-deterministic-first-event-scalar-2026-07-30.md`. It admits explicit scalar
statements without an LLM by matching a CLOSED, versioned registry of surface templates and
routing every constructed raw observation through `typed_scalar_rules.parse_scalar_row` — the
SAME validation pipeline the LLM path uses — so unique-span grounding, kind-typed value
validation, span-local hedge abstention, temporal disposition, duration/frequency normalization,
and source identity cannot fork between extractors.

v0.1 boundaries (deliberately narrow; abstention is the safe default):

- Subject authority is CANONICAL SELF ONLY (`SELF_SUBJECT_DISPLAY`). No named third parties, no
  owned-object inference ("my car has 4 wheels" abstains), no head-noun-to-slot guessing.
- Attribute/slot mapping is the closed `TEMPLATE_REGISTRY`. Unknown property phrases,
  unregistered synonyms, and template collisions abstain. There is no free-form attribute
  naming.
- Supported classes: absolute count / money / measurement / percent, standing-property
  clock_time, duration, normalized frequency, numeric range, deltas whose admitted span names
  an explicit held-slot accumulator ("I added 5 coins to my collection" — a bare "I added
  5 coins" is NOT admitted), expire, previous/current pairs, and correction admissions whose
  grounded span INCLUDES the correction cue (optional "actually," on a safe measurement
  surface). Boolean/status/weekday are NOT supported: weekday names, status adjectives
  (married/employed/...), and true/false surface as unmatched scalar cues so the router sends
  the episode to the LLM.
- One-off purchases/payments/events abstain ("I paid $250", "I met my friend at 3:00 pm").
- Quote contract: `stated_span` is the shortest COMPLETE source fragment containing every
  consumed semantic cue (cue-bearing pronouns and verbs are kept, never stripped; an omitted
  slot noun is never inferred — "now I have 37" without the noun is fallback-only).
- Any candidate drop — duplicate-span ambiguity, hedge/temporal/parse failure, template
  mismatch, or collision — marks the episode NOT fully covered and NOT router-eligible;
  abstentions are explicit with stable reason codes.
- A sentence whose matched spans leave unconsumed semantic text ("I have 37 coins?", "Maybe I
  have 37 coins.", "I have 37 coins for now.") reports `unconsumed_context`: the claim may be
  receipted as admitted, but the episode is NOT router-eligible. Only whitespace, terminal
  punctuation, commas/semicolons, and the connector "and" may remain outside the spans.
- The extractor NEVER fabricates a k-sample consensus, never calls an LLM, and never persists.
  It reports proposals plus per-episode/per-candidate receipts with stable reason codes so a
  later shadow comparison and router-completeness measurement can run offline.

Content coordinate space: episodes arrive as they do in the LLM path (content may carry a
`[YYYY-MM-DD] ` prefix); templates locate quotes against the SAME string the LLM path sees, so
offsets share one coordinate space with `source_key`. The synthetic date prefix is excluded from
scalar-cue detection so it cannot masquerade as an unmatched numeric claim.

Reasons and outcomes are stable strings (constants below); `parse_scalar_row` drop reasons
(`span_not_uniquely_located`, `hedged_value`, `temporal_past_only`, ...) pass through
unchanged so the shadow comparison can attribute every abstention.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from menhir.services.deterministic_scalar_extractor_receipts import (
    CandidateReceipt,
    DeterministicExtraction,
    EpisodeReceipt,
)
from menhir.services.deterministic_scalar_extractor_registry import (
    TEMPLATE_REGISTRY,
    SurfaceTemplate,
    _clock_value_or_raise as _clock_value_or_raise,
    _parse_number,
)
from menhir.services.deterministic_scalar_extractor_text import (
    _SELF_ATTRIBUTE_NOUNS,
    _POSSESSIVE_RE,
    _collision_indices,
    _has_unconsumed_context,
    _prefix_len,
    _scalar_cues,
    _sentence_index,
    _split_sentences,
)
from menhir.services.typed_scalar_rules import (
    TypedScalarProposal,
    _normalize_interval_frequency,
    classify_absolute_semantics,
    parse_scalar_row,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "TEMPLATE_VERSION",
    "OUTCOME_ADMITTED",
    "OUTCOME_DROPPED",
    "REASON_NON_SELF_SUBJECT",
    "REASON_TEMPLATE_COLLISION",
    "REASON_TEMPLATE_MISMATCH",
    "REASON_FREQUENCY_NORMALIZATION",
    "REASON_UNCONSUMED_CONTEXT",
    "CandidateReceipt",
    "DeterministicExtraction",
    "DeterministicScalarExtractor",
    "EpisodeReceipt",
    "SurfaceTemplate",
    "TEMPLATE_REGISTRY",
]

EXTRACTOR_VERSION = "det-v0.1"
TEMPLATE_VERSION = "templates-v0.1"

OUTCOME_ADMITTED = "admitted"
OUTCOME_DROPPED = "dropped"

REASON_NON_SELF_SUBJECT = "non_self_subject"
REASON_TEMPLATE_COLLISION = "template_collision"
REASON_TEMPLATE_MISMATCH = "template_mismatch"
REASON_FREQUENCY_NORMALIZATION = "frequency_normalization_failed"
REASON_UNCONSUMED_CONTEXT = "unconsumed_context"

# ------------------------------------------------------------------------------------------------ #
# Extractor
# ------------------------------------------------------------------------------------------------ #


@dataclass(frozen=True)
class DeterministicScalarExtractor:
    """Pure v0.1 deterministic scalar extractor. Stateless; every call replays identically.

    `templates` defaults to the closed `TEMPLATE_REGISTRY`; tests may inject a bespoke registry.
    """

    extractor_version: str = EXTRACTOR_VERSION
    template_version: str = TEMPLATE_VERSION
    templates: tuple[SurfaceTemplate, ...] = TEMPLATE_REGISTRY

    def extract(self, episodes: list[Any]) -> DeterministicExtraction:
        """Parse all episodes; return admitted proposals plus full abstention receipts."""
        episode_receipts: list[EpisodeReceipt] = []
        proposals: list[TypedScalarProposal] = []
        eligible_uuids: list[str] = []
        reasons: Counter[str] = Counter()

        for index, episode in enumerate(episodes):
            content = str(getattr(episode, "content", "") or "")
            uuid = str(getattr(episode, "uuid", "") or "").strip()
            prefix_len = _prefix_len(content)
            sentences = _split_sentences(content, prefix_len)

            matches: list[tuple[int, int, SurfaceTemplate, re.Match[str]]] = []
            for template in self.templates:
                for match in template.pattern.finditer(content):
                    matches.append((match.start(), match.end(), template, match))
            matches.sort(key=lambda item: (item[0], item[1], item[2].template_id))

            per_sentence: list[list[tuple[int, int, SurfaceTemplate, re.Match[str]]]] = [
                [] for _ in sentences
            ]
            for item in matches:
                si = _sentence_index(item[0], sentences)
                if si is not None:
                    per_sentence[si].append(item)

            episode_reason_codes: list[str] = []
            candidate_receipts: list[CandidateReceipt] = []

            for si, (s_start, s_end) in enumerate(sentences):
                sentence_items = per_sentence[si]
                collided = _collision_indices(sentence_items)
                sentence_candidates: list[CandidateReceipt] = []

                for item_index, (start, end, template, match) in enumerate(sentence_items):
                    receipt = self._candidate_receipt(
                        template, match, index, episodes, start, end,
                        collided=item_index in collided,
                    )
                    sentence_candidates.append(receipt)
                    if receipt.outcome == OUTCOME_ADMITTED and receipt.proposal is not None:
                        proposals.append(receipt.proposal)
                    elif receipt.drop_reason is not None:
                        # Any candidate drop makes the episode NOT fully covered and NOT
                        # router-eligible. Collisions are counted ONCE per collided sentence
                        # below, not per candidate; every other drop counts per candidate.
                        if receipt.drop_reason != REASON_TEMPLATE_COLLISION:
                            reasons[receipt.drop_reason] += 1
                            episode_reason_codes.append(receipt.drop_reason)

                # previous/current composition: an absolute alongside an expire on the same slot.
                expire_attributes = {
                    c.attribute for c in sentence_candidates
                    if c.outcome == OUTCOME_ADMITTED and c.class_id == "c_expire"
                }
                if expire_attributes:
                    sentence_candidates = [
                        replace(c, class_id="c_prev_current")
                        if (c.outcome == OUTCOME_ADMITTED and c.class_id != "c_expire"
                            and c.attribute in expire_attributes)
                        else c
                        for c in sentence_candidates
                    ]
                candidate_receipts.extend(sentence_candidates)

                # Coverage: every scalar cue must lie inside a template match; collisions abstain.
                if collided:
                    episode_reason_codes.append(REASON_TEMPLATE_COLLISION)
                    reasons[REASON_TEMPLATE_COLLISION] += 1
                cue_codes = self._uncovered_cue_codes(content, s_start, s_end, sentence_items)
                for code in cue_codes:
                    episode_reason_codes.append(code)
                    reasons[code] += 1
                if self._non_self_possessive(content, s_start, s_end, sentence_items):
                    episode_reason_codes.append(REASON_NON_SELF_SUBJECT)
                    reasons[REASON_NON_SELF_SUBJECT] += 1
                if sentence_items and _has_unconsumed_context(
                        content, s_start, s_end, sentence_items):
                    episode_reason_codes.append(REASON_UNCONSUMED_CONTEXT)
                    reasons[REASON_UNCONSUMED_CONTEXT] += 1

            episode_receipts.append(EpisodeReceipt(
                episode_uuid=uuid,
                episode_index=index,
                sentence_count=len(sentences),
                fully_covered=not episode_reason_codes,
                reasons=tuple(episode_reason_codes),
                candidate_receipts=tuple(candidate_receipts),
            ))
            if not episode_reason_codes and uuid:
                eligible_uuids.append(uuid)

        return DeterministicExtraction(
            extractor_version=self.extractor_version,
            template_version=self.template_version,
            episode_receipts=tuple(episode_receipts),
            proposals=tuple(proposals),
            fully_eligible_episode_uuids=tuple(eligible_uuids),
            reason_counts=tuple(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        )

    # -- helpers -------------------------------------------------------------------------------

    def _candidate_receipt(
        self,
        template: SurfaceTemplate,
        match: re.Match[str],
        episode_index: int,
        episodes: list[Any],
        start: int,
        end: int,
        *,
        collided: bool,
    ) -> CandidateReceipt:
        """Build the row `parse_scalar_row` consumes, run the shared pipeline, and receipt it."""
        span = match.group(0).strip()
        checks: list[str] = ["exact_span", "self_subject", "closed_slot_mapping"]
        try:
            subject_text = template.subject
            attribute = (template.attribute_from(match) if template.attribute_from else
                         template.attribute)
            unit = (template.unit_from(match) if template.unit_from else template.unit)
            value = (template.value_from(match) if template.value_from else
                     _parse_number(match.group("n")))
        except (AttributeError, IndexError, ValueError, KeyError):
            return CandidateReceipt(
                template_id=template.template_id, class_id=template.class_id,
                episode_index=episode_index, source_start=start, source_end=end,
                stated_span=span, subject_text=template.subject, attribute=template.attribute,
                scope=template.scope, value_kind=template.value_kind, unit=template.unit,
                operation=template.operation, value=None, outcome=OUTCOME_DROPPED,
                drop_reason=REASON_TEMPLATE_MISMATCH, checks=tuple(checks), proposal=None,
            )

        if collided:
            return CandidateReceipt(
                template_id=template.template_id, class_id=template.class_id,
                episode_index=episode_index, source_start=start, source_end=end,
                stated_span=span, subject_text=subject_text, attribute=attribute,
                scope=template.scope, value_kind=template.value_kind, unit=unit,
                operation=template.operation, value=value, outcome=OUTCOME_DROPPED,
                drop_reason=REASON_TEMPLATE_COLLISION, checks=tuple(checks), proposal=None,
            )

        # Interval-frequency forms must normalize through the SHARED interval normalizer; a span
        # that would silently fall back to the placeholder value is an abstention, never a value.
        if (template.value_kind == "frequency" and template.operation == "absolute"
                and template.unit == ""):
            if _normalize_interval_frequency(span) is None:
                return CandidateReceipt(
                    template_id=template.template_id, class_id=template.class_id,
                    episode_index=episode_index, source_start=start, source_end=end,
                    stated_span=span, subject_text=subject_text, attribute=attribute,
                    scope=template.scope, value_kind=template.value_kind, unit=unit,
                    operation=template.operation, value=value, outcome=OUTCOME_DROPPED,
                    drop_reason=REASON_FREQUENCY_NORMALIZATION, checks=tuple(checks),
                    proposal=None,
                )
            checks.append("frequency_normalized")

        row: dict[str, Any] = {
            "episode": episode_index,
            "subject": subject_text,
            "attribute": attribute,
            "scope": template.scope,
            "unit": unit,
            "value_kind": template.value_kind,
            "operation": template.operation,
            "value": value,
            "when": None,
            "stated_span": span,
            "display": "",
        }
        captured: list[str] = []

        def _drop(reason: str) -> None:
            captured.append(reason)

        proposal = parse_scalar_row(row, episodes, _drop)
        if proposal is None:
            drop_reason = captured[0] if captured else "parse_failed"
            return CandidateReceipt(
                template_id=template.template_id, class_id=template.class_id,
                episode_index=episode_index, source_start=start, source_end=end,
                stated_span=span, subject_text=subject_text, attribute=attribute,
                scope=template.scope, value_kind=template.value_kind, unit=unit,
                operation=template.operation, value=value, outcome=OUTCOME_DROPPED,
                drop_reason=drop_reason, checks=tuple(checks), proposal=None,
            )

        class_id = template.class_id
        if (proposal.operation == "absolute"
                and classify_absolute_semantics(proposal.stated_span, "absolute") == "correction"):
            class_id = "c_correction"
        return CandidateReceipt(
            template_id=template.template_id, class_id=class_id,
            episode_index=episode_index, source_start=start, source_end=end,
            stated_span=span, subject_text=subject_text, attribute=attribute,
            scope=template.scope, value_kind=template.value_kind, unit=unit,
            operation=template.operation, value=value, outcome=OUTCOME_ADMITTED,
            drop_reason=None, checks=tuple(checks), proposal=proposal,
        )

    @staticmethod
    def _uncovered_cue_codes(
        content: str,
        s_start: int,
        s_end: int,
        sentence_items: list[tuple[int, int, SurfaceTemplate, re.Match[str]]],
    ) -> list[str]:
        """Reason codes for scalar cues no template match covers in this sentence."""
        codes: list[str] = []
        for code, cue_start, cue_end in _scalar_cues(content, s_start, s_end):
            covered = any(
                ms <= cue_start and cue_end <= me
                for ms, me, _t, _m in sentence_items
            )
            if not covered:
                codes.append(code)
        return codes

    @staticmethod
    def _non_self_possessive(
        content: str,
        s_start: int,
        s_end: int,
        sentence_items: list[tuple[int, int, SurfaceTemplate, re.Match[str]]],
    ) -> bool:
        """True when a scalar-cued sentence carries a possessive OUTSIDE every template match
        whose noun is not a registry self attribute ("my car has 4 wheels", "my battery is at
        75%") — an owned-object claim the v0.1 authority refuses, so the caller routes the
        episode to the LLM. Possessives consumed by a template (the accumulator phrase in
        "I added 5 coins to my collection") are matched regions and are never flagged."""
        if not _scalar_cues(content, s_start, s_end):
            return False
        for match in _POSSESSIVE_RE.finditer(content, s_start, s_end):
            noun = match.group(1).lower()
            if noun in _SELF_ATTRIBUTE_NOUNS:
                continue
            covered = any(
                ms <= match.start() and match.end() <= me
                for ms, me, _t, _m in sentence_items
            )
            if not covered:
                return True
        return False
