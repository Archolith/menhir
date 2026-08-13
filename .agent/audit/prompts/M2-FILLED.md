
# Menhir M2 - Compound Correctness, Security, Architecture, Performance, Maintainability, Test-Coverage, LLM/AI and Compliance Audit

**Repository:** https://github.com/Archolith/menhir
**Commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`
**Rule:** read-only. The only file you write is your report.

## Scope - 24 files, 5,565 lines

`src/menhir/api/`:

| File | Lines |
|---|---|
| `src/menhir/api/__init__.py` | 2 |
| `src/menhir/api/auth.py` | 676 |
| `src/menhir/api/auth_code_store.py` | 91 |
| `src/menhir/api/auth_mode.py` | 15 |
| `src/menhir/api/client_token_store.py` | 283 |
| `src/menhir/api/errors.py` | 61 |
| `src/menhir/api/jose_provider.py` | 110 |
| `src/menhir/api/mcp_remote.py` | 111 |
| `src/menhir/api/oauth.py` | 287 |
| `src/menhir/api/oauth_as_metadata.py` | 65 |
| `src/menhir/api/oauth_as_register.py` | 197 |
| `src/menhir/api/oauth_authorize.py` | 684 |
| `src/menhir/api/oauth_client_store.py` | 65 |
| `src/menhir/api/oauth_keys.py` | 80 |
| `src/menhir/api/oauth_metadata.py` | 77 |
| `src/menhir/api/oauth_preflight.py` | 287 |
| `src/menhir/api/oauth_rate_limit.py` | 145 |
| `src/menhir/api/oauth_token.py` | 109 |
| `src/menhir/api/request_context.py` | 71 |
| `src/menhir/api/routes.py` | 799 |
| `src/menhir/api/routes_handlers.py` | 312 |
| `src/menhir/api/routes_support.py` | 710 |
| `src/menhir/api/server.py` | 87 |
| `src/menhir/api/server_support.py` | 241 |
| **TOTAL** | **5565** |

Read every one. There is no depth-versus-breadth tradeoff to make: the scope is
already narrowed to one module. Reconcile the measured line total at the end.
Any file not read must be marked NOT READ - an unread file must never inherit a
"covered" row.

Supporting context (read, do not audit as scope): `src/menhir/config/oauth.py`, `src/menhir/config/settings.py`, `src/menhir/config/auth_mode.py`, `src/menhir/mcp/contracts.py`, `src/menhir/mcp/service_access.py`, `src/menhir/mcp/formatters.py`, `src/menhir/mcp/lifecycle.py`, `src/menhir/core/backend_impl.py`, `src/menhir/infrastructure/paths.py`

## Stack and deployment

Python 3.12. Starlette/FastAPI + FastMCP over uvicorn, ASGI. Neo4j via Graphiti 0.29.2 for the graph; SQLite sidecar stores for OAuth clients, authorization codes, and client tokens. asyncio throughout - the API layer is fully `async def`, and the SQLite stores are synchronous. Deployed as a long-lived server; `MENHIR_API_HOST` is `0.0.0.0` in the live `.env`, so treat every route as remotely reachable unless you trace a control that prevents it. Auth is a three-tier model (readonly / agent / operator) plus an OAuth 2.1 authorization-server implementation.

## Mechanical probe - run this first

The probe has already been run for you at this commit. Its **verbatim** output:

```
==============================================================================
AUDITPROBE -- mechanical structural checks
==============================================================================
  module    : src/menhir/api
  repo root : C:\Users\thron\IdeaProjects\projects\archolith\menhir
  package   : menhir  (10 subpackages)
  files     : 24
  plugin    : a2

  Structural checks only. Nothing here judges intent or correctness.
  Any report claim not traceable to this output must be labelled
  NOT MECHANICALLY VERIFIED.

