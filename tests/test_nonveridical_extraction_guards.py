"""Issue #283: sarcasm and another speaker's claim must not become stored facts or scalar values.

The #283 probe (scripts/probe_nonveridical_extraction.py) found negated, asked and hypothetical
turns already handled by the "Preserve negation" rule, but sarcastic turns stored literally at both
layers ("user is definitely a morning person"; chronotype="morning person") and a contradicted
third-party claim stored as a scalar (brother smoking=false from "my brother says he quit smoking,
but I saw him smoking yesterday"). Offline tests pin what the model is told; the probe measures
what it does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from menhir.infrastructure import graphiti_extraction_policy as policy
from menhir.services.typed_scalar_rules import (
    TYPED_SCALAR_SYSTEM_PROMPT,
    TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _Envelope:
    marker = "MENHIR_SELF_0123"


def _blocks() -> list[str]:
    return [policy._subject_endpoint_instructions(_Envelope()),
            policy._inferred_endpoint_instructions(_Envelope(), "user")]


def test_both_endpoint_blocks_carry_the_sarcasm_rule_after_negation():
    for block in _blocks():
        assert "Sarcasm and irony are not affirmative facts" in block
        assert block.index("Preserve negation") < block.index("Sarcasm and irony")
        assert "never the ironic claim" in block


def test_endpoint_blocks_still_end_with_the_marker_rule():
    for block in _blocks():
        assert block.rstrip().splitlines()[-1].startswith(f"- Emit `{_Envelope.marker}` only")


@pytest.mark.parametrize("prompt", [TYPED_SCALAR_SYSTEM_PROMPT, TYPED_SCALAR_SYSTEM_PROMPT_APPROXIMATE])
def test_scalar_prompt_rejects_sarcasm_and_unaffirmed_claims(prompt):
    rule = prompt[prompt.index("Emit only values the episode itself presents as TRUE"):]
    assert "SARCASM or irony is not a value" in rule
    assert "ANOTHER PERSON'S CLAIM" in rule
    assert "emit nothing for that claim" in rule


def test_scalar_rule_sits_before_the_schema():
    """Read as a scope rule for every observation, not as part of one field."""
    assert (TYPED_SCALAR_SYSTEM_PROMPT.index("presents as TRUE")
            < TYPED_SCALAR_SYSTEM_PROMPT.index("Each observation is an object"))


def _shingles(text: str, n: int = 4) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def test_prompts_do_not_quote_the_probe_corpus():
    """The probe measures these rules, so the rules must not teach its turns: no 4-word run of any
    corpus turn may appear in a prompt that the probe evaluates."""
    corpus = json.loads((ROOT / "tests/fixtures/nonveridical/extraction_probe_283.json")
                        .read_text(encoding="utf-8"))["cases"]
    prompts = {"scalar": _shingles(TYPED_SCALAR_SYSTEM_PROMPT),
               **{f"endpoint{i}": _shingles(b) for i, b in enumerate(_blocks())}}
    leaks = {(name, case["id"], s) for case in corpus for s in _shingles(case["turn"])
             for name, grams in prompts.items() if s in grams}
    assert not leaks, sorted(leaks)
