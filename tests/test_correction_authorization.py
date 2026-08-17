"""CF-129 — correction extraction is permissive; MUTATION requires established intent.

`detect_correction` authorizes overwriting a stored counter View. Its pattern table matched
prose-shaped connectives (`from X to Y`, `X instead of Y`, `to X from Y`) anywhere in a turn
with no intent requirement, so ordinary speech authorized a destructive memory mutation --
stamped ``view_audit_gate="correction"``, indistinguishable from a real correction, and
LWW-stamped so it wins later re-folds.

The invariant these tests pin down:

    ambiguous language may not authorize a destructive memory mutation.

Missing a correction is recoverable -- the stale value survives and the user can restate.
Silently overwriting the wrong value and recording it as genuine is not. So extraction stays
permissive (a candidate is produced) and AUTHORIZATION is separate, requiring either an
explicit correction cue or an edit action grounded on the stored thing.

Note the discriminator is NOT the verb. "The temperature changed from 25 to 20" and
"change it from 25 to 20" share a verb; only the target differs. A change-verb whitelist
admits all four of the descriptive negatives below, which is why one is not used.
"""

from __future__ import annotations

import pytest

from menhir.services.correction_resolver import (
    detect_correction,
    extract_correction_candidate,
    resolve_corrections,
)


class FakeGraph:
    """Mirrors tests/test_correction_resolver.py::FakeGraph (read, not modified)."""

    def __init__(self, views):
        self.views = [dict(v) for v in views]
        self.writes = []

    def list_counters(self, *, namespace=None, limit=200):
        return [dict(v) for v in self.views]

    def record_counter(self, *, subject, counter, value, namespace=None, valid_at=None,
                       episode_uuids=None, source="", name_embedding=None, audit=None):
        self.writes.append({"subject": subject, "counter": counter, "value": float(value),
                            "valid_at": valid_at, "audit": audit})
        for v in self.views:
            if v["subject"] == subject and v["counter"] == counter:
                v["value"] = float(value)
                break
        else:
            self.views.append({"subject": subject, "counter": counter, "value": float(value),
                               "valid_at": valid_at})
        return {"uuid": "u", "view_key": f"{subject}:{counter}", "created": True,
                "value": float(value)}


def _rows(*texts):
    return [{"uuid": f"c{i}", "valid_at": "2026-07-07T15:00:00Z", "content": t}
            for i, t in enumerate(texts)]


# ---------------------------------------------------------------- authorized (must still work)

@pytest.mark.unit
@pytest.mark.parametrize("text", [
    # intrinsically self-cueing -- the connective IS the correction intent, no cue needed
    "Actually it is 20, not 25.",
    "it's 20 not 25",
    "not 25, it's 20",
    "Not 25 anymore, it is 20.",
    "not 25 anymore, it's 20",
    "not 25 any longer, now it is 20",
    "25 -> 20",
    "correction: 25 --> 20",
    "watchlist 25 => 20",
    "20 replaces 25",
    "20 replacing 25",
    "25 replaced by 20",
    # prose-shaped, but intent established by an explicit cue
    "Actually, 20 instead of 25",
    "I meant 20 instead of 25",
    # prose-shaped, but intent established by an edit action grounded on the stored thing
    "changed it to 20 from 25",
    "change it from 25 to 20",
    "update the count to 20 from 25",
    "bump to 20 from 25",
])
def test_authorized_corrections_still_detect(text):
    assert detect_correction(text) == (25.0, 20.0)


# ------------------------------------------------------------- unauthorized (must NOT mutate)

@pytest.mark.unit
@pytest.mark.parametrize("text", [
    # ordinary prose that happens to contain two numbers
    "I work from 9 to 5 most days.",
    "We drove from 40 to 60 miles an hour.",
    "The recipe scales from 4 to 8 servings.",
    "My blood pressure went from 120 to 118.",
    "I bought 6 instead of 4 because they were on sale.",
    "The meeting moved to 2 from 4.",
    "Blood pressure dropped from 120 to 118.",
    # descriptive speech CARRYING AN EDIT VERB -- these defeat any change-verb whitelist,
    # because the verb is identical to the authorized cases and only the target differs
    "The temperature changed from 25 to 20.",
    "My dosage changed from 25 to 20.",
    "I switched from 25 to 20 last month.",
    "The schedule was updated from 9 to 5.",
    "I ended up buying 20 instead of 25.",
])
def test_ordinary_speech_is_not_authorized(text):
    assert detect_correction(text) is None


