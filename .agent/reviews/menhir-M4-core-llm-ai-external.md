# Menhir M4 — LLM & AI Security Audit (A7)

**Repository:** `Archolith/menhir`  
**Source branch:** `main`  
**Source commit audited:** `25c2b62109219354dd88c0233c665d2a5ff5431d`  
**Scope:** exactly the 18 files under `src/menhir/core/` plus `src/menhir/{__init__,__main__,main,operator_diagnostics,privacy}.py`  
**Dynamic AI-security tooling:** Garak — **NOT RUN — no execution environment**; DeepTeam — **NOT RUN — no execution environment**; Promptfoo — **NOT RUN — no execution environment**.

`wc -l` was **NOT RUN**. There was no local checkout available to the shell because `github.com` could not be resolved from that environment. I measured the pinned source another way: every one of the 23 named files was read from GitHub at the exact commit above, and its EOF was independently established with line-bounded `fetch_file` probes. That measurement is **5,077 physical lines: 4,585 core + 492 root-level**, not the supplied 5,097. The supplied total is therefore 20 lines higher than the pinned source I could measure. I do not rewrite the measured result to make it match the brief.

Out-of-scope files were opened only where necessary to prove a reachability edge, default, display sink, or server-side dispatch behavior. They are identified as supporting trace evidence below and are not included in coverage totals.

## Executive Summary

This pass found **three findings: one Medium and two Low**. There are no Critical or High findings in this slice after excluding the already-confirmed items named in the brief.

The most consequential issue is not a model-prompt bug inside `privacy.py`; it is a persistence-boundary gap at the remote backend seam. `BackendClient.queue_episode()` places raw memory text into a JSON request body (`src/menhir/core/backend_client_ops.py:25-31`), `_request()` sends that body to the internal backend route (`src/menhir/core/backend_client.py:75-80`), and the server dispatcher logs the entire body with `%r` whenever an operation raises (`src/menhir/api/routes_handlers.py:223-227`, supporting trace outside scope). ERROR records are enabled under the normal INFO logging threshold and Menhir configures a rotating `server.log` file (`src/menhir/infrastructure/logging_config.py:11`, `src/menhir/infrastructure/logging_config.py:132-140`, supporting trace). `privacy.py` does not sit on this write boundary, so turning privacy mode on cannot prevent the raw body from first being persisted. This is C-1 (Medium).

`privacy.py` itself is a display-time masking module, not PII detection and not a model-input/model-output filter. It masks selected mapping keys and heuristically masks quoted free-text fragments in rendered log lines. That heuristic has a concrete bypass: an existing correlation-judge DEBUG record formats stored entity names through unquoted `%s` fields (`src/menhir/services/correlation_service.py:563-566`, supporting trace). The console then passes the already-rendered line into `redact_log_line()` (`src/menhir/cli/console.py:135-138`, supporting trace), while `redact_log_line()` ultimately substitutes only `_QUOTED` matches (`src/menhir/privacy.py:138-162`). With DEBUG logging enabled and privacy mode enabled, the stored names remain visible. This is C-2 (Low).

`operator_diagnostics.py` does **not** read graph records, prompts, model responses, or raw bearer-key values. It reports configuration/auth posture and key-presence booleans. One credential-shaped configuration value can nevertheless escape: its MCP backend diagnostic delegates URL sanitization to a helper that removes only `user:pass@` userinfo and returns the URL unchanged when userinfo is absent (`src/menhir/mcp/service_access.py:81-96`, supporting trace). Thus a configured backend URL such as `https://host/path?token=<secret>` or a secret-bearing fragment is returned and printed verbatim. This is C-3 (Low).

For backend parity, I compared `backend_protocol.py`, `backend_client.py`, `backend_client_ops.py`, `backend_runtime.py`, `backend_runtime_data_ops.py`, `backend_runtime_admin_ops.py`, and `backend_runtime_ops.py` method-by-method. I found **no additional wire parameter-name mismatch** beyond the excluded CF-65 `supersede_artifact` issue. The remote path does, however, have two trust differences worth recording: it accepts successful JSON without semantic response-schema validation, and its identity headers come from configured MCP client settings rather than the live in-process `caller_session`. Neither produced a second proven security effect in this pass, so both remain parity observations/Open Questions rather than findings.

## Findings

### Medium

### C-1 — Remote backend exceptions persist raw memory/request bodies to ordinary server logs

**Severity: Medium.**

