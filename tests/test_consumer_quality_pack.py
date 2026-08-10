"""Phase 3 consumer-quality pack v1 — deterministic tests for:

  * item 1: `count_spend_compound` detector + the count-vs-spend partial co-extraction receipt
            (observability only; never changes what commits — DECISION 1 = safety-only).
  * item 2: `verify_retries` bounded verifier retry (precision bar unchanged) + verifier receipt
            clarity (`verify_votes`/`verify_k`/`verify_attempts` on GateDecision).
  * item 3: new correction phrasings (arrow / to-from / replaces / replaced-by), each protected by
            the resolver's unique-value-match safety net.

All deterministic — a fake LLM / fake verifier and a fake graph, no live model or Neo4j. The governing
invariant under test: a wrong current-state View is worse than a miss, so every new lever either keeps
precision identical or trades a miss for a clearer receipt.
"""

from __future__ import annotations

import pytest

from menhir.domain.fold_algebra import Event
from menhir.services.correction_resolver import detect_correction, resolve_corrections
from menhir.services.perception import (
    Episode,
    PerceivedGroup,
    count_spend_compound,
    gate,
    perceive_and_fold,
    verify_candidate_detailed,
)


# ----------------------------------------------------------------------------- fakes


class FakeAdapter:
    def __init__(self):
        self.calls: list[dict] = []

    def record_counter(self, **kwargs):
        self.calls.append(kwargs)
        return {"uuid": "u", "view_key": "k", "created": True}

    def counters(self, name: str) -> list[dict]:
        return [c for c in self.calls if c.get("counter") == name]


class SeqLLM:
    """Returns pre-baked completion strings in order (cycling), for extractor + verifier sampling."""

    def __init__(self, responses: list[str]):
        self._responses = responses
        self.calls = 0

    def __call__(self, system: str, user: str) -> str:
        out = self._responses[self.calls % len(self._responses)]
        self.calls += 1
        return out


def _grp(events, reducer="sum", measure="bike_spend", stated=None):
    return PerceivedGroup(subject="user", measure=measure, reducer=reducer,
                          events=list(events), stated_total=stated)


def _bike_events():
    return [Event(when="2026-01-10", kind="purchase", value=50.0),
            Event(when="2026-03-02", kind="purchase", value=75.0)]


# ============================================================================= item 3: corrections


@pytest.mark.unit
@pytest.mark.parametrize("text,expected", [
    # arrow forms (ASCII arrow is the required connective)
    ("25 -> 20", (25.0, 20.0)),
    ("correction: 25 --> 20", (25.0, 20.0)),
    ("watchlist 25 => 20", (25.0, 20.0)),
    # reverse from/to
    ("changed it to 20 from 25", (25.0, 20.0)),
    ("bump to 20 from 25", (25.0, 20.0)),
    # replacement phrasings
    ("20 replaces 25", (25.0, 20.0)),
    ("20 replacing 25", (25.0, 20.0)),
    ("25 replaced by 20", (25.0, 20.0)),
    # safety: no connective / equal values -> not a correction
    ("I have 25 20 movies", None),      # two bare numbers, no connective
    ("25 -> 25", None),                 # old == new
])
def test_new_correction_phrasings_detect(text, expected):
    assert detect_correction(text) == expected


@pytest.mark.unit
def test_existing_correction_phrasings_still_detect():
    # guard: the additions did not disturb the original connectives.
    assert detect_correction("Actually it is 20, not 25.") == (25.0, 20.0)
    assert detect_correction("changed from 25 to 20") == (25.0, 20.0)
    assert detect_correction("Not 25 anymore, it is 20.") == (25.0, 20.0)


@pytest.mark.unit
def test_new_phrasing_binds_only_to_value_matching_view():
    """An arrow correction re-values ONLY a current View that already holds `old`; the value-match net
    means the new phrasing cannot write a wrong View."""
    g = FakeAdapter()
    g.calls.append({"counter": "movies", "subject": "user", "value": 25.0})

    def list_counters(namespace, limit=200):
        return [{"subject": "user", "counter": "movies", "value": 25.0}]

    g.list_counters = list_counters
    rows = [{"uuid": "c0", "valid_at": "2026-07-08T15:00:00Z", "content": "25 -> 20"}]
    out = resolve_corrections(rows, g, namespace="ns")
    assert out["corrections_applied"] == 1
    assert out["details"][0]["old"] == 25.0 and out["details"][0]["new"] == 20.0


@pytest.mark.unit
def test_new_phrasing_with_no_matching_view_abstains():
    g = FakeAdapter()
    g.list_counters = lambda namespace, limit=200: [
        {"subject": "user", "counter": "movies", "value": 30.0}]  # no 25-valued View
    rows = [{"uuid": "c0", "valid_at": "2026-07-08T15:00:00Z", "content": "25 -> 20"}]
    out = resolve_corrections(rows, g, namespace="ns")
    assert out["corrections_applied"] == 0 and out["corrections_no_target"] == 1


# ============================================================================= item 1: count-vs-spend