==============================================================================
CORE 1. LINE RECONCILIATION
==============================================================================
  src/menhir/api/__init__.py                               2
  src/menhir/api/auth.py                                 676
  src/menhir/api/auth_code_store.py                       91
  src/menhir/api/auth_mode.py                             15
  src/menhir/api/client_token_store.py                   283
  src/menhir/api/errors.py                                61
  src/menhir/api/jose_provider.py                        110
  src/menhir/api/mcp_remote.py                           111
  src/menhir/api/oauth.py                                287
  src/menhir/api/oauth_as_metadata.py                     65
  src/menhir/api/oauth_as_register.py                    197
  src/menhir/api/oauth_authorize.py                      684
  src/menhir/api/oauth_client_store.py                    65
  src/menhir/api/oauth_keys.py                            80
  src/menhir/api/oauth_metadata.py                        77
  src/menhir/api/oauth_preflight.py                      287
  src/menhir/api/oauth_rate_limit.py                     145
  src/menhir/api/oauth_token.py                          109
  src/menhir/api/request_context.py                       71
  src/menhir/api/routes.py                               799
  src/menhir/api/routes_handlers.py                      312
  src/menhir/api/routes_support.py                       710
  src/menhir/api/server.py                                87
  src/menhir/api/server_support.py                       241
  ----------------------------------------------------------
  TOTAL: 5565 lines across 24 files

  Report this number verbatim. Do not restate a figure from the brief.

==============================================================================
CORE 2. DUPLICATE DEFINITIONS (body-compare, not signature)
==============================================================================

  [CROSS FILE] _settings_for defined in 2 files
      src/menhir/api/oauth_authorize.py              line 94    body=13108dbe751491b0
      src/menhir/api/oauth_token.py                  line 24    body=13108dbe751491b0
      -> bodies IDENTICAL (DRY candidate)

  [CROSS FILE] new_client_id defined in 2 files
      src/menhir/api/client_token_store.py           line 19    body=784c01fded0b58e4
      src/menhir/api/oauth_client_store.py           line 11    body=784c01fded0b58e4
      -> bodies IDENTICAL (DRY candidate)

  Duplicate name groups reported: 2

==============================================================================
CORE 3. MODULE CONSTANTS NEVER READ
==============================================================================
  none found

  Constants scanned: 36   never read: 0

==============================================================================
CORE 4. CROSS-MODULE PRIVATE-SYMBOL IMPORTS  [package: menhir]
==============================================================================
  src/menhir/api/auth_code_store.py           :58    CROSS-PACKAGE  _get_setting <- menhir.config.oauth
  src/menhir/api/mcp_remote.py                :11    CROSS-PACKAGE  _tier_allows <- menhir.mcp.contracts
  src/menhir/api/oauth.py                     :20    CROSS-PACKAGE  _as_bool <- menhir.config.oauth
  src/menhir/api/oauth.py                     :20    CROSS-PACKAGE  _as_tuple <- menhir.config.oauth
  src/menhir/api/oauth.py                     :20    CROSS-PACKAGE  _get_setting <- menhir.config.oauth
  src/menhir/api/oauth_as_metadata.py         :10    CROSS-PACKAGE  _as_bool <- menhir.config.oauth
  src/menhir/api/oauth_as_metadata.py         :10    CROSS-PACKAGE  _get_setting <- menhir.config.oauth
  src/menhir/api/oauth_as_register.py         :18    same-package   _as_enabled <- menhir.api.oauth_as_metadata
  src/menhir/api/oauth_as_register.py         :22    CROSS-PACKAGE  _get_setting <- menhir.config.oauth
  src/menhir/api/oauth_authorize.py           :37    same-package   _as_enabled <- menhir.api.oauth_as_metadata
  src/menhir/api/oauth_authorize.py           :41    CROSS-PACKAGE  _get_setting <- menhir.config.oauth
  src/menhir/api/oauth_client_store.py        :40    CROSS-PACKAGE  _get_setting <- menhir.config.oauth
  src/menhir/api/oauth_keys.py                :49    CROSS-PACKAGE  _get_setting <- menhir.config.oauth
  src/menhir/api/oauth_metadata.py            :9     same-package   _as_enabled <- menhir.api.oauth_as_metadata
  src/menhir/api/oauth_rate_limit.py          :21    CROSS-PACKAGE  _as_bool <- menhir.config.oauth
  src/menhir/api/oauth_rate_limit.py          :21    CROSS-PACKAGE  _as_tuple <- menhir.config.oauth
  src/menhir/api/oauth_rate_limit.py          :21    CROSS-PACKAGE  _get_setting <- menhir.config.oauth
  src/menhir/api/oauth_token.py               :10    same-package   _as_enabled <- menhir.api.oauth_as_metadata
  src/menhir/api/routes.py                    :13    CROSS-PACKAGE  _drain_background_errors <- menhir.core.backend_impl
  src/menhir/api/routes.py                    :207   CROSS-PACKAGE  _normalize_reader_id <- menhir.mcp.formatters
  src/menhir/api/routes.py                    :208   CROSS-PACKAGE  _remember_flagged_bootstrap_read <- menhir.mcp.lifecycle
  src/menhir/api/routes.py                    :232   CROSS-PACKAGE  _compact_scored_item <- menhir.mcp.formatters
  src/menhir/api/routes.py                    :232   CROSS-PACKAGE  _normalize_reader_id <- menhir.mcp.formatters
  src/menhir/api/routes.py                    :233   CROSS-PACKAGE  _has_recent_flagged_bootstrap_read <- menhir.mcp.lifecycle

  Private imports: 24 total, 20 cross-package

