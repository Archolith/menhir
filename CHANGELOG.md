## 2026-09-16 - local MVP tracked-write receipts and observation guidance

- `src/menhir/mcp/formatters.py`: status/watch observations direct continuation to the
  existing episode; remove duplicate-write advice and unsupported completion/retry promises.
- `src/menhir/mcp/tools/ingest/add_memory_and_track.py`: clarify that the tool queues a
  new write; preserve its accepted receipt when subsequent collection or formatting fails,
  without exposing raw exception text. Optional queue diagnostics cannot hide an observed
  episode status. Cancellation and existing write/auth arguments remain unchanged.
- `docs/agent-usage.md`, `docs/templates/AGENTS.menhir.md`: document the #118 owner decision,
  actual tool options, restricted-client behavior, and the separate TEMPORAL direct-write path.
- `tests/test_mvp_tracked_write_contract.py`: 31 focused formatter and bound-endpoint
  regression cases. Live stdio E2E-2 and exact-commit repository CI remain release gates.
- Keep the newest ten dated entries per `.agent/maintenance.md`; older entries remain in Git history.

## 2026-09-16 - a crash between an upload's two writes returned a 500

P2B's second inherited counterexample. Every write in the receiver is two steps -- `begin` writes
the record then creates the blob, `put_chunk` writes the blob then updates the record, `abort`
writes the record then drops the blob -- and a crash in any of those windows leaves durable state
no caller ever produced. Four tests force each window directly instead of hoping a killed container
lands there.

Three of the four passed, which is worth recording: unrecorded bytes are correctly reported missing
and the resend is an ordinary first delivery (the write ordering in `put_chunk` is the safe one); a
half-written record with only a temp file is reclaimed as garbage rather than resumed; and an abort
that died before dropping its bytes still frees them at the retention boundary.

The fourth failed. A record whose blob never existed -- a crash between `begin`'s record write and
its blob creation -- made `put_chunk` raise an uncaught `FileNotFoundError`, which reaches the
client as a 500. From the caller, an upload id that produces a 500 is indistinguishable from the
server being broken.

- New stable code `snapshot.upload.staged_bytes_lost`, and the upload is marked FAILED durably, so
  `status` tells the truth and later chunks are refused by the state check rather than re-running
  the same path.
- **The blob is not recreated.** That looks like recovery and is corruption whenever `received` is
  non-empty: those indices would become zero-filled while the record still claims them, and the
  upload would SEAL over a bundle whose chunk digests were never re-checked.
- The guard covers the write as well as the open, deliberately. A partly-written chunk leaves the
  blob in a state no digest in the record describes -- the same disagreement by a different route.

## 2026-09-16 - every staging quota was advisory under concurrency

P2B opens with the counterexamples P2A's closure deferred, and the first one found a real defect.
`begin` scanned the staging directory, counted RECEIVING records against the caps, then wrote a new
record -- a check-then-act with nothing reserved in between. Two processes on one staging root both
read "a slot is free" and both take it.

Three tests force that window deterministically, by pausing one receiver between its check and its
write rather than with threads and sleeps, so they describe real behaviour and cannot flake. Before
the fix all three failed: both caps exceeded, and **1024 bytes admitted against a 900-byte disk
budget**.

Not reachable in today's deployment -- tool calls are not dispatched to threads, so one event loop
serialises `begin` -- which is exactly the problem. The safety rested on deployment shape rather
than on the receiver, and a second uvicorn worker, a second replica, or a future threadpool
dispatch would have removed it silently. P2B is the durable multi-tenant receiver, so this sat
directly under everything it will build.

- `begin` now **reserves first and verifies after**: the record is written, making the reservation
  durable and visible to every other process, and only then are the quotas checked. A caller that
  finds itself over quota removes its own record and refuses.
- No lock file. A crashed lock holder would block every future `begin` until someone noticed,
  whereas a reserver that dies between the write and the verification leaves a RECEIVING record
  that the inactivity TTL already reclaims -- the same path as any abandoned upload, no new class
  of leak.
- `_verify_reservation` counts EVERY active reservation, not a prefix. A first attempt ordered
  records by `(created_at, upload_id)` so exactly one of two racers would keep the slot; the
  project-cap test caught it, because tied timestamps let an earlier-admitted record sort after
  the new one and escape the count. Ordering cannot substitute for admission order without a
  sequence number that does not exist here. Counting everything means both racers may back off and
  lose a free slot -- retriable, cheap, and self-correcting, which is the right trade against a
  disk budget that does not bound anything.

