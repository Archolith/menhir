# Menhir M4 — Core Runtime and Backend Security Audit (External)

**Repository:** `Archolith/menhir`  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m4-core-security-external`  
**Scope:** 23 files under `src/menhir/core/` and the `src/menhir/` root  
**Measured scope:** **5,097 lines**, exactly reconciled  
**Status:** **COMPLETE.** All scope files were read, REST and MCP trust paths were traced, the required six bug classes were swept with controlled instruments, and targeted security behavior was executed. The requested Windows project interpreter was unavailable in this environment; the static and exact-source executions were run against a materialization of the pinned GitHub blobs with the active Python interpreter and vendored official Pyflakes 3.4.0.

## 1. Executive Summary, highest-risk result first

The audit found **5 High, 7 Medium, and 2 Low** security issues. The highest-risk result is a transport authorization mismatch: when Explorer is enabled, a readonly-authenticated remote caller—or a direct loopback caller with no credential—can approve or reject candidates even though the canonical backend policy classifies those actions as operator-only.

### M4-SEC-01 — High — Explorer exposes operator candidate decisions to readonly and unauthenticated-loopback callers

The canonical generic-backend policy classifies `promote_candidate`, `reject_candidate`, and `approve_candidate` as operator operations (`src/menhir/api/routes_support.py:624-674`). Explorer independently exposes `POST /explorer/candidates/{uuid}/approve` and `/reject`, then calls the candidate service directly without a tier check (`src/menhir/explorer/app.py:832-844`). Explorer is mounted in the live application when enabled (`src/menhir/api/server_support.py:193-221`). The authentication middleware permits any valid remote tier to reach Explorer and bypasses authentication entirely for direct loopback Explorer requests (`src/menhir/api/auth.py:300-386`). A lower tier can therefore perform operator-classified memory-governance decisions.

### M4-SEC-02 — High — static-key callers can self-select `client_name` and bypass MCP namespace/tool restrictions

Static bearer mode trusts caller-supplied identity headers and MCP query metadata; `x-menhir-client-name` or `client_name` becomes the bound client name (`src/menhir/api/auth.py:208-286`, `378-411`). MCP namespace pins and tool allowlists are looked up solely by that name, and an absent or unconfigured name means unrestricted (`src/menhir/mcp/service_access.py:189-232`). `BaseTool.execute()` enforces the *selected* allowlist and pin (`src/menhir/mcp/contracts.py:282-346`), but a holder of a shared static tier key can select an unconfigured name and evade both controls. OAuth and per-client-token modes derive identity from validated credentials and do not share this defect.

### M4-SEC-03 — High — REST ignores server-configured client namespace pins enforced by MCP

MCP forcibly replaces caller namespace input with the namespace configured for the bound client (`src/menhir/mcp/contracts.py:282-300`). REST `_resolve_namespace()` accepts the body namespace first, then the namespace header, and never consults the authenticated client's configured pin (`src/menhir/api/routes_support.py:128-143`). Core accepts namespace values without an ownership decision. A client restricted to one namespace through MCP can reuse its HTTP credential to read or write another namespace through REST.

### M4-SEC-04 — High — generic backend failures log complete caller bodies without redaction

`backend_invoke_impl()` accepts a generic dictionary and, for every non-preset exception, logs `body=%r` with a traceback before re-raising (`src/menhir/api/routes_handlers.py:199-231`). The operation body can carry complete episodes, diffs, paths, scan dictionaries, and identity values (`src/menhir/api/routes_support.py:544-674`). Executed reproduction printed the full synthetic secret, password, private path, and forged identity into the rendered log. A remote caller able to invoke an operation can therefore cause sensitive input to reach server logs.

### M4-SEC-05 — Medium — REST permits authenticated callers to forge memory user/session attribution

`MemoryRequest` exposes body `user_id` and `session_id` fields (`src/menhir/api/routes_support.py:288-299`). The agent-tier `/api/memory` handler replaces the authenticated caller session whenever either is supplied and forwards the replacement to `queue_episode()` (`src/menhir/api/routes.py:305-333`). This remains true under OAuth and per-client-token authentication. MCP's corresponding path derives both values from the bound request session (`src/menhir/mcp/tools/ingest/add_memory.py:109-126`). The result is provenance and session forgery, not a demonstrated tier escalation.

### M4-SEC-06 — Medium — agent-reachable compatibility rescan bypasses ingest-root containment

Primary document/project ingestion calls `ensure_ingest_path_allowed()` before touching the filesystem (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). Separately, `write_project_structure()` accepts a caller-provided scan dictionary; if `symbols` is absent, it schedules `_background_symbol_rescan()` on the supplied `root_path` (`src/menhir/core/backend_runtime_data_ops.py:428-443`). The rescan checks only `os.path.isdir()` and invokes `ProjectScanner.scan()` without the guard (`src/menhir/core/backend_runtime_data_ops.py:453-481`). `ProjectScanner` resolves and recursively walks that host directory (`src/menhir/infrastructure/project_scanner.py:211-240`). The generic REST policy exposes `write_project_structure` at agent tier (`src/menhir/api/routes_support.py:544-674`). This restores host-directory scanning outside configured ingest roots.

### M4-SEC-07 — Medium — privacy redaction fails open and its documented structural invariant is unused

`redact_mapping()` performs shallow, case-sensitive key matching, while `redact_text()` masks only non-empty strings (`src/menhir/privacy.py:49-82`). Execution left nested dictionaries, nested lists, an integer, `None`, an empty string, `Content`, and `SUMMARY` visible. A 10,000-character lowercase protected string was masked. `redact_log_line()` masked a qualifying double-quoted phrase but left a single-quoted contraction, an unquoted phrase, and malformed quoted text visible (`src/menhir/privacy.py:103-162`). The module also defines `STRUCTURAL_FIELDS` as the documented allowlist of fields that may survive, but the executed constant sweep found no read of it anywhere (`src/menhir/privacy.py:31-48`). Unknown keys therefore pass through rather than being checked against the stated invariant. Explorer relies on these helpers as a privacy display control (`src/menhir/explorer/app.py:538-583`, `607-650`), and the console uses the log helper for its live tail (`src/menhir/cli/console.py:130-150`).

### M4-SEC-08 — Medium — readonly backend operation scans an arbitrary host Git repository and returns corpus metadata

`fetch_artifact_corpus_audit()` accepts caller-controlled `repo_path` without a core path guard (`src/menhir/core/backend_runtime_admin_ops.py:439-458`). It is absent from the agent/operator sets and therefore falls to readonly (`src/menhir/api/routes_support.py:603-674`). The adapter resolves the path, runs a repository audit, and returns commit/cursor evidence, counts, conflicts, and contradictions (`src/menhir/infrastructure/memory_graph_adapter.py:1368-1402`). The scanner runs `git -C <caller path>` with argv separation, reads routed corpus files, hashes them, and extracts title/status/type/UUID metadata (`src/menhir/infrastructure/artifact_corpus_scanner.py:48-105`, `160-244`). A readonly caller can perform bounded reconnaissance against arbitrary local Git worktrees. Shell injection was disproved because no shell is used.

### M4-SEC-09 — Medium — readonly callers can retrieve internal provider and topology configuration

`get_provider_config()` returns the Neo4j URI/database, LLM and embedding endpoints, backend URL, provider kinds, and model names (`src/menhir/core/backend_runtime_admin_ops.py:294-319`). It is in the generic backend allowlist but absent from the agent/operator sets, so the total policy assigns readonly (`src/menhir/api/routes_support.py:544-674`). Core has no additional gate. This reveals internal service topology and implementation details to the lowest authenticated tier.

### M4-SEC-10 — Medium — payload-controlled session IDs permit cross-session background-warning injection

Background errors are process-global buckets keyed only by a string and returned verbatim up to 300 characters (`src/menhir/core/backend_shared.py:25-47`). Background write and symbol-rescan paths push errors under the payload-supplied `session_id` (`src/menhir/core/backend_runtime_data_ops.py:389-416`, `430-481`), while REST drains by the authenticated caller's session ID (`src/menhir/api/routes_handlers.py:217-239`). Execution showed a warning pushed under `victim-session` was invisible to `attacker-session` and appeared on the victim drain. An agent who knows another session ID can inject attacker-influenced failure text into that caller's later response.

### M4-SEC-11 — Low — provider preflight misclassifies hostname prefixes as loopback and suppresses authorization

`_should_bypass_local_auth()` uses raw string prefixes (`src/menhir/core/runtime_preflight.py:94-96`), and `check_llama_connectivity()` omits its bearer header when that predicate is true (`src/menhir/core/runtime_preflight.py:157-178`). Execution returned `True` for `http://localhost.evil.example/v1`, `http://127.0.0.1.evil.example/v1`, and `http://localhost@evil.example/v1`. The URL is operator-controlled and the defect withholds rather than exposes the credential, so impact is limited.

