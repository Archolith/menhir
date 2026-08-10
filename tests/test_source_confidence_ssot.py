"""`source_confidence_for` is the single authority for source -> trust tier.

THE DIVERGENCE THIS PREVENTS. `structure_queries` named SOURCE_CONFIDENCE_STRUCTURAL directly while
`source_confidence_for` had no entry for 'project-scan' and fell through to the agent default. Both
files were internally correct and disagreed with each other. Measured in production 2026-07-28:
48,781 :Entity carried 0.9 and 76 :Episodic carried 0.5 for the identical source string.

WHY IT WAS DANGEROUS RATHER THAN UNTIDY. The proposed repair for the entity-merge inflation bug is to
recompute confidence from source via this function on merge. Against the old table that would have
demoted every project-scan entity from structural to agent tier -- 86% of the graph -- as a silent
side effect of fixing a defect affecting 2%.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from menhir.domain.truth.kinds import (
    SOURCE_CONFIDENCE_AGENT,
    SOURCE_CONFIDENCE_AGENT_REVIEWED,
    SOURCE_CONFIDENCE_STRUCTURAL,
    SOURCE_CONFIDENCE_USER,
)
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure.structure_queries import (
    _ENTITY_DEFAULTS,
    STRUCTURE_SOURCE,
    STRUCTURE_SOURCE_CONFIDENCE,
)

_STRUCTURE_QUERIES = (Path(__file__).resolve().parents[1]
                      / "src" / "menhir" / "infrastructure" / "structure_queries.py")


# --------------------------------------------------------------- the divergence, pinned

def test_the_scanner_and_the_ladder_agree_on_project_scan():
    """THE regression. These two disagreed in production for 48,781 entities."""
    assert STRUCTURE_SOURCE_CONFIDENCE == source_confidence_for(STRUCTURE_SOURCE)
    assert _ENTITY_DEFAULTS["source_confidence"] == source_confidence_for(
        _ENTITY_DEFAULTS["source"])


def test_project_scan_is_structural_not_agent():
    """The value the scanner was already writing, now what the ladder returns too. If this flips to
    0.5, a merge-time recompute silently demotes the largest population in the graph."""
    assert source_confidence_for("project-scan") == SOURCE_CONFIDENCE_STRUCTURAL
    assert source_confidence_for("project-scan") != SOURCE_CONFIDENCE_AGENT


def test_the_scanner_does_not_restate_the_confidence():
    """It must DERIVE. Naming SOURCE_CONFIDENCE_STRUCTURAL again is exactly how the two parted."""
    body = _STRUCTURE_QUERIES.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "SOURCE_CONFIDENCE_STRUCTURAL" not in code


def test_the_cypher_literals_still_match_the_declared_source():
    """The Cypher writes `source = 'project-scan'` as a literal while the tier is derived from
    STRUCTURE_SOURCE. If someone renames the constant and not the literals, nodes get a label whose
    tier was computed for a different string.

    Matches ANY single-quoted value, not `[a-z-]+`: a narrow character class makes an off-pattern
    rename (`'project-scan-v2'`) fail to match at all, so the literal goes UNSEEN rather than caught.
    Mutation testing found exactly that hole in the first version of this test.
    """
    body = _STRUCTURE_QUERIES.read_text(encoding="utf-8")
    literals = set(re.findall(r"\bsource = '([^']*)'", body))
    assert literals == {STRUCTURE_SOURCE}, literals


# ------------------------------------------------------------------- the table's contents

def test_test_is_not_promoted_to_structural():
    """kinds.py describes the structural category as 'git, file, test, project-scan', but across this
    suite `source="test"` is a FIXTURE PLACEHOLDER, not a claim a test produced the fact. Adding it
    on the strength of that prose would promote fixtures to the human-reviewed tier."""
    assert source_confidence_for("test") == SOURCE_CONFIDENCE_AGENT


@pytest.mark.parametrize("label,expected", [
    ("manual", SOURCE_CONFIDENCE_USER),
    ("user", SOURCE_CONFIDENCE_USER),
    ("project-scan", SOURCE_CONFIDENCE_STRUCTURAL),
    ("claude-code", SOURCE_CONFIDENCE_AGENT_REVIEWED),
])
def test_known_labels_resolve_to_named_constants(label, expected):
    """Every tier is a named constant, so the ladder cannot drift by someone editing a bare float."""
    assert source_confidence_for(label) == expected


@pytest.mark.parametrize("label", ["PROJECT-SCAN", "  project-scan  ", "Project-Scan"])
def test_lookup_is_case_and_whitespace_insensitive(label):
    assert source_confidence_for(label) == SOURCE_CONFIDENCE_STRUCTURAL


def test_an_exact_key_wins_over_a_substring_rule():
    """Ordering contract: exact keys are consulted first. A future label that both matches a key and
    contains a model substring must take its key's tier."""
    assert source_confidence_for("claude-code") == SOURCE_CONFIDENCE_AGENT_REVIEWED


@pytest.mark.parametrize("label", ["unknown-tool", "", "   ", "some-new-harness"])
def test_an_unrecognised_label_falls_back_to_agent_tier(label):
    """Fail LOW. An unknown writer must not inherit a trust tier it never earned."""
    assert source_confidence_for(label) == SOURCE_CONFIDENCE_AGENT


def test_none_is_handled():
    assert source_confidence_for(None) == SOURCE_CONFIDENCE_AGENT


# ------------------------------------------------------- ceiling semantics (documented downgrade)

def test_a_writer_may_stamp_below_the_tier_but_the_table_states_the_ceiling():
    """structure_queries stamps an INFERRED import target source='project-scan' at agent tier,
    because the node's existence is inferred rather than scanned. That downgrade is legitimate and
    deliberate; this test records that the table states a ceiling, not a mandate."""
    body = _STRUCTURE_QUERIES.read_text(encoding="utf-8")
    assert '"sc_inferred": SOURCE_CONFIDENCE_AGENT' in body
    assert SOURCE_CONFIDENCE_AGENT < source_confidence_for(STRUCTURE_SOURCE)
