"""CONSUMER SESSION E2E -- the acceptance gate.

**The question this answers.** The remediation programme proved individual safety properties one
at a time. Each was verified in isolation, against a scenario built to exercise it. None of them
asked the question a user actually cares about:

    Can Menhir survive an ordinary multi-turn working session end to end, produce useful memory,
    and still hold every invariant we repaired?

So this is deliberately NOT another CF-specific regression suite. It is one believable coding
session -- a developer working on a billing service -- driven through the real surfaces, against a
disposable graph and sidecar, using the real configured LLM. The assertions are far-end: what the
system can RECALL and what ended up in the stores, not whether a function returned 200.

**Why a story rather than sentinels.** `TEST_SENTINEL_123` proves plumbing. It cannot show that
extraction produces something useful, that a correction supersedes rather than duplicates, or that
recall leads with the current fact. The story carries a correction ($500 -> $750), a durable
preference, a structural relationship, and unrelated turns in between, because those are the
things that break in real use.

**Tenant B is adversarial background noise, not a separate security test.** B holds a CONFLICTING
threshold ($200) and its own `PaymentGateway` at identical file paths. Every A-side assertion
therefore doubles as a tenancy assertion, continuously, as part of ordinary consumer behaviour --
which is where isolation actually has to hold.

**Diagnostic, not merely pass/fail.** The run always prints a session report. A failure here should
tell you what the system DID, not just that an assertion tripped, so the report is built before
the assertions run and the graph evidence is dumped when one fails.

Run:
    pytest --run-online -m online tests/test_consumer_session_e2e.py -s

Requires: the disposable Neo4j on :7688 (`docker compose -f docker-compose.test.yml up -d`) and a
configured chat/embedding provider. It SKIPS rather than fails when either is absent -- an
acceptance gate that silently degrades to "no LLM" would be worse than one that does not run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.online]

# The neo4j driver emits a WARNING for every optional property a Graphiti query touches. At the
# volume this suite generates it buries the report, and none of it indicates a fault.
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

TURN_HOOK = "claude_code_hook"


# ---------------------------------------------------------------------------
# The session report
# ---------------------------------------------------------------------------


@dataclass
class SessionReport:
    """What the session DID. Built before any assertion runs, so a failure is diagnosable."""

    counters: dict[str, Any] = field(default_factory=dict)
    recall_checks: list[tuple[str, bool, str]] = field(default_factory=list)
    integrity: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.recall_checks.append((name, ok, detail))

    def render(self) -> str:
        out = ["", "=" * 66, "SESSION E2E", "=" * 66]
        for key, value in self.counters.items():
            out.append(f"  {key:<28}{value}")
        out.append("")
        out.append("Recall:")
        for name, ok, detail in self.recall_checks:
            out.append(f"  {name:<28}{'PASS' if ok else 'FAIL'}  {detail}")
        out.append("")
        out.append("Integrity:")
        for key, value in self.integrity.items():
            out.append(f"  {key:<28}{value}")
        out.append("=" * 66)
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_settings():
    """Real settings, with the graph pinned to the disposable instance.

    `.env` carries the provider credentials AND a PRODUCTION `NEO4J_URI`. `pytest_configure` has
    already pinned `NEO4J_URI` to the test instance, and python-dotenv defaults to
    `override=False`, so loading it here cannot undo that. The assertion below is not decoration:
    this module writes, enriches, and deletes, and a misconfiguration would do that to the
    operator's real memories.
    """
    import tempfile

    from dotenv import load_dotenv

    from menhir.config import MemorySettings

    load_dotenv(override=False)

    # The sidecar must be pinned BEFORE `from_env()`, because the built stack resolves its
    # telemetry/pending-action paths once, at construction. The autouse `isolated_telemetry_db`
    # fixture is function-scoped and cannot help: this fixture is module-scoped and runs first,
    # so without this the gate wrote its PendingActionStore rows into the operator's REAL
    # `.agent/mcp_telemetry.db`. Caught by reading the fixture repr in a failure dump.
    sidecar = Path(tempfile.mkdtemp(prefix="menhir-session-e2e-")) / "telemetry.db"
    os.environ["MENHIR_MCP_TELEMETRY_DB"] = str(sidecar)

    settings = MemorySettings.from_env()

    test_uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://localhost:7688")
    assert settings.neo4j_uri == test_uri, (
        f"REFUSING TO RUN: settings resolve to {settings.neo4j_uri}, not the disposable test "
        f"instance {test_uri}. This suite performs destructive writes."
    )

    if not (settings.openai_api_key or settings.gemini_api_key or settings.local_llm_base_url):
        pytest.skip("no chat provider configured; an acceptance gate must not run without an LLM")
    return settings


@pytest.fixture(scope="module")
def stack(live_settings):
    """The production composition root -- the same builder the server uses."""
    import importlib.util

    if importlib.util.find_spec("graphiti_core") is None:
        pytest.skip("graphiti_core is not installed")

    from menhir.core import build_memory_services
    from menhir.infrastructure.neo4j import Neo4jRepository

    probe = Neo4jRepository(
        uri=live_settings.neo4j_uri,
        database=live_settings.neo4j_database,
        user=live_settings.neo4j_user,
        password=live_settings.neo4j_password,
    )
    try:
        probe.execute("RETURN 1")
    except Exception as exc:  # noqa: BLE001
        probe.close()
        pytest.skip(f"disposable Neo4j unreachable ({type(exc).__name__}); start it first")
    finally:
        probe.close()

    # Built here, but NOT prepared here. `prepare_memory_runtime` opens async driver
    # connections, and anything opened on a different event loop than the test's raises
    # "attached to a different loop" the moment recall touches it. The test awaits it inside
    # its own loop instead.
    built = build_memory_services(live_settings)

    # Belt and braces on the sidecar pin above: assert the built stack did not resolve the
    # operator's real telemetry database. A gate that pollutes production telemetry is not a gate.
    # Scans the whole built object graph rather than one attribute: the store that was pointing
    # at the real sidecar is nested several layers down, and naming a single attribute would only
    # check the one place it happened to surface in a traceback.
    real_sidecar = str(Path.home() / "IdeaProjects" / ".agent" / "mcp_telemetry.db")
    assert real_sidecar.replace("\\", "/") not in repr(built).replace("\\", "/"), (
        f"REFUSING TO RUN: the built stack resolved the operator's real sidecar "
        f"({real_sidecar}). An acceptance gate must not write production telemetry."
    )

    yield built
    built.neo4j.execute("MATCH (n) DETACH DELETE n")


# ---------------------------------------------------------------------------
# The project on disk -- scanned by the production scanner, not hand-built
# ---------------------------------------------------------------------------


def _write_project(root: Path) -> None:
    """A small real Python project. Written to disk so the PRODUCTION scanner derives the
    imports/tests edges, rather than a hand-built ProjectScan asserting what we already believe."""
    root.mkdir(parents=True, exist_ok=True)
    # Not decoration: `_parse_imports` returns [] unless the scanner detects a python stack, and
    # stack detection keys off a marker file. Without this the project ingests with files and
    # symbols but ZERO import edges, and "what depends on PaymentGateway" answers nothing --
    # which is what the first run of this suite actually did.
    (root / "pyproject.toml").write_text(
        '[project]\nname = "billing"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / "payment_gateway.py").write_text(
        textwrap.dedent(
            '''
            """Card authorisation and capture."""


            class PaymentGateway:
                def charge(self, amount, card):
                    raise NotImplementedError
            '''
        ).strip(),
        encoding="utf-8",
    )
    (root / "checkout_service.py").write_text(
        textwrap.dedent(
            '''
            from payment_gateway import PaymentGateway


            class CheckoutService:
                def __init__(self):
                    self.gateway = PaymentGateway()
            '''
        ).strip(),
        encoding="utf-8",
    )
    (root / "refund_service.py").write_text(
        textwrap.dedent(
            '''
            APPROVAL_THRESHOLD = 500


            def needs_manager_approval(amount):
                return amount > APPROVAL_THRESHOLD
            '''
        ).strip(),
        encoding="utf-8",
    )
    (root / "test_checkout.py").write_text(
        textwrap.dedent(
            '''
            from payment_gateway import PaymentGateway


            def test_gateway_charges():
                assert PaymentGateway() is not None
            '''
        ).strip(),
        encoding="utf-8",
    )


def _ingest_project(stack, root: Path, project: str, session_id: str) -> dict[str, int]:
    from menhir.infrastructure.project_scanner import ProjectScanner

    scan = ProjectScanner().scan(str(root), project)
    return stack.graph_adapter.write_project_structure(scan, session_id, "dev")


# ---------------------------------------------------------------------------
# Real client behaviour
# ---------------------------------------------------------------------------


class Client:
    """Behaves like a real client: capture the turn, then write memory referencing it."""

    def __init__(self, stack, namespace: str, session_id: str) -> None:
        self.stack = stack
        self.namespace = namespace
        self.session_id = session_id
        self.turns: list[str] = []
        self.episodes: list[str] = []

    def capture_turn(self, text: str, role: str = "user") -> str:
        from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

        turn = TurnEvidenceRepository(self.stack.neo4j).record_turn_evidence(
            text=text,
            role=role,
            declarant=role,
            namespace=self.namespace,
            source_kind=TURN_HOOK,
            session_id=self.session_id,
            prompt_id=f"p-{uuid4().hex[:8]}",
        )["turn_id"]
        self.turns.append(turn)
        return turn

    async def remember(self, text: str, *, turn_id: str | None, source: str = "user") -> str:
        from menhir.domain.session import new_session

        result = await self.stack.ingest_service.queue_episode_for_enrichment(
            text,
            new_session(user_id="dev", session_id=self.session_id),
            source,
            namespace=self.namespace,
            turn_evidence_uuid=turn_id,
        )
        self.episodes.append(result.episode_id)
        return result.episode_id

    async def say_and_remember(self, text: str, **kw: Any) -> str:
        return await self.remember(text, turn_id=self.capture_turn(text), **kw)

    async def recall(self, query: str, **kw: Any):
        return await self.stack.recall_service.recall(
            query, namespace=self.namespace, include_session=True, **kw
        )

    async def await_enrichment(self, timeout_s: float = 240.0) -> dict[str, str]:
        """Wait for every queued episode to leave the pipeline. Reports terminal states."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        states: dict[str, str] = {}
        while asyncio.get_event_loop().time() < deadline:
            rows = self.stack.neo4j.execute(
                "MATCH (e:Episodic) WHERE e.uuid IN $ids "
                "RETURN e.uuid AS uuid, e.processing_state AS state",
                params={"ids": self.episodes},
            )
            states = {r["uuid"]: str(r["state"]) for r in rows}
            if states and all(s in ("READY", "FAILED") for s in states.values()):
                return states
            await asyncio.sleep(2.0)
        return states


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def _q(stack, cypher: str, **params: Any) -> list[dict[str, Any]]:
    return stack.neo4j.execute(cypher, params=params or None)


