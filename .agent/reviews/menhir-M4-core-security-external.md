# Menhir M4 — Core Runtime and Backend Security Audit (External)

**Repository:** `Archolith/menhir`  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m4-core-security-external`  
**Scope:** 23 files under `src/menhir/core/` and `src/menhir/` root  
**Measured scope:** **5,097 lines**, exactly reconciled  
**Status:** COMPLETE WITH ENVIRONMENT LIMITATIONS — all scope read; both transports traced; targeted security behavior executed; repository-wide static commands honestly marked `NOT RUN` where the clean checkout/tool was unavailable.

## 1. Executive Summary, highest-risk result first

The audit found **4 High, 6 Medium, and 2 Low** security issues. The highest-risk result is a real transport authorization mismatch: when Explorer is enabled, a readonly-authenticated remote caller—or a direct loopback caller with no credential—can approve or reject candidates even though the canonical backend policy classifies those actions as operator-only.

### M4-SEC-01 — High — Explorer exposes operator candidate decisions to readonly and unauthenticated-loopback callers

The canonical dispatch policy classifies `promote_candidate`, `reject_candidate`, and `approve_candidate` as operator operations (`src/menhir/api/routes_support.py:624-674`). Explorer instead exposes `POST /explorer/candidates/{uuid}/approve` and `/reject` and calls the candidate service directly, with no tier check (`src/menhir/explorer/app.py:832-844`). Explorer is mounted into the live application when enabled (`src/menhir/api/server_support.py:193-221`). Remote readonly credentials pass the authentication middleware, and direct loopback Explorer requests bypass authentication entirely (`src/menhir/api/auth.py:300-386`). A lower tier can therefore make operator-classified memory-governance decisions.

### M4-SEC-02 — High — static-key callers can self-select `client_name` and bypass MCP namespace/tool restrictions

Static bearer mode trusts caller-supplied identity headers and MCP query metadata; `x-menhir-client-name` or `client_name` becomes the bound client name (`src/menhir/api/auth.py:208-286`, `378-411`). MCP namespace pins and tool allowlists are selected solely by that name, and an absent or unconfigured name means unrestricted (`src/menhir/mcp/service_access.py:189-232`). `BaseTool.execute()` correctly enforces the *selected* allowlist and pin (`src/menhir/mcp/contracts.py:282-346`), but a holder of a shared static tier key can select an unconfigured name and evade both controls. OAuth and per-client-token modes derive identity from validated credentials and do not share this defect.

### M4-SEC-03 — High — REST ignores server-configured client namespace pins enforced by MCP

MCP forcibly replaces caller namespace input with `MENHIR_CLIENT_NAMESPACES[client_name]` (`src/menhir/mcp/contracts.py:282-300`). REST `_resolve_namespace()` accepts the body namespace first, then the namespace header, and never consults the authenticated client's configured pin (`src/menhir/api/routes_support.py:128-143`). Core performs no namespace ownership check. A client restricted to one namespace through MCP can reuse its HTTP credential to read or write another namespace through REST. This is a transport-asymmetric privilege escalation wherever namespace pins are relied on for isolation.

### M4-SEC-04 — High — generic backend failures log complete caller bodies without redaction

`backend_invoke_impl()` accepts a generic dictionary and, for every non-preset exception, logs `body=%r` with a traceback before re-raising (`src/menhir/api/routes_handlers.py:199-231`). The operation body can contain complete episodes, diffs, paths, scan dictionaries, and identity values (`src/menhir/api/routes_support.py:544-674`). Executed reproduction printed the full synthetic secret, password, private path, and forged identity into the rendered log. A remote caller able to invoke an operation can therefore cause its sensitive input to reach server logs.

### M4-SEC-05 — Medium — REST permits authenticated callers to forge memory user/session attribution

`MemoryRequest` exposes unconstrained body `user_id` and `session_id` fields (`src/menhir/api/routes_support.py:288-299`). The agent-tier `/api/memory` handler replaces the authenticated caller session whenever either field is supplied and forwards the replacement to `queue_episode()` (`src/menhir/api/routes.py:305-333`). This remains true under OAuth and per-client-token authentication. MCP's corresponding path derives both values from the bound request session (`src/menhir/mcp/tools/ingest/add_memory.py:109-126`). The result is provenance and session forgery, not a demonstrated tier escalation.

### M4-SEC-06 — Medium — agent-reachable compatibility rescan bypasses ingest-root containment

Primary document/project ingestion calls `ensure_ingest_path_allowed()` before touching the filesystem (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). Separately, `write_project_structure()` accepts a caller-provided scan dictionary; if `symbols` is absent, it schedules `_background_symbol_rescan()` on the supplied `root_path` (`src/menhir/core/backend_runtime_data_ops.py:428-443`). The rescan checks only `os.path.isdir()` and invokes `ProjectScanner.scan()` without the guard (`src/menhir/core/backend_runtime_data_ops.py:453-481`). `ProjectScanner` resolves and recursively walks that host directory (`src/menhir/infrastructure/project_scanner.py:211-240`). The generic REST policy exposes `write_project_structure` at agent tier (`src/menhir/api/routes_support.py:544-674`). This restores host-directory scanning outside configured ingest roots, although the immediate output is asynchronous structure ingestion rather than raw file content.

### M4-SEC-07 — Medium — privacy redaction fails open for structured, non-string, and case-variant protected values

`redact_mapping()` performs shallow, case-sensitive key matching, while `redact_text()` masks only non-empty strings (`src/menhir/privacy.py:49-82`). Execution of the exact pinned 162-line file—Git blob `a3b475c0406dd1162cda8feff36c73fbf44ce623`—left nested dictionaries, nested lists, an integer, `None`, an empty string, `Content`, and `SUMMARY` visible. A 10,000-character lowercase protected string was masked. `redact_log_line()` masked a qualifying double-quoted phrase but left a single-quoted contraction, an unquoted phrase, and malformed quoted text visible (`src/menhir/privacy.py:103-162`). Explorer relies on these helpers as a display privacy control (`src/menhir/explorer/app.py:538-583`, `607-650`), so protected content can remain visible in realistic noncanonical row/log shapes.

### M4-SEC-08 — Medium — readonly backend operation scans an arbitrary host Git repository and returns corpus metadata

`fetch_artifact_corpus_audit()` accepts caller-controlled `repo_path` and delegates it without a core path guard (`src/menhir/core/backend_runtime_admin_ops.py:439-458`). It is deliberately absent from the agent/operator tier sets and therefore falls to readonly (`src/menhir/api/routes_support.py:603-674`). The adapter resolves that path, runs a repository audit, and returns commit/cursor evidence, counts, conflicts, and contradictions (`src/menhir/infrastructure/memory_graph_adapter.py:1368-1402`). The scanner runs `git -C <caller path>` with argv separation, reads routed corpus files, hashes them, and extracts titles/status/type/UUID metadata (`src/menhir/infrastructure/artifact_corpus_scanner.py:48-105`, `160-244`). A readonly caller can therefore perform bounded reconnaissance against arbitrary local Git worktrees. Command injection is not present because no shell is used.

### M4-SEC-09 — Medium — readonly callers can retrieve internal provider and topology configuration

`get_provider_config()` returns the Neo4j URI/database, LLM and embedding endpoints, backend URL, provider kinds, and model names (`src/menhir/core/backend_runtime_admin_ops.py:294-319`). It is in the generic backend allowlist but absent from the agent/operator tier sets, so the total policy assigns readonly (`src/menhir/api/routes_support.py:544-674`). Core has no additional gate. This reveals internal service topology and implementation details to the lowest authenticated tier.

### M4-SEC-10 — Medium — payload-controlled session IDs permit cross-session background-warning injection

Background errors are process-global buckets keyed only by a string and are returned verbatim up to 300 characters (`src/menhir/core/backend_shared.py:25-47`). The background write and symbol-rescan paths push errors under the payload-supplied `session_id` (`src/menhir/core/backend_runtime_data_ops.py:389-416`, `430-481`), while the REST dispatcher drains by the authenticated caller's session ID (`src/menhir/api/routes_handlers.py:217-239`). Execution showed that a warning pushed under `victim-session` was invisible to `attacker-session` and appeared on the victim drain. An agent who knows or can derive another session ID can inject attacker-influenced failure text into that caller's later response; the precondition and bounded 300-character channel limit the consequence.

### M4-SEC-11 — Low — provider preflight misclassifies hostname prefixes as loopback and suppresses authorization

`_should_bypass_local_auth()` uses raw string prefixes (`src/menhir/core/runtime_preflight.py:94-96`), and `check_llama_connectivity()` omits its bearer header when that predicate is true (`src/menhir/core/runtime_preflight.py:157-178`). Execution returned `True` for `http://localhost.evil.example/v1`, `http://127.0.0.1.evil.example/v1`, and `http://localhost@evil.example/v1`. The URL is operator-controlled and the defect withholds rather than exposes the credential, so impact is limited.

