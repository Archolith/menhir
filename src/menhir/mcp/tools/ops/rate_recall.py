"""MCP tool: rate_recall — self-reported usefulness feedback for a prior recall."""

from __future__ import annotations

from typing import Literal

from menhir.mcp.feedback import SCORE_SCALE, is_valid_score, score_value
from menhir.mcp.service_access import get_request_session
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope

RecallScore = Literal["useful", "partial", "noise", "unused"]

_RATE_RECALL_DOC = """Report how useful a prior recall/context result was.

    This is an operational quality signal for the usage dashboard. It is
    self-reported (agent_inference grade) and never affects memory heat,
    promotion, or ranking — so rate honestly without worrying about
    reinforcing a memory.

    Call this shortly after acting on a recall_memories / recall_context_memories
    / read_flagged_memories / build_context result.

    Rate on what you ACTUALLY USED — what changed your answer or action — not on
    whether the results looked plausible. A plausible-looking result you did not
    use is "unused" or "noise", not "useful".

    Args:
        score: One of:
            - "useful": you used specific content from the result — cited a fact,
              reused a prior decision, avoided re-asking/re-deriving, or it directly
              answered the question.
            - "partial": it confirmed or narrowed your direction, but you still
              needed other sources; only some items were relevant.
            - "noise": irrelevant, stale, or wrong — you ignored it or it pointed
              you the wrong way.
            - "unused": you did not consult it (speculative/precautionary recall, or
              you already had the answer in context).
        recall_id: The recall_id token from the recall response. If omitted, the
            most recent unrated recall in this session is rated.
        reason: Optional compatibility note. It is accepted but deliberately NOT persisted;
            usefulness telemetry stores only the structured score so a session-wide receipt
            does not become an unerasable free-text sidecar copy.

    Returns:
        Confirmation of the rated recall, or an error if no matching recall exists.
    """


async def rate_recall(
    score: RecallScore,
    recall_id: str | None = None,
    reason: str | None = None,
) -> str:
    return await RateRecallTool().execute(score=score, recall_id=recall_id, reason=reason)


rate_recall.__doc__ = _RATE_RECALL_DOC


class RateRecallTool(BaseJsonTool):
    name = "rate_recall"
    # GLOBAL, not OBJECT. `recall_id` is a telemetry receipt token, not a graph object -- this
    # writes a usefulness score to the sidecar and touches no tenant memory, so there is no
    # object to check ownership of and no namespace to inject.
    #
    # Its actual boundary is the receipt lookup, which was `WHERE token = ?` with no caller
    # check while the no-token branch of the same method scoped by session/client. That is
    # fixed at the store (`recall_store.record_recall_feedback`), where the scoping already
    # lived, rather than by giving this tool a namespace it has no use for.
    scope = ToolScope.GLOBAL
    required_tier = "agent"
    description = "Report how useful a prior recall result was (operational signal only)."
    title = "Rate Recall Usefulness"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    async def endpoint(
        self,
        score: RecallScore,
        recall_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        label = (score or "").strip().lower()
        if not is_valid_score(label):
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {
                        "message": (
                            f"Invalid score '{score}'. Use one of: "
                            f"{', '.join(SCORE_SCALE)}."
                        )
                    },
                }
            )

        session = get_request_session()
        session_id = session.session_id if session and session.session_id else ""
        client_id = session.client_id if session and session.client_id else ""

        from menhir.mcp.telemetry import telemetry_store

        rated = telemetry_store.record_recall_feedback(
            score_label=label,
            score_value=score_value(label),
            token=recall_id or None,
            session_id=session_id,
            client_id=client_id,
            # CF-165 closure: a receipt can cover a global/workspace recall and there is no
            # sound session->namespace mapping. Persist the structured score, not arbitrary prose.
            reason=None,
        )
        if rated is None:
            hint = (
                f"no recall found for recall_id '{recall_id}'"
                if recall_id
                else "no unrated recall found for this session"
            )
            return self.render_json(
                {"ok": False, "tool": self.operation, "error": {"message": hint}}
            )

        return self.render_json(
            {
                "ok": True,
                "tool": self.operation,
                "rated": {
                    "recall_id": rated["token"],
                    "operation": rated["operation"],
                    "score": label,
                    "score_value": score_value(label),
                },
            }
        )

    endpoint.__doc__ = _RATE_RECALL_DOC