### M4-SEC-12 — Low — tier checks fail open when request tier is unbound

The request-tier context defaults to `""` (`src/menhir/core/request_context.py:14-20`, `71-74`). `ensure_ingest_path_allowed()` treats an empty tier like operator and skips containment (`src/menhir/core/ingest_guard.py:58-70`); `BaseTool.execute()` rejects insufficient tier only when the current tier is nonempty (`src/menhir/mcp/contracts.py:323-333`). Current authenticated HTTP paths bind a tier, stdio explicitly binds operator, and default nonloopback no-auth startup is rejected, so this is not a default LAN bypass. It remains a fail-open internal boundary under the explicit insecure no-auth override or any future unbound entry path.

### M4-SEC-13 — High — duplicate static tier secrets silently resolve to the highest matching privilege

`MemorySettings.__post_init__()` validates numeric, OAuth, proxy, and bind settings but performs no pairwise uniqueness check over `api_key`, `operator_key`, `agent_key`, and `readonly_key` (`src/menhir/config/settings_model.py:397-469`). The middleware stores the configured values and resolves a token in operator → agent → readonly order (`src/menhir/api/auth.py:140-209`). Exact-source execution produced:

```text
distinct_readonly        -> readonly
readonly_equals_operator -> operator
agent_equals_operator    -> operator
readonly_equals_agent    -> agent
```