@pytest.mark.unit
@pytest.mark.parametrize("text,expected", [
    ("I bought 2 bikes for $125 total.", ("bike", 2, 125.0)),
    ("bought 3 books for $30", ("book", 3, 30.0)),
    ("I picked up 4 mugs for $18.50", ("mug", 4, 18.5)),
    # negatives
    ("I bought a bike for $125", None),           # no count digit
    ("bought 1 bike for $125", None),             # count of 1 is not a count-case (floored anyway)
    ("bought 2 tickets for the 3pm show", None),  # 'for <not money>'
    ("I have 2 bikes", None),                     # no spend clause
])
def test_count_spend_compound_detector(text, expected):
    assert count_spend_compound(text) == expected


@pytest.mark.unit
def test_count_vs_spend_partial_receipt_when_only_spend_commits():
    """The extractor lands only the spend (125) for a 'bought 2 bikes for $125' compound; the boundary
    detects the compound and records a legible count_vs_spend_partial receipt (never a View)."""
    episodes = [Episode(uuid="e0", content="I bought 2 bikes for $125 total.")]
    # extractor emits only the SUM (two purchase events totalling 125), no count events.
    resp = ('[{"episode":0,"subject":"user","measure":"bike_spend","kind":"purchase",'
            '"when":"2026-01-10","value":50,"category":"biking","what":"bike"},'
            '{"episode":0,"subject":"user","measure":"bike_spend","kind":"purchase",'
            '"when":"2026-01-11","value":75,"category":"biking","what":"bike"}]')
    adapter = FakeAdapter()
    result = perceive_and_fold(
        episodes=episodes, llm_complete=SeqLLM([resp]), graph_adapter=adapter,
        k=3, namespace="ns", record_abstentions=True)
    # spend committed...
    assert any(abs(float(c.get("value", 0)) - 125.0) < 0.5 for c in result.committed)
    # ...and a partial receipt recorded (count side missing).
    partials = adapter.counters("count_vs_spend_partial")
    assert partials and partials[0]["value"] == 1.0


@pytest.mark.unit
def test_no_partial_receipt_without_compound_text():
    episodes = [Episode(uuid="e0", content="I spent 50 and 75 on bikes.")]
    resp = ('[{"episode":0,"subject":"user","measure":"bike_spend","kind":"purchase",'
            '"when":"2026-01-10","value":50,"category":"biking","what":"bike"},'
            '{"episode":0,"subject":"user","measure":"bike_spend","kind":"purchase",'
            '"when":"2026-01-11","value":75,"category":"biking","what":"bike"}]')
    adapter = FakeAdapter()
    perceive_and_fold(episodes=episodes, llm_complete=SeqLLM([resp]), graph_adapter=adapter,
                      k=3, namespace="ns", record_abstentions=True)
    assert adapter.counters("count_vs_spend_partial") == []


# ============================================================================= item 2: retry + receipt


@pytest.mark.unit
def test_verify_candidate_detailed_returns_votes():
    ok, votes, kk = verify_candidate_detailed(
        "bike_spend", 125.0, _bike_events(), lambda s, u: '{"correct": true}', k=3)
    assert ok is True and votes == 3 and kk == 3
    ok2, votes2, _ = verify_candidate_detailed(
        "bike_spend", 125.0, _bike_events(), lambda s, u: '{"correct": false}', k=3)
    assert ok2 is False and votes2 == 0


@pytest.mark.unit
def test_verify_retries_rescues_a_flaky_but_correct_sum():
    """A verifier that fails the first attempt then passes commits WITH verify_retries, and abstains
    WITHOUT — precision per attempt is unchanged (each attempt still needs the verifier to pass)."""
    samples = [[_grp(_bike_events())] for _ in range(3)]  # unanimous SUM=125

    class FlakyVerifier:
        def __init__(self): self.n = 0
        def __call__(self, measure, value, events):
            self.n += 1
            return (self.n >= 2, 3 if self.n >= 2 else 1, 3)  # attempt 1 fails, attempt 2 passes

    [d0] = gate(samples, threshold=1.0, verifier=FlakyVerifier(), verify_retries=0)
    assert d0.committed is False and d0.veto == "verification"
    assert d0.verify_attempts == 1 and d0.verify_votes == 1  # receipt clarity: how close it was

    [d1] = gate(samples, threshold=1.0, verifier=FlakyVerifier(), verify_retries=2)
    assert d1.committed is True and d1.value == 125.0
    assert d1.verify_attempts == 2 and d1.verify_votes == 3


@pytest.mark.unit
def test_verify_retries_never_commits_a_verifier_rejected_value():
    """Retries only give MORE chances to pass the SAME bar — an always-failing verifier is never
    rescued, no matter how many retries (precision floor preserved)."""
    samples = [[_grp(_bike_events())] for _ in range(3)]
    always_false = lambda m, v, e: (False, 0, 3)  # noqa: E731
    [d] = gate(samples, threshold=1.0, verifier=always_false, verify_retries=5)
    assert d.committed is False and d.veto == "verification"
    assert d.verify_attempts == 6  # 1 + 5 retries, all failed


@pytest.mark.unit
def test_bool_verifier_still_supported_without_vote_detail():
    samples = [[_grp(_bike_events())] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, verifier=lambda m, v, e: True)
    assert d.committed is True and d.value == 125.0
    assert d.verify_votes is None and d.verify_k is None  # bare bool -> no vote detail