### M4-SEC-12 — Low — tier checks fail open when request tier is unbound

The request-tier context defaults to `""` (`src/menhir/core/request_context.py:14-20`, `71-74`). `ensure_ingest_path_allowed()` treats an empty tier like operator and skips containment (`src/menhir/core/ingest_guard.py:58-70`); `BaseTool.execute()` rejects insufficient tier only when the current tier is nonempty (`src/menhir/mcp/contracts.py:323-333`). Current authenticated HTTP paths bind a tier, stdio explicitly binds operator, and default nonloopback no-auth startup is rejected, so this is not a default LAN bypass. It remains a fail-open internal boundary that becomes dangerous under the explicit insecure no-auth override or a future unbound entry path.

## 2. Trust Boundary Register — every caller assumption and both transports

| Assumption made by core | REST | MCP | Result |
|---|---|---|---|
| Caller tier authorizes the action. | Generic backend uses a total operation map; Explorer candidate writes bypass it. | `BaseTool` checks `required_tier` when tier is bound. | Violated by M4-SEC-01; empty-tier checks also fail open (M4-SEC-12). |
| Supplied identity belongs to authenticated caller. | `/api/memory` accepts body overrides; static auth trusts identity headers. | Memory ingestion derives user/session from bound context, but static `client_name` remains caller-controlled. | M4-SEC-02 and M4-SEC-05. |
| Namespace belongs to caller. | Body/header namespace accepted; no pin/ownership check. | Configured pin is forced, but unpinned clients have no general ownership check. | M4-SEC-03; neither transport implements universal ownership. |
| Filesystem path is permitted. | Primary ingest is guarded; `write_project_structure` rescan and readonly corpus audit are not. | Public ingest tools delegate to guarded primary methods; no registered public tool was found for `write_project_structure`. | M4-SEC-06 and M4-SEC-08. |
| Input shape is valid. | Named models validate selected fields; internal backend accepts arbitrary dictionaries and passes `**body`. | Tool signatures constrain normal public calls; remote backend JSON remains generic internally. | Malformed calls fail, but M4-SEC-04 logs the complete body. |
| Input size is bounded. | Several numeric limits exist, but episode/diff/source/identity/namespace and generic body values have no field maxima (`src/menhir/api/routes_support.py:274-299`, `544-674`). | Most tool strings/collections have no explicit maxima. | Deployment-level body limit was not verified; retained under Open Questions. |
| Session key isolates background errors. | Producers may use payload session; route drains authenticated session. | Normal tools derive session, then append drained warnings verbatim (`src/menhir/mcp/contracts.py:347-355`). | M4-SEC-10. |
| Privacy helper masks protected data. | Explorer uses the helpers for rows/details. | MCP intentionally returns authorized memory content and has no privacy-display layer. | M4-SEC-07. |