Thus a secret intended for a lower tier becomes a higher-tier credential when two configured values collide. This is a privilege escalation bounded by a startup misconfiguration, and startup accepts it rather than failing closed.

### M4-SEC-14 — Medium — malformed MCP client restriction settings are silently dropped into the unrestricted default

`parse_client_tools()` skips malformed entries and drops empty allowlists; `parse_client_namespaces()` likewise skips malformed or empty entries (`src/menhir/config/settings_helpers.py:38-83`). The runtime meaning of no entry is unrestricted tools and no namespace pin (`src/menhir/mcp/service_access.py:189-232`). Exact-source execution returned `{}` for `bot-add_memory`, `bot=`, `bot= | `, `bot-project-a`, and `bot=   `. A typo in a server-side restriction therefore removes the restriction without a startup error. The caller remains bounded by its credential tier, so this is Medium rather than High.

## 2. Trust Boundary Register — every caller assumption and both transports

| Assumption made behind the transport boundary | REST enforcement | MCP enforcement | Result |
|---|---|---|---|
| Caller tier authorizes the action. | Generic backend has a total operation map; Explorer candidate writes bypass it. | `BaseTool.execute()` checks `required_tier` only when tier is bound. | M4-SEC-01 and M4-SEC-12. |
| Supplied identity belongs to the authenticated caller. | `/api/memory` accepts body overrides; static auth trusts identity headers. | Memory ingestion derives user/session, but static `client_name` remains caller-controlled. | M4-SEC-02 and M4-SEC-05. |
| Namespace belongs to the caller. | Body/header namespace accepted; no configured pin or ownership decision. | Configured pin is forced only when a valid client entry is selected. | M4-SEC-03 and M4-SEC-14. |
| Static tier secrets are distinct. | Not validated; shared middleware resolves highest match. | Same middleware precedes HTTP MCP. | M4-SEC-13 affects both transports. |
| Client restriction syntax is valid. | REST does not honor MCP pins regardless. | Malformed entries disappear and default to unrestricted/unpinned. | M4-SEC-14. |
| Filesystem path is permitted. | Primary ingest guarded; compatibility rescan and readonly corpus audit are not. | Public primary ingest tools reach guarded methods; no public tool was found for `write_project_structure`. | M4-SEC-06 and M4-SEC-08. |
| Input shape is valid. | Named models validate selected routes; generic backend accepts arbitrary dictionaries and dispatches `**body`. | Tool signatures constrain normal calls. | Malformed calls fail, but M4-SEC-04 logs the full body. |
| Input size is bounded. | Several numeric limits exist, but episode/diff/source/identity/namespace and generic body values have no field maxima. | Most tool strings and collections have no explicit maxima. | Deployment body ceiling remains Open. |
| Session key isolates background errors. | Producers may use payload session; route drains authenticated session. | Normal tools derive session and append drained warnings. | M4-SEC-10. |
| Privacy helper masks protected data. | Explorer and console rely on it. | MCP intentionally returns authorized content without this display layer. | M4-SEC-07. |