**Primary in-scope evidence:**

- `src/menhir/core/backend_client_ops.py:25-31` — `queue_episode()` constructs the remote payload; line 28 places the caller-controlled `text` value directly under the `text` key.
- `src/menhir/core/backend_client.py:75-80` — `_request()` POSTs the payload as JSON to `/api/internal/backend/{operation}`.
- `src/menhir/core/privacy.py:138-162` — the log redactor operates on an already-created display string and does not participate in backend request serialization or log-file writes.

**Reachability-supporting evidence outside the 23-file coverage corpus:**

- `src/menhir/config/settings_model.py:386-390` — backend-client mode is configuration-gated; `backend_url` defaults to the empty string at line 387.
- `src/menhir/api/routes.py:745-758` — the internal backend route dispatches the body into `backend_invoke_impl()`.
- `src/menhir/api/routes_handlers.py:223-227` — all non-`InvalidQueryPresetError` exceptions hit `logger.exception("backend_invoke failed: operation=%s body=%r", operation, body)` at line 226 before being re-raised.
- `src/menhir/infrastructure/logging_config.py:11` — normal logging threshold is INFO.
- `src/menhir/infrastructure/logging_config.py:132-140` — the normal application file handler is assigned `server.log`; the root level is set from that normal threshold.

**Concrete triggering payload shape:** a backend-client `queue_episode` call containing memory/user/model-derived text in `text`, for example a normal non-empty episode string. The same persistence risk applies to other remote methods whose bodies contain free text, but `queue_episode(text=...)` is sufficient to prove the mechanism.

**Reachability chain:**

1. **Entry point:** a caller reaches `BackendClient.queue_episode(text=...)` in backend-client mode.
2. **Gate and default:** backend-client mode requires a non-empty `backend_url`; the default is empty (`settings_model.py:387`), so this path is not the default in-process deployment.
3. **Transport:** the raw `text` is put in the JSON body (`backend_client_ops.py:28`) and sent to the internal route (`backend_client.py:76-80`). The bearer credential is a header, not part of this body; this finding does **not** claim the Authorization value is logged by this sink.
4. **Failure gate:** the invoked backend operation raises an exception other than the separately handled invalid-preset error.
5. **Effect:** the generic exception handler emits the entire request body with `%r` at ERROR level (`routes_handlers.py:226`). ERROR is above the normal INFO threshold, and Menhir's logging configuration writes normal application logs to `server.log`; the raw request body therefore becomes persistent log content.
6. **Privacy consequence:** enabling `privacy_redact` can affect later display of a log line, but the persistent log write has already happened. `privacy.py` is not on the persistence boundary.

**Why Medium:** this can persist user/model/memory content in operational logs without requiring DEBUG mode and can broaden retention/access beyond the graph itself. Exploitation requires the remote-backend deployment mode plus an operation failure and access to the resulting logs; I found no unauthenticated remote exfiltration from this issue alone, so High would overstate it.

**Recommendation:** never log backend request bodies wholesale. Log an operation name, request/correlation ID, selected structural identifiers, and bounded field lengths instead. If any payload fields must be retained for diagnostics, classify/sanitize them before the logging call at the persistence boundary rather than relying on a later viewer. Add a regression test that sends a unique canary in `queue_episode.text`, forces backend dispatch to raise, and proves the canary is absent from all configured log files.

### Low

### C-2 — Privacy-mode log redaction does not mask unquoted stored free text

**Severity: Low.**

**Primary in-scope evidence:**

- `src/menhir/privacy.py:138-162` — `redact_log_line()` preserves the line body and finishes by applying `_QUOTED.sub(...)` only. Free text that was formatted into the log without quote characters is not considered by this substitution.

**Reachability-supporting evidence outside coverage:**

- `src/menhir/config/settings_model.py:347-352` — `privacy_redact` exists and defaults to `False` at line 352.
- `src/menhir/cli/console.py:135-138` — the console takes each already-rendered log line and invokes `redact_log_line(ln, reveal=not redact)` at line 137.
- `src/menhir/services/correlation_service.py:563-566` — the correlation LLM judge DEBUG record formats `meta_a["name"]` and `meta_b["name"]` into `%s` placeholders without surrounding quotes.
- `src/menhir/infrastructure/logging_config.py:11` — default logging is INFO, so the demonstrated correlation record requires non-default DEBUG logging.