**REST generic chain:** authentication middleware binds tier/session → `/api/internal/backend/{operation}` → total tier map → `RuntimeProvider.<operation>(**body)` (`src/menhir/api/auth.py:300-411`; `src/menhir/api/routes.py:742-759`; `src/menhir/api/routes_handlers.py:199-239`).

**MCP chain:** HTTP middleware or explicit stdio operator binding → FastMCP handler → `BaseTool.execute()` tier/allowlist/pin checks → local `RuntimeProvider` or authenticated `BackendClient` (`src/menhir/mcp/contracts.py:282-367`; `src/menhir/mcp/service_access.py:234-314`).

## 3. Authorization Surface — privileged actions and gates

Core contains no general authorization decision. It reads tier only for the two primary filesystem-ingest methods. The following privileged core functions trust transport authorization:

- memory and namespace mutation: `queue_episode`, `flag_memory`, `unflag_memory`, `promote_memory`, `delete_memory`, `delete_namespace`, `enqueue_pending_episode` (`src/menhir/core/backend_runtime_data_ops.py:24-143`);
- host filesystem and structure: `ingest_document`, `scan_and_write_project`, `write_project_structure`, `_background_symbol_rescan`, `query_structure` (`src/menhir/core/backend_runtime_data_ops.py:305-513`);
- conflict, enrichment, and scheduler controls (`src/menhir/core/backend_runtime_admin_ops.py:25-207`);
- diagnostics/telemetry including `record_conflict_resolution` and provider configuration (`src/menhir/core/backend_runtime_admin_ops.py:209-319`);
- todo, artifact, temporal, and candidate mutations (`src/menhir/core/backend_runtime_admin_ops.py:321-603`).