def _gather(
    stack, report: SessionReport, ns_a: str, ns_b: str, proj_a: str, client_episodes: list[str]
) -> None:
    # Scoped to the episodes THIS CLIENT wrote. A namespace-wide count also picks up the
    # :Episodic nodes Graphiti creates internally, which have no `processing_state` -- that is
    # what made an earlier run report "Episodes READY: 5/11" for a session that wrote 3.
    states = _q(
        stack,
        "MATCH (e:Episodic) WHERE e.uuid IN $ids "
        "RETURN coalesce(e.processing_state, 'NO_STATE') AS s, count(*) AS n",
        ids=client_episodes,
    )
    by_state = {str(r["s"]): int(r["n"]) for r in states}
    total = len(client_episodes)

    graphiti_internal = _q(
        stack,
        "MATCH (e:Episodic) WHERE e.namespace = $ns AND NOT e.uuid IN $ids RETURN count(e) AS n",
        ns=ns_a,
        ids=client_episodes,
    )[0]["n"]

    turns = _q(
        stack,
        "MATCH (t:TurnEvidence) WHERE t.namespace = $ns RETURN count(t) AS n", ns=ns_a
    )[0]["n"]
    admitted = _q(
        stack,
        "MATCH (e:Episodic)-[:ADMITTED_ON]->(t:TurnEvidence) "
        "WHERE e.uuid IN $ids RETURN count(*) AS n",
        ids=client_episodes,
    )[0]["n"]
    entities = _q(
        stack, "MATCH (n:Entity) WHERE n.namespace = $ns RETURN count(n) AS n", ns=ns_a
    )[0]["n"]
    rels = _q(
        stack,
        "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE a.namespace = $ns "
        "RETURN count(r) AS n",
        ns=ns_a,
    )[0]["n"]

    report.counters.update(
        {
            "Turns captured:": turns,
            "Memories written:": total,
            "Episodes READY:": f"{by_state.get('READY', 0)}/{total}",
            "Episodes FAILED:": by_state.get("FAILED", 0),
            "Graphiti sub-episodes:": graphiti_internal,
            "Admission coverage:": f"{admitted}/{total}",
            "Entities created:": entities,
            "Relationships created:": rels,
        }
    )

    # --- integrity -------------------------------------------------------
    # Structure nodes (file/symbol/project) are scoped by `structure_project`, not by namespace --
    # they describe a repository, not a tenant's memories. Counting them as "unnamespaced" made
    # this check fail on correct behaviour, so the query excludes them and the breakdown below
    # records what was excluded rather than hiding it.
    unnamespaced_rows = _q(
        stack,
        "MATCH (n) WHERE (n:Entity OR n:Episodic OR n:TurnEvidence) "
        "AND (n.namespace IS NULL OR trim(n.namespace) = '') "
        "RETURN coalesce(n.structure_role, 'memory') AS role, labels(n) AS labels, "
        "count(*) AS n ORDER BY n DESC",
    )
    report.evidence["unnamespaced_breakdown"] = unnamespaced_rows

    # `group_id` is the load-bearing isolation boundary; the `namespace` property is
    # defense-in-depth. Graphiti writes its own :Episodic nodes carrying group_id but not
    # namespace, so counting those as an isolation failure asserts something stricter than the
    # design. What actually must never exist is a memory object isolated by NEITHER.
    unisolated = _q(
        stack,
        "MATCH (n) WHERE (n:Entity OR n:Episodic OR n:TurnEvidence) "
        "AND n.structure_role IS NULL "
        "AND (n.namespace IS NULL OR trim(n.namespace) = '') "
        "AND (n.group_id IS NULL OR trim(n.group_id) = '') RETURN count(n) AS n",
    )[0]["n"]
    ungoverned_ns = sum(
        int(r["n"]) for r in unnamespaced_rows if str(r["role"]) == "memory"
    )
    report.evidence["episodics_without_namespace_but_group_scoped"] = ungoverned_ns
    cross_edges = _q(
        stack,
        "MATCH (a)-[r]->(b) WHERE a.namespace IN [$a, $b] AND b.namespace IN [$a, $b] "
        "AND a.namespace <> b.namespace RETURN count(r) AS n",
        a=ns_a,
        b=ns_b,
    )[0]["n"]
    orphan_admission = _q(
        stack,
        "MATCH (e:Episodic)-[:ADMITTED_ON]->(t:TurnEvidence) "
        "WHERE e.namespace <> t.namespace RETURN count(*) AS n",
    )[0]["n"]
    dupes = _q(
        stack,
        "MATCH (n:Entity) WHERE n.namespace = $ns WITH toLower(n.name) AS nm, count(*) AS c "
        "WHERE c > 1 RETURN count(*) AS n",
        ns=ns_a,
    )[0]["n"]
    foreign_structure = _q(
        stack,
        "MATCH (f:Entity {structure_project: $p})-[r]-(o:Entity) "
        "WHERE o.structure_project IS NOT NULL AND o.structure_project <> $p "
        "RETURN count(r) AS n",
        p=proj_a,
    )[0]["n"]

    report.integrity.update(
        {
            "duplicate entities": dupes,
            "unisolated objects": unisolated,
            "  (group-scoped only)": ungoverned_ns,
            "cross-tenant edges": cross_edges,
            "cross-tenant admission": orphan_admission,
            "cross-project structure": foreign_structure,
            "unexpected FAILED": by_state.get("FAILED", 0),
        }
    )
    report.evidence["episode_states"] = by_state


