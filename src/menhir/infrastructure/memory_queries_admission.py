"""Admission-provenance classification for the memory read repository (CF-229).

Split from ``memory_queries.py``; every symbol here is re-exported from there,
so the original import path keeps working unchanged.
"""

from __future__ import annotations

#: Turn capture is running and the admission join has NEVER been drawn (CF-229).
ADMISSION_NEVER_LINKED = "never_linked"
#: Turns captured and at least one join drawn -- the wiring works.
ADMISSION_LINKED = "linked"
#: No turns captured, so there is nothing to pair and nothing to report.
ADMISSION_NO_TURNS = "no_turns"


def admission_provenance_state(*, turn_evidence_count: int, admission_edge_count: int) -> str:
    """Classify the `:TurnEvidence` -> `ADMITTED_ON` wiring from two counts (CF-229).

    Deliberately narrow. The only state asserted as broken is `never_linked`: turns exist and NOT
    ONE edge does. That is unambiguous -- a producer captures turns and no caller ever reports the
    pairing -- and it is the state this deployment sat in unnoticed while every test was green.

    A partial ratio is NOT flagged. Not every memory is admitted on a turn, so "fewer edges than
    turns" is the normal, healthy shape and a threshold on it would be a guess dressed as a
    diagnosis. Zero-of-many is the only signal that carries its own proof.
    """
    if turn_evidence_count <= 0:
        return ADMISSION_NO_TURNS
    return ADMISSION_LINKED if admission_edge_count > 0 else ADMISSION_NEVER_LINKED
