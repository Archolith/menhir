"""Perception writes `approximate` (#519 phase 2) for a hedged single number, behind a default-off flag.

The model never names the operation: it emits `absolute` with the stated number and code relabels it
from the quoted span alone, so all k samples agree. Every other hedge still drops. Each flag-on
behaviour is paired with the flag-off outcome it replaces.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.assertion_operations import (
    MODEL_OPERATIONS,
    OPERATIONS,
    approximates_value,
    folds_into_view,
)
from menhir.domain.projection_coverage import EligibilityRole
from menhir.domain.projection_coverage_types import ProjectionStatus
from menhir.services.projection_coverage_service import ProjectionCoverageService
from menhir.services.typed_scalar_perception import (
    TypedScalarDecision,
    bind_and_persist_typed_scalars,
)
from menhir.services.typed_scalar_rules import (
    TYPED_SCALAR_SYSTEM_PROMPT,
    TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE,
    extract_typed_scalars_once,
    gate_typed_scalars,
    parse_scalar_row,
)

pytestmark = pytest.mark.unit

#: the scalar-lane prompt exactly as it was before #519 (the text the flag-off path must still send).
_PROMPT_SENTENCES_BEFORE = (
    "Tentative, hypothetical, approximate, vague, or alternative totals "
    "('I might wear them six times', 'about six times', 'a few times', 'six or seven times') emit "
    "NOTHING rather than an exact scalar. ",
    "But if it gives DISCRETE alternatives ('30 or 40', 'maybe 1500 or 1600'), an APPROXIMATION "
    "('about 30', 'around 7:30'), a VAGUE quantity ('a few times', 'several'), or MULTIPLE contextual "
    "values ('usually 7, sometimes 7:30'), emit NOTHING for that value -- it has no single current "
    "scalar. ",
)


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _row(span, *, kind="count", value=50, operation="absolute", attribute="followers", unit=""):
    return dict(episode=0, subject="user", attribute=attribute, scope="", value_kind=kind,
                unit=unit, operation=operation, value=value, stated_span=span)


def _parse(text, span, *, approximate, **row):
    ep = _Ep(uuid="ep-1", content=text)
    drops: list[str] = []
    proposal = parse_scalar_row(_row(span, **row), [ep], drops.append, approximate=approximate)
    return proposal, drops


# ---------------------------------------------------------------- a. relabel, same label for k samples

@pytest.mark.parametrize("kind,value,attribute,text,span", [
    ("money", 400000, "preapproval_amount", "I got pre-approved for about $400,000 last week.",
     "about $400,000"),
    ("count", 400000, "followers", "I have about 400,000 followers now.", "about 400,000 followers"),
])
def test_a_a_single_hedged_number_is_kept_as_approximate(kind, value, attribute, text, span):
    proposal, drops = _parse(text, span, approximate=True, kind=kind, value=value,
                             attribute=attribute)
    assert drops == []
    assert proposal is not None
    assert proposal.operation == "approximate" and proposal.value == 400000

    # flag off: the identical row is dropped, exactly as before.
    off, off_drops = _parse(text, span, approximate=False, kind=kind, value=value,
                            attribute=attribute)
    assert off is None and off_drops == ["hedged_value"]


def test_a_every_sample_of_one_claim_gets_the_same_label_and_the_gate_commits_it():
    ep = _Ep(uuid="ep-1", content="I have about 400,000 followers now.")
    replies = ('[{"episode": 0, "observations": [{"subject": "user", "attribute": "followers", '
               '"scope": "", "value_kind": "count", "unit": "", "operation": "absolute", '
               '"value": 400000, "stated_span": "about 400,000 followers"}]}]')
    samples = [extract_typed_scalars_once([ep], lambda s, u: replies, approximate=True)
               for _ in range(3)]
    assert [[p.operation for p in s] for s in samples] == [["approximate"]] * 3
    decisions = gate_typed_scalars(samples, threshold=1.0)
    assert [d.committed for d in decisions] == [True]
    assert decisions[0].proposal.operation == "approximate"


# ---------------------------------------------------------------- b. every other hedge still drops

@pytest.mark.parametrize("text,span,row", [
    ("I have 30 or 40 followers.", "30 or 40 followers", dict(value=30)),
    ("I wake between 7:00 and 7:30.", "between 7:00 and 7:30",
     dict(kind="clock_time", value="07:00", attribute="wake_time")),
    ("I gained about 5 more followers.", "about 5 more followers",
     dict(value=5, operation="delta")),
    ("I wake up around 7:30.", "around 7:30",
     dict(kind="clock_time", value="07:30", attribute="wake_time")),
    ("I have a few followers.", "a few followers", dict(value=3)),
    ("I have about 7 or 8 followers.", "about 7 or 8 followers", dict(value=7)),
    ("I have about 12.5 followers.", "about 12.5 followers", dict(value=12.5)),
])
def test_b_other_hedges_drop_with_the_flag_on_and_off(text, span, row):
    for flag in (True, False):
        proposal, drops = _parse(text, span, approximate=flag, **row)
        assert proposal is None, (flag, proposal)
        assert drops and drops[0] in {"hedged_value", "value_failed_validation",
                                      "count_value_unresolved", "clock_time_unresolved"}, (flag, drops)
    on, on_drops = _parse(text, span, approximate=True, **row)
    off, off_drops = _parse(text, span, approximate=False, **row)
    assert on_drops == off_drops


def test_b_a_stated_interval_stays_a_range_and_is_never_relabelled():
    # A numeric "between 7 and 8" is stored as a [lo, hi] range today (flag off); the flag must not
    # turn it into an approximation of one number.
    text, span = "I have between 7 and 8 followers.", "between 7 and 8 followers"
    on, on_drops = _parse(text, span, approximate=True, value=7)
    off, off_drops = _parse(text, span, approximate=False, value=7)
    assert on_drops == off_drops == []
    assert (on.operation, on.value) == (off.operation, off.value) == ("absolute", [7, 8])

    # an approximated interval ("about 7 to 8") behaves identically with the flag on and off.
    text, span = "I have about 7 to 8 followers.", "about 7 to 8 followers"
    on, on_drops = _parse(text, span, approximate=True, value=7)
    off, off_drops = _parse(text, span, approximate=False, value=7)
    assert on_drops == off_drops
    assert (on and on.operation) == (off and off.operation) != "approximate"


# ---------------------------------------------------------------- c. new cues

@pytest.mark.parametrize("text,span", [
    ("It is nearly 50 followers now.", "nearly 50 followers"),
    ("It is close to 50 followers now.", "close to 50 followers"),
    ("It is almost 50 followers now.", "almost 50 followers"),
    ("I think it's 50 followers now.", "I think it's 50 followers"),
])
def test_c_new_cues_relabel_only_with_the_flag_on(text, span):
    on, on_drops = _parse(text, span, approximate=True)
    assert on_drops == [] and on is not None
    assert on.operation == "approximate" and on.value == 50

    # flag off: these cues are not hedges today, so the row is stored as an exact absolute.
    off, off_drops = _parse(text, span, approximate=False)
    assert off_drops == [] and off is not None
    assert off.operation == "absolute" and off.value == 50


def test_c_new_cues_never_add_a_drop():
    # a non-numeric value hedged by a new cue is kept as before (flag on), not newly dropped.
    on, drops = _parse("I think my status is single.", "I think my status is single",
                       approximate=True, kind="status", value="single", attribute="relationship")
    assert on is not None and drops == [] and on.operation == "absolute"
    # a delta hedged only by a new cue is also unchanged.
    delta, drops = _parse("I gained almost 5 followers.", "almost 5 followers",
                          approximate=True, value=5, operation="delta")
    assert delta is not None and drops == [] and delta.operation == "delta"


# ---------------------------------------------------------------- e. the model cannot emit it

@pytest.mark.parametrize("flag", [True, False])
def test_e_a_model_emitted_approximate_is_rejected(flag):
    proposal, drops = _parse("I have 1300 followers now.", "I have 1300 followers",
                             approximate=flag, value=1300, operation="approximate")
    assert proposal is None and drops == ["bad_attribute_or_operation"]
    # positive control: the same row as absolute parses.
    ok, ok_drops = _parse("I have 1300 followers now.", "I have 1300 followers",
                          approximate=flag, value=1300)
    assert ok is not None and ok_drops == []


def test_e_the_table_separates_stored_from_model_emittable():
    assert "approximate" in OPERATIONS and "approximate" not in MODEL_OPERATIONS
    assert MODEL_OPERATIONS == {"absolute", "delta", "expire"}


# ---------------------------------------------------------------- f. ended state

def test_f_an_ended_hedged_value_is_not_stored_as_approximate():
    text = "I used to weigh about 90 kg."
    span = "used to weigh about 90 kg"
    kw = dict(kind="measurement", value=90, attribute="weight", unit="kg")
    for flag in (True, False):
        proposal, drops = _parse(text, span, approximate=flag, **kw)
        assert proposal is None, flag
        assert drops and drops[0] in {"ended_state", "hedged_value"}
    on, on_drops = _parse(text, span, approximate=True, **kw)
    assert on_drops == ["ended_state"]   # relabelled, then stopped by the ended-state check

    # the expire contract is untouched: an expire row is never relabelled.
    expired, drops = _parse(text, span, approximate=True, operation="expire", **kw)
    assert expired is not None and drops == [] and expired.operation == "expire"


def test_f_a_hedged_value_with_a_past_only_frame_drops_through_the_temporal_check():
    proposal, drops = _parse("Yesterday I had about 400 followers.", "Yesterday I had about 400 followers",
                             approximate=True, value=400)
    assert proposal is None and drops and drops[0].startswith("temporal_")


# ---------------------------------------------------------------- g. prompt

def test_g_flag_off_prompt_is_unchanged_and_flag_on_asks_for_the_stated_number():
    for sentence in _PROMPT_SENTENCES_BEFORE:
        assert TYPED_SCALAR_SYSTEM_PROMPT.count(sentence) == 1
        assert sentence not in TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE
    assert "operation=absolute with the stated number" in TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE
    assert "operation=absolute with the stated number" not in TYPED_SCALAR_SYSTEM_PROMPT
    # tentative, vague and alternative still emit nothing with the flag on.
    assert "Tentative, hypothetical, vague, or alternative totals" in TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE
    assert "'a few times'" in TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE

    # the rest of the prompt is identical.
    restored = TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE
    assert len(restored) > len(TYPED_SCALAR_SYSTEM_PROMPT)


def _prompt_sent(approximate: bool) -> str:
    seen: list[str] = []

    def llm(system, user):
        seen.append(system)
        return "[]"

    extract_typed_scalars_once([_Ep(uuid="e", content="x")], llm, approximate=approximate)
    return seen[0]


def test_g_the_flag_selects_the_prompt_sent_to_the_model():
    assert _prompt_sent(False) is TYPED_SCALAR_SYSTEM_PROMPT
    assert _prompt_sent(True) == TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE


def test_g_a_lane_that_passes_its_own_prompt_keeps_it():
    seen: list[str] = []
    extract_typed_scalars_once([_Ep(uuid="e", content="x")], lambda s, u: seen.append(s) or "[]",
                               system_prompt="custom", approximate=True)
    assert seen == ["custom"]


# ---------------------------------------------------------------- flag

def test_the_flag_defaults_off_and_reads_its_env_var(monkeypatch):
    assert MemorySettings().personal_memory_scalar_approximate_enabled is False
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_APPROXIMATE_ENABLED", "true")
    assert MemorySettings.from_env().personal_memory_scalar_approximate_enabled is True


# ---------------------------------------------------------------- h. projection_pending

def _approx_decision():
    proposal, drops = _parse("I have about 400,000 followers now.", "about 400,000 followers",
                             approximate=True, value=400000)
    assert drops == [] and proposal is not None
    return TypedScalarDecision(
        source_key=proposal.source_key, committed=True, reason="unanimous", veto="commit",
        agreement=1.0, k=1, distribution={}, proposal=proposal)


def test_h_a_bound_approximate_row_is_rebuilt_and_its_pending_marker_cleared():
    assert approximates_value("approximate") and not folds_into_view("approximate")
    recorded, marked, rebuilt = [], [], []

    def record(assertion):
        recorded.append(assertion)
        return {"assertion_id": "apx-1", "binding_pending": False, "binding_mismatch": False,
                "created": True}

    out = bind_and_persist_typed_scalars(
        [_approx_decision()],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "User"}],
        record_assertion=record,
        rebuild_scalar_state=lambda u: rebuilt.append(u) or {"complete": True},
        mark_projection_complete=marked.append, now=lambda: "2026-07-10T12:00:00+00:00",
    )
    assert recorded[0].operation == "approximate" and recorded[0].evidence_tier == "agent"
    assert out["bound"] == 1 and out["rebuilt"] == 1
    assert rebuilt == ["ent-1"] and marked == [["apx-1"]]


NS, SUBJECT = "tenant-a", "entity-1"


def _cov_row(operation, pending):
    return {
        "assertion_id": "x1", "assertion_key": "ak-x1", "source_key": "sk-x1",
        "subject_uuid": SUBJECT, "subject_display": "Entity One", "namespace": NS,
        "attribute": "followers", "scope": "", "value_kind": "count", "unit": "",
        "operation": operation, "value": 1300, "valid_at": "2026-07-01T00:00:00+00:00",
        "learned_at": "2026-07-01T00:00:00+00:00", "episode_uuid": "ep-x1",
        "evidence_tier": "agent", "perceiver_version": "v1", "absolute_semantics": "ordinary",
        "superseded": False, "binding_pending": False, "projection_pending": pending,
        "head_subject_uuid": SUBJECT, "head_current_subject_uuid": SUBJECT,
        "head_current_assertion_id": "x1",
    }


class _Source:
    def __init__(self, rows):
        self.rows = rows

    def assertions_for_projection_audit(self, subject_uuid, *, namespace=None):
        return [dict(r) for r in self.rows]

    def list_scalar_state_views_for_audit(self, *, subject_uuid, namespace=None):
        return []


def test_h_coverage_never_waits_on_a_pending_approximate_row():
    def status(operation):
        report = ProjectionCoverageService(
            _Source([_cov_row(operation, True)]), counting_enabled=True, count_match=True
        ).audit_entity(SUBJECT, namespace=NS)
        return report.accounting[0]

    approx = status("approximate")
    assert approx.eligibility_role is EligibilityRole.HISTORY_ONLY
    assert approx.projection_status is ProjectionStatus.NOT_REQUIRED
    # positive control: a pending absolute IS still waiting on its View.
    assert status("absolute").projection_status is ProjectionStatus.PROJECTION_PENDING


# ------------------------------------------------- b2. tentative or contextual hedges are not approximations

@pytest.mark.parametrize("text,span", [
    ("Maybe 400,000 people follow me.", "Maybe 400,000"),
    ("Possibly 30 people came.", "Possibly 30"),
    ("Perhaps 12 of them stayed.", "Perhaps 12"),
    ("We have 5 cats, sometimes 6 when fostering.", "5 cats, sometimes 6"),
    ("About 30, maybe more, signed up.", "About 30, maybe more"),
])
def test_b2_a_tentative_or_contextual_hedge_still_drops_with_the_flag_on(text, span):
    """Only an approximation of ONE number is kept; "maybe"/"sometimes" are not approximations."""
    for flag in (False, True):
        proposal, drops = _parse(text, span, approximate=flag, value=int(
            "".join(ch for ch in span.split(",")[0] if ch.isdigit()) or 0) or 5)
        assert proposal is None, (flag, proposal)
        assert len(drops) == 1, (flag, drops)  # hedged_value, or an earlier count check