==============================================================================
CORE 5. LAYERING EDGES  [package: menhir]
==============================================================================
  layers detected: api, cli, config, core, domain, explorer, infrastructure, mcp, pipeline, services

  src/menhir/api/auth.py                      :14    api -> config           menhir.config.auth_mode
  src/menhir/api/auth.py                      :15    api -> config           menhir.config.oauth
  src/menhir/api/auth.py                      :16    api -> config           menhir.config.settings
  src/menhir/api/auth.py                      :17    api -> mcp              menhir.mcp.service_access
  src/menhir/api/auth_code_store.py           :41    api -> infrastructure   menhir.infrastructure.paths
  src/menhir/api/auth_code_store.py           :58    api -> config           menhir.config.oauth
  src/menhir/api/auth_mode.py                 :3     api -> config           menhir.config.auth_mode
  src/menhir/api/client_token_store.py        :244   api -> infrastructure   menhir.infrastructure.paths
  src/menhir/api/client_token_store.py        :262   api -> config           menhir.config.settings
  src/menhir/api/client_token_store.py        :280   api -> infrastructure   menhir.infrastructure.paths
  src/menhir/api/mcp_remote.py                :11    api -> mcp              menhir.mcp.contracts
  src/menhir/api/mcp_remote.py                :12    api -> mcp              menhir.mcp.resources
  src/menhir/api/mcp_remote.py                :13    api -> mcp              menhir.mcp.service_access
  src/menhir/api/mcp_remote.py                :14    api -> mcp              menhir.mcp.tools
  src/menhir/api/mcp_remote.py                :22    api -> mcp              menhir.mcp.tools
  src/menhir/api/oauth.py                     :20    api -> config           menhir.config.oauth
  src/menhir/api/oauth_as_metadata.py         :9     api -> config           menhir.config
  src/menhir/api/oauth_as_metadata.py         :10    api -> config           menhir.config.oauth
  src/menhir/api/oauth_as_register.py         :21    api -> config           menhir.config
  src/menhir/api/oauth_as_register.py         :22    api -> config           menhir.config.oauth
  src/menhir/api/oauth_as_register.py         :23    api -> config           menhir.config.settings
  src/menhir/api/oauth_authorize.py           :40    api -> config           menhir.config
  src/menhir/api/oauth_authorize.py           :41    api -> config           menhir.config.oauth
  src/menhir/api/oauth_authorize.py           :120   api -> infrastructure   menhir.infrastructure.paths
  src/menhir/api/oauth_client_store.py        :23    api -> infrastructure   menhir.infrastructure.paths
  src/menhir/api/oauth_client_store.py        :40    api -> config           menhir.config.oauth
  src/menhir/api/oauth_keys.py                :33    api -> infrastructure   menhir.infrastructure.paths
  src/menhir/api/oauth_keys.py                :49    api -> config           menhir.config.oauth
  src/menhir/api/oauth_metadata.py            :11    api -> config           menhir.config
  src/menhir/api/oauth_preflight.py           :8     api -> config           menhir.config.oauth
  src/menhir/api/oauth_preflight.py           :9     api -> config           menhir.config.settings
  src/menhir/api/oauth_rate_limit.py          :20    api -> config           menhir.config
  src/menhir/api/oauth_rate_limit.py          :21    api -> config           menhir.config.oauth
  src/menhir/api/oauth_token.py               :16    api -> config           menhir.config
  src/menhir/api/routes.py                    :13    api -> core             menhir.core.backend_impl
  src/menhir/api/routes.py                    :14    api -> domain           menhir.domain.bootstrap_scope
  src/menhir/api/routes.py                    :15    api -> domain           menhir.domain.recall
  src/menhir/api/routes.py                    :16    api -> domain           menhir.domain.session
  src/menhir/api/routes.py                    :17    api -> domain           menhir.domain.structural_memory
  src/menhir/api/routes.py                    :18    api -> mcp              menhir.mcp.service_access
  src/menhir/api/routes.py                    :190   api -> domain           menhir.domain.namespace
  src/menhir/api/routes.py                    :207   api -> mcp              menhir.mcp.formatters
  src/menhir/api/routes.py                    :208   api -> mcp              menhir.mcp.lifecycle
  src/menhir/api/routes.py                    :232   api -> mcp              menhir.mcp.formatters
  src/menhir/api/routes.py                    :233   api -> mcp              menhir.mcp.lifecycle
  src/menhir/api/routes_handlers.py           :12    api -> domain           menhir.domain.recall
  src/menhir/api/routes_handlers.py           :40    api -> infrastructure   menhir.infrastructure.sync_llm
  src/menhir/api/routes_handlers.py           :41    api -> infrastructure   menhir.infrastructure.view_embedder
  src/menhir/api/routes_handlers.py           :42    api -> services         menhir.services.scheduler_tasks
  src/menhir/api/routes_support.py            :11    api -> core             menhir.core.backend_impl
  src/menhir/api/routes_support.py            :12    api -> core             menhir.core.backend_protocol
  src/menhir/api/routes_support.py            :13    api -> core             menhir.core.runtime
  src/menhir/api/routes_support.py            :14    api -> domain           menhir.domain.session
  src/menhir/api/routes_support.py            :15    api -> infrastructure   menhir.infrastructure.telemetry
  src/menhir/api/routes_support.py            :16    api -> mcp              menhir.mcp.service_access
  src/menhir/api/server.py                    :13    api -> config           menhir.config
  src/menhir/api/server.py                    :14    api -> infrastructure   menhir.infrastructure.logging_config
  src/menhir/api/server_support.py            :29    api -> config           menhir.config
  src/menhir/api/server_support.py            :30    api -> config           menhir.config.settings
  src/menhir/api/server_support.py            :31    api -> core             menhir.core.runtime
  src/menhir/api/server_support.py            :32    api -> explorer         menhir.explorer.integration
  src/menhir/api/server_support.py            :33    api -> infrastructure   menhir.infrastructure.memory_graph_adapter
  src/menhir/api/server_support.py            :34    api -> services         menhir.services.candidate_service
  src/menhir/api/server_support.py            :35    api -> services         menhir.services.lifecycle_service

  Edge summary:
    api          -> config           25
    api          -> mcp              12
    api          -> infrastructure   11
    api          -> domain           7
    api          -> core             5
    api          -> services         3
    api          -> explorer         1

  Total cross-layer import statements: 64
  NOTE: an edge is not automatically a violation. Judge direction against
        the project's intended layering; report the judgement separately.

