"""E2E-17 - scalar authority through MCP recall AND context (#170 item 6).

A View in the graph or a debug endpoint is not enough: this lane asks the questions an agent
asks, through the stdio ``recall_memories`` and ``build_context`` tools, and check the
structured verdict and the text the agent would read. It covers current, as-of and
history queries, stale-fact suppression, additive leads, provenance and the advisory fallback.
It then restarts the backend with the authority flag off to show recall falls back to ordinary
memories.

Foundation: user turns are captured over REST and admitted over stdio (user foundation). The
advisory control uses ``add_memory(user_statement=...)``, which records the words as an
agent_quoted turn. That turn feeds perception but never counts as user foundation.
"""

from __future__ import annotations

import json
import urllib.request
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.phase3 import (
    SCALAR_PROMPT,
    Obs,
    admit_turn,
    close_pass,
    envelope_reply,
    lane_backend,
    record_turn,
    run_phase3,
    scalar_views,
    typed_assertions,
)
from tests.e2e._harness.scripted_provider import Rule, Script, scripted_model

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(getattr(item, "text", "") or "" for item in content)


async def _ask(client, namespace: str, query: str, evidence: LaneEvidence, label: str
               ) -> tuple[dict, str]:
    """One question through both agent surfaces: (recall JSON, build_context text)."""
    recall_text = _text(await client.call_tool(
        "recall_memories", {"query": query, "namespace": namespace}))
    context = _text(await client.call_tool(
        "build_context", {"query": query, "namespace": namespace, "max_tokens": 4000}))
    evidence.attach(f"recall-{label}.json", recall_text)
    evidence.attach(f"context-{label}.txt", context)
    try:
        recall = json.loads(recall_text)
    except ValueError:
        recall = {"_unparsed": recall_text[:2000]}
    return recall, context


def _scalar(recall: dict, attribute: str, kind: str | None = None) -> list[dict]:
    """Verdicts for one slot. The harness embeddings are not semantic, so every slot surfaces for
    every query; filtering by slot (and kind) isolates the one asked about."""
    return [v for v in recall.get("authority_layer") or []
            if v.get("attribute") == attribute and (kind is None or v.get("kind") == kind)]


def _items_text(recall: dict) -> str:
    return json.dumps(recall.get("items") or [], default=str).lower()


def _sources_text(recall: dict) -> str:
    """The raw-episode section recall returns next to the ranked items."""
    return json.dumps(recall.get("source_memories") or [], default=str).lower()


# ---- scalar -------------------------------------------------------------------------------------

SCALAR_ON = {
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
    "MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED": "true",
}


def _count(phrase: str, attribute: str, value: float, operation: str = "absolute") -> Obs:
    return Obs(phrase, {"attribute": attribute, "value_kind": "count", "operation": operation,
                        "value": value})


SCALAR_OBS = [
    _count("I have 37 coins", "owned_coin_count", 37),
    _count("I bought 5 more coins", "owned_coin_count", 5, operation="delta"),
    _count("I have 3 cats", "cat_count", 3),
    _count("I no longer have my 3 cats", "cat_count", 3, operation="expire"),
    _count("I have 20 books", "owned_book_count", 20),
    _count("Actually, I have 22 books", "owned_book_count", 22),
    _count("I have 4 dogs", "dog_count", 4),
]

#: (text, occurred_at) per pass, all captured from the user and admitted.
SCALAR_PASSES = [
    [("I have 37 coins in my jar.", "2026-07-01T10:00:00+00:00"),
     ("I have 3 cats at home.", "2026-07-01T11:00:00+00:00"),
     ("I have 20 books on my shelf.", "2026-07-02T10:00:00+00:00")],
    [("Yesterday I bought 5 more coins at the fair.", "2026-07-03T10:00:00+00:00"),
     ("I no longer have my 3 cats.", "2026-07-12T10:00:00+00:00"),
     ("Actually, I have 22 books on my shelf.", "2026-07-08T10:00:00+00:00")],
]

#: Agent-quoted only: perceived, but with no user foundation.
DOG_STATEMENT = "I have 4 dogs in the yard."

SCALAR_CRITERIA = [
    "current_query_leads_with_the_folded_value",
    "context_renders_the_current_lead_and_its_quotes",
    "lead_is_additive_to_ranked_memories",
    "provenance_contributors_match_the_view",
    "as_of_queries_fold_at_the_asked_date",
    "history_query_surfaces_advisory_history",
    "expired_slot_reports_expired_not_a_current_value",
    "stale_value_is_not_served_as_current",
    "agent_quoted_value_stays_advisory",
    "authority_off_falls_back_to_ordinary_recall",
    "no_unscripted_model_prompt",
]