**REST generic chain:** auth middleware binds tier/session (`src/menhir/api/auth.py:300-411`) → `/api/internal/backend/{operation}` (`src/menhir/api/routes.py:742-759`) → total tier map (`src/menhir/api/routes_support.py:624-674`) → `RuntimeProvider.<operation>(**body)` (`src/menhir/api/routes_handlers.py:199-239`).

**MCP chain:** HTTP middleware or explicit stdio operator binding (`src/menhir/mcp/service_access.py:234-260`) → FastMCP handler → `BaseTool.execute()` tier/allowlist/pin checks (`src/menhir/mcp/contracts.py:282-355`) → local `RuntimeProvider` or authenticated `BackendClient` (`src/menhir/mcp/service_access.py:261-314`).

## 3. Authorization Surface — privileged actions and what gates them

Core contains no general authorization decision. It reads request tier only in the two primary filesystem-ingest methods (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). The generic REST map marks these **18 operator operations**, all of which trust the transport gate:

`approve_candidate`, `confirm_pending_conflicts`, `delete_memory`, `delete_namespace`, `force_release_episode_lease`, `force_reset_failed_episode`, `promote_candidate`, `promote_memory`, `record_conflict_resolution`, `recover_orphans`, `recover_stale_enrichment_leases`, `reject_candidate`, `requeue_conflicts_for_llm_review`, `resolve_conflict_group`, `scan_for_conflicts`, `scheduler_force_takeover`, `scheduler_pause`, `scheduler_resume` (`src/menhir/api/routes_support.py:624-653`; implementations in `src/menhir/core/backend_runtime_data_ops.py:24-143` and `backend_runtime_admin_ops.py:25-603`).

These **18 agent operations** likewise trust transport authorization:

`close_stale_todos`, `close_todo`, `complete_temporal`, `create_candidate`, `create_temporal`, `create_todo`, `delete_todo`, `enqueue_pending_episode`, `flag_memory`, `ingest_document`, `link_artifacts`, `queue_episode`, `relocate_artifact_source`, `scan_and_write_project`, `supersede_artifact`, `transition_artifact_status`, `unflag_memory`, `write_project_structure` (`src/menhir/api/routes_support.py:654-674`).

Representative REST trace: readonly bearer → Explorer approve/reject route → direct candidate-service mutation, no operator gate (`src/menhir/api/auth.py:300-386`; `src/menhir/explorer/app.py:832-844`).

Representative MCP trace: bound operator → `BaseTool.execute()` → operator endpoint → backend method (`src/menhir/mcp/contracts.py:305-355`).

Representative internal REST trace: agent bearer → generic backend `write_project_structure` → unguarded background rescan (`src/menhir/api/routes_support.py:624-674`; `src/menhir/core/backend_runtime_data_ops.py:428-481`).

## 4. Redaction Verification — executed adversarial inputs and real output

**Command:**

```text
PYTHONPATH=/mnt/data/vendor_pyflakes python /mnt/data/m4_security_probe_packed.py \
  --root /mnt/data/menhir-m4-reconstruction --adversarial --pyflakes --json
```

The probe executed selected AST nodes from the pinned `privacy.py`, not a reimplementation.

**Mapping output:**

