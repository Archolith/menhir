"""Fallback answers for the prompts of lanes that run by default since the #168 freeze.

A lane scripts the prompts it is about. Scalar state, counting, related-count matching and the
anchored-time resolver are now on by default, so a lane about something else still sends their
prompts. These rules answer them as a model would for turns that hold nothing for that lane:
no observations, no happenings, every new happening counted, no stated time. They are consulted
only after the lane's own rules, so a lane that scripts one of these prompts keeps its answers.

Each rule matches a unique prompt substring, like the lanes' own rules: a reworded prompt still
reaches ``Script.unmatched`` and fails the lane.
"""

from __future__ import annotations

import json
import re

from tests.e2e._harness.phase3 import (
    COUNTING_PROMPT,
    DECIDE_PROMPT,
    SCALAR_PROMPT,
    decide_reply,
    envelope_reply,
)
from tests.e2e._harness.scripted_provider import Call, Rule

__all__ = ["ANCHORED_TIME_PROMPT", "shipped_default_rules"]

#: Unique substring of the resolver's system prompt (infrastructure/anchored_time.py).
ANCHORED_TIME_PROMPT = "You identify WHEN things happened"

#: The real input follows this marker; the worked examples above it list FACTS too.
_REAL_INPUT = "Now the real input."
_FACT_LINE = re.compile(r"^(\d+)\. ", re.MULTILINE)


def _no_stated_times(call: Call) -> str:
    """Every listed fact gets basis "none"; no missing events."""
    _, _, real = call.prompt.partition(_REAL_INPUT)
    facts_block = real.partition("FACTS:")[2].partition("OUTPUT:")[0]
    facts = [
        {"i": int(index), "expression": None, "basis": "none", "kind": "state",
         "offset": None, "calendar": None, "date": None, "anchor": None}
        for index in _FACT_LINE.findall(facts_block)
    ]
    return json.dumps({"facts": facts, "missing_events": []})


def shipped_default_rules() -> list[Rule]:
    return [
        Rule("default:scalar", SCALAR_PROMPT, envelope_reply([])),
        Rule("default:counting", COUNTING_PROMPT, envelope_reply([])),
        Rule("default:decide", DECIDE_PROMPT, decide_reply([], family="")),
        Rule("default:anchored_time", ANCHORED_TIME_PROMPT, _no_stated_times),
    ]
