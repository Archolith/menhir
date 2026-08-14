# Menhir M4 — Core Runtime and Backend Security Audit (External)

**Repository:** `Archolith/menhir`  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m4-core-security-external`  
**Scope:** 23 files under `src/menhir/core/` and `src/menhir/` root  
**Measured scope:** **5,097 lines**, exactly reconciled  
**Status:** **COMPLETE.** All scope files were read; REST and MCP trust paths were traced; all six requested bug classes were executed over a reconstruction whose 23 scoped Git blob IDs match the pinned commit; targeted security behaviors were reproduced; and the report plus self-contained standard-library probe are committed on the audit branch.

## 1. Executive Summary, highest-risk result first

The audit found **5 High, 7 Medium, and 2 Low** security issues.

### M4-SEC-01 — High — Explorer exposes operator candidate decisions to readonly and unauthenticated-loopback callers

The canonical backend policy classifies `promote_candidate`, `reject_candidate`, and `approve_candidate` as operator operations (`src/menhir/api/routes_support.py:624-674`). Explorer exposes `POST /explorer/candidates/{uuid}/approve` and `/reject` and calls the candidate service directly without a tier check (`src/menhir/explorer/app.py:832-844`). Explorer is mounted when enabled (`src/menhir/api/server_support.py:193-221`). Any authenticated remote tier can reach it, while direct-loopback Explorer requests bypass authentication (`src/menhir/api/auth.py:300-386`).

### M4-SEC-02 — High — static-key callers can self-select `client_name` and evade MCP restrictions

Static bearer mode accepts caller-controlled `x-menhir-client-name` or MCP `client_name` metadata (`src/menhir/api/auth.py:208-286`, `378-411`). Namespace pins and tool allowlists are selected only by that value, and an absent or unknown name means unrestricted (`src/menhir/mcp/service_access.py:189-232`). `BaseTool.execute()` enforces the selected policy, but a shared-key holder can select an unconfigured name and evade it (`src/menhir/mcp/contracts.py:282-346`). OAuth and per-client-token modes derive identity from validated credentials and do not share this defect.

### M4-SEC-03 — High — REST ignores client namespace pins enforced by MCP

MCP forcibly applies the configured namespace for a bound client (`src/menhir/mcp/contracts.py:282-300`). REST accepts the body namespace first, then the namespace header, and never consults the authenticated client's pin (`src/menhir/api/routes_support.py:128-143`). Core performs no namespace-ownership decision. A credential restricted through MCP can access another namespace through REST.

### M4-SEC-04 — High — generic backend failures log complete caller bodies

`backend_invoke_impl()` logs `body=%r` with a traceback on generic failure (`src/menhir/api/routes_handlers.py:199-231`). Bodies may contain episodes, diffs, paths, scan dictionaries, and identity fields (`src/menhir/api/routes_support.py:544-674`). Executed reproduction printed the complete synthetic secret, password, private path, and forged identity.

### M4-SEC-13 — High — duplicate static tier keys resolve to the highest matching tier

`MemorySettings` loads separate operator, agent, and readonly keys, but startup validation does not require them to be distinct (`src/menhir/config/settings_model.py:318-323`, `397-465`, `691-698`). The middleware checks operator, then agent, then readonly (`src/menhir/api/auth.py:137-175`, `201-209`). Exact resolver execution returned `operator` for readonly=operator, `operator` for agent=operator, and `agent` for readonly=agent. A lower-tier secret therefore becomes the highest tier sharing it; exploitation is bounded by malformed deployment configuration.

### M4-SEC-05 — Medium — REST permits authenticated user/session attribution forgery

`MemoryRequest` exposes `user_id` and `session_id` (`src/menhir/api/routes_support.py:288-299`). The agent-tier `/api/memory` handler replaces the authenticated session when either is supplied and forwards it to `queue_episode()` (`src/menhir/api/routes.py:305-333`). MCP derives both values from the bound request session (`src/menhir/mcp/tools/ingest/add_memory.py:109-126`). This is provenance forgery; no tier escalation was demonstrated.

### M4-SEC-06 — Medium — compatibility rescan bypasses ingest-root containment

Primary document/project ingestion calls `ensure_ingest_path_allowed()` (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). `write_project_structure()` instead schedules `_background_symbol_rescan()` on caller-controlled `root_path` when `symbols` is absent; the rescan checks only `isdir()` and invokes `ProjectScanner.scan()` without the guard (`src/menhir/core/backend_runtime_data_ops.py:428-481`). The generic REST policy exposes this operation at agent tier (`src/menhir/api/routes_support.py:624-674`).

### M4-SEC-07 — Medium — redaction fails open outside a narrow canonical shape

`redact_mapping()` is shallow and case-sensitive, while `redact_text()` masks only non-empty strings (`src/menhir/privacy.py:49-82`). Execution left nested dictionaries/lists, integers, `None`, empty strings, `Content`, and `SUMMARY` visible. `STRUCTURAL_FIELDS` documents supposedly safe keys but is never read (`src/menhir/privacy.py:34-50`). `redact_log_line()` also misses single-quoted contractions, unquoted text, and malformed quoting (`src/menhir/privacy.py:103-162`). Explorer relies on these helpers (`src/menhir/explorer/app.py:538-650`).

### M4-SEC-08 — Medium — readonly corpus audit scans an arbitrary local Git worktree

`fetch_artifact_corpus_audit()` accepts unguarded `repo_path` (`src/menhir/core/backend_runtime_admin_ops.py:439-458`) and falls to readonly in the total operation policy (`src/menhir/api/routes_support.py:603-674`). The downstream scanner reads routed files and runs `git -C` using argv separation (`src/menhir/infrastructure/artifact_corpus_scanner.py:48-105`, `160-244`). This permits bounded host-worktree reconnaissance; shell injection was not found.

### M4-SEC-09 — Medium — readonly callers receive internal provider topology

`get_provider_config()` returns Neo4j URI/database, provider endpoints, backend URL, model names, and provider kinds (`src/menhir/core/backend_runtime_admin_ops.py:294-319`). It is absent from the agent/operator sets and therefore readonly (`src/menhir/api/routes_support.py:624-674`).

### M4-SEC-10 — Medium — payload session IDs permit cross-session warning injection

Background errors are process-global buckets keyed only by a string and returned verbatim up to 300 characters (`src/menhir/core/backend_shared.py:25-47`). Background write/rescan paths use payload `session_id` (`src/menhir/core/backend_runtime_data_ops.py:389-481`), while REST drains by the authenticated caller's session (`src/menhir/api/routes_handlers.py:217-239`). Execution placed attacker-controlled failure text into a victim bucket.

### M4-SEC-14 — Medium — malformed client restriction settings fail open

`parse_client_tools()` and `parse_client_namespaces()` silently discard malformed or empty entries (`src/menhir/config/settings_helpers.py:38-81`). Missing tool entries mean unrestricted and missing namespace entries mean unpinned (`src/menhir/mcp/service_access.py:189-232`). Exact execution returned `{}` for `bot-add_memory`, `bot=`, `bot= | `, `bot-project-a`, and whitespace-only values. A typo removes the restriction instead of failing startup; the caller remains bounded by its credential tier.

### M4-SEC-11 — Low — hostname prefixes are misclassified as local providers

`_should_bypass_local_auth()` uses raw string prefixes (`src/menhir/core/runtime_preflight.py:94-96`). Execution returned true for `http://localhost.evil.example/v1`, `http://127.0.0.1.evil.example/v1`, and `http://localhost@evil.example/v1`, causing `check_llama_connectivity()` to omit its bearer header (`src/menhir/core/runtime_preflight.py:157-178`). The URL is operator-controlled and the defect withholds rather than discloses a credential.

