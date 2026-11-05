"""Helpers for the scalar and counting lanes: capture turns, run Phase 3, read Views back.

The scalar and counting pipelines do not read :Episodic memories. They read user
:TurnEvidence, written through ``POST /api/turn-evidence``, and they run when
``POST /api/phase3/run`` is called for a namespace. Both are the documented black-box
surfaces the benchmark harness drives, so the lanes drive them the same way.

Scripted replies: every extraction prompt lists the page's turns as
``[i] [YYYY-MM-DD] <text>``. :func:`envelope_reply` answers with one envelope per listed turn
and puts in it the observations whose phrase occurs in that turn, with the phrase as the
stated span. Every turn gets an envelope, even an empty one, because a missing envelope
marks the turn as not fully read.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.scripted_provider import Call
from tests.e2e._harness.stack import BackendProcess, graph_query, start_backend

__all__ = [
    "COUNTING_PROMPT",
    "DECIDE_PROMPT",
    "SCALAR_PROMPT",
    "Obs",
    "Retelling",
    "admit_turn",
    "authority_verdicts",
    "close_pass",
    "decide_reply",
    "envelope_reply",
    "lane_backend",
    "listed_turns",
    "post_json",
    "record_turn",
    "run_phase3",
    "scalar_views",
    "typed_assertions",
]

#: Unique substrings of the three prompts, from typed_scalar_rules.py, counting_consolidation.py
#: and domain/count_decide.py. If a prompt is reworded, its rule stops matching and the lane
#: fails on the unmatched call, which is the point.
SCALAR_PROMPT = "You convert a user's memory episodes into TYPED STATE OBSERVATIONS"
COUNTING_PROMPT = "record SINGLE HAPPENINGS"
DECIDE_PROMPT = "You check a user's counted happenings for retellings."

_TURN_LINE = re.compile(r"^\[(\d+)\] \[\d{4}-\d{2}-\d{2}\] (.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Obs:
    """One scripted observation: emitted for every listed turn whose text contains ``phrase``."""

    phrase: str
    fields: dict[str, Any]


def listed_turns(prompt: str) -> list[tuple[int, str]]:
    """The ``(index, text)`` turns an extraction prompt lists, in order."""
    return [(int(m.group(1)), m.group(2)) for m in _TURN_LINE.finditer(prompt)]


def envelope_reply(observations: list[Obs]) -> Callable[[Call], str]:
    def reply(call: Call) -> str:
        envelopes = []
        for index, text in listed_turns(call.prompt):
            hits = [obs for obs in observations if obs.phrase.lower() in text.lower()]
            # A phrase inside a longer phrase that also matched this turn is the same claim;
            # emit only the longer one ("Like I said, I finished X" vs "I finished X").
            hits = [obs for obs in hits
                    if not any(obs is not other and obs.phrase.lower() in other.phrase.lower()
                               for other in hits)]
            rows = [
                {"subject": "user", "scope": "", "unit": "", "when": "", **obs.fields,
                 "stated_span": obs.phrase}
                for obs in hits
            ]
            envelopes.append({"episode": index, "observations": rows})
        return json.dumps(envelopes)

    return reply


_DECIDE_LINE = re.compile(r'^(?:NEW|RELATED) ([NR]\d+): .*quote=(".*")$', re.MULTILINE)
_NEW_LINE = re.compile(r"^NEW (N\d+): ", re.MULTILINE)


@dataclass(frozen=True)
class Retelling:
    """Decide that the NEW happening quoting ``new`` is ``decision`` of the one quoting ``target``."""

    new: str
    target: str
    decision: str = "repeat"


def decide_reply(retellings: list[Retelling], *, family: str) -> Callable[[Call], str]:
    """Answer the count-decide prompt: each NEW handle is ``counted`` unless a :class:`Retelling`
    names it, in which case its target is the other handle whose quote contains ``target``."""

    def reply(call: Call) -> str:
        quotes = {handle: json.loads(quote) for handle, quote in _DECIDE_LINE.findall(call.prompt)}
        out = []
        for handle in _NEW_LINE.findall(call.prompt):
            quote = quotes.get(handle, "")
            item = {"new": handle, "decision": "counted", "target": "", "family": family}
            for rule in retellings:
                if rule.new.lower() not in quote.lower():
                    continue
                target = next((h for h, q in quotes.items()
                               if h != handle and rule.target.lower() in q.lower()
                               and rule.new.lower() not in q.lower()), "")
                if target:
                    item.update(decision=rule.decision, target=target)
                break
            out.append(item)
        return json.dumps(out)

    return reply


def post_json(config: E2EConfig, path: str, body: dict[str, Any], *, timeout: float = 300.0) -> dict:
    request = urllib.request.Request(
        f"{config.backend_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"POST {path} -> {exc.code}: {detail[:800]}") from exc


def record_turn(config: E2EConfig, namespace: str, text: str, *, occurred_at: str) -> str:
    """Capture one user turn as :TurnEvidence and return its turn id."""
    result = post_json(config, "/api/turn-evidence", {
        "text": text, "role": "user", "declarant": "user", "namespace": namespace,
        "occurred_at": occurred_at, "source_kind": "e2e",
    })
    assert result.get("created") is True, result
    return str(result["turn_id"])


async def admit_turn(client, namespace: str, text: str, turn_id: str) -> str:
    """Admit a memory on a captured turn, as an agent does after the hook records it.

    This is what links the turn to an :Episodic (ADMITTED_ON). A scalar View built only from
    a turn nobody admitted gets an empty provenance receipt and is never recall-eligible
    (typed_assertion_write_repository maps a turn-anchored assertion to its admitted
    episode), so the recall criteria need the admitted path.
    """
    result = await client.call_tool("add_memory", {
        "text": text, "namespace": namespace, "turn_evidence_uuid": turn_id})
    content = getattr(result, "content", None) or []
    body = "\n".join(getattr(item, "text", "") or "" for item in content)
    assert "episode_id" in body, body[:400]
    return body


def authority_verdicts(recall_text: str) -> list[dict]:
    """The scalar authority layer of a ``recall_memories`` JSON answer ([] when absent)."""
    try:
        return list(json.loads(recall_text).get("authority_layer") or [])
    except (ValueError, AttributeError):
        return []


def run_phase3(config: E2EConfig, evidence: LaneEvidence, namespace: str, *, k: int = 3,
               label: str = "phase3") -> dict:
    """One Phase 3 pass over ``namespace`` (the scalar and counting stages)."""
    result = post_json(config, "/api/phase3/run",
                       {"namespace": namespace, "k": k})
    evidence.attach(f"{label}.json", json.dumps(result, indent=2, default=str))
    return result


def scalar_views(config: E2EConfig, namespace: str, *, kind: str = "scalar_state") -> list[dict]:
    """Current scalar Views in ``namespace``, straight from the graph."""
    return graph_query(
        config,
        "MATCH (v:Entity {view_kind: $kind}) WHERE v.group_id = $ns "
        "AND coalesce(v.view_current, true) "
        "RETURN v.uuid AS uuid, v.ss_attribute AS attribute, v.ss_scope AS scope, "
        "       v.ss_kind AS value_kind, v.ss_value AS value, v.scalar_basis AS basis, "
        "       v.count_uncertain AS count_uncertain, v.view_subject_uuid AS subject_uuid, "
        "       v.name AS name "
        "ORDER BY attribute, scope",
        kind=kind, ns=namespace,
    )


def typed_assertions(config: E2EConfig, namespace: str) -> list[dict]:
    """Every :TypedAssertion in ``namespace``, with whether its head points at it."""
    return graph_query(
        config,
        "MATCH (a:TypedAssertion) WHERE a.namespace = $ns "
        "OPTIONAL MATCH (h:TypedAssertionHead)-[:CURRENT]->(a) "
        "RETURN a.assertion_id AS id, a.attribute AS attribute, a.operation AS operation, "
        "       a.value AS value, a.claim_ordinal AS claim_ordinal, a.count_status AS status, "
        "       a.count_target_id AS target, a.perceiver_version AS perceiver_version, "
        "       a.stated_span AS span, a.binding_pending AS binding_pending, "
        "       a.evidence_tier AS evidence_tier, "
        "       h IS NOT NULL AS current "
        "ORDER BY a.valid_at, a.stated_span",
        ns=namespace,
    )


@contextmanager
def lane_backend(config: E2EConfig, installed, evidence: LaneEvidence, env: dict[str, str],
                 *, name: str = "backend") -> Iterator[BackendProcess]:
    """A backend started with this lane's own flags, torn down with its log kept."""
    evidence.record_stack(**{f"{name}_env": {k: v for k, v in env.items() if k.startswith("MENHIR_")}})
    backend = start_backend(config, installed, log_path=evidence.directory / f"{name}.log",
                            feature_env=env, require_enrichment=True)
    try:
        yield backend
    finally:
        backend.terminate()


def close_pass(evidence: LaneEvidence, criteria: list[str]) -> None:
    """Close PASS only when every declared criterion was recorded and passed.

    These modules hold several lanes each, so the conftest's module-level CRITERIA check
    does not apply; this is the same check, per lane.
    """
    missing = sorted(set(criteria) - set(evidence.criteria))
    failed = sorted(name for name, item in evidence.criteria.items() if not item["passed"])
    if missing or failed:
        evidence.close(status="FAIL")
        raise AssertionError(f"criteria missing={missing} failed={failed}")
    evidence.close(status="PASS")