==============================================================================
CORE 6. TOP-LEVEL SYMBOLS WITH NO REFERENCE OUTSIDE THEIR DEFINITION
==============================================================================
  src/menhir/api/oauth_as_register.py:67  register_client
  src/menhir/api/oauth_authorize.py:524  authorize_get
  src/menhir/api/oauth_authorize.py:589  authorize_post
  src/menhir/api/routes.py:176  scalar_authority_contributors
  src/menhir/api/routes.py:200  bootstrap_flagged
  src/menhir/api/routes.py:228  bootstrap_context
  src/menhir/api/routes.py:307  ingest_memory
  src/menhir/api/routes.py:448  record_tool_event
  src/menhir/api/routes.py:522  tool_events_dirty
  src/menhir/api/routes.py:536  tool_events_stale
  src/menhir/api/routes.py:685  phase3_run
  src/menhir/api/routes.py:698  phase3_status
  src/menhir/api/routes.py:711  phase3_views

  Top-level symbols: 202   unreferenced: 13
  NOTE: decorator-dispatched handlers (FastAPI routes, MCP tools) appear here
        and are NOT dead. Confirm the dispatch mechanism before reporting.

==============================================================================
CORE 7. CONTROL-ASSERTING COMMENTS (candidates for verification)
==============================================================================
  src/menhir/api/auth.py:36  # it always carries provenance.
  src/menhir/api/auth.py:39  # mint route keys the atomic empty-store guard (CT-003) on this value.
  src/menhir/api/auth.py:168  # object-driven and settings-driven callers can never disagree.
  src/menhir/api/auth.py:179  """Constant-time token comparison (tracker Q5 ? no timing side-channel)."""
  src/menhir/api/auth.py:267  # identity). Gated on trust_identity_headers so it never fires on the
  src/menhir/api/auth.py:329  # browsers cannot attach a bearer token to ordinary navigation. Forwarded
  src/menhir/api/auth.py:330  # requests are excluded so a same-host reverse proxy cannot turn remote clients
  src/menhir/api/auth.py:343  # CORS preflight: browsers send OPTIONS with an Origin header and never
  src/menhir/api/auth.py:368  # identity for provenance/telemetry. Safe because startup guarantees a
  src/menhir/api/auth.py:440  # always carries an identity.
  src/menhir/api/auth.py:444  # list, revoke) always requires a real operator credential.
  src/menhir/api/auth.py:449  # proxy cannot mint an operator token during the empty-store window
  src/menhir/api/auth.py:535  # caller cannot relabel itself (tamper-proof).
  src/menhir/api/client_token_store.py:110  """Atomically mint a token ONLY while the store has no active token.
  src/menhir/api/jose_provider.py:21  # Callers MUST NOT introspect these objects; treat them as opaque handles.
  src/menhir/api/jose_provider.py:47  """True if *kid* is present in the cached key set; never raises."""
  src/menhir/api/mcp_remote.py:56  # invocation gate skips too, so the catalog must not be filtered either.
  src/menhir/api/oauth.py:208  # A malformed / expired / wrong-audience token must NOT trigger a
  src/menhir/api/oauth_as_register.py:38  # so an attacker cannot grow the client table without bound or amass attacker-controlled
  src/menhir/api/oauth_as_register.py:41  # Never-exchanged clients older than this are reaped before the cap is enforced,
  src/menhir/api/oauth_as_register.py:42  # so an attacker cannot permanently brick DCR by filling the table with
  src/menhir/api/oauth_as_register.py:43  # registrations that never complete a token exchange (AS-002). Default 24h.
  src/menhir/api/oauth_as_register.py:80  # Opportunistically reap never-exchanged stale registrations so a slow
  src/menhir/api/oauth_authorize.py:9  * unknown ``client_id`` / bad ``redirect_uri`` never redirect (open-redirect / code
  src/menhir/api/oauth_authorize.py:13  * consent requires the operator secret (constant-time); an unconfigured operator key
  src/menhir/api/oauth_authorize.py:77  # AS-004: throttle failed/approve POSTs per IP so a single consent token cannot be used to
  src/menhir/api/oauth_authorize.py:81  # AS-004: a consent token is single-use. Each token carries a random ``jti``; once an
  src/menhir/api/oauth_authorize.py:83  # same token is rejected. Guarded by a lock and pruned on access so it cannot grow without
  src/menhir/api/oauth_authorize.py:163  """Return ``b64(payload).b64(hmac)`` binding *fields* + issue time + a single-use
  src/menhir/api/oauth_authorize.py:235  """Atomically record *jti* as spent. Return True if it was fresh (redeem allowed),
  src/menhir/api/oauth_authorize.py:272  """Direct 400 (untrusted target ? never redirect)."""
  src/menhir/api/oauth_authorize.py:489  """Issue a single-use code and 302 back to *redirect_uri* (shared by one-click GET
  src/menhir/api/oauth_authorize.py:534  # Untrusted-target validation FIRST ? never redirect on these.
  src/menhir/api/oauth_authorize.py:552  # directly. Validation above always runs first, so a stale cookie cannot bypass the
  src/menhir/api/oauth_authorize.py:559  # consent page, so a CSRF'd GET cannot silently mint a code.
  src/menhir/api/oauth_authorize.py:607  # 1b. Single-use (AS-004): burn the consent token's jti now, before evaluating the
  src/menhir/api/oauth_authorize.py:636  # secret is ever evaluated, so an attacker cannot rapidly guess the admin secret.
  src/menhir/api/oauth_authorize.py:649  # 5. Admin gate: an unconfigured operator key can never approve.
  src/menhir/api/oauth_authorize.py:665  # 6. Approve: issue a single-use code bound to the admin subject, and remember the
  src/menhir/api/routes.py:486  # a reconciliation refusal cannot roll back or suppress stale detection --
  src/menhir/api/routes_support.py:156  # until the scheduler promoted them -- which never happens under MENHIR_BENCHMARK_MODE.
  src/menhir/api/routes_support.py:159  # history/provenance. Default false so they never compete with current state; set true for
  src/menhir/api/routes_support.py:194  # belief-time metadata and must never substitute for a missing ``valid_at``.
  src/menhir/api/routes_support.py:417  # marking and cannot fail it: a path outside the corpus, an ambiguous move,
  src/menhir/api/routes_support.py:658  # document is -- reversible, and never a lifecycle or relationship change,

  Control-asserting comments: 45
  Each is a CLAIM TO VERIFY, not documentation. This codebase has nine
  confirmed cases where such a comment described a control the code lacks.

==============================================================================
A2-1. ROUTE / TIER COVERAGE
==============================================================================
  src/menhir/api/oauth_as_metadata.py     :43    /.well-known/oauth-authorization-server/{_as_path:path} tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/oauth_as_register.py     :67    /oauth/register                    tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/oauth_authorize.py       :524   /oauth/authorize                   tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/oauth_authorize.py       :589   /oauth/authorize                   tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/oauth_metadata.py        :17    /.well-known/jwks.json             tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/oauth_metadata.py        :35    /.well-known/oauth-protected-resource/{_resource_path:path} tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/oauth_token.py           :51    /oauth/token                       tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :89    /health                            tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :103   /ready                             tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :123   /recall                            tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :176   /scalar-authority/{view_uuid}/contributors tier=readonly 
  src/menhir/api/routes.py                :200   /bootstrap/flagged                 tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :228   /bootstrap/context                 tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :284   /context                           tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :307   /memory                            tier=agent    
  src/menhir/api/routes.py                :350   /turn-evidence                     tier=agent    
  src/menhir/api/routes.py                :396   /episode-admission                 tier=agent    
  src/menhir/api/routes.py                :448   /tool-events                       tier=agent    
  src/menhir/api/routes.py                :522   /tool-events/dirty                 tier=readonly 
  src/menhir/api/routes.py                :536   /tool-events/stale                 tier=readonly 
  src/menhir/api/routes.py                :549   /tool-events/stale-verifications   tier=agent    
  src/menhir/api/routes.py                :578   /tool-events/stale-verifications   tier=readonly 
  src/menhir/api/routes.py                :597   /memory/{uuid}                     tier=operator 
  src/menhir/api/routes.py                :606   /namespace/{namespace}             tier=operator 
  src/menhir/api/routes.py                :633   /memory/{uuid}/flag                tier=agent    
  src/menhir/api/routes.py                :652   /memory/{uuid}/unflag              tier=agent    
  src/menhir/api/routes.py                :660   /stats                             tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :685   /phase3/run                        tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :698   /phase3/status                     tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :711   /views                             tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :728   /phase3/reset                      tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :746   backend_invoke                     tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :770   /admin/clients                     tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :783   /admin/clients                     tier=NONE       <- NO TIER CHECK IN BODY
  src/menhir/api/routes.py                :792   /admin/clients/{client_id}/revoke  tier=NONE       <- NO TIER CHECK IN BODY

  Routes: 35   without an inline _require_tier: 22
  NOTE: a route may be gated by middleware or a helper instead. Trace the
        ASGI chain before calling any of these a bypass.

==============================================================================
A2-2. REQUEST DATA REACHING LOGS UNREDACTED
==============================================================================
  src/menhir/api/routes_handlers.py             :226   logs body       redaction-in-file=False  <- FILE HAS NO redact() CALL

  Log calls passing request-shaped data: 1

==============================================================================
A2-3. SYNCHRONOUS I/O ON AN ASYNC PATH (direct and one hop)
==============================================================================
  Direct blocking calls inside async def:
    none

  One hop: async caller -> sync helper that performs blocking I/O:
    src/menhir/api/auth.py            :516   async _call_with_client_token    -> resolve()  <- CALLER FILE NEVER USES to_thread
        helper src/menhir/api/client_token_store.py:146 performs sqlite3.connect
    src/menhir/api/auth.py            :461   async _call_with_client_token    -> resolve()  <- CALLER FILE NEVER USES to_thread
        helper src/menhir/api/client_token_store.py:146 performs sqlite3.connect
    src/menhir/api/auth.py            :468   async _call_with_client_token    -> has_active()  <- CALLER FILE NEVER USES to_thread
        helper src/menhir/api/client_token_store.py:202 performs sqlite3.connect
    src/menhir/api/routes_handlers.py :309   async revoke_client_impl         -> revoke()
        helper src/menhir/api/client_token_store.py:166 performs sqlite3.connect
    src/menhir/api/routes_handlers.py :260   async mint_client_impl           -> mint_bootstrap()
        helper src/menhir/api/client_token_store.py:109 performs sqlite3.connect
    src/menhir/api/routes_handlers.py :268   async mint_client_impl           -> mint()
        helper src/menhir/api/client_token_store.py:80 performs sqlite3.connect
    src/menhir/api/routes_handlers.py :293   async list_clients_impl          -> all()
        helper src/menhir/api/client_token_store.py:215 performs sqlite3.connect

  Blocking helpers defined in this module: 8
  Direct: 0   one-hop: 7
  NOTE: hop detection is intra-module and name-based. A helper imported
        from another package is NOT resolved here.

==============================================================================
END OF PROBE OUTPUT
==============================================================================
```

Quote this output in your Bug-Class Sweep section. Do not summarise
its numbers; quote them. Where your narrative and the probe disagree, **the
probe wins**, and you report the disagreement.

Every report section must map to a probe check or be labelled
**NOT MECHANICALLY VERIFIED**. That label is not a failure - many real findings
are judgement calls - it tells the verifier which claims to re-derive.

## Run all eight audits over this scope

**A1 Functional correctness.** Logic errors, inverted conditions, off-by-one,
type coercion, API contract violations (parameter order, arity, return-type
assumptions). Boundary cases: empty, None, zero, negative, oversized, malformed.
Concurrency: shared mutable state, TOCTOU, missing `await`, fire-and-forget
where the result matters, cancellation handling.

**A2 Security.** Authorization completeness - find anything reachable without
the privilege it should require, and trace the actual call chain rather than
trusting decorators. Credential handling, comparison timing, storage, expiry,
revocation. Injection reaching a query language, subprocess, or filesystem.
Trust derived from client-controllable input. Error and info disclosure.

**A3 Architecture.** Layering violations, blast radius of shared plumbing,
failure-mode analysis of the request path, observability gaps, unbounded
fan-out.

**A4 Maintainability.** God files - count DISTINCT responsibilities with line
ranges; a file is not a god file because it is long. DRY violations quantified
with line ranges on BOTH sides. Dead code with the proving search. Comment rot.
Cross-module private-symbol imports.

**A5 Performance.** Per-request work on hot paths, unbounded in-memory
structures, synchronous I/O inside `async def` without an executor, repeated
parsing or hashing, unbounded result sets.

**A6 Test coverage.** `tests/test_api_auth.py`, `tests/test_api_routes.py`, `tests/test_api_tier_enforcement.py`,
`tests/test_auth_code_store.py`, `tests/test_auth_mode.py`, `tests/test_client_token_tier_auth.py`,
`tests/test_config_api_boundaries.py`, `tests/test_loopback_auth_safety.py`,
`tests/test_oauth_as_consent_secret.py`, `tests/test_oauth_as_e2e.py`,
`tests/test_oauth_as_metadata.py`, `tests/test_oauth_as_register.py`,
`tests/test_oauth_as_self_wiring.py`, `tests/test_oauth_authorize.py`,
`tests/test_oauth_client_store.py`, `tests/test_oauth_consent_session.py`,
`tests/test_oauth_jwt_verifier.py`, `tests/test_oauth_keys.py`,
`tests/test_oauth_local_smoke.py`, `tests/test_oauth_metadata.py`,
`tests/test_oauth_operator_preflight.py`, `tests/test_oauth_rate_limit.py`,
`tests/test_oauth_settings_snapshot.py`, `tests/test_oauth_token.py`,
`tests/test_query_auth_policy.py` cover this module. Identify which properties are asserted
versus merely exercised, and which of your findings no existing test would
catch. A test that asserts a state transition but not the property around it is
a coverage gap - say so.

**A7 LLM/AI.** Does request-derived or user-derived text reach a model prompt
through this layer, and is any model output trusted for a control-flow or
authorization decision? If the answer is none, say so plainly rather than
manufacturing findings.

**Compliance.** Licensing headers, secret material in source or logs, PII
handling, and whether error responses leak information a public deployment
should not expose.

## Confirmed bug classes in this codebase - sweep for all six

The probe covers most of these mechanically. Confirm against its output and
extend by hand where it states a limit.

1. **Duplicate definitions** - the later silently overrides. Compare BODIES, not
   signatures: two confirmed instances had compatible signatures and dispatched
   to different implementations; one silently drops data.
2. **Names used only in `except` handlers, never bound** - an unbound `logger`
   produced `NameError` in 9 handlers, destroying the original exception.
3. **`except Exception` where `asyncio.CancelledError` escapes** and skips
   cleanup or state reset. It derives from `BaseException`.
4. **Lexicographic timestamp comparison** - Python `isoformat()` (`T`) versus
   SQLite `datetime('now')` (space) compared as TEXT; also mixed UTC offsets
   sorted as strings.
5. **Module constants documenting an invariant nothing reads.**
6. **Keyword-argument contract mismatch between a caller and the implementation
   it selects at runtime** - one confirmed case raised `TypeError` on every
   invocation, swallowed by a bare `except Exception` into a misleading message.

## Non-negotiable rules

- **A comment is NOT evidence of the invariant it asserts.** Nine confirmed
  findings here are comments describing controls the code does not implement,
  including a docstring claiming "fail-closed" over a function that fails open.
  This codebase's comments are articulate and have misled prior reviewers.
- Cite exact `file:line` for every claim. No claim without a citation.
- Report every issue including low severity. Do not pre-filter - a separate
  verification pass does the filtering. Omissions are the failure mode.
- If you investigate a candidate and disprove it, say so **with the evidence
  that disproved it**. A disproof needs the same rigor as a finding; two
  disproofs here were later shown wrong by execution.
- Anything believed but not traced goes under Open Questions, labelled.
- Severity by consequence: **Critical** = data loss/corruption, auth bypass,
  crash on valid input, silent wrong result in a security-critical path.
  **High** = wrong results for common inputs, silent failure swallowing real
  errors, deadlock off the exceptional path. **Medium** = wrong results in rare
  edge cases, resource leak. **Low** = cosmetic, recoverable on pathological
  input.

## Execute rather than reason

Where a reproduction is cheap, run it and paste real output. Every Critical
found in this project came with an executed reproduction; every incorrect claim
came from reasoning about behavior instead of running it. Project venv:
`.venv/Scripts/python.exe`.

## Output

One report: `.agent/reviews/menhir-m2-compound-audit-gemini-results.md`. Write a draft as soon as you have first findings
and refine it in place - do not batch all writing to the end.

Sections:

1. Executive Summary, highest-risk result stated first
2. Findings by audit type (A1-A7 + Compliance): severity, `file:line`,
   reproduction or code-path trace, impact, fix
3. Route / Tier Authorization Matrix - every route in the module: declared tier, the control actually enforcing it (inline check, middleware, or helper - name it), and whether it is reachable unauthenticated
4. Bug-Class Sweep Results - probe output quoted, plus hand-extension
5. Test Coverage Gap Analysis - which findings no existing test would catch
6. Disproved Candidates, with the evidence that disproved them
7. Open Questions - suspected but unproven, and what would settle each
8. Coverage Table - every file, with line reconciliation against probe output
9. What Was Checked, and what could not be verified in this environment
10. Review Confidence (/100) using the rubric in `.agent/audit/README.md`

Work autonomously to completion. Do not ask questions.