## 2026-09-16 - P2A closed, P2B unblocked

Owner sign-off on the gate as written: chunk default and hard ceiling recorded, quota/TTL/restart
and redaction tests passing, graph-inertness held by AST test and confirmed live. The plan's status
header said "measurement NOT RUN, P2B BLOCKED" and is now accurate.

Closed with three things stated rather than resolved, each carried into P2B as a counterexample to
construct rather than a test to pass:

- **Disk budget under real pressure.** Today's coverage is a 32-byte budget and a fake clock. The
  case that matters is a begin arriving as a concurrent upload's writes cross the budget.
- **Restart mid-upload.** The receiver's resume logic is tested; no process has been killed between
  two chunks of one bundle.
- **Concurrency.** Every P2A upload was sequential, so the caps of 2 per principal and 8 per project
  have never actually raced.

Open decision added: **telemetry cannot correlate an upload's rows**, because `_preview_of` redacts
`upload_id` along with everything not allowlisted AND identifier-shaped. Allowlisting a
server-minted opaque id would restore the join key without weakening invariant 5. Decided in P2B
design, not during the first incident.

`PROVISIONAL_LIMITS` keeps its name. `max_file_bytes` is untested against real repositories and the
quota figures have met only a unit-test disk budget; renaming would claim more confidence than
those values have earned.

## 2026-09-16 - the P2A soak: the receiver streams, and telemetry stays clean under load

`scripts/probe/p2a_soak.py` answers what the size probe structurally could not. The probe finds a
ceiling with one request; tail latency, memory behaviour and whether staging bytes come back need
sustained traffic. 4 bundles x 16 MiB at the new 1 MiB chunk, both locally and through a real
Cloudflare ingress, 128 chunk calls, **zero failures**.

- **Tail latency.** p95 0.086s local, **0.347s through the edge**; p99 0.098s / 0.641s. The edge
  widens the tail from 1.5x the median to 2.9x. Relevant if chunking is ever made concurrent: the
  0.64s worst case sets the timeout floor.
- **Memory is bounded and transient** -- established by trend, not by a peak, after two wrong
  answers. The sampler first used `docker stats --no-stream`, which costs 1-2s per call, so a
  30-second soak collected THREE samples and its "peak" was a floor dressed as a maximum. With
  sampling fixed (cgroup counter, one exec), five consecutive 32 MiB uploads read anon 140 -> 190
  -> 236 -> 224 -> 193 -> 181 MiB: it rises, then comes back down. Retention would climb
  monotonically toward +160 MiB. `put_chunk` also demonstrably writes each chunk straight to the
  blob at its offset, so nothing accumulates. Page cache was ruled out separately (`file` 164 KiB
  -> 68 KiB). Peaks from a default soak are NOT quotable: it collects 4-8 samples, and the script
  now says so.
- **Staging bytes return.** Peak 16.8 MB -- one bundle, so uploads do not accumulate -- settling to
  24 KB of `record.json` files with no payload bytes. That is the one-hour terminal retention
  holding records, confirmed by listing the directory rather than inferred from a byte total.
- **Redaction verified against this run's rows.** All 256 chunk rows preview as
  `{"declared_len": ..., "digest": "[redacted]", "index": N, "upload_id": "[redacted]"}`, and a
  scan of all 609 rows found no 200+ char base64 run. `graph_operations` held 0 rows after 16
  completed uploads -- the graph-inertness the AST test pins, confirmed live.

Noted for P2B, not fixed here: `upload_id` and `digest` are redacted too, so **telemetry cannot
correlate rows belonging to one upload**. That is `_preview_of` masking anything not allowlisted
AND identifier-shaped. It wants a deliberate allowlist decision rather than being discovered during
an incident.

Still open despite the soak: disk-budget refusal under real pressure (unit-tested with a 32-byte
budget), restart mid-upload against a real stack, and concurrency -- every upload here was
sequential, so no two have actually raced.

## 2026-09-16 - the snapshot chunk default is now a measurement, not a guess

`SnapshotLimits.chunk_bytes` goes from 256 KiB to **1 MiB**, the value P2A's rule selects: 2 MiB was
accepted 3/3 both locally and through a real Cloudflare ingress, and the rule is one rung below the
largest repeatedly stable size. 1 MiB is 1.33 MiB on the wire against a 4 MiB ceiling -- 3x
headroom. Through the edge that is 6.7 MiB/s against 1.3 MiB/s at the old default, because
per-request overhead dominates small bodies and dominates harder the further away the server is.