Representative REST trace: readonly bearer → Explorer candidate approve/reject → service mutation, with no operator gate (M4-SEC-01).  
Representative MCP trace: bound operator → `BaseTool.execute()` → operator endpoint → backend method (`src/menhir/mcp/contracts.py:305-355`).  
Representative internal REST trace: agent bearer → `write_project_structure` → unguarded background rescan (M4-SEC-06).

## 4. Redaction Verification — executed adversarial inputs and real output

**Proving command:**

```text
python <standard-library harness executing the exact pinned privacy.py and preflight predicate>
```

The executed `privacy.py` had 162 lines and Git blob SHA-1 `a3b475c0406dd1162cda8feff36c73fbf44ce623`, matching the pinned GitHub blob.

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

`reveal=True` returned the original object; the 10,000-character lowercase protected string returned `[hidden]`.

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

**Fail-open/closed conclusion:** mapping and log redaction are **fail open outside the narrow canonical case**. Exact lowercase keys with nonempty string values fail closed; structured/non-string/case-variant values and several log syntaxes pass through.

Call-site analysis: Explorer relies on `redact_mapping()` and `redact_rows()` for memory-facing pages, with manual nested handling only for selected `neighbors`, `episodes`, and `nodes` containers (`src/menhir/explorer/app.py:538-583`, `607-650`). The console uses `redact_log_line()` to mask its live server-log tail (`src/menhir/cli/console.py:1-14`, `130-150`). Both callers rely on shapes/syntax the helper does not universally guarantee.

## 5. Diagnostics Exposure — `operator_diagnostics.py` reachability by tier

`build_operator_diagnostics()` reveals bind host/port, loopback classification, effective auth mode, key-presence booleans, insecure override state, OAuth resource/authorization-server posture, consent/proxy checks, MCP backend checks, and warnings (`src/menhir/operator_diagnostics.py:42-297`). It does **not** return raw keys.

The directly traced call site is the local `menhir diagnostics` CLI (`src/menhir/cli/__init__.py:188-260`). No REST route or registered MCP tool calling this function was found in the directly enumerated route/tool modules. Remote exposure of this specific function is therefore disproved for the audited tree. The separate readonly `get_provider_config()` exposure is M4-SEC-09.

## 6. Startup and Credential Handling

`collect_runtime_failures()` checks interpreter, Graphiti, Neo4j/schema, provider compatibility/configuration, and connectivity (`src/menhir/core/runtime_preflight.py:98-456`). Runtime initialization fails closed only for interpreter or Neo4j failure; other failed checks produce degraded startup and continue (`src/menhir/core/runtime.py:442-469`). Core preflight does not enforce transport bind/auth safety; settings construction does that separately.

`bootstrap.py` writes no credential file, so there is no bootstrap-created credential mode to report. It passes Neo4j credentials and provider configuration into repositories/clients (`src/menhir/core/bootstrap.py:160-193`). Adapter-construction and edge-sync exceptions are logged or returned verbatim (`src/menhir/core/bootstrap.py:181-193`, `299-316`). No scope statement deliberately prints a raw key; whether third-party exception strings can contain credentials remains an Open Question.

The local-provider hostname predicate was executed separately; M4-SEC-11 records the result.

## 7. Guard and Identity Analysis

`allowed_ingest_roots()` resolves configured roots and falls back to resolved current working directory when none survive (`src/menhir/core/ingest_guard.py:31-50`). `_is_within()` compares resolved paths (`src/menhir/core/ingest_guard.py:53-54`). The guard allows operator and empty tier without containment and includes the resolved attempted path, tier, and environment-variable name in rejection text (`src/menhir/core/ingest_guard.py:58-74`). Primary ingest paths are guarded; M4-SEC-06 and M4-SEC-08 identify unguarded alternatives.

`normalize_reader_id()` strips input and maps `None`, empty, and whitespace-only values to shared literal `default` (`src/menhir/core/reader_identity.py:4-8`). Bootstrap receipt state is process-global and keyed by that normalized ID plus workspace selection (`src/menhir/core/runtime_support.py:141-167`). Cross-principal consequences of sharing `default` were not executed and remain an Open Question.

## 8. Injection and Traversal Register

