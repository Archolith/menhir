"""Perception boundary — episodes -> typed `Event`s, precision-first and abstaining.

Design of record: `.agent/for-review/HANDOFF-2026-07-02-perception-boundary.md`.

The one invariant: **perception may be probabilistic; folds and Views must stay deterministic.**
The LLM's ONLY job is here — turn prose episodes into typed `fold_algebra.Event`s and decide
*whether* the derived value is trustworthy enough to materialize as a View. Once an Event list is
committed, everything downstream (`event_fold.fold_events_to_counter`, `ViewRepository`) is pure
arithmetic. No probability ever crosses into rho/delta.

The rule (the spine): **when uncertain, do not write the View.** A missed View is annoying; a wrong
current-state View is dangerous (it ranks well and looks authoritative — Arm B: FP >> FN). Abstention
is safe *for free*: raw episodes always ingest as normal memory and Views are purely additive, so the
absence of a View IS the fallback (recall returns the raw episode). No fallback code to write.

Confidence is a CONJUNCTIVE veto-gate over three signals (no fitted weights — ~14 labeled questions
is nowhere near enough to calibrate a score):

  1. self-consistency entropy  — extract k times (temp>0); commit only if the derived value is
     concentrated (near-unanimous). Scattered -> abstain. The primary gate.
  2. fold triangulation        — if perception emits BOTH item events and a STATED total, they are
     two independent derivations; SUM(items) must agree with the stated total or we abstain.
  3. embedding dedup           — DISTINCT-COUNT is right only if "5-gallon tank" and "the 5 gallon
     one" resolve to one item; cluster identities conservatively (bias toward SEPARATE).

Any single red flag -> abstain. Missing signals don't veto (no stated total => triangulation simply
doesn't apply). The agreement fraction, triangulation check, and cluster count are all computed
DETERMINISTICALLY from the stochastic samples — stochastic input, deterministic decision.

Implementation lives in the `perception_parts` subpackage; this module remains the import surface —
every public (and historically imported) symbol is re-exported here unchanged.
"""

from __future__ import annotations

import logging

#: (system, user) -> completion text. Injected so perception is decoupled from any specific LLM.
from menhir.services.seam_types import Embed, LlmComplete
from menhir.services.perception_parts.extract import _category_spend_groups, extract_once
from menhir.services.perception_parts.families import (
    _canonical_family_label,
    _collapse_measure_families,
    _measure_noun_sig,
)
from menhir.services.perception_parts.gate import gate
from menhir.services.perception_parts.gate_helpers import _price_token_count, _sum_arithmetic_grounded
from menhir.services.perception_parts.keys import (
    _SUBJECT_MAX_CHARS,
    canonicalize_measure_key,
    canonicalize_samples,
    count_spend_compound,
    sanitize_measure_key,
    sanitize_subject_name,
)
from menhir.services.perception_parts.levers import (
    COREF_MERGE,
    COREF_SEPARATE,
    COREF_UNSURE,
    extract_stated_total,
    resolve_coreference,
    verify_candidate,
    verify_candidate_detailed,
)
from menhir.services.perception_parts.model import (
    VETO_COMMIT,
    VETO_COUNT_FLOOR,
    VETO_CROSS_CHECK,
    VETO_SELF_CONSISTENCY,
    VETO_TRIANGULATION,
    VETO_UNRESOLVED_COREFERENCE,
    VETO_UNSUPPORTED_STATED,
    VETO_VERIFICATION,
    Episode,
    GateDecision,
    PerceivedGroup,
)
from menhir.services.perception_parts.pipeline import PerceptionResult, perceive_and_fold
from menhir.services.perception_parts.prompts import (
    COREFERENCE_PROMPT,
    STATED_TOTAL_PROMPT,
    SYSTEM_PROMPT,
    VERIFY_PROMPT,
    VERIFY_PROMPT_WITH_ANCHOR,
    VERIFY_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)