```json
{
  "Content": "case-variant secret",
  "SUMMARY": "upper-case secret",
  "content": "[hidden]",
  "label": "",
  "name": null,
  "notes": 8675309,
  "preview": ["nested list value", {"token": "abc"}],
  "summary": {"secret": "nested dict value"},
  "summary_preview": "[hidden]",
  "uuid": "structural-uuid"
}
```

`reveal=True` returned the original object; a 10,000-character lowercase protected string returned `[hidden]`.

**Log output:**

```text
IN : ... content="Alice's confidential launch plan"
OUT: ... content="[hidden]"

IN : ... content='Alice's confidential launch plan'
OUT: ... content='Alice's confidential launch plan'

IN : ... content=Alice confidential launch plan
OUT: ... content=Alice confidential launch plan

IN : malformed content="Alice confidential launch plan
OUT: malformed content="Alice confidential launch plan
```

Conclusion: mapping and log redaction fail open outside the narrow canonical case. The executed invariant sweep also found `STRUCTURAL_FIELDS` unread, so the implementation does not enforce its documented structural-field boundary.

## 5. Diagnostics Exposure — `operator_diagnostics.py` reachability by tier

`build_operator_diagnostics()` reveals bind host/port, loopback classification, effective auth mode, key-presence booleans, insecure override state, OAuth resource/authorization-server posture, consent/proxy checks, MCP backend checks, and warnings (`src/menhir/operator_diagnostics.py:42-297`). It does **not** return raw keys.

The directly traced caller is the local `menhir diagnostics` CLI (`src/menhir/cli/__init__.py:188-260`). No REST route or registered MCP tool calls this function in the pinned tree. Remote exposure of this specific snapshot is disproved. The distinct readonly `get_provider_config()` disclosure is M4-SEC-09.

## 6. Startup and Credential Handling

`collect_runtime_failures()` checks interpreter, Graphiti, Neo4j/schema, provider compatibility/configuration, and connectivity (`src/menhir/core/runtime_preflight.py:98-456`). Runtime initialization fails closed for interpreter or Neo4j failure; other failed checks produce degraded startup and continue (`src/menhir/core/runtime.py:442-469`). Bind safety separately fails closed for unauthenticated nonloopback startup unless the explicit insecure override is set.

Security-setting handling is not uniformly fail closed:

- duplicate static tier keys pass settings construction and create M4-SEC-13;
- malformed or empty client tool/namespace entries are dropped and create M4-SEC-14;
- the local-provider hostname test misclassifies prefixes and creates M4-SEC-11.

`bootstrap.py` writes no credential file. It passes Neo4j credentials and provider configuration into collaborators (`src/menhir/core/bootstrap.py:160-193`). Adapter-construction and edge-sync exceptions are logged or returned verbatim (`src/menhir/core/bootstrap.py:181-193`, `299-316`). No scope statement intentionally prints a raw key; whether third-party exception strings can carry credentials remains Open.

## 7. Guard and Identity Analysis

`allowed_ingest_roots()` resolves configured roots and falls back to the resolved current working directory when none survive (`src/menhir/core/ingest_guard.py:31-50`). `_is_within()` compares resolved paths (`src/menhir/core/ingest_guard.py:53-54`). The guard allows operator and empty tier without containment and includes the resolved attempted path, tier, and environment-variable name in rejection text (`src/menhir/core/ingest_guard.py:58-74`). Primary ingest paths are guarded; M4-SEC-06 and M4-SEC-08 identify unguarded alternatives.

`normalize_reader_id()` strips input and maps `None`, empty, and whitespace-only values to shared literal `default` (`src/menhir/core/reader_identity.py:4-8`). Bootstrap receipt state is process-global and keyed by that normalized ID plus workspace selection (`src/menhir/core/runtime_support.py:141-167`). Cross-principal consequences of sharing `default` remain Open.

## 8. Injection and Traversal Register