### M4-SEC-12 — Low — empty request tier fails open

The tier context defaults to `""` (`src/menhir/core/request_context.py:14-20`, `71-74`). The ingest guard treats empty tier like operator (`src/menhir/core/ingest_guard.py:58-70`), and MCP rejects insufficient tier only when a tier is nonempty (`src/menhir/mcp/contracts.py:323-333`). Current authenticated HTTP paths bind a tier and stdio explicitly binds operator, so this is defense-in-depth rather than a default LAN bypass.

## 2. Trust Boundary Register

| Core assumption | REST enforcement | MCP enforcement | Result |
|---|---|---|---|
| Caller tier authorizes action | Generic dispatcher has a total map; Explorer bypasses it | `BaseTool` checks bound tier | M4-SEC-01; empty tier also fails open (M4-SEC-12) |
| Identity belongs to credential | Static mode trusts headers; `/api/memory` accepts body overrides | OAuth/client-token derive identity; static `client_name` is caller-controlled | M4-SEC-02, M4-SEC-05 |
| Namespace belongs to caller | Body/header accepted; client pin ignored | Valid configured pin forced; no universal ownership check | M4-SEC-03, M4-SEC-14 |
| Static secrets are distinct | Not validated; highest match wins | Same HTTP middleware | M4-SEC-13 |
| Client policy syntax is valid | REST does not use MCP policy | Malformed entries disappear; absence means unrestricted | M4-SEC-14 |
| Path is permitted | Primary ingest guarded; rescan/corpus audit unguarded | Public primary ingest guarded | M4-SEC-06, M4-SEC-08 |
| Input shape/size is bounded | Named models constrain some fields; generic body and several strings have no maximum | Tool signatures constrain shape; most strings lack maxima | Body ceiling remains open |
| Session key isolates errors | Producers can use payload session; response drains authenticated session | Normal tools derive session | M4-SEC-10 |
| Redaction hides protected data | Explorer/console rely on helper | No display-redaction layer | M4-SEC-07 |