| Input | Sink | Result |
|---|---|---|
| primary ingest path | file read / project scan | Resolved-path containment exists; empty tier fails open (`src/menhir/core/ingest_guard.py:53-74`). |
| compatibility `scan.root_path` | recursive `ProjectScanner.scan()` | No guard; M4-SEC-06. |
| corpus-audit `repo_path` | filesystem reads and `git -C` | No guard; readonly reconnaissance M4-SEC-08. Git argv is separated and no shell is used (`src/menhir/infrastructure/artifact_corpus_scanner.py:48-63`). |
| `query_type` / params | `query_<type>` method and static Cypher methods | Unknown names are rejected; caller values are passed as query parameters. No arbitrary Cypher text injection was found (`src/menhir/infrastructure/memory_graph_adapter.py:1065-1074`; `src/menhir/infrastructure/structure_queries.py:1-90`, `380-900`). |
| generic backend operation | `getattr(RuntimeProvider, operation)` | Exact `_BACKEND_METHODS` allowlist prevents method traversal (`src/menhir/api/routes_handlers.py:213-225`). |
| generic body keys | Python keyword dispatch | Unknown keywords raise rather than execute code; M4-SEC-04 then logs the full body. |
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
| ingest guard rejection | agent path attempt | resolved host path, tier, and control variable (`src/menhir/core/ingest_guard.py:71-74`) |
| ingest result | authorized agent | up to 4,000 characters of selected file content and absolute structure path (`src/menhir/core/backend_runtime_data_ops.py:319-339`) |
| operator diagnostics | local CLI | bind/auth/OAuth/MCP posture, no raw keys |

**Executed generic-log reproduction:**

```text
propagated: RuntimeError: synthetic backend failure
logger_format: backend_invoke failed: operation=%s body=%r
logger_args: ('queue_episode', {'episode': 'TOP SECRET: production signing key is in vault path X', 'diff': "password='super-secret'", 'path': 'C:/Users/alice/private/repo', 'user_id': 'forged-user'})
rendered_log: backend_invoke failed: operation=queue_episode body={'episode': 'TOP SECRET: production signing key is in vault path X', 'diff': "password='super-secret'", 'path': 'C:/Users/alice/private/repo', 'user_id': 'forged-user'}
```

**Executed warning-scope reproduction:**

```text
warning attacker drain: []
warning victim drain: ['symbol-rescan secret-project failed: /srv/private/repo']
warning victim second drain: []
```

## 10. Bug-Class Sweep Results — command and output, or `NOT RUN`

The probe's synthetic control suite passed before its heuristics were trusted:

```text
command: python /mnt/data/m4_security_probe.py --self-test-only
exit=0
passed=True
duplicate_body_difference=True
except_only_unbound=True
cancelled_error_candidate=True
timestamp_candidate=True
unused_constant=True
keyword_mismatch=True
nested_redaction_leak=True
host_prefix_bypass=True
full_body_logged=True
cross_session_warning=True
```

The active container did not contain the repository checkout or requested project venv, and direct clone/archive networking was unavailable. The probe proved this rather than returning an empty scan:

```text
command: python /mnt/data/m4_security_probe.py --root . --pyflakes --json
exit=1
missing_count=23
missing_files=[all 23 declared scope paths]

command: .venv/Scripts/python.exe --version
output: .venv/Scripts/python.exe: NOT FOUND

command: python -m pyflakes --version
exit=1
stderr: /opt/pyvenv/bin/python: No module named pyflakes
```

Accordingly:

1. **Duplicate definitions — NOT RUN against the pinned checkout.** Body-comparison control passed. Full scope was manually read and no duplicate is promoted as a finding, but no executed negative count is claimed.
2. **Except-only unbound names — NOT RUN against the pinned checkout.** `pyflakes` was unavailable and the requested venv was absent. Undefined-name control passed in the probe; no executed repository result is claimed.
3. **`CancelledError` cleanup — EXECUTED candidate.** Exact `_get_services()` control flow was reproduced. Output:

   ```text
   caller: CancelledError propagated
   after caller cancellation: state.init_task_is_inner=True
   after caller cancellation: inner_done=False inner_cancelled=False
   after inner completion: state.init_task_is_inner=True
   after inner completion: built_set=True session_set=True
   ```

   This confirms cancellation skips the `Exception` cleanup and leaves the shielded task referenced (`src/menhir/core/runtime.py:549-572`). No security consequence was established; it is listed under Open Questions as a non-security lifecycle issue.
