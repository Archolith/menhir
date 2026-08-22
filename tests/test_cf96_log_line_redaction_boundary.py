"""CF-96 -- the log-line redactor is heuristic, and the one producer that exploited that is fixed.

The finding was reframed once already and the reframing is the point. `redact_log_line` does exactly
what its own docstring says: it masks quoted spans in an already-rendered string, best-effort. It is
not broken. What was wrong was the gap between that and what an operator was told -- the security
posture doc promised `MENHIR_PRIVACY_REDACT=true` hides memory contents "in the explorer UI and the
console dashboard's log tail", with no hint that the second one is a regex over finished text.

So this file pins three things, in descending order of how much they matter:

1. The **demonstrated leak is gone at the source**. `correlation_service` logged two entity names
   through unquoted `%s`; it now logs uuids. That is a real reduction, and it is the only one here.
2. The **redactor's limits are real and stay documented**, asserted by demonstrating them rather
   than by trusting a docstring. If someone later "fixes" the heuristic, these tests say what
   changed.
3. The **field-exact tier is genuinely exact**, so the two-tier claim in the module docstring is not
   itself an overclaim in the other direction.

Nothing here asserts that log lines are safe, because they are not. The durable fix is field-aware
structured logging; until then the producer rule is "log the uuid, not the content".
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from menhir.privacy import MASK, REDACTED_FIELDS, redact_log_line, redact_mapping

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[1]
PREFIX = "2026-08-21 10:00:00,000 - menhir.services.correlation_service - DEBUG - "


# ---------------------------------------------------------------------------
# 1. the demonstrated leak, fixed at the producer
# ---------------------------------------------------------------------------


def test_the_judge_vote_line_logs_uuids_not_names() -> None:
    """THE ACTUAL FIX. CF-96's evidence was `logger.debug("Judge %d: %s vs %s -> %s", judge_id,
    meta_a.get("name"), meta_b.get("name"), vote)` -- memory content into a log line through an
    unquoted `%s`, which the redactor structurally cannot see.

    Asserted against the source rather than by capturing a log record: reaching that line at runtime
    needs an LLM judge and a live merge proposal, and a test that elaborate would be pinning the
    mock, not the fix.
    """
    src = (REPO / "src/menhir/services/correlation_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    judge_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("Judge %d:")
    ]
    assert len(judge_calls) == 1, "expected exactly one judge-vote log call"

    rendered = [ast.unparse(a) for a in judge_calls[0].args[1:]]
    assert "survivor_uuid" in rendered and "absorbed_uuid" in rendered
    assert not any("name" in a for a in rendered), (
        f"judge log line still interpolates a name: {rendered}"
    )


def test_no_name_lookup_is_interpolated_into_any_log_call_in_that_module() -> None:
    """The producer rule, not just the one line. `meta_a.get("name")` reaching any logger call in
    this module is the same defect wearing a different message string."""
    tree = ast.parse((REPO / "src/menhir/services/correlation_service.py").read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
            continue
        for arg in node.args[1:]:
            text = ast.unparse(arg)
            if ".get('name')" in text or ".get('summary')" in text or ".get('content')" in text:
                offenders.append((node.lineno, text))

    assert offenders == [], f"memory content interpolated into a log call: {offenders}"


# ---------------------------------------------------------------------------
# 2. the limits are real -- demonstrated, not asserted from the docstring
# ---------------------------------------------------------------------------


def test_unquoted_free_text_passes_through_untouched() -> None:
    """CF-96's mechanism, pinned. This is NOT a bug being locked in -- it is the boundary being made
    executable, so a future change to the heuristic has to update a test that says out loud what the
    old behaviour was."""
    line = PREFIX + "Judge 0: Charles Harvey vs Charlie Harvey -> True"
    assert redact_log_line(line, reveal=False) == line
    assert "Charles Harvey" in redact_log_line(line, reveal=False)


def test_a_short_quoted_value_also_passes() -> None:
    """The other limit the docs now name. A single-word name survives even WITH quotes, and TWO
    independent gates in `_is_free_text` each let it through: it is under the 12-char floor, and it
    contains no whitespace. Lowering the floor alone does not change this -- verified by mutation.
    That is why the producer fix logs uuids rather than just adding quotes: quoting would not have
    closed it.

    Asserts on the BODY, not the whole line. The first version searched the whole line for the
    token and passed vacuously: `PREFIX` carries the logger name `menhir.services...`, so the
    needle was always present no matter what the redactor did. Caught by the over-mask mutation,
    which this test should have failed and did not.
    """
    body = "resolved to 'Ada'"
    out = redact_log_line(PREFIX + body, reveal=False)
    assert out[len(PREFIX):] == body


def test_quoted_sentence_length_free_text_is_masked() -> None:
    """POSITIVE CONTROL. Without this, every assertion above would also pass against a redactor
    that had silently become a no-op."""
    line = PREFIX + "matched \"the workspace rename discussion\" against candidate"
    out = redact_log_line(line, reveal=False)
    assert "workspace rename discussion" not in out
    assert MASK in out


def test_the_structural_prefix_survives() -> None:
    """Second positive control, in the other direction: a redactor that masked everything would
    also hide the content above. The line must stay diagnosable."""
    line = PREFIX + "matched \"the workspace rename discussion\" against candidate"
    assert redact_log_line(line, reveal=False).startswith(PREFIX)


# ---------------------------------------------------------------------------
# 3. the field-exact tier really is exact, so the two-tier claim holds both ways
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(REDACTED_FIELDS))
def test_the_mapping_tier_masks_by_key_regardless_of_shape(field: str) -> None:
    """The explorer's tier. Short, unquoted, identifier-shaped -- none of it matters here, because
    this one selects on the KEY. That difference is the whole content of the module's two-tier
    docstring, and it would be an overclaim if this tier were heuristic too."""
    masked = redact_mapping({field: "x", "uuid": "u-1"}, reveal=False)
    assert masked[field] == MASK
    assert masked["uuid"] == "u-1", "structural field must survive"


def test_the_two_tiers_disagree_on_the_same_content() -> None:
    """The finding in one assertion: identical memory text is hidden by one tier and shown by the
    other, under one setting."""
    name = "Ada"
    body = f"judged {name} vs Bob"
    assert redact_mapping({"name": name}, reveal=False)["name"] == MASK
    # Body-only, for the reason given in `test_a_short_quoted_value_also_passes`.
    assert redact_log_line(PREFIX + body, reveal=False)[len(PREFIX):] == body


# ---------------------------------------------------------------------------
# the operator-facing claim, which is what CF-96 was actually about
# ---------------------------------------------------------------------------


def test_the_security_posture_doc_states_the_log_tail_is_not_a_guarantee() -> None:
    """The register's fix line: "so nobody builds a policy on the wider reading." The policy is
    built from this document, not from the function's docstring -- it previously listed the explorer
    UI and the console log tail together with no distinction between them."""
    doc = (REPO / "docs/security-posture.md").read_text(encoding="utf-8").lower()
    assert "cf-96" in doc
    assert "field-exact" in doc
    assert "heuristic" in doc
    assert "not" in doc and "guarantee" in doc