- The dataclass docstring now carries the evidence and names the ceiling's real owner:
  `RequestBodyLimitMiddleware` in the MCP SDK, defaulting `max_request_body_size` to 4 MiB, which
  Menhir does not override. 3.83 MiB on the wire passes, 4.00 MiB returns 413.
- `test_measured_chunk_default_is_pinned` pins the value, because it is evidence now -- changing it
  means re-running the measurement, not editing a guess.
- `test_a_max_size_chunk_still_fits_the_request_body_ceiling` pins the finding instead of the
  number. It fails if `max_chunk_bytes` is raised to 3 MiB (the measured 413) and also at 2.5 MiB,
  which clears the ceiling but keeps no headroom. Both counterexamples were checked, not assumed.
  The ceiling is invisible from the snapshot layer and is a dependency DEFAULT, so an SDK upgrade
  can move it with no change here; this test is where that would surface.
- **PROVISIONAL stays.** `max_chunk_bytes` and `max_file_bytes` are still unmeasured, and P2A's
  soak, disk-pressure, p95 and peak-memory checks have not run. One measured value does not close
  the gate.

## 2026-09-16 - the backend client names itself

`BackendClient` sent no `User-Agent`, leaving httpx's default. Measured against a Cloudflare zone
with Browser Integrity Check on: httpx's default passes, but `Python-urllib/3.12` and **a request
with no agent at all** are both refused at the edge with a 403 (error 1010) before the origin sees
them -- a failure indistinguishable from the server being down.

So this is insurance, not a bug fix; nothing is broken today. It is worth the two lines because the
blocked-signature list is Cloudflare's to change without notice, and the empty-agent case is
already refused -- which is the one a future refactor could reintroduce for free.

- `client_user_agent()` returns `menhir/<version>`, and `_default_headers` seeds the dict with it
  unconditionally. Every other header there is conditional on configuration; an agent that appears
  only when some setting happens to be set is exactly the case that gets refused.
- `deploy/remote_sim_healthcheck.py` uses urllib and now names itself too. Latent while that stack
  is loopback-only, which is why it would be missed the first time it went behind a hostname: the
  symptom is a container reporting unhealthy forever against a healthy server.
- `test_backend_client_reuses_owned_async_client_across_requests` asserted headers were exactly
  empty for an unconfigured client. It now pins paths and payloads (its actual subject) and
  separately asserts the agent is present on every request, so a regression to "no agent" still
  fails there.

## 2026-09-16 - the snapshot chunk ceiling is ours, not the ingress's

P2A's transport measurement ran, against the local stack and through a real Cloudflare ingress
(a throwaway tunnel, created and deleted for the run). Both paths give the same ceiling:
**a 4 MiB request body**, enforced by `DEFAULT_MAX_REQUEST_BODY_SIZE` in the MCP SDK's
`RequestBodyLimitMiddleware`, which Menhir never overrides. 3.83 MiB on the wire passes and
4.00 MiB returns 413. Cloudflare imposed nothing lower.

- `max_chunk_bytes = 2 MiB` (2.67 MiB encoded) clears that by 1.33 MiB -- safe, but by accident.
  Raising it to 3 MiB would 413 every chunk.
- 2 MiB chunks were stable 3/3 on both paths, so the plan's own rule selects **1 MiB** as the
  default, up from the guessed 256 KiB. Through the edge that is 6.7 MiB/s against 1.3 MiB/s:
  per-request overhead dominates small chunks, and dominates harder the further away the server
  is. Not yet applied -- it changes a frozen-protocol constant.
- A request carrying urllib's default `Python-urllib/3.12` agent is refused at the Cloudflare edge
  with a 403 (error 1010) and **never reaches the origin**, which from the client looks exactly
  like the server being down. Sending no agent at all is refused the same way. `httpx`, which is
  what `BackendClient` actually uses, is NOT refused -- so production and the shipping client are
  unaffected. The exposure is ad-hoc urllib tooling (including
  `deploy/remote_sim_healthcheck.py`, latent while that stack stays local) and any client that
  sends no agent.

`scripts/probe/p2a_transport_probe.py` takes a base URL as its only path-dependent input, so one
script measures any two paths. It classifies each body by who answered -- a structured MCP reply,
success or refusal, proves the bytes crossed the ingress; an HTTP status or reset proves they did
not. Full results and what the run does NOT establish (p95, peak memory, staging soak, telemetry
under load): `.agent/reports/menhir-p2a-transport-measurement-2026-09-16.md`.