| Caller-controlled input | Sink | Result |
|---|---|---|
| primary ingest path | file read / project scan | Resolved containment exists; empty tier fails open. |
| compatibility `scan.root_path` | recursive `ProjectScanner.scan()` | No guard; M4-SEC-06. |
| corpus-audit `repo_path` | filesystem reads and `git -C` | No guard; M4-SEC-08. Git uses argv separation and no shell. |
| `query_type` / params | `query_<type>` method and static Cypher | Unknown methods rejected; inspected Cypher is static with parameters. No Cypher injection confirmed. |
| generic backend operation | `getattr(RuntimeProvider, operation)` | Exact `_BACKEND_METHODS` allowlist prevents method traversal. |
| generic body keys | Python keyword dispatch | Unknown keys raise; M4-SEC-04 then logs the full body. |
| provider base URL | `urlopen(.../models)` | Prefix-based auth suppression; M4-SEC-11. |

No Cypher injection, shell command injection, or arbitrary backend-method traversal was confirmed.

## 9. Information Disclosure Register

| Surface | Minimum reach | Disclosure |
|---|---|---|
| generic backend exception log | tier required for selected operation | complete body + traceback; High M4-SEC-04 |
| `get_provider_config` | readonly | internal Neo4j/provider/backend topology; Medium M4-SEC-09 |
| corpus audit | readonly | arbitrary local worktree commit/corpus metadata; Medium M4-SEC-08 |
| Explorer privacy mode | authenticated remote or direct loopback | noncanonical protected values/log phrases; Medium M4-SEC-07 |
| background warning header/tool suffix | victim's later operation | injected exception/project/path text, max 300 chars; Medium M4-SEC-10 |
| ingest guard rejection | agent path attempt | resolved host path, tier, and control variable |
| ingest result | authorized agent | up to 4,000 characters of selected file content and absolute structure path |
| operator diagnostics | local CLI | bind/auth/OAuth/MCP posture, no raw keys |

**Executed generic-log reproduction:**

```text
propagated: RuntimeError: synthetic backend failure
logger_format: backend_invoke failed: operation=%s body=%r
rendered_log: backend_invoke failed: operation=queue_episode body={'episode': 'TOP SECRET: production signing key is in vault path X', 'diff': "password='super-secret'", 'path': 'C:/Users/alice/private/repo', 'user_id': 'forged-user'}
```

**Executed warning-scope reproduction:**

```text
attacker drain: []
victim drain: ['symbol-rescan secret-project failed: /srv/private/repo']
victim second drain: []
```

## 10. Bug-Class Sweep Results — proving command and output

### Instrument controls

```text
command: python /mnt/data/m4_security_probe_packed.py --self-test-only
exit=0
passed=True
controls=19/19
```

Controls include different-body duplicate definitions, undefined except-handler names, nested-scope blind spots, `CancelledError`, timestamp matching, unused constants across modules, keyword mismatch, redaction, hostname-prefix handling, full-body logs, warning isolation, and backend protocol/client/runtime divergence.

### 1. Duplicate definitions — RUN; no scope duplicate found

```text
same_scope_duplicates=0
protocol_operation_count=78
client_operation_count=78
runtime_operation_count=78
runtime_data_admin_overlap=[]
signature_issues=[]
payload_issues=[]
```

The backend family has 79 expected cross-file same-name collisions when protocol declarations, client wrappers, and runtime implementations are inventoried by body; the actual runtime data/admin mixins overlap on zero method names. No silently shadowed scope definition or competing MRO implementation was found.

### 2. Names used only in except handlers — RUN with Pyflakes

```text
command: PYTHONPATH=/mnt/data/vendor_pyflakes python /mnt/data/m4_security_probe_packed.py --root /mnt/data/menhir-m4-reconstruction --adversarial --pyflakes --json
pyflakes_version=3.4.0
files_passed=23
undefined-name diagnostics=0
```

Pyflakes exited 1 because it reported eight non-security unused/redefinition diagnostics: three unused imports and one unused local in `runtime.py`, plus two unused imports and two local import redefinitions in `backend_runtime_data_ops.py`. It reported no undefined logger or other unbound handler name. The probe's independently controlled lexical scan also returned `except_only_unbound_names=0`.

### 3. `CancelledError` skips cleanup/state reset — RUN; two non-security lifecycle defects confirmed

`_get_services()` exact-source output:

```text
caller_propagated=CancelledError
before_inner_completion: state.init_task_is_inner=True inner_done=False inner_cancelled=False
after_inner_completion:  state.init_task_is_inner=True built_set=True session_set=True
```