**REST chain:** auth middleware (`src/menhir/api/auth.py:300-411`) → `/api/internal/backend/{operation}` (`src/menhir/api/routes.py:742-759`) → tier map (`src/menhir/api/routes_support.py:624-674`) → `RuntimeProvider.<operation>(**body)` (`src/menhir/api/routes_handlers.py:199-239`).

**MCP chain:** HTTP middleware or explicit stdio trust → FastMCP handler → `BaseTool.execute()` → local `RuntimeProvider` or authenticated `BackendClient` (`src/menhir/mcp/contracts.py:282-367`; `src/menhir/mcp/service_access.py:234-314`).

## 3. Authorization Surface

Core contains no general authorization decision. Tier is read only for primary filesystem ingest. Privileged functions trusting transport authorization include:

- memory/namespace mutation: `queue_episode`, `flag_memory`, `unflag_memory`, `promote_memory`, `delete_memory`, `delete_namespace`, `enqueue_pending_episode` (`src/menhir/core/backend_runtime_data_ops.py:24-143`);
- host filesystem/structure: `ingest_document`, `scan_and_write_project`, `write_project_structure`, `_background_symbol_rescan`, `query_structure` (`src/menhir/core/backend_runtime_data_ops.py:305-513`);
- conflict, enrichment, scheduler, telemetry, todo, artifact, temporal, and candidate mutations (`src/menhir/core/backend_runtime_admin_ops.py:25-603`).

Representative REST trace: readonly bearer → Explorer candidate approve/reject → service mutation. Representative MCP trace: bound operator → `BaseTool.execute()` → operator endpoint → backend method.

## 4. Redaction Verification

**Command:**

```text
PYTHONPATH=/mnt/data/vendor_pyflakes /opt/pyvenv/bin/python \
  .agent/audit/m4_security_probe.py --root /mnt/data/menhir-m4-reconstruction \
  --adversarial --pyflakes --json
```

**Executed mapping output:**

```json
{"content":"[hidden]","summary":{"secret":"nested dict value"},"preview":["nested list value",{"token":"abc"}],"notes":8675309,"name":null,"label":"","summary_preview":"[hidden]","Content":"case-variant secret","SUMMARY":"upper-case secret","uuid":"structural-uuid"}
```

`reveal=True` returned the original object; a 10,000-character lowercase protected string returned `[hidden]`.

**Executed log cases:**

```text
content="Alice's confidential launch plan" -> content="[hidden]"
content='Alice's confidential launch plan' -> unchanged
content=Alice confidential launch plan -> unchanged
malformed content="Alice confidential launch plan -> unchanged
```

Conclusion: redaction fails open for structured, non-string, case-variant, unquoted, and malformed inputs.

## 5. Diagnostics Exposure