**Concrete triggering payload shape:** a stored entity name containing human-readable text, for example a multi-word name, which reaches either of the `%s` name fields in the correlation judge DEBUG record. The formatted line contains the name directly, not as a quoted string.

**Reachability chain:**

1. **Entry point:** the operator runs the live console and enables privacy redaction.
2. **Gate and default:** privacy redaction defaults off (`settings_model.py:352`) and must be enabled; the demonstrated producer is DEBUG while the normal logging threshold is INFO, so DEBUG must also be enabled.
3. **Stored-content source:** the correlation judge reads stored merge metadata names and logs them with unquoted `%s` formatting (`correlation_service.py:563-566`).
4. **Display path:** the console passes that completed string to `redact_log_line()` (`cli/console.py:137`).
5. **Effect:** because `redact_log_line()` substitutes only quoted substrings (`privacy.py:162`), the unquoted names are displayed unchanged despite privacy mode.

**Why Low:** the privacy control fails its stated operator-display purpose for a proven stored-content log shape, but the demonstrated producer requires non-default DEBUG logging, privacy mode is itself opt-in by default, and the effect is local/operator display rather than remote disclosure.

**Recommendation:** do not try to infer sensitivity from the final rendered log string. Prefer structured logs where fields that can hold memory/model content are marked and removed before rendering. If the string-level helper must remain, known memory-bearing log events should be replaced wholesale or passed through a field-aware formatter; matching only quoted substrings cannot provide a dependable privacy boundary.

### C-3 — Diagnostics URL redaction preserves query/fragment credentials

**Severity: Low.**

**Primary in-scope evidence:**

- `src/menhir/operator_diagnostics.py:255-270` — the operator snapshot calls `build_mcp_backend_diagnostics(settings)` at line 268 and includes the resulting block in its output.

**Reachability-supporting evidence outside coverage:**

- `src/menhir/config/settings_model.py:386-390` — `backend_url` is an arbitrary string setting and defaults empty at line 387.
- `src/menhir/mcp/service_access.py:81-96` — `redact_url_for_diagnostics()` strips only URL userinfo. If `parsed.username` and `parsed.password` are absent, line 94 returns the raw URL; its exception path also returns raw at line 96.
- `src/menhir/mcp/service_access.py:117-124` — the returned diagnostics block places that sanitizer output in `backend_url` at line 121 while reporting bearer keys only as booleans.
- `src/menhir/cli/__init__.py:264-268` — the diagnostics command prints `mcp['backend_url']` at line 267.

**Concrete triggering payload shape:** `MENHIR_BACKEND_URL=https://backend.example/path?token=<secret>` (or a secret-bearing URL fragment) with no `user:pass@` userinfo component.

**Reachability chain:**

1. **Entry point:** an operator runs `menhir diagnostics` (plain-text or JSON output).
2. **Gate and default:** `backend_url` must be configured; it defaults to empty.
3. **Sanitizer:** `redact_url_for_diagnostics()` parses the URL, sees no username/password, and returns the original string unchanged (`service_access.py:87-96`). Query parameters and fragments are never removed.
4. **Snapshot:** `operator_diagnostics.py:268` incorporates the MCP diagnostic block.
5. **Effect:** the diagnostic output contains the query/fragment secret verbatim; the normal CLI prints the URL at `cli/__init__.py:267`, and JSON mode serializes the same snapshot.

**Why Low:** this requires an operator to put a credential into a URL-shaped configuration value and then explicitly emit diagnostics. It does not expose Menhir's normal bearer-key settings, and I found no stored-memory or prompt content in the diagnostic snapshot. The realistic harm is accidental disclosure when diagnostics are copied into tickets, chat, or logs.

**Recommendation:** for diagnostics, discard URL query and fragment components entirely unless an allowlisted diagnostic need requires them. Redact userinfo as today, but return only scheme/host/port and a safe path. Apply the same rule consistently to other diagnostic URL helpers.

## `privacy.py` Inventory

`src/menhir/privacy.py` is **not** a PII recognizer and **not** a model-boundary filter. Its functions operate on already-materialized values:

- `redact_text()` masks a non-empty string with the constant `[hidden]` unless `reveal=True` (`privacy.py:55-65`).
- `redact_mapping()` makes a shallow copy and masks keys in a fixed configured field set (`privacy.py:68-87`). The default set is content-oriented fields such as `content`, `summary`, `preview`, `notes`, `name`, and `label` (`privacy.py:22-33`). It does not inspect arbitrary nested structures for PII.
- `redact_rows()` applies that mapping operation across rows (`privacy.py:90-100`).
- `redact_log_line()` is a best-effort string renderer that masks only quoted fragments that pass `_is_free_text`; the final operation is `_QUOTED.sub(...)` (`privacy.py:128-162`). C-2 shows a concrete unquoted bypass.

**What calls it / whether it is in force:** the concrete console call site is `src/menhir/cli/console.py:137`, after the console has already read a line from the log file. The explorer also imports and uses `redact_mapping`/`redact_rows` on rendered graph/UI data; those call sites are outside this 23-file coverage corpus and were used only to establish that the module is live rather than dead. The baseline setting is `privacy_redact=False` (`src/menhir/config/settings_model.py:352`, supporting trace), so privacy display masking is opt-in by default.

**Model I/O:** none of the `privacy.py` functions construct prompts, call an LLM, parse model output, or wrap an LLM transport. The concrete usages traced are human-facing display paths. Therefore this module does not prevent memory content from being sent to a model and does not redact model responses before persistence. Its security value is presentation privacy only.

**Persistence:** the module is not in force at the raw logging boundary. C-1 demonstrates a persistent log write that occurs before any console display redaction.

## Diagnostics Exposure Trace

`build_operator_diagnostics(settings)` (`src/menhir/operator_diagnostics.py:38-297`) is a configuration/auth-posture snapshot. It does not obtain a runtime backend, graph adapter, recall result, prompt, model client, or model response. The values it directly derives include bind host/port, loopback status, effective auth mode, boolean key-presence indicators, safety warnings, OAuth preflight output, and MCP backend diagnostics.

The raw bearer-key fields are converted to booleans near the start of the builder (`operator_diagnostics.py:49-57`) and only those booleans are placed under the returned `auth` object (`operator_diagnostics.py:277-285`). I found no path from stored graph content or prompt/model-response text into this snapshot.

The one concrete credential exposure is C-3: the MCP backend URL sanitizer handles URL userinfo but preserves query strings and fragments. This is a configuration-secret exposure, not stored-memory exposure.

OAuth preflight was followed outside scope to ensure the diagnostic aggregation was not hiding a second obvious raw-token path. Its diagnostic helper similarly reasons over configured URLs and counts rather than bearer token values; no runtime access-token value is placed in the operator snapshot. URL sanitization there is also userinfo-oriented, so the recommendation from C-3 should be applied consistently to diagnostic URLs rather than treating query/fragment components as inherently safe.

## Backend Seam Parity

### Method/payload parity

`MemoryBackend` in `src/menhir/core/backend_protocol.py` is implemented by two compositions:

- `RuntimeProvider` (`src/menhir/core/backend_runtime.py:13-41`) uses the in-process data/admin operation mixins.
- `BackendClient` uses `BackendClientOpsMixin` and serializes method arguments to the internal HTTP endpoint (`src/menhir/core/backend_client.py:70-102`; `src/menhir/core/backend_client_ops.py`).

I compared the client payload keys against the protocol and the live `RuntimeProviderDataOpsMixin`/`RuntimeProviderAdminOpsMixin` signatures. **No additional parameter-name mismatch was found** beyond CF-65, which the brief explicitly excludes. I did not re-file CF-65.

### Identity/context parity

The in-process provider explicitly stores a `caller_session` and prefers it over the process session when deriving an effective session ID (`src/menhir/core/backend_runtime.py:16-25`, `src/menhir/core/backend_runtime.py:39-41`). The remote client's request headers instead come from configured MCP client settings: `mcp_client_user_id`, `mcp_client_id`, and `mcp_client_name` (`src/menhir/core/backend_client.py:47-61`). `BackendClient` does not read the live request-context object when it builds those headers.

That is a real behavioral difference: remote calls are represented at the backend as the configured service/client identity rather than by forwarding the in-process `caller_session` object. I did **not** promote this to a finding because this pass did not establish a concrete tier bypass or cross-user read/write caused by that difference; front-side MCP tier enforcement may intentionally make the backend connection a service identity. It is recorded under Open Questions because attribution, per-reader receipts, or any backend operation that derives behavior from caller session can differ between deployment modes even if the payload keys match.

### Response trust parity