The shielded initializer completes, but caller cancellation skips the `except Exception` and post-await cleanup, leaving the completed task referenced (`src/menhir/core/runtime.py:548-573`).

`_shutdown_runtime()` exact-source output:

```text
caller_propagated=CancelledError
calls=['ingest_started', 'state_cleared']
later_cleanup_skipped=True
state_clear_ran=True
```

Cancellation during the first awaited service shutdown escapes the inner `except Exception` and skips recall, Graphiti, Neo4j, and scheduler cleanup; the outer `finally` still clears runtime state (`src/menhir/core/runtime.py:312-384`). No authorization, secret, or security-state consequence was established, so both remain non-security Open Questions.

### 4. Lexicographic timestamp comparison — RUN; candidate class disproved

The controlled detector returned four candidates:

```text
runtime.py:102      report.get('evidence_base_valid') is False
runtime.py:612      _state.startup_runtime_task is current
runtime.py:626      _state.startup_runtime_task is startup_task
runtime_preflight.py:203  _time.monotonic() < deadline
```

None compares ISO/SQLite timestamp text. The only ordered comparison is numeric monotonic time. No mixed `T`/space or offset-string ordering defect was found in scope.

### 5. Module invariant constants — RUN; one confirmed instance

```text
unused_module_constants=[src/menhir/privacy.py:35 STRUCTURAL_FIELDS]
```

The constant is not loaded locally or imported elsewhere. Because it documents which fields may remain visible but the redactor never consults it, this is incorporated into M4-SEC-07 rather than reported as a duplicate finding.

### 6. Keyword-argument mismatch — RUN; candidate class disproved

```text
keyword_mismatch_candidates=0
backend protocol/client/runtime operations=78/78/78
signature_issues=[]
payload_issues=[]
```

Every client wrapper sends one literal operation name and a payload accepted by the runtime method selected at execution. The one private runtime helper is `_background_symbol_rescan`; it is not part of the public protocol.

## 11. Disproved Candidates, with evidence

- **Primary ingest lacks containment:** disproved; both primary methods call the guard before filesystem access (`backend_runtime_data_ops.py:305-319`, `342-360`).
- **Default remote empty-tier bypass:** disproved; nonloopback no-auth startup is rejected unless the explicit insecure override is enabled, and stdio binds operator.
- **Bootstrap writes credential files:** disproved; `bootstrap.py` performs collaborator assembly and schema preparation only.
- **Operator diagnostics returns raw keys:** disproved; it returns presence booleans, not key values.
- **Operator diagnostics is remotely exposed:** disproved for enumerated routes/tools; the traced caller is local CLI.
- **Arbitrary backend method traversal:** disproved; operation allowlisting precedes `getattr()`.
- **Artifact-audit command injection:** disproved; Git invocation uses an argv list with no shell.
- **Structure-query Cypher injection:** disproved; method names resolve to existing `query_*` functions and inspected queries use static Cypher with parameters.
- **Backend duplicate dispatch:** disproved quantitatively across all 78 operations; runtime data/admin overlap is empty.
- **Except-handler unbound logger class:** disproved by Pyflakes and the independently controlled lexical probe.
- **Lexicographic timestamp defect:** disproved after reviewing all four executed candidates.
- **Backend keyword mismatch:** disproved by literal payload-to-runtime reconciliation for all 78 operations.

## 12. Open Questions

- **OPEN — reader receipt isolation:** execute two authenticated principals using `reader_id=default` and determine whether process-global bootstrap receipt state suppresses work across principals.
- **OPEN — provider exception secrecy:** test whether third-party client construction exceptions can include Authorization headers or API keys (`src/menhir/core/bootstrap.py:181-193`, `299-316`).
- **OPEN — request-size ceiling:** verify Starlette/server/reverse-proxy body limits; several remote strings and the generic dictionary have no field-level maximum.
- **OPEN — warning session discoverability:** M4-SEC-10 is confirmed when a target session ID is known; measure how readily session IDs can be derived in each auth mode.
- **OPEN — non-security lifecycle:** `_get_services()` retains the completed shielded initializer after caller cancellation.
- **OPEN — non-security lifecycle:** cancellation during `_shutdown_runtime()` skips later collaborator shutdowns before state is cleared.
- **OPEN — non-security maintainability:** resolve the eight Pyflakes unused/redefinition diagnostics separately; none destroys an exception or changes a security decision.