def _contributors(config: E2EConfig, view_uuid: str, namespace: str) -> dict:
    url = (f"{config.backend_url}/api/scalar-authority/{view_uuid}/contributors"
           f"?namespace={namespace}&limit=50")
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


async def test_e2e_17_scalar_authority(
    e2e_config: E2EConfig, e2e_installed, fresh_graph, feature_combo: FeatureCombo,
    feature_env: dict[str, str], lane_evidence: LaneEvidence,
) -> None:
    namespace = f"e2e17s-{uuid4().hex[:12]}"
    script = Script([Rule("scalar", SCALAR_PROMPT, envelope_reply(SCALAR_OBS))])
    with scripted_model(script) as model:
        env = {**feature_env, **model.env, **SCALAR_ON}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, env):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=env) as client:
                for number, turns in enumerate(SCALAR_PASSES, start=1):
                    for text, at in turns:
                        await admit_turn(client, namespace, text,
                                         record_turn(e2e_config, namespace, text, occurred_at=at))
                    if number == 1:
                        dog = _text(await client.call_tool("add_memory", {
                            "text": "The user told me how many dogs they have.",
                            "namespace": namespace, "user_statement": DOG_STATEMENT}))
                        assert "episode_id" in dog, dog[:400]
                    run_phase3(e2e_config, lane_evidence, namespace, label=f"phase3-pass{number}")
                views = scalar_views(e2e_config, namespace)
                lane_evidence.attach("views.json", json.dumps(views, indent=2, default=str))
                lane_evidence.attach("typed-assertions.json", json.dumps(
                    typed_assertions(e2e_config, namespace), indent=2, default=str))

                coins_now, coins_ctx = await _ask(
                    client, namespace, "How many coins do I have now?", lane_evidence, "coins-now")
                coins_early, _ = await _ask(
                    client, namespace, "How many coins did I have as of 2026-07-02?",
                    lane_evidence, "coins-asof-0702")
                coins_late, _ = await _ask(
                    client, namespace, "How many coins did I have as of 2026-07-05?",
                    lane_evidence, "coins-asof-0705")
                coins_hist, coins_hist_ctx = await _ask(
                    client, namespace, "How many coins did I have before?", lane_evidence,
                    "coins-history")
                cats_now, _ = await _ask(
                    client, namespace, "How many cats do I have now?", lane_evidence, "cats-now")
                books_now, books_ctx = await _ask(
                    client, namespace, "How many books do I have now?", lane_evidence, "books-now")
                dogs_now, dogs_ctx = await _ask(
                    client, namespace, "How many dogs do I have now?", lane_evidence, "dogs-now")
                coin_view = [v for v in views if v["attribute"] == "owned_coin_count"]
                provenance = (_contributors(e2e_config, coin_view[0]["uuid"], namespace)
                              if len(coin_view) == 1 else {})
                lane_evidence.attach("coin-contributors.json",
                                     json.dumps(provenance, indent=2, default=str))

        # Scalar authority is always on (#168 freeze) and follows the scalar lane, so the opt-out is the lane.
        off_env = {**env, "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "false"}
        with lane_backend(e2e_config, e2e_installed, lane_evidence, off_env, name="backend-off"):
            async with stdio_session(e2e_config, e2e_installed.venv_python, lane_evidence,
                                     feature_env=off_env) as client:
                coins_off, coins_off_ctx = await _ask(
                    client, namespace, "How many coins do I have now?", lane_evidence, "coins-off")

    lead = _scalar(coins_now, "owned_coin_count", "current")
    lane_evidence.record(
        "current_query_leads_with_the_folded_value",
        passed=len(lead) == 1 and lead[0].get("kind") == "current"
        and lead[0].get("status") == "leads" and str(lead[0].get("value")) == "42"
        and lead[0].get("has_foundation") is True and len(coin_view) == 1
        and lead[0].get("view_uuid") == coin_view[0]["uuid"],
        detail={"verdicts": lead, "views": coin_view},
    )
    lane_evidence.record(
        "context_renders_the_current_lead_and_its_quotes",
        passed="[Scalar authority: LEADS]" in coins_ctx and "owned_coin_count = 42" in coins_ctx
        and "I have 37 coins" in coins_ctx and "I bought 5 more coins" in coins_ctx,
        detail=coins_ctx[:3000],
    )
    lane_evidence.record(
        "lead_is_additive_to_ranked_memories",
        passed=bool(lead) and "coin count: 42" in _items_text(coins_now)
        and "i have 37 coins in my jar." in _sources_text(coins_now),
        detail={"items": coins_now.get("count")},
    )
    contributor_ids = sorted(str(c.get("assertion_id")) for c in provenance.get("contributors") or [])
    verdict_ids = sorted(str(c.get("assertion_id")) for c in (lead[0].get("contributors") if lead else []) or [])
    lane_evidence.record(
        "provenance_contributors_match_the_view",
        passed=len(contributor_ids) == 2 and contributor_ids == verdict_ids
        and provenance.get("total") == 2
        and {str(c.get("relation")) for c in (lead[0].get("contributors") if lead else [])}
        >= {"CURRENT_ANCHOR"},
        detail={"rest": provenance, "verdict": lead[0].get("contributors") if lead else None},
    )
    early = _scalar(coins_early, "owned_coin_count", "as_of")
    late = _scalar(coins_late, "owned_coin_count", "as_of")
    lane_evidence.record(
        "as_of_queries_fold_at_the_asked_date",
        passed=len(early) == 1 and str(early[0].get("value")) == "37"
        and early[0].get("view_uuid") is None
        and len(late) == 1 and str(late[0].get("value")) == "42"
        and not _scalar(coins_early, "owned_coin_count", "current"),
        detail={"0702": _scalar(coins_early, "owned_coin_count"),
                "0705": _scalar(coins_late, "owned_coin_count")},
    )
    # A history question gets the slot's history as an advisory verdict. The current View is still
    # shown, but advisory: today's 42 is not the answer to a question about the past.
    hist = _scalar(coins_hist, "owned_coin_count", "history")
    hist_current = _scalar(coins_hist, "owned_coin_count", "current")
    hist_entries = [str(c.get("stated_span")) for c in (hist[0].get("contributors") if hist else [])
                    if c.get("relation") == "HISTORY_ENTRY"]
    lane_evidence.record(
        "history_query_surfaces_advisory_history",
        passed=len(hist) == 1 and hist[0].get("status") == "advisory" and hist[0].get("value") is None
        and hist_entries == ["I have 37 coins", "I bought 5 more coins"]
        and "owned_coin_count history, 2 entries" in coins_hist_ctx
        and not _scalar(coins_now, "owned_coin_count", "history")
        and len(hist_current) == 1 and hist_current[0].get("status") == "advisory"
        and "[Scalar authority: ADVISORY] owned_coin_count = 42" in coins_hist_ctx
        and "[Scalar authority: LEADS] owned_coin_count" not in coins_hist_ctx,
        detail={"verdicts": _scalar(coins_hist, "owned_coin_count"),
                "context": coins_hist_ctx[:2000]},
    )
    cats = _scalar(cats_now, "cat_count", "expired")
    lane_evidence.record(
        "expired_slot_reports_expired_not_a_current_value",
        passed=len(cats) == 1 and str(cats[0].get("value")) == "3"
        and not _scalar(cats_now, "cat_count", "current")
        and not any(v["attribute"] == "cat_count" for v in views),
        detail=_scalar(cats_now, "cat_count"),
    )
    books = _scalar(books_now, "owned_book_count", "current")
    lane_evidence.record(
        "stale_value_is_not_served_as_current",
        passed=len(books) == 1 and books[0].get("kind") == "current"
        and books[0].get("status") == "leads" and str(books[0].get("value")) == "22"
        and "owned_book_count = 22" in books_ctx and "owned_book_count = 20" not in books_ctx,
        detail={"verdicts": books, "context": books_ctx[:2000]},
    )
    dogs = _scalar(dogs_now, "dog_count", "current")
    lane_evidence.record(
        "agent_quoted_value_stays_advisory",
        passed=len(dogs) == 1 and dogs[0].get("status") == "advisory"
        and dogs[0].get("has_foundation") is False and str(dogs[0].get("value")) == "4"
        and "[Scalar authority: ADVISORY]" in dogs_ctx,
        detail={"verdicts": dogs, "context": dogs_ctx[:2000]},
    )
    # History stays on in this backend, so its own advisory lane still answers; nothing leads.
    lane_evidence.record(
        "authority_off_falls_back_to_ordinary_recall",
        passed=not _scalar(coins_off, "owned_coin_count")
        and all(v.get("kind") == "history" and v.get("status") == "advisory"
                for v in coins_off.get("authority_layer") or [])
        and "[Scalar authority: LEADS]" not in coins_off_ctx
        and "i have 37 coins in my jar." in _sources_text(coins_off)
        and "I have 37 coins in my jar." in coins_off_ctx,
        detail={"keys": sorted(coins_off), "verdicts": coins_off.get("authority_layer"),
                "context": coins_off_ctx[:1500]},
    )
    lane_evidence.attach("model-calls.json", script.evidence())
    lane_evidence.record("no_unscripted_model_prompt", passed=not script.unmatched,
                         detail=script.unmatched[:5])
    close_pass(lane_evidence, SCALAR_CRITERIA)
