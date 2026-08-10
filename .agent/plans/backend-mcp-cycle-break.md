# Backend/MCP Cycle Break

## Why

- `menhir.core` currently imports MCP service-access and tool modules, creating a backend/MCP import cycle.
- Backend transport, request identity, and structural-ingest narration need neutral ownership so MCP remains an adapter.

## Scope

- Move backend auth resolution, request context, and the complete structural-project ingest workflow
  to neutral Menhir modules.
- Keep existing public MCP tool signatures, response text, and authorization behavior unchanged.
- Do not change graph schema, scalar/recall behavior, ingest policy, or the Archolith MCP framework.

## Proposed Design

- Put request-scoped caller identity, auth mode, and tier in a neutral core context module.
- Put backend client configuration helpers in a neutral core configuration module.
- Put structural-ingest validation, scan/write orchestration, narrative construction, and episode
  queueing in the application/service layer.
- Let `mcp.service_access` re-export or delegate compatibility names while MCP tools remain thin adapters.
- Enforce one-way dependency direction: MCP -> core/services; core must not import `menhir.mcp` or
  any Archolith/MCP framework package.

## Alternatives Considered

- Extend Archolith MCP Framework with Menhir concepts: rejected because identity, namespaces, and memory ingestion are product-specific.
- Leave lazy imports in core: rejected because they hide rather than remove the dependency cycle.

## Risks

- Existing tests or callers may monkeypatch old MCP-owned helper names.
- ContextVar identity must remain shared across HTTP middleware, MCP tools, and runtime providers.
- Structural-ingest narrative output must remain byte-for-byte compatible.

## Invariants

- Existing MCP/API contracts and auth-tier behavior remain unchanged.
- No core module imports any `menhir.mcp` module or MCP framework package, including type-only imports.
- The MCP `ingest_project` module contains only transport adaptation and result formatting.

## Validation

- Focused backend, MCP contract, auth, and structural-ingest tests.
- AST import-cycle check for the former backend/MCP strongly connected component.
- Full offline test suite if the focused suite passes.

## Docs To Update

- `.agent/architecture.md`
- `.agent/file-index.md` if it indexes the moved ownership
- `CHANGELOG.md`

## Result

- Backend auth, request context, and project narrative construction now have neutral ownership.
- The old MCP helper imports remain available as compatibility surfaces.
- Focused validation: 160 passed, 1 skipped.
- Full offline validation: 3,939 passed, 2 skipped, 2 pre-existing failures. The failures are an
  exact worktree-name assertion and a stale scalar contract expectation; neither failing file is
  changed by this refactor.

## Boundary Closeout

- Project-ingest validation, scan/write orchestration, narrative queueing, timeout/error handling,
  and structured outcomes now live in `services/project_ingest.py`; the MCP module delegates once
  and formats the returned outcome.
- Reader-id normalization is core-owned, lifecycle/runtime telemetry is infrastructure-owned, and
  the framework-only `FastMCP` annotation was replaced by a transport-neutral object contract.
- Architecture coverage now rejects all `core` imports of `menhir.mcp`, `mcp`, `fastmcp`,
  `cth_mcp_framework`, or `archolith_mcp_framework`.
- Closeout validation: 66 focused boundary/service/runtime tests passed with 5 skips; 108 additional
  MCP/API/backend tests passed; the full offline suite passed with 3,952 tests and 2 skips.