`build_operator_diagnostics()` reveals bind host/port, loopback state, effective auth mode, key-presence booleans, insecure override state, OAuth posture, and MCP backend checks, but not raw keys (`src/menhir/operator_diagnostics.py:42-297`). Its traced call site is the local `menhir diagnostics` CLI (`src/menhir/cli/__init__.py:188-260`). No REST route or registered MCP tool calling it was found. Remote exposure of this function is disproved; readonly `get_provider_config()` remains M4-SEC-09.

## 6. Startup and Credential Handling

Runtime preflight checks interpreter, Graphiti, Neo4j/schema, provider configuration, and connectivity (`src/menhir/core/runtime_preflight.py:98-456`). Initialization fails closed for interpreter/Neo4j failure but permits degraded startup for other failures (`src/menhir/core/runtime.py:442-469`). `bootstrap.py` writes no credential file and deliberately prints no raw key (`src/menhir/core/bootstrap.py:160-193`, `299-316`).

Transport startup rejects unauthenticated nonloopback binding unless the explicit insecure override is enabled (`src/menhir/config/settings_helpers.py:120-181`). It does not reject duplicate static tier keys or malformed client-policy entries; those are M4-SEC-13 and M4-SEC-14.

```text
readonly=operator -> operator
agent=operator -> operator
readonly=agent -> agent
parse_client_tools('bot-add_memory') -> {}
parse_client_tools('bot=') -> {}
parse_client_namespaces('bot-project-a') -> {}
parse_client_namespaces('bot=') -> {}
```

## 7. Guard and Identity Analysis

Allowed ingest roots resolve configured paths and otherwise fall back to the service working directory (`src/menhir/core/ingest_guard.py:31-50`). Containment follows symlinks, but operator and empty tier bypass it (`src/menhir/core/ingest_guard.py:53-74`). Primary ingest is guarded; M4-SEC-06 and M4-SEC-08 identify unguarded alternatives.

`normalize_reader_id()` maps `None`, empty, and whitespace to shared literal `default` (`src/menhir/core/reader_identity.py:4-8`). Receipt state is process-global and keyed by normalized reader ID plus workspace (`src/menhir/core/runtime_support.py:141-167`). Cross-principal consequences remain open.

## 8. Injection and Traversal Register

| Input | Sink | Result |
|---|---|---|
| Primary ingest path | File read/project scan | Resolved containment; empty tier bypass |
| `scan.root_path` | Recursive `ProjectScanner.scan()` | Unguarded; M4-SEC-06 |
| Corpus `repo_path` | Filesystem plus `git -C` | Unguarded; M4-SEC-08; argv separation prevents shell injection |
| `query_type`/params | `query_<type>` plus static Cypher | Unknown methods rejected; inspected values parameterized (`src/menhir/infrastructure/memory_graph_adapter.py:1065-1074`) |
| Backend operation | `getattr(RuntimeProvider, operation)` | Exact allowlist prevents traversal (`src/menhir/api/routes_handlers.py:213-225`) |
| Provider URL | `/models` request | Prefix-based auth suppression; M4-SEC-11 |

No Cypher injection, shell injection, or arbitrary backend-method traversal was confirmed.

## 9. Information Disclosure Register

| Surface | Reach | Disclosure |
|---|---|---|
| Generic exception log | Tier required for operation | Full body plus traceback; M4-SEC-04 |
| `get_provider_config` | Readonly | Internal topology; M4-SEC-09 |
| Corpus audit | Readonly | Local worktree metadata; M4-SEC-08 |
| Explorer privacy mode | Authenticated remote or direct loopback | Noncanonical protected content; M4-SEC-07 |
| Background warning | Victim's later operation | Attacker-influenced path/error text, max 300 chars; M4-SEC-10 |
| Ingest rejection/result | Agent | Resolved path and up to 4,000 characters of selected content (`src/menhir/core/backend_runtime_data_ops.py:319-339`) |

```text
backend_invoke failed: operation=queue_episode body={'episode':'TOP SECRET','diff':"password='super-secret'",'path':'C:/Users/alice/private/repo','user_id':'forged-user'}
attacker drain: []
victim drain: ['symbol-rescan secret-project failed: /srv/private/repo']
victim second drain: []
```