4. **Lexicographic timestamp comparison — NOT RUN against the pinned checkout.** Synthetic detector control passed; manual reading found no confirmed scope instance, but no executed negative is claimed.
5. **Unread invariant constants — NOT RUN against the pinned checkout.** Synthetic control passed; manual reading found no confirmed scope instance, but no executed negative is claimed.
6. **Keyword mismatch — NOT RUN against the pinned checkout.** Synthetic cross-file mismatch control passed; manual reading found no confirmed scope instance, but no executed negative is claimed.

## 11. Disproved Candidates

- **Primary ingest lacks containment — disproved:** both primary methods call the guard before their filesystem sink (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`).
- **Default remote empty-tier bypass — disproved:** nonloopback no-auth startup is rejected unless the explicit insecure override is enabled, and stdio binds operator. M4-SEC-12 is defense in depth, not a default LAN exploit.
- **Bootstrap writes credential files — disproved:** `bootstrap.py` performs collaborator assembly and schema preparation only (`src/menhir/core/bootstrap.py:1-316`).
- **Operator diagnostics returns raw keys — disproved:** it returns presence booleans, not key values (`src/menhir/operator_diagnostics.py:50-57`, `278-286`).
- **Operator diagnostics is remotely exposed — disproved for directly enumerated routes/tools:** the traced call site is local CLI (`src/menhir/cli/__init__.py:188-260`).
- **Arbitrary backend method traversal — disproved:** operation allowlisting precedes `getattr()` (`src/menhir/api/routes_handlers.py:213-225`).
- **Artifact-audit command injection — disproved:** Git invocation uses an argv list with no shell (`src/menhir/infrastructure/artifact_corpus_scanner.py:48-63`).
- **Structure-query Cypher injection — disproved:** method names must resolve to existing `query_*` functions and inspected queries use static Cypher plus parameters (`src/menhir/infrastructure/memory_graph_adapter.py:1065-1074`; `src/menhir/infrastructure/structure_queries.py:1-90`, `380-900`).

## 12. Open Questions

- **OPEN — reader receipt isolation:** execute two authenticated principals using `reader_id=default` and determine whether process-global bootstrap receipt state suppresses work across principals.
- **OPEN — provider exception secrecy:** third-party client construction exceptions are logged verbatim; test whether this stack can include Authorization headers or API keys in exception strings (`src/menhir/core/bootstrap.py:181-193`, `299-316`).
- **OPEN — request-size ceiling:** verify Starlette/server/proxy body limits. Several remotely supplied strings and the generic backend dictionary have no field-level maximum.
- **OPEN — warning session discoverability:** M4-SEC-10 is confirmed when a target session ID is known; measure how readily session IDs can be derived or observed in each auth mode.
- **OPEN — non-security:** `_get_services()` cancellation leaves the completed shielded task referenced until a later call (`src/menhir/core/runtime.py:549-572`).
- **OPEN — non-security:** stdio startup records general bootstrap failure and continues lifespan entry (`src/menhir/core/runtime.py:574-610`).

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

No unread file inherited coverage. EOF bounds were independently checked during the checkpoint commits, and the total reconciles exactly.

## 14. What Was Checked, and what could not be verified

**Checked:** every scope line; exact coverage total; REST static/client-token/OAuth/no-auth middleware; generic internal dispatcher; named REST memory path; MCP tier, allowlist, namespace-pin, and stdio-trust plumbing; Explorer mounting and mutation routes; redaction implementation and call sites; operator diagnostics; startup/preflight/bootstrap; path guards; reader normalization; downstream project scanner, structure-query adapter, artifact corpus scanner, and reconciliation adapter.

**Executed:** redaction adversarial matrix; hostname predicate; generic full-body failure log; background-warning scope behavior; `CancelledError` cleanup behavior; synthetic controls for all six requested bug classes and the targeted security probes.

**Could not be verified in this environment:** repository-wide probe execution, pyflakes, and exact negative counts for duplicate definitions, timestamp comparisons, unread constants, and keyword mismatches. The environment had authenticated GitHub read/write access but no materialized checkout, no usable archive download, no `.venv/Scripts/python.exe`, and no installed pyflakes. These are reported as `NOT RUN`, not inferred passes. GitHub code search also failed its control test by returning no result for a visibly defined symbol, so absence conclusions were based on direct tree/file enumeration rather than that index.

## 15. Review Confidence (/100)

**Review confidence: 83/100.** All 23 scope files and the relevant transport/downstream context were directly read; the highest-risk findings have exact call chains, and five behavioral issues have executed reproductions. Confidence is reduced by the unavailable clean-checkout static sweeps and pyflakes, so the report does not claim negative results for those classes.