## 2026-09-16 - a first install with no AI provider could not start, and a harness that finds that

`prepare_memory_runtime` skips Graphiti's index build when there is no usable LLM or embedder, so
the server can "start against Neo4j alone (graph adapter works; Graphiti features degrade)
instead of crashing" -- its own comment. The readiness check then required three indexes that
skipped call is the only creator of. The degraded branch raised at startup every time and could
never be taken: a fresh install with no AI key could not start at all, which is exactly the
install-and-run scenario the snapshot plan's release gate requires.

- `phase_one_schema_ready(require_graphiti=...)` drops the three Graphiti-owned indexes from the
  requirement only when the caller has already established that Graphiti is unavailable. The
  default stays strict, and an instance that HAS Graphiti still refuses on a missing index --
  there it means the build failed rather than never ran. Indexes Menhir creates itself are
  required unconditionally in both modes.
- `bootstrap` derives the build decision once and passes the same value to both readiness calls.
  Deriving them separately is how they came to disagree.

Found by `tests/remote_sim`: a Docker stack running Menhir with its own throwaway graph and NO
host mount, driven over HTTP, brought up and torn down by the test itself
(`pytest --run-remote-sim tests/remote_sim`, skipped by default, needs Docker). It is the first
test of any kind against a Menhir that cannot see the files it is being asked about -- which is
the premise of the whole snapshot feature and had never been exercised. The bug needed an empty
graph AND no AI provider at once; every existing test had one of those covered.

The smoke proves the server cannot see the host's files, that a bundle built by the real bundler
survives the round trip byte-intact, and that the receiver's refusals (conflicting replay, unknown
upload) hold over the wire. **It is not the P2A transport measurement:** it carries no CDN, TLS
termination, proxy body limit or WAN latency, and the chunk ceiling P2A must measure is a property
of that path.

## 2026-09-16 - the stale-identity check on the payload path actually checks something (#98)

`write_project_structure` re-read the project's identity binding and overwrote the caller's
claim with what it had just read, microseconds before the write boundary compared that value
against the same row. The comparison compared a number with itself, so it could never fail --
the safeguard was present, ran on every call, and was incapable of refusing anything.

- The claim generation now comes from the caller and is passed through untouched. The binding
  lookup may reject a write; it may not supply the value it is checked against.
- A payload carrying no claim is refused with a message naming `scan_and_write_project`, rather
  than being handed a freshly-minted generation. Nothing to compare means nothing was checked.
- `bind_project_identity` is still called for its other effect (stamping `root_key` on bindings
  written before that property existed); only its return value is no longer used.

The race this restores protection against: a caller settles an identity, time passes, the
directory transfers to another checkout, and the caller writes anyway -- landing one project's
files in another's silo, with the stale prune deleting whatever the first does not have. This
was the path where a caller is furthest in time from its own scan, so it was the one that needed
the check most.

## 2026-09-16 - P2A staging receiver: the real chunk handler, off by default

The instrument the transport measurement runs against. It exercises the real
begin/chunk/status/abort path -- same middleware, auth, parser and telemetry a release would
use -- and stops at SEALED. No extraction, no manifest read, no graph; a test reads the module's
AST and fails if it so much as imports `zipfile` or anything graph-shaped.

- `menhir.snapshot.receive`: filesystem-backed staging. Ownership is bound at creation and
  re-checked on every load, and a foreign or guessed id reports NOT FOUND rather than forbidden.
  Bounds are enforced before allocation and again after decode. An exact chunk replay is a no-op;
  the same index with a different digest fails the upload, because that is not a retry -- client
  and server disagree about what is being uploaded.
- Records hold ids, counts, digests and timestamps, and nothing derived from content. The chunk
  tool overrides `call_payload` so telemetry records the upload id, index, byte count and digest
  -- the default would have recorded the base64 of a user's source file into every row.
- State is durable and never inferred from a directory: an upload resumes across a restart, and
  staged bytes with no readable record are reclaimed rather than adopted.
- Quotas are the approved pilot figures (2 per principal, 8 per project, 24h inactivity TTL, 1h
  terminal retention, disk budget), kept out of `SnapshotLimits` because that ships to clients.
  A begin is refused ahead of exhaustion, counting what the upload could add.
- Four MCP tools, operator tier, registered only while `MENHIR_SNAPSHOT_RECEIVE_MODE=staging`,
  so while off they are neither advertised nor invocable. Each endpoint re-checks the mode at
  call time as well; anything but `staging` fails closed. P2B replaces this env read with the
  registered feature flag and adds commit.