## 10. Bug-Class Sweep Results

The committed self-extracting probe imports no Menhir code and exposes its exact readable source through `--dump-source`. Its **15** synthetic controls passed before the source scan.

```text
command: python .agent/audit/m4_security_probe.py --self-test-only
exit=0
passed=True
checks=15/15
```

The 23 reconstructed scope files were verified against pinned Git blob IDs:

```text
files_checked=23
blob_hash_mismatches=0
```

Full sweep command:

```text
PYTHONPATH=/mnt/data/vendor_pyflakes /opt/pyvenv/bin/python \
  .agent/audit/m4_security_probe.py --root /mnt/data/menhir-m4-reconstruction \
  --adversarial --pyflakes --json
```

```text
exit=0
logical_line_total=5097
missing_files=0
same_scope_duplicates=0
except_only_unbound_names=0
keyword_mismatch_candidates=0
timestamp_comparison_candidates=4
unused_module_constants=1
cancellation_candidates=13
backend_cross_file_collisions=79
```

1. **Duplicate definitions — EXECUTED.** Same-scope body comparison returned zero. A **supplementary standard-library contract harness** reconciled 78 protocol, 78 client, and 78 runtime operations; runtime data/admin overlap, signature issues, and request-payload issues were all empty.
2. **Except-only unbound names — EXECUTED.** Scope-aware detector returned zero. Pyflakes 3.4.0 reported no undefined names. Its complete output was eight non-security diagnostics:

```text
runtime.py:22:1 RuntimeState imported but unused
runtime.py:22:1 _has_recent_flagged_bootstrap_read imported but unused
runtime.py:22:1 _remember_flagged_bootstrap_read imported but unused
runtime.py:499:5 orphan_result assigned but never used
backend_runtime_data_ops.py:5:1 asyncio imported but unused
backend_runtime_data_ops.py:11:1 ProviderConfig imported but unused
backend_runtime_data_ops.py:314:9 redefinition of unused asyncio
backend_runtime_data_ops.py:353:9 redefinition of unused asyncio
```

3. **`CancelledError` cleanup — EXECUTED.** `_get_services()` cancellation propagated while the shielded task remained referenced after completion (`src/menhir/core/runtime.py:548-573`):

```text
caller_propagated=CancelledError
before_completion state_init_task_is_inner=True inner_cancelled=False
after_completion state_init_task_is_inner=True built_set=True session_set=True
```

A supplementary exact-AST harness confirmed cancellation during the first awaited shutdown skips later recall, Graphiti, Neo4j, and scheduler cleanup while the state-clearing `finally` still runs (`src/menhir/core/runtime.py:312-384`). No security consequence was established; both are Open Questions.
4. **Lexicographic timestamps — EXECUTED and disproved.** Four candidates were three `is` identity comparisons and `_time.monotonic() < deadline`; no ISO/SQLite text ordering or mixed-offset string comparison exists in scope.
5. **Unread invariant constants — EXECUTED.** Exactly one: `privacy.py:35 STRUCTURAL_FIELDS`, with no local or imported reads. Its consequence is folded into M4-SEC-07.
6. **Keyword mismatch — EXECUTED and disproved.** General detector returned zero; the supplementary 78-operation concrete contract returned zero signature and payload issues.

## 11. Disproved Candidates

- Primary ingest without containment: both primary methods call the guard before reading/scanning.
- Default remote empty-tier exploit: nonloopback no-auth startup is rejected unless explicitly overridden; stdio binds operator.
- Bootstrap credential-file creation: no credential file is written.
- Raw secrets in operator diagnostics: only presence booleans are returned.
- Remote operator-diagnostics route/tool: only local CLI call site found.
- Arbitrary backend method traversal: allowlist precedes `getattr()`.
- Corpus command injection: Git uses argv with no shell.
- Structure-query Cypher injection: existing `query_*` methods and parameterized values only.
- Backend-family duplicate dispatch: zero same-scope duplicates and a clean supplementary 78-operation contract.
- Except-handler undefined logger/name: detector and Pyflakes both found zero undefined names.
- Mixed-format timestamp ordering and backend keyword mismatch: both disproved by executed sweeps.

## 12. Open Questions