## 13. Coverage Table — all 23 files and line reconciliation

| # | Scope file | Declared | Measured | Status |
|---:|---|---:|---:|---|
| 1 | `src/menhir/core/backend_client_ops.py` | 703 | 703 | READ |
| 2 | `src/menhir/core/backend_protocol.py` | 683 | 683 | READ |
| 3 | `src/menhir/core/runtime.py` | 646 | 646 | READ |
| 4 | `src/menhir/core/backend_runtime_admin_ops.py` | 603 | 603 | READ |
| 5 | `src/menhir/core/backend_runtime_data_ops.py` | 513 | 513 | READ |
| 6 | `src/menhir/core/runtime_preflight.py` | 456 | 456 | READ |
| 7 | `src/menhir/core/bootstrap.py` | 316 | 316 | READ |
| 8 | `src/menhir/operator_diagnostics.py` | 297 | 297 | READ |
| 9 | `src/menhir/core/runtime_support.py` | 167 | 167 | READ |
| 10 | `src/menhir/privacy.py` | 162 | 162 | READ |
| 11 | `src/menhir/core/backend_shared.py` | 129 | 129 | READ |
| 12 | `src/menhir/core/backend_client.py` | 102 | 102 | READ |
| 13 | `src/menhir/core/request_context.py` | 74 | 74 | READ |
| 14 | `src/menhir/core/ingest_guard.py` | 74 | 74 | READ |
| 15 | `src/menhir/core/backend_runtime.py` | 41 | 41 | READ |
| 16 | `src/menhir/core/backend_impl.py` | 30 | 30 | READ |
| 17 | `src/menhir/core/__init__.py` | 27 | 27 | READ |
| 18 | `src/menhir/core/backend_config.py` | 18 | 18 | READ |
| 19 | `src/menhir/__init__.py` | 16 | 16 | READ |
| 20 | `src/menhir/main.py` | 14 | 14 | READ |
| 21 | `src/menhir/core/backend_runtime_ops.py` | 12 | 12 | READ |
| 22 | `src/menhir/core/reader_identity.py` | 11 | 11 | READ |
| 23 | `src/menhir/__main__.py` | 3 | 3 | READ |
|  | **Totals** | **5,097** | **5,097** | **23/23 READ** |

No unread file inherited coverage. Both `splitlines()` and literal newline totals were 5,097, every declared per-file count matched, and all files parsed successfully.

## 14. What Was Checked, and what could not be verified in this environment

**Checked:** every scope line; exact line reconciliation; REST static/client-token/OAuth/no-auth middleware; generic internal dispatcher; named REST memory route; MCP tier, allowlist, namespace pin, and stdio trust plumbing; Explorer mount and mutations; redaction implementation and display callers; operator diagnostics; startup/preflight/bootstrap; path guards; reader normalization; downstream project scanner, structure-query adapter, artifact corpus scanner, and reconciliation adapter; all 78 backend protocol/client/runtime operations.

**Executed:** 19 controlled probe self-tests; full 23-file AST scan; official Pyflakes 3.4.0; exact redaction nodes; hostname predicate; duplicate static-tier resolution; client-policy parsers; generic full-body failure log; background-warning buckets; `_get_services()` cancellation; `_shutdown_runtime()` cancellation; backend dispatch reconciliation.

**Not verified end to end:** live network exploitation against a running Neo4j/Graphiti deployment; actual reverse-proxy request-size limits; third-party exception secrecy; target-session discoverability; reader receipt cross-principal behavior. The requested `.venv/Scripts/python.exe` was unavailable, so the deterministic source/AST executions used the active Python interpreter against pinned blobs. No executed result is represented as a project-venv result.

## 15. Review Confidence (/100)

**Review confidence: 92/100.** All 23 scope files and relevant transport/downstream context were read, all six requested bug classes were executed with controlled instruments, the backend surface was quantitatively reconciled, and the highest-risk findings have exact call chains or real output. Confidence is reduced only by the unavailable project venv and the remaining live-deployment questions above.