def _leaks(result: Any, forbidden: tuple[str, ...]) -> list[str]:
    """Any forbidden token appearing anywhere in a recall result."""
    blob = json.dumps(_jsonable(result), default=str).lower()
    return [tok for tok in forbidden if tok.lower() in blob]


def _jsonable(obj: Any, _depth: int = 0) -> Any:
    """Flatten a recall result to something dumpable.

    Depth-bounded on purpose: recall results carry nodes that reference their own neighbours, and
    an unbounded walk recurses forever. The leak scan only needs the TEXT, so falling back to
    `repr` past the cap loses nothing that matters and keeps every token visible to the scan.
    """
    if _depth > 6:
        return repr(obj)[:2000]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v, _depth + 1) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v, _depth + 1) for k, v in vars(obj).items()}
    return repr(obj)[:2000]


def _text_of(result: Any) -> str:
    return json.dumps(_jsonable(result), default=str)


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


@pytest.mark.online
@pytest.mark.asyncio
async def test_an_ordinary_working_session(stack, tmp_path) -> None:
    """One believable session, driven through real surfaces, asserted at the far end."""
    from menhir.core import prepare_memory_runtime

    await prepare_memory_runtime(stack)

    report = SessionReport()
    suffix = uuid4().hex[:8]
    ns_a, ns_b = f"acme-{suffix}", f"other-{suffix}"
    proj_a, proj_b = f"billing-{suffix}", f"rival-{suffix}"

    a = Client(stack, ns_a, f"sess-a-{suffix}")
    b = Client(stack, ns_b, f"sess-b-{suffix}")

    # --- Tenant B: adversarial background noise, written through the SAME real surfaces ----
    _write_project(tmp_path / "b")
    _ingest_project(stack, tmp_path / "b", proj_b, b.session_id)
    # Deliberately as substantial as tenant A's memories. A one-line "Refunds over $200 require
    # approval." reached READY but extracted ZERO entities, which made B invisible to recall --
    # and an invisible tenant cannot leak, so the isolation assertions passed for the wrong
    # reason. The positive control below is what exposed that.
    await b.say_and_remember(
        "The billing service uses Adyen for card processing. Refunds over $200 require "
        "approval from a team lead before they are issued."
    )

    # --- Tenant A: the session ------------------------------------------------------------
    _write_project(tmp_path / "a")
    _ingest_project(stack, tmp_path / "a", proj_a, a.session_id)

    # 1. the domain fact
    await a.say_and_remember(
        "The billing service uses Stripe. Refunds over $500 require manager approval."
    )
    # 2. the correction, two turns later
    a.capture_turn("Which file holds the refund logic?")
    a.capture_turn("refund_service.py, and it reads APPROVAL_THRESHOLD.", role="assistant")
    await a.say_and_remember(
        "Actually, the refund approval threshold is $750 now. We changed it last month."
    )
    # 3. the edit, recorded as a real tool event
    (tmp_path / "a" / "refund_service.py").write_text(
        "APPROVAL_THRESHOLD = 750\n\n\ndef needs_manager_approval(amount):\n"
        "    return amount > APPROVAL_THRESHOLD\n",
        encoding="utf-8",
    )
    stack.graph_adapter.record_file_event(
        path=str(tmp_path / "a" / "refund_service.py"),
        operation="edit",
        project=proj_a,
        source_client="claude-code",
    )
    # 4. the durable preference
    await a.say_and_remember(
        "For this project, don't automatically retry declined cards."
    )
    # 5. unrelated turns
    a.capture_turn("What's the CI runner for this repo?")
    a.capture_turn("GitHub Actions on ubuntu-latest.", role="assistant")

    # BOTH tenants, not just A. An earlier version awaited only A, so B's noise was often still
    # mid-enrichment at recall time -- which made "B never leaks into A" true for the wrong
    # reason and the positive control below unable to pass.
    states = await a.await_enrichment()
    await b.await_enrichment()
    assert states, "no episode reached a terminal state"

    _gather(stack, report, ns_a, ns_b, proj_a, a.episodes)

    # --- Far-end behaviour ------------------------------------------------------------
    FORBIDDEN = ("$200", "200 require", proj_b, ns_b)

    r_threshold = await a.recall("What was the refund approval threshold?")
    text = _text_of(r_threshold)
    leads_current = "750" in text
    report.check(
        "refund threshold",
        leads_current and "$200" not in text,
        f"$750 present={leads_current}, foreign $200 present={'$200' in text}",
    )

    r_pref = await a.recall("What did I say about retrying declined cards?")
    pref_text = _text_of(r_pref).lower()
    pref_ok = "declined" in pref_text and ("retry" in pref_text or "retrie" in pref_text)
    report.check("declined-card preference", pref_ok, f"matched={pref_ok}")

    ctx = stack.graph_adapter.query_structure(
        proj_a, "context", path="payment_gateway.py"
    )
    importers = set(ctx.get("imported_by") or [])
    # Both the consumer AND the test import PaymentGateway, so both are genuine blast radius.
    # `tested_by` is deliberately NOT asserted here: the TESTS edge is derived by name, so
    # test_checkout.py tests checkout_service.py -- asserting it on PaymentGateway would be
    # asserting a relationship the model does not claim to have.
    structural_ok = {"checkout_service.py", "test_checkout.py"} <= importers
    report.check(
        "PaymentGateway impact",
        structural_ok,
        f"importers={sorted(importers)} testers={ctx.get('tested_by')}",
    )

    r_hist = await a.recall(
        "refund approval threshold history", include_superseded=True
    )
    hist_text = _text_of(r_hist)
    hist_ok = "500" in hist_text
    report.check("historical $500 fact", hist_ok, f"$500 recoverable={hist_ok}")

    # POSITIVE CONTROL. Without this the leak scan is vacuous: "B never appears in A's results"
    # is trivially true if B's memory is unfindable by anyone, or if the query simply does not
    # match it. Proving B can retrieve its OWN $200 is what makes A's inability to see it mean
    # isolation rather than absence.
    #
    # This is not hypothetical. Removing `namespace=` from `Client.recall` entirely still
    # produced "Foreign-tenant leakage: 0" -- the first version of this suite would have passed
    # with tenant scoping switched off.
    r_b = await b.recall("What is the refund approval threshold?")
    b_text = _text_of(r_b)
    control_ok = "200" in b_text
    b_entities = [
        r["name"]
        for r in _q(
            stack,
            "MATCH (n:Entity) WHERE n.namespace = $ns RETURN n.name AS name",
            ns=ns_b,
        )
    ]
    b_states = _q(
        stack,
        "MATCH (e:Episodic) WHERE e.uuid IN $ids RETURN e.processing_state AS s",
        ids=b.episodes,
    )
    report.check(
        "control: B sees own $200",
        control_ok,
        f"found={control_ok} b_entities={b_entities} b_states={[r['s'] for r in b_states]}",
    )

    leaked = sorted(
        {tok for res in (r_threshold, r_pref, r_hist) for tok in _leaks(res, FORBIDDEN)}
        | set(_leaks(ctx, FORBIDDEN))
    )
    report.counters["Foreign-tenant leakage:"] = len(leaked)

    print(report.render())

    # --- Assertions ------------------------------------------------------------------
    failures: list[str] = []
    if leaked:
        failures.append(f"tenant B leaked into tenant A's session output: {leaked}")
    for key in ("cross-tenant edges", "cross-tenant admission", "cross-project structure"):
        if report.integrity[key]:
            failures.append(f"{key} = {report.integrity[key]} (must be 0)")
    if report.integrity["unisolated objects"]:
        failures.append(
            f"unisolated objects = {report.integrity['unisolated objects']} -- a memory object "
            "carrying neither a namespace nor a group_id belongs to no tenant (must be 0)"
        )
    failed_checks = [name for name, ok, _ in report.recall_checks if not ok]
    if failed_checks:
        failures.append(f"recall checks failed: {failed_checks}")

    if failures:
        dump = {
            "namespaces": {"a": ns_a, "b": ns_b},
            "episode_states": states,
            "entities_a": _q(
                stack,
                "MATCH (n:Entity) WHERE n.namespace = $ns RETURN n.name AS name",
                ns=ns_a,
            ),
            "report": report.counters | report.integrity,
            "unnamespaced_breakdown": report.evidence.get("unnamespaced_breakdown"),
        }
        pytest.fail(
            "\n".join(failures) + "\n\nEVIDENCE:\n" + json.dumps(dump, indent=2, default=str)
        )