The remote path performs HTTP status handling and JSON parsing but no per-operation semantic response validation. `_request()` calls `raise_for_status()` and then returns `response.json()` if content exists (`src/menhir/core/backend_client.py:81-102`). Individual client methods sometimes cast the returned value with `bool(...)` or `str(...)`, while others return dictionaries/lists directly. In-process calls receive native values from the local services instead of crossing this JSON trust boundary.

A malformed but syntactically valid response can therefore be normalized into a misleading type (for example a non-empty JSON string where a boolean was expected is truthy). I found no pinned server path that actually emits such a malformed success response, and no evidence that an attacker controls the configured backend in the intended trust model. This remains an Open Question rather than a finding.

### Error/warning parity

The remote exception path has the C-1 persistence difference: the wire dispatcher logs raw request bodies on failure, whereas an equivalent in-process call does not traverse that HTTP body logger.

`backend_shared.py` also maintains a bounded background-error/warning channel (`src/menhir/core/backend_shared.py:27-72`): server-side strings are truncated to 300 characters, transported to the remote client, and later surfaced as warnings. The strings are not privacy-redacted in that shared helper. I did not find a proven producer in this pass whose exception text necessarily embeds stored/model content, so I did not file a speculative warning-channel disclosure.

## Disproved Candidates

### `privacy.py` is a model-input/model-output privacy control

**Disproved.** Its code masks already-materialized mapping fields and rendered log strings (`privacy.py:55-100`, `privacy.py:138-162`). The traced call sites are display/UI paths, not LLM adapters or prompt construction. It neither blocks content before a model call nor sanitizes model output before persistence.

### `privacy.py` is unused/dead

**Disproved.** The live console invokes `redact_log_line()` (`src/menhir/cli/console.py:137`, supporting trace), and the explorer uses the row/mapping helpers. The problem is limited enforcement/default-off behavior and a heuristic bypass, not dead code.

### `operator_diagnostics.py` renders stored graph content or prompts

**Disproved.** The builder only consumes `MemorySettings` and configuration-diagnostic helpers (`operator_diagnostics.py:38-297`). It does not open a graph repository, obtain a backend, call recall, or invoke an LLM. No stored memory body, prompt, or model response reaches the snapshot in the traced path.

### `operator_diagnostics.py` emits configured bearer-key values

**Disproved for the normal bearer-key settings.** `api_key`/agent/read-only/operator key material is reduced to presence booleans before output (`operator_diagnostics.py:49-57`, `operator_diagnostics.py:277-285`). C-3 is narrower: a secret manually embedded in a diagnostic URL query/fragment is not stripped.

### A second CF-65-style wire-key mismatch exists in this seam

**Disproved by the static comparison performed for this pass.** Client payload keys were compared against the protocol and live in-process method signatures across `backend_client_ops.py`, `backend_protocol.py`, `backend_runtime_data_ops.py`, and `backend_runtime_admin_ops.py`. No second mismatch was found. CF-65 itself was excluded and not re-filed.

### C-1 also logs the backend bearer credential

**Disproved for the identified sink.** `_request()` sends credentials through HTTP headers (`backend_client.py:47-61`) and sends the operation data separately as JSON (`backend_client.py:75-80`). The failing server log statement prints the body, not request headers (`routes_handlers.py:226`). C-1 is a memory/request-content disclosure, not evidence of Authorization-header leakage.

## Open Questions

1. **Remote caller identity semantics.** `RuntimeProvider` can use a live `caller_session`, while `BackendClient` emits configured client identity headers. Confirm the intended invariant for multi-user/remote MCP deployments: should the backend see the frontend service identity, the original reader identity, or both? If original-reader attribution is required, add an authenticated/verified propagation mechanism rather than self-declared headers.

2. **Semantic response validation.** The HTTP client validates transport status and JSON syntax, not operation-specific schemas (`backend_client.py:81-102`). If backend version skew, proxy corruption, or a compromised backend is within the threat model, define typed response contracts and reject wrong shapes rather than coercing them. No malformed successful response was observed in the pinned implementation, so this is not a finding.

3. **Background warning content.** `_push_background_error()` stores up to 300 characters of an arbitrary error string without privacy classification (`backend_shared.py:35-42`). Trace every producer that converts exceptions to these strings and determine whether model/provider errors can include prompt fragments, memory text, URLs with credentials, or response bodies. I found the transport mechanism but not a concrete sensitive producer in this pass.