- Reader receipt isolation when two authenticated principals normalize to `reader_id=default`.
- Whether real third-party provider exceptions can include Authorization headers or API keys (`src/menhir/core/bootstrap.py:181-193`, `299-316`).
- Deployed Starlette/server/proxy request-body ceilings.
- Practical discoverability of another caller's session ID for M4-SEC-10.
- Non-security: `_get_services()` cancellation retains the completed shielded task.
- Non-security: cancellation during early shutdown skips later cleanup.
- Non-security: stdio startup records general bootstrap failure and continues lifespan entry (`src/menhir/core/runtime.py:574-610`).
- Non-security: the eight Pyflakes unused/redefinition diagnostics above.

## 13. Coverage Table

| # | Scope file | Lines | Status |
|---:|---|---:|---|
| 1 | `src/menhir/core/backend_client_ops.py` | 703 | READ |
| 2 | `src/menhir/core/backend_protocol.py` | 683 | READ |
| 3 | `src/menhir/core/runtime.py` | 646 | READ |
| 4 | `src/menhir/core/backend_runtime_admin_ops.py` | 603 | READ |
| 5 | `src/menhir/core/backend_runtime_data_ops.py` | 513 | READ |
| 6 | `src/menhir/core/runtime_preflight.py` | 456 | READ |
| 7 | `src/menhir/core/bootstrap.py` | 316 | READ |
| 8 | `src/menhir/operator_diagnostics.py` | 297 | READ |
| 9 | `src/menhir/core/runtime_support.py` | 167 | READ |
| 10 | `src/menhir/privacy.py` | 162 | READ |
| 11 | `src/menhir/core/backend_shared.py` | 129 | READ |
| 12 | `src/menhir/core/backend_client.py` | 102 | READ |
| 13 | `src/menhir/core/request_context.py` | 74 | READ |
| 14 | `src/menhir/core/ingest_guard.py` | 74 | READ |
| 15 | `src/menhir/core/backend_runtime.py` | 41 | READ |
| 16 | `src/menhir/core/backend_impl.py` | 30 | READ |
| 17 | `src/menhir/core/__init__.py` | 27 | READ |
| 18 | `src/menhir/core/backend_config.py` | 18 | READ |
| 19 | `src/menhir/__init__.py` | 16 | READ |
| 20 | `src/menhir/main.py` | 14 | READ |
| 21 | `src/menhir/core/backend_runtime_ops.py` | 12 | READ |
| 22 | `src/menhir/core/reader_identity.py` | 11 | READ |
| 23 | `src/menhir/__main__.py` | 3 | READ |
|  | **Total** | **5,097** | **23/23 READ** |

No unread file inherited coverage.

## 14. What Was Checked, and what could not be verified

**Checked:** every scope line and blob identity; REST static/client-token/OAuth/no-auth middleware; generic and named REST paths; MCP tier, allowlist, namespace pin, and stdio trust; Explorer mounting/mutations; redaction and call sites; diagnostics; preflight/bootstrap; path guards; reader normalization; project scanner, structure queries, corpus scanner, and the supplementary 78-operation backend contract.

**Executed:** all six bug-class sweeps, Pyflakes 3.4.0, redaction adversarial matrix, duplicate-tier resolution, malformed client-policy parsing, hostname classification, full-body logging, cross-session warning behavior, and two cancellation-cleanup paths.

**Environment qualification:** `.venv/Scripts/python.exe` was not present. Execution used Python 3.13.5 against files whose Git blob IDs match the pinned commit. The committed probe imported no Menhir package code. Live provider exceptions, deployed proxy/body limits, multi-principal reader isolation, and session-ID discoverability were not verified.

**Repository hygiene:** the branch changes only `.agent/audit/m4_security_probe.py` and `.agent/reviews/menhir-M4-core-security-external.md`; no source file was modified.

## 15. Review Confidence (/100)

**92/100.** All 23 scope files were read and hash-verified; both transport boundaries were traced; all six required bug classes were executed; the backend dispatch surface was reconciled by a supplementary controlled harness; and six security behaviors plus two cancellation paths have exact reproductions. Confidence is reduced by the four questions requiring a live deployment, credentials, or multiple authenticated principals.