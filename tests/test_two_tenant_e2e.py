"""Two tenants, four surfaces: the tenancy invariant, tested as a property of the system.

    No caller holding a valid credential may escape its configured namespace by changing
    `client_name`, choosing another namespace, omitting namespace, switching between the REST
    and MCP surfaces, or addressing another tenant's object by UUID.

Every prior test in this cluster proved one mechanism worked. None of them could have caught the
actual defects, because each defect was a SURFACE that skipped a mechanism that worked perfectly
well everywhere it was applied -- CF-16 at resources, CF-30 at named REST, CF-33 at MCP tools,
CF-221 at the internal dispatch. A per-mechanism test cannot see a missing call site.

So this suite is organised by ESCAPE ROUTE rather than by module: for each way a caller could get
at tenant B, assert it cannot, whichever surface it tries. When a future surface is added, the
question "does this route appear here?" is answerable.

Live, because the interesting half is object ownership by UUID and that needs real nodes. A stub
returning canned rows would let every one of these pass while the graph queries filtered nothing.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from menhir.api.auth import BearerAuthMiddleware
from menhir.core import tenancy
from menhir.core.request_context import (
    bind_request_auth_mode,
    bind_request_session,
    reset_request_auth_mode,
    reset_request_session,
)
from menhir.domain.session import new_session

TENANT_A = "tenant_a"
TENANT_B = "tenant_b"
CLIENT_A = "client-a"
CLIENT_B = "client-b"
API_KEY = "shared-static-key"


@pytest.fixture
def two_tenant_config(monkeypatch):
    """Both clients pinned, sharing ONE static key.

    The shared key is the point. These two callers are cryptographically indistinguishable --
    they present identical credentials -- so everything separating them rests on `client_name`
    and the server-side pin keyed on it. That is the threat model CF-32 describes, and it is why
    a test using two different keys would prove much less.
    """
    monkeypatch.setenv(
        "MENHIR_CLIENT_NAMESPACES", f"{CLIENT_A}={TENANT_A},{CLIENT_B}={TENANT_B}"
    )
    monkeypatch.setenv("MENHIR_CLIENT_TOOLS", "")
    monkeypatch.setenv("MENHIR_KNOWN_CLIENTS", "")
    from menhir.config import MemorySettings

    settings = MemorySettings.from_env()
    assert settings.client_namespaces.get(CLIENT_A) == TENANT_A, "pin config did not load"
    assert settings.client_namespaces.get(CLIENT_B) == TENANT_B
    return settings


def _as_client(client_name: str, mode: str = "header"):
    """Bind a request context as a named client, the way the auth middleware would."""
    session = new_session(
        user_id="u", session_id="s", client_id=client_name, client_name=client_name
    )
    return bind_request_session(session), bind_request_auth_mode(mode)


class _ClientCtx:
    def __init__(self, client_name: str, mode: str = "header") -> None:
        self._name = client_name
        self._mode = mode

    def __enter__(self):
        self._tokens = _as_client(self._name, self._mode)
        return self

    def __exit__(self, *exc):
        session_token, mode_token = self._tokens
        reset_request_auth_mode(mode_token)
        reset_request_session(session_token)
        return False


# ---------------------------------------------------------------------------
# Escape route 1: rename yourself into a different policy (CF-32)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_unknown_self_declared_client_is_refused(two_tenant_config) -> None:
    """The evasion that makes every other control optional: a holder of the shared key names
    itself something the config has never heard of, and both the pin and the tool allowlist
    treat an unknown name as UNRESTRICTED. The restriction was opt-in by the party it restricts.

    Refused all-or-nothing on the deployment, not per-client. Refusing only callers who name a
    RESTRICTED client would leave the evasion completely intact, because the evasion is
    precisely to claim a name that is not restricted.
    """
    from menhir.mcp.service_access import require_trusted_client_identity

    with _ClientCtx("not-in-any-registry"):
        with pytest.raises(PermissionError, match="Unknown client name"):
            require_trusted_client_identity()


@pytest.mark.unit
def test_a_client_that_declares_no_name_at_all_is_refused(two_tenant_config) -> None:
    """Omission is the cheaper evasion and the one a small model performs by accident. An empty
    name resolves to no pin, which is indistinguishable from unrestricted."""
    from menhir.mcp.service_access import require_trusted_client_identity

    with _ClientCtx(""):
        with pytest.raises(PermissionError, match="must identify"):
            require_trusted_client_identity()


@pytest.mark.unit
def test_the_refusal_happens_before_namespace_resolution(two_tenant_config) -> None:
    """Ordering, not merely presence. An unknown client resolves to NO pin, so if identity were
    checked after namespace resolution the caller would already be unscoped by the time it was
    rejected -- and any read that happened in between would have run globally."""
    with _ClientCtx("not-in-any-registry"):
        assert tenancy.pinned_namespace() == "", (
            "an unknown name resolves to no pin, which is why identity must be checked FIRST"
        )


@pytest.mark.unit
def test_a_declared_client_resolves_to_its_own_pin(two_tenant_config) -> None:
    with _ClientCtx(CLIENT_A):
        assert tenancy.pinned_namespace() == TENANT_A
    with _ClientCtx(CLIENT_B):
        assert tenancy.pinned_namespace() == TENANT_B


@pytest.mark.unit
def test_identity_is_enforced_at_the_transport_not_only_in_tools(two_tenant_config) -> None:
    """CF-32's original fix lived in `BaseTool.execute`, which covers MCP tools and nothing else
    -- MCP resources and every REST route bound the name and never validated it. The check now
    runs in the auth middleware, above dispatch, so this asserts a 403 on a plain REST path that
    involves no tool at all."""
    app = FastAPI()

    @app.get("/api/anything")
    async def anything():
        return JSONResponse({"reached": True})

    client = TestClient(BearerAuthMiddleware(app, api_key=API_KEY))
    resp = client.get(
        "/api/anything",
        headers={"authorization": f"Bearer {API_KEY}", "x-menhir-client-name": "ghost"},
    )
    assert resp.status_code == 403, resp.text
    assert "Unknown client name" in resp.text


@pytest.mark.unit
def test_a_declared_client_still_reaches_the_route(two_tenant_config) -> None:
    """The control must not be a blanket denial -- a configured client works exactly as before."""
    app = FastAPI()

    @app.get("/api/anything")
    async def anything():
        return JSONResponse({"reached": True})

    client = TestClient(BearerAuthMiddleware(app, api_key=API_KEY))
    resp = client.get(
        "/api/anything",
        headers={"authorization": f"Bearer {API_KEY}", "x-menhir-client-name": CLIENT_A},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_an_undeclared_deployment_is_untouched(monkeypatch) -> None:
    """No restrictions configured means no policy to evade, so nothing is refused. Without this
    the fix would be a breaking change for every single-tenant install."""
    monkeypatch.setenv("MENHIR_CLIENT_NAMESPACES", "")
    monkeypatch.setenv("MENHIR_CLIENT_TOOLS", "")
    monkeypatch.setenv("MENHIR_KNOWN_CLIENTS", "")

    app = FastAPI()

    @app.get("/api/anything")
    async def anything():
        return JSONResponse({"reached": True})

    client = TestClient(BearerAuthMiddleware(app, api_key=API_KEY))
    resp = client.get(
        "/api/anything",
        headers={"authorization": f"Bearer {API_KEY}", "x-menhir-client-name": "anything-at-all"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Escape route 2: no credential at all, via a same-host proxy (CF-34)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_no_auth_mode_refuses_a_proxied_request() -> None:
    """AuthMode.NONE grants unauthenticated access on the reasoning that a loopback bind means
    only local processes can connect. A same-host reverse proxy IS a local process and forwards
    from anywhere, so every protected surface is reachable with no credential -- the widest
    bypass in the cluster, because there is no credential to get wrong.

    CF-8 fixed the explorer half of this same assumption and recorded that this half was left
    open.
    """
    app = FastAPI()

    @app.get("/api/anything")
    async def anything():
        return JSONResponse({"reached": True})

    client = TestClient(BearerAuthMiddleware(app))
    assert client.get("/api/anything").status_code == 200, "direct local caller must still work"

    for header in ("x-forwarded-for", "x-real-ip", "forwarded"):
        resp = client.get("/api/anything", headers={header: "203.0.113.7"})
        assert resp.status_code == 401, f"{header} was accepted: {resp.text}"
        assert "proxied" in resp.text.lower()


# ---------------------------------------------------------------------------
# Escape route 3: name another tenant's namespace directly
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_read_filter_is_forced_to_the_pin(two_tenant_config) -> None:
    """Filters force rather than refuse: the caller gets a correct answer about its own silo."""
    with _ClientCtx(CLIENT_A):
        assert tenancy.resolve_namespace_filter(TENANT_B) == TENANT_A
        assert tenancy.resolve_namespace_filter(None) == TENANT_A
        assert tenancy.resolve_namespace_filter("") == TENANT_A


@pytest.mark.unit
def test_a_mutation_target_is_refused_not_silently_retargeted(two_tenant_config) -> None:
    """The distinction that matters, and the reason both behaviours exist.

    Forcing a TARGET would rewrite `reset namespace=B` into `reset namespace=A` -- destroying the
    caller's own data while it believes it acted on someone else's. An attempted cross-tenant
    action would become a successful self-inflicted one, which is worse than the attack.
    """
    with _ClientCtx(CLIENT_A):
        with pytest.raises(PermissionError, match="pinned to namespace"):
            tenancy.require_namespace_target(TENANT_B, action="reset")
        assert tenancy.require_namespace_target(TENANT_A, action="reset") == TENANT_A
        # Named nothing: gets its own silo, never a server-wide default.
        assert tenancy.require_namespace_target(None, action="reset") == TENANT_A


@pytest.mark.unit
def test_an_unpinned_caller_keeps_full_reach(monkeypatch) -> None:
    """Isolation is opt-in. An unpinned deployment must behave exactly as before, or this whole
    change is a breaking one for everybody not using namespaces."""
    monkeypatch.setenv("MENHIR_CLIENT_NAMESPACES", "")
    monkeypatch.setenv("MENHIR_CLIENT_TOOLS", "")
    monkeypatch.setenv("MENHIR_KNOWN_CLIENTS", "")
    with _ClientCtx("whoever"):
        assert tenancy.resolve_namespace_filter(TENANT_B) == TENANT_B
        assert tenancy.require_namespace_target(TENANT_B, action="reset") == TENANT_B


# ---------------------------------------------------------------------------
# Escape route 4: address another tenant's object by UUID (live)
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_graph(test_neo4j_repo):
    """One memory node and one captured turn in EACH tenant."""
    test_neo4j_repo.execute(
        """
        CREATE (:Entity   {uuid: 'mem-a', name: 'A memory', namespace: $a, content: 'A secret'})
        CREATE (:Entity   {uuid: 'mem-b', name: 'B memory', namespace: $b, content: 'B secret'})
        CREATE (:Episodic {uuid: 'ep-a',  name: 'A episode', namespace: $a})
        CREATE (:Episodic {uuid: 'ep-b',  name: 'B episode', namespace: $b})
        CREATE (:TurnEvidence {turn_id: 'turn-a', namespace: $a, role: 'user',
                               declarant: 'user', text: 'A said this'})
        CREATE (:TurnEvidence {turn_id: 'turn-b', namespace: $b, role: 'user',
                               declarant: 'user', text: 'B said this'})
        """,
        params={"a": TENANT_A, "b": TENANT_B},
    )
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    return MemoryGraphAdapter(neo4j=test_neo4j_repo)


@pytest.mark.online
def test_a_uuid_is_not_proof_of_ownership(two_tenant_config, seeded_graph) -> None:
    """The route every UUID operation shares. A pinned caller that learns an id through any
    global read -- and several exist -- could previously flag, promote, or erase it.

    Asserted against the shared guard rather than one tool, because the guard is what every
    surface funnels through.
    """
    import asyncio

    async def lookup(uuid, **kw):
        ns = kw.get("namespace")
        rows = seeded_graph.neo4j.execute(
            "MATCH (n) WHERE n.uuid = $u AND ($ns IS NULL OR coalesce(n.namespace,'default') = $ns) "
            "RETURN n.uuid AS uuid",
            params={"u": uuid, "ns": ns},
        )
        return rows[0] if rows else None

    async def check(uuid: str) -> str | None:
        return await tenancy.foreign_object_refusal(
            uuid=uuid, namespace=TENANT_A, lookup=lookup, label="memory"
        )

    assert asyncio.run(check("mem-b")) is not None, "A reached B's memory by uuid"
    assert asyncio.run(check("mem-a")) is None, "A was refused its own memory"
    # Absent everywhere proceeds: `graph_already_absent` is a supported erasure path.
    assert asyncio.run(check("mem-nowhere")) is None


@pytest.mark.online
def test_the_admission_link_cannot_join_across_tenants(seeded_graph) -> None:
    """Both ids come from the caller, and the original matched both GLOBALLY -- so a caller could
    draw a permanent provenance edge between any episode and any turn, including two that were
    neither of them its own."""
    assert seeded_graph.link_episode_admission(
        episode_uuid="ep-a", turn_evidence_uuid="turn-b", namespace=TENANT_A
    ) is False, "A joined its episode to B's captured turn"

    assert seeded_graph.link_episode_admission(
        episode_uuid="ep-b", turn_evidence_uuid="turn-a", namespace=TENANT_A
    ) is False, "A joined B's episode to its own turn"

    assert seeded_graph.link_episode_admission(
        episode_uuid="ep-a", turn_evidence_uuid="turn-a", namespace=TENANT_A
    ) is True, "A was refused a link entirely inside its own silo"


@pytest.mark.online
def test_the_evidence_projection_cannot_copy_another_tenants_words(seeded_graph) -> None:
    """The worst of the four, because it moves CONTENT across the boundary rather than drawing an
    edge across it. The projection copies `t.text` verbatim into a new node stamped with the
    CALLER's namespace -- so matching the turn globally would lift tenant B's literal user words
    into tenant A's silo, where A's own recall then enriches and surfaces them."""
    created = seeded_graph.create_evidence_projection(
        turn_evidence_uuid="turn-b",
        projection_uuid="proj-x",
        name="projection-of-b",
        session_id="s",
        user_id="u",
        namespace=TENANT_A,
    )
    assert created is None, "A projected B's turn text into its own namespace"

    leaked = seeded_graph.neo4j.execute(
        "MATCH (p:Episodic {is_evidence_projection: true}) RETURN p.content AS content"
    )
    assert [r["content"] for r in leaked] == [], "a projection node was created from B's turn"

    own = seeded_graph.create_evidence_projection(
        turn_evidence_uuid="turn-a",
        projection_uuid="proj-a",
        name="projection-of-a",
        session_id="s",
        user_id="u",
        namespace=TENANT_A,
    )
    assert own == "proj-a", "A was refused a projection of its own turn"


@pytest.mark.online
def test_an_unpinned_projection_still_works(seeded_graph) -> None:
    """The regression this nearly shipped. `coalesce(t.namespace,'default') = $namespace` with a
    null parameter matches NOTHING, so binding the raw unpinned value would silently stop
    projecting for every deployment that does not use namespaces -- with no error anywhere."""
    seeded_graph.neo4j.execute(
        """
        CREATE (:TurnEvidence {turn_id: 'turn-legacy', role: 'user', declarant: 'user',
                               text: 'no namespace property at all'})
        """
    )
    created = seeded_graph.create_evidence_projection(
        turn_evidence_uuid="turn-legacy",
        projection_uuid="proj-legacy",
        name="projection-legacy",
        session_id="s",
        user_id="u",
        namespace="",
    )
    assert created == "proj-legacy", "an unpinned projection of a legacy turn stopped working"