4. **Diagnostic URL policy beyond MCP backend URL.** C-3 proves query/fragment retention for the MCP backend URL. OAuth preflight uses a similar userinfo-oriented URL-redaction strategy outside this scope. Decide whether diagnostics should universally reduce URLs to scheme/host/port/safe path and drop query/fragment components.

## Coverage Table

### Group A — all 18 files in `src/menhir/core/`

| File | Measured lines | A7 coverage focus |
|---|---:|---|
| `src/menhir/core/__init__.py` | 27 | exports/bootstrap surface |
| `src/menhir/core/backend_client.py` | 102 | HTTP transport, auth/identity headers, response trust |
| `src/menhir/core/backend_client_ops.py` | 703 | wire payload construction, per-operation return coercion |
| `src/menhir/core/backend_config.py` | 18 | backend bearer-key resolution |
| `src/menhir/core/backend_impl.py` | 30 | compatibility/re-export seam |
| `src/menhir/core/backend_protocol.py` | 683 | authoritative backend method contract |
| `src/menhir/core/backend_runtime.py` | 41 | in-process provider/caller-session behavior |
| `src/menhir/core/backend_runtime_admin_ops.py` | 589 | live admin operation signatures/returns |
| `src/menhir/core/backend_runtime_data_ops.py` | 513 | live data operation signatures/returns |
| `src/menhir/core/backend_runtime_ops.py` | 12 | in-process mixin composition |
| `src/menhir/core/backend_shared.py` | 129 | JSON conversion, background warning/error channel |
| `src/menhir/core/bootstrap.py` | 316 | service construction and degraded startup handling |
| `src/menhir/core/ingest_guard.py` | 74 | ingest-path gate |
| `src/menhir/core/reader_identity.py` | 11 | reader-ID normalization |
| `src/menhir/core/request_context.py` | 68 | request session/tier/auth-mode context |
| `src/menhir/core/runtime.py` | 646 | lifecycle/runtime construction; excluded known items respected |
| `src/menhir/core/runtime_preflight.py` | 456 | provider/connectivity preflight; CF-86 not re-filed |
| `src/menhir/core/runtime_support.py` | 167 | runtime state/helpers; CF-82 not re-filed |
| **Core subtotal** | **4,585** | |

### Group B — five root-level files

| File | Measured lines | A7 coverage focus |
|---|---:|---|
| `src/menhir/__init__.py` | 16 | package entry metadata |
| `src/menhir/__main__.py` | 3 | CLI entry |
| `src/menhir/main.py` | 14 | CLI launch |
| `src/menhir/operator_diagnostics.py` | 297 | diagnostics content/credential trace |
| `src/menhir/privacy.py` | 162 | masking/redaction behavior and enforcement |
| **Root subtotal** | **492** | |

### Reconciliation

**Measured corpus:** exactly the 23 files listed above at commit `25c2b62109219354dd88c0233c665d2a5ff5431d`.

- Core: **4,585**
- Root: **492**
- **Measured total: 5,077**
- Supplied brief total: **5,097**
- Difference: **20 lines**

The 18-file core directory list and all five named root files were individually present and measured. A second EOF-boundary pass rechecked the large/suspicious tails, including `runtime.py`, `backend_client_ops.py`, `operator_diagnostics.py`, `privacy.py`, and `main.py`, and did not move the measurement. Because `wc -l` could not be run against a local checkout, I cannot independently explain the 20-line discrepancy; I can only report that the pinned GitHub source available to this review measures 5,077 by the stated method.

### Explicit exclusions honored

- CF-65 (`supersede_artifact` wire payload keys) — not re-filed.
- CF-82 (`_state = RuntimeState()` module singleton / injection seam) — not re-filed.
- `core/runtime.py:617` dead `mcp_lifespan` superseded by `mcp/lifecycle.py` — not re-filed.
- CF-86 (scheduler-supplied unvalidated LLM base URL) — not re-filed.

## Review Confidence — 93/100

**Why 93:** the source was pinned to the requested commit; all 23 scoped files were read; method/payload parity was compared against the live in-process signatures rather than comments; privacy and diagnostics were followed through concrete call sites; every finding has a concrete payload shape and reachability chain; and line counts were independently measured instead of forced to the brief's checksum.

Confidence is not higher because there was no executable checkout/runtime for fault injection or end-to-end tests, Garak/DeepTeam/Promptfoo were not runnable, several reachability/default/display edges necessarily cross into supporting files outside the 23-file coverage corpus, and the independently measured line total differs by 20 from the supplied total.