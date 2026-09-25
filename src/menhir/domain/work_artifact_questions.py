"""OpenQuestion statuses and state machine, split from ``domain/work_artifact.py``.

Part of the ``work_artifact`` facade: the parent module re-exports every public
name defined here, so callers keep importing from ``menhir.domain.work_artifact``.
"""

from __future__ import annotations


class QuestionStatus:
    OPEN = "open"
    ANSWERED = "answered"
    DEFERRED = "deferred"


QUESTION_STATUSES: frozenset[str] = frozenset({
    QuestionStatus.OPEN,
    QuestionStatus.ANSWERED,
    QuestionStatus.DEFERRED,
})

#: An :OpenQuestion is an Owned Record that is nonetheless *addressable*: a
#: review must be able to say which question it answered. Being referenceable is
#: not the same as having semantic identity -- it still never surfaces in
#: recall, carries no meaning alone, and dies with its artifact.
#:
#: Answering requires naming what answered it, mirroring RESOLVES_TODO: an
#: answered question with no answering artifact is a claim with no evidence.
#: Deferring needs no evidence, because deferring is a decision rather than an
#: answer.
ANSWERS_QUESTION_EDGE = "ANSWERS_QUESTION"


#: The OpenQuestion state machine (CF-48). It previously existed only as a ``q.status = $open``
#: clause inside each of ``answer_question`` and ``defer_question``, so a fourth question status
#: would have meant two edits in infrastructure and none in the domain that defines the statuses --
#: the asymmetry :func:`can_transition` exists to prevent for artifacts, reproduced one class down.
#:
#: Both non-open states are terminal. Re-answering would overwrite which artifact actually resolved
#: the question, and that record is the whole point of answering rather than closing.
_QUESTION_FORWARD: dict[str, frozenset[str]] = {
    QuestionStatus.OPEN: frozenset({QuestionStatus.ANSWERED, QuestionStatus.DEFERRED}),
    QuestionStatus.ANSWERED: frozenset(),
    QuestionStatus.DEFERRED: frozenset(),
}


def question_can_transition(from_status: str, to_status: str) -> bool:
    """Whether an OpenQuestion may move ``from_status -> to_status`` (CF-48)."""
    return to_status in _QUESTION_FORWARD.get(from_status, frozenset())


def question_statuses_allowing(to_status: str) -> frozenset[str]:
    """Every status from which ``to_status`` is reachable in one step (CF-48).

    The compare-and-set form the repository needs. Its guard runs inside the same statement as the
    mutation -- an answered question with no answering edge is a claim with no evidence, so the two
    cannot be split -- but the guard can still ask the domain which statuses are admissible instead
    of restating the rule in Cypher. An unrecognised target yields the empty set, which admits
    nothing: fail closed, matching how the artifact status graph treats an unknown type.
    """
    return frozenset(
        source for source, targets in _QUESTION_FORWARD.items() if to_status in targets
    )
