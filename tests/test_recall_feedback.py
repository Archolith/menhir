"""Unit tests for recall usefulness feedback: scale, helper, and rate_recall tool."""

from unittest.mock import MagicMock, patch

import pytest

from menhir.mcp.feedback import SCORE_SCALE, is_valid_score, score_value


class TestScoreScale:
    def test_scale_values(self):
        assert SCORE_SCALE == {"useful": 1.0, "partial": 0.5, "noise": 0.0, "unused": None}

    def test_is_valid_score(self):
        assert is_valid_score("useful")
        assert is_valid_score("unused")
        assert not is_valid_score("great")
        assert not is_valid_score("")

    def test_score_value(self):
        assert score_value("useful") == 1.0
        assert score_value("noise") == 0.0
        assert score_value("unused") is None
        assert score_value("bogus") is None


class TestMintRecallReceipt:
    def test_returns_token_and_records(self):
        from menhir.mcp import feedback

        fake_session = MagicMock(session_id="s1", client_id="c1")
        fake_store = MagicMock()
        with patch("menhir.mcp.service_access.get_request_session", return_value=fake_session), \
             patch("menhir.mcp.telemetry.telemetry_store", fake_store):
            token = feedback.mint_recall_receipt("recall_memories")
        assert token and token.startswith("r")
        fake_store.record_recall_receipt.assert_called_once()
        kwargs = fake_store.record_recall_receipt.call_args.kwargs
        assert kwargs["operation"] == "recall_memories"
        assert kwargs["session_id"] == "s1"

    def test_swallows_errors_and_returns_none(self):
        from menhir.mcp import feedback

        with patch("menhir.mcp.service_access.get_request_session", side_effect=RuntimeError("boom")):
            assert feedback.mint_recall_receipt("recall_memories") is None

    def test_attach_sets_recall_id_on_payload(self):
        from menhir.mcp import feedback

        payload: dict = {"count": 1}
        with patch.object(feedback, "mint_recall_receipt", return_value="rABCD"):
            feedback.attach_recall_receipt("recall_memories", payload)
        assert payload["recall_id"] == "rABCD"

    def test_attach_noop_when_no_token(self):
        from menhir.mcp import feedback

        payload: dict = {"count": 1}
        with patch.object(feedback, "mint_recall_receipt", return_value=None):
            feedback.attach_recall_receipt("recall_memories", payload)
        assert "recall_id" not in payload

    def test_token_has_64_bits_of_entropy(self):
        from menhir.mcp import feedback

        token = feedback._mint_token()
        # "r" + 16 hex chars (8 bytes) — collision-safe across many receipts
        assert token.startswith("r") and len(token) == 17

    def test_footer_carries_token_or_empty(self):
        from menhir.mcp import feedback

        with patch.object(feedback, "mint_recall_receipt", return_value="rABCD"):
            footer = feedback.recall_receipt_footer("build_context")
        assert "recall_id: rABCD" in footer and "rate_recall" in footer

        with patch.object(feedback, "mint_recall_receipt", return_value=None):
            assert feedback.recall_receipt_footer("build_context") == ""


class TestRenderRecallJsonIntegration:
    def test_recall_id_stamped_into_json_body(self):
        """A recall tool's success render carries recall_id through the contract."""
        from menhir.mcp.contracts import BaseJsonTool

        class _FakeRecallTool(BaseJsonTool):
            name = "recall_memories"

            async def endpoint(self, **kwargs):  # pragma: no cover - not called here
                return self.render_recall_json({"count": 0, "items": []})

        fake_session = MagicMock(session_id="s1", client_id="c1")
        fake_store = MagicMock()
        with patch("menhir.mcp.service_access.get_request_session", return_value=fake_session), \
             patch("menhir.mcp.telemetry.telemetry_store", fake_store):
            out = _FakeRecallTool().render_recall_json({"count": 0, "items": []})
        assert '"recall_id":' in out
        fake_store.record_recall_receipt.assert_called_once()


class TestRateRecallTool:
    def test_endpoint_has_usage_docstring(self):
        # FastMCP exposes the *endpoint* docstring to the LLM; it must carry the
        # usage guidance (score values, recall_id fallback), not be empty.
        from menhir.mcp.tools.ops.rate_recall import RateRecallTool

        doc = RateRecallTool.endpoint.__doc__ or ""
        assert "useful" in doc and "recall_id" in doc and "never affects memory heat" in doc
        # behavioral rubric must be present so ratings stay consistent, not vibes-based
        assert "what you ACTUALLY USED" in doc
        assert all(s in doc for s in ("partial", "noise", "unused"))

    @pytest.mark.asyncio
    async def test_invalid_score_returns_error(self):
        from menhir.mcp.tools.ops.rate_recall import RateRecallTool

        out = await RateRecallTool().endpoint(score="amazing")
        assert '"ok":false' in out
        assert "Invalid score" in out

    @pytest.mark.asyncio
    async def test_valid_score_records_feedback(self):
        from menhir.mcp.tools.ops import rate_recall as mod

        fake_session = MagicMock(session_id="s1", client_id="c1")
        fake_store = MagicMock()
        fake_store.record_recall_feedback.return_value = {"token": "r1", "operation": "recall_memories"}
        with patch.object(mod, "get_request_session", return_value=fake_session), \
             patch("menhir.mcp.telemetry.telemetry_store", fake_store):
            out = await mod.RateRecallTool().endpoint(score="Useful", recall_id="r1")
        assert '"ok":true' in out
        assert "recall_memories" in out
        kwargs = fake_store.record_recall_feedback.call_args.kwargs
        assert kwargs["score_label"] == "useful"  # normalised lowercase
        assert kwargs["score_value"] == 1.0
        assert kwargs["token"] == "r1"

    @pytest.mark.asyncio
    async def test_no_matching_recall_returns_error(self):
        from menhir.mcp.tools.ops import rate_recall as mod

        fake_store = MagicMock()
        fake_store.record_recall_feedback.return_value = None
        with patch.object(mod, "get_request_session", return_value=MagicMock(session_id="", client_id="")), \
             patch("menhir.mcp.telemetry.telemetry_store", fake_store):
            out = await mod.RateRecallTool().endpoint(score="useful")
        assert '"ok":false' in out
        assert "no unrated recall" in out