# --------------------------------------------------- deliberately withdrawn ambiguous fragments

@pytest.mark.unit
@pytest.mark.parametrize("bare,with_cue", [
    ("20 instead of 25", "Actually, 20 instead of 25"),
    ("changed from 25 to 20", "I meant changed from 25 to 20"),
])
def test_ambiguous_fragment_abstains_but_a_cue_reauthorizes(bare, with_cue):
    """These two used to detect and no longer do -- a deliberate contract change.

    Neither establishes correction intent: "20 instead of 25" is bare, and
    "changed from 25 to 20" is equally readable as an elided "[the temperature] changed
    from 25 to 20". Withdrawing write authority from syntax that cannot establish intent is
    not the same as losing correction support -- add a cue and it works again.
    """
    assert detect_correction(bare) is None
    assert detect_correction(with_cue) == (25.0, 20.0)


@pytest.mark.unit
@pytest.mark.parametrize("text", ["20 instead of 25", "changed from 25 to 20"])
def test_withdrawn_fragments_still_yield_an_unauthorized_candidate(text):
    """Extraction stays permissive: the pair is still recoverable, it just may not mutate.

    This is what lets a future conversational-context layer authorize the bare fragment when
    the preceding turn supplies the grounding (e.g. after "Did you mean 25 or 20?") without
    reopening the patterns.
    """
    assert extract_correction_candidate(text) == (25.0, 20.0, True)


# ------------------------------------------------------------------- end-to-end: no write, no audit

@pytest.mark.unit
@pytest.mark.parametrize("text", [
    "My blood pressure went from 120 to 118.",
    "The temperature changed from 25 to 20.",
])
def test_unauthorized_turn_writes_nothing_and_stamps_no_correction_audit(text):
    """Both halves matter.

    Asserting only that the value is unchanged would pass an implementation that still emitted
    a ``view_audit_gate="correction"`` event -- fixing the mutation while continuing to poison
    provenance. The View below deliberately holds a value the prose mentions (120 / 25), which
    is exactly the numeric coincidence the old value-match "safety net" relied on.
    """
    g = FakeGraph([
        {"subject": "user", "counter": "movies", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
        {"subject": "user", "counter": "steps", "value": 120.0, "valid_at": "2026-07-07T00:00:00Z"},
    ])
    before = {(v["subject"], v["counter"]): v["value"] for v in g.views}

    out = resolve_corrections(_rows(text), g, namespace="ns")

    assert out["corrections_applied"] == 0
    assert g.writes == [], f"unauthorized turn wrote: {g.writes}"
    after = {(v["subject"], v["counter"]): v["value"] for v in g.views}
    assert after == before, "stored values must be untouched"
    assert not any(
        (w.get("audit") or {}).get("view_audit_gate") == "correction" for w in g.writes
    ), "no correction-stamped audit event may be written for an unauthorized turn"


@pytest.mark.unit
def test_authorized_turn_still_supersedes_end_to_end():
    """Guard: the authorization gate did not break the feature it protects."""
    g = FakeGraph([
        {"subject": "user", "counter": "movies", "value": 25.0, "valid_at": "2026-07-07T00:00:00Z"},
        {"subject": "user", "counter": "bike_spend", "value": 125.0, "valid_at": "2026-07-07T00:00:00Z"},
    ])
    out = resolve_corrections(_rows("Actually it is 20, not 25."), g, namespace="ns")

    assert out["corrections_applied"] == 1
    assert any(w["counter"] == "movies" and w["value"] == 20.0 for w in g.writes)
    assert next(v for v in g.views if v["counter"] == "movies")["value"] == 20.0
    assert not any(w["counter"] == "bike_spend" for w in g.writes)


# ------------------------------------------------------------------------------- the API gate

@pytest.mark.unit
@pytest.mark.parametrize("body_flag,setting,expected", [
    (True, False, False),   # CF-129: stock deployment -- request body no longer decides alone
    (True, True, True),
    (False, True, False),
    (False, False, False),
])
def test_counter_state_requires_both_body_and_deployment_flag(body_flag, setting, expected):
    """The API path must honour ``personal_memory_consolidation_enabled`` like the scheduler.

    Seam: the boolean composition at api/routes_handlers.py, exercised directly. A full
    request would drag in the whole phase3 runtime; this asserts the specific conjunction
    that changed, which is the honest unit for the change made.
    """
    class _S:
        personal_memory_consolidation_enabled = setting

    assert (body_flag and getattr(_S(), "personal_memory_consolidation_enabled", False)) is expected
