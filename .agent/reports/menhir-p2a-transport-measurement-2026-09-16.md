# P2A transport measurement — results

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P2A)
Probe: `scripts/probe/p2a_transport_probe.py`
Measured: 2026-09-16

## What was measured, and against what

Two paths, same probe, same build:

| label | path |
| --- | --- |
| `local` | host -> `127.0.0.1:8099` -> container. No ingress. |
| `tunnel` | host -> Cloudflare edge -> `cloudflared` -> container, via a throwaway hostname on a real zone. |

The tunnel and its DNS record were created for this run and deleted after it. The origin was the
`docker-compose.remote-sim.yml` stack — a Menhir with no host mount — not a deployed server.

The probe classifies each body by **who answered**, which is the only distinction that makes the
number meaningful:

- a structured MCP reply, success *or* refusal, proves the bytes crossed the ingress and reached
  the handler;
- an HTTP status or a reset with no MCP reply proves they did not.

A 413 from an edge and a refusal from the handler are indistinguishable if you only check whether
the call succeeded. Conflating them is the one way to produce a confidently wrong ceiling here.

## Result: the ceiling is ours, not Cloudflare's

Both paths give the same answer, to the byte.

| decoded chunk | wire body | local | tunnel |
| --- | --- | --- | --- |
| 64 KiB | 85.6 KiB | accepted 3/3 | accepted 3/3 |
| 256 KiB | 341.6 KiB | accepted 3/3 | accepted 3/3 |
| 1 MiB | 1.33 MiB | accepted 3/3 | accepted 3/3 |
| 2 MiB | 2.67 MiB | accepted 3/3 | accepted 3/3 |
| 2.88 MiB | 3.83 MiB | — | delivered 3/3 |
| 3 MiB | 4.00 MiB | — | **413** 3/3 |
| 4 MiB | 5.33 MiB | **413** | **413** 3/3 |

**The hard ceiling is a 4 MiB request body, and it is enforced by our own origin.** It is
`DEFAULT_MAX_REQUEST_BODY_SIZE = 4 * 1024 * 1024` in `mcp/server/transport_security.py`
(`RequestBodyLimitMiddleware`, mcp SDK 1.30.0), which Menhir never overrides. 3.83 MiB passes and
4.00 MiB fails, which matches the constant exactly.

Cloudflare imposed no lower limit. The 413s seen through the tunnel are the origin's, forwarded.

Two consequences worth stating plainly:

1. `max_chunk_bytes = 2 MiB` is safe, but by luck rather than by design: 2 MiB decoded is 2.67 MiB
   on the wire, 67% of a ceiling nobody chose. **Raising it to 3 MiB would 413 every chunk.**
2. The ceiling is a dependency default. An SDK upgrade can move it without any change to Menhir,
   and nothing currently fails loudly if it does.

## Result: the default chunk should rise from 256 KiB to 1 MiB

The plan's own rule is one rung below the largest repeatedly stable size, keeping 2x envelope
headroom. 2 MiB was stable 3/3 on both paths, so the rung below is **1 MiB** — 1.33 MiB on the
wire against a 4 MiB ceiling, 3x headroom.

The throughput data says the same thing more loudly. Per-request overhead dominates at small
sizes, and it dominates far harder across a real edge:

| decoded chunk | local | tunnel |
| --- | --- | --- |
| 64 KiB | 2.1 MiB/s | 0.6 MiB/s |
| 256 KiB | 7.8 MiB/s | 1.3 MiB/s |
| 1 MiB | 19.3 MiB/s | 6.7 MiB/s |
| 2 MiB | 28.0 MiB/s | 10.9 MiB/s |

For a 64 MiB bundle at the pilot quota, through the tunnel: ~256 requests at 256 KiB versus ~64 at
1 MiB. The current 256 KiB default is using roughly a fifth of the available throughput on the
path that matters.

**Applied 2026-09-16** (owner sign-off): `SnapshotLimits.chunk_bytes` is now 1 MiB, with the
measurement recorded as its basis in the dataclass docstring and pinned by
`test_measured_chunk_default_is_pinned`.

`test_a_max_size_chunk_still_fits_the_request_body_ceiling` pins the finding rather than the
number: it fails if `max_chunk_bytes` is raised to 3 MiB (the measured 413 case) and also if it is
raised to 2.5 MiB, which clears the hard ceiling but leaves no headroom. That guard was verified
against both counterexamples, not just asserted.

**The PROVISIONAL marker stays.** One measured value does not close the P2A gate; see the section
below for what is still unrun.

## Finding: some agents are refused at the edge before reaching the origin — but not Menhir's

Reproduced deliberately as a control, twice, during the tunnel run:

```
control: HTTP 403 - cloudflare error 1010 ("browser signature banned")
```

A request carrying urllib's default `Python-urllib/3.12` agent is rejected at the Cloudflare edge
with a 403. **Zero bytes reach the origin**, and from the client side that is indistinguishable
from the server being down.

**An earlier draft of this report claimed every `menhir sync` against a Cloudflare-fronted
deployment would fail this way. That was wrong,** and it was wrong because the probe's client was
taken as representative of Menhir's. It is not: `BackendClient` uses `httpx`, not `urllib`. The
four-way check against a proxied hostname on a Browser-Integrity-Check zone:

| agent sent | result |
| --- | --- |
| `Python-urllib/3.12` (what the probe sent) | **403, blocked at edge** |
| `python-httpx/0.27.0` (what `BackendClient` sends) | 200, reaches origin |
| a named product agent | 200, reaches origin |
| no `User-Agent` header at all | **403, blocked at edge** |

So the real exposure is narrower and worth stating exactly:

- **Production is not affected.** `memory.ctharvey.me` is on `ctharvey.me`, which has Browser
  Integrity Check ON and no custom firewall rule exempting it — but its clients send httpx's
  agent, which passes.
- **What is affected** is anything speaking to a Cloudflare-fronted Menhir with urllib, or with no
  agent header: ad-hoc operator scripts, `curl -H` invocations that drop the default, and
  `deploy/remote_sim_healthcheck.py`, which uses urllib and would fail if that stack were ever put
  behind a zone (it is local-only today, so this is latent, not live).

Setting an explicit agent on the client is still worth doing, for a reason that survives the
correction: **the empty-agent case is blocked too**, BIC's signature list is Cloudflare's to change
without notice, and the failure mode is a silent 403 that looks like an outage. It is cheap
insurance against a class of failure nobody would diagnose quickly. It is not, as previously
written, a live bug blocking installs.

## The soak: sustained cost, tail latency, and what grows

`scripts/probe/p2a_soak.py`, run against both paths at the newly-set 1 MiB chunk: 4 bundles of
16 MiB each, 64 chunk calls per path, 128 in total. **Zero failures on either path.**

| | local | through the edge |
| --- | --- | --- |
| p50 | 0.066s | 0.222s |
| **p95** | 0.086s | **0.347s** |
| p99 | 0.098s | 0.641s |
| max | 0.098s | 0.641s |
| failures | 0 / 64 | 0 / 64 |

The tail is where the edge shows up. Locally p99 is 1.5x the median; through the edge it is 2.9x,
and the worst chunk took 0.64s against a 0.22s median. For a 64 MiB bundle that is a handful of
slow chunks in ~64 requests, not a stall — but it is the number to watch if chunking is ever made
concurrent, because a 0.64s tail sets the timeout floor.

**Memory: bounded and transient, established by trend rather than by a peak.**

This one took three attempts to measure honestly, and the first two answers were wrong. The record
matters more than the number, because the same mistakes are available to anyone re-running this:

1. *"Idle 212 MiB, peak 251 MiB, so it streams."* Wrong evidence. The sampler shelled out to
   `docker stats --no-stream`, which costs 1–2s per call, so a 30-second soak collected **three
   samples**. The peak was a floor from three readings that looked exactly like a maximum.
2. *"Peak memory tracks bundle size ~1:1, so it buffers."* Also wrong. With sampling fixed
   (cgroup counter, one exec), arms at constant total bytes read 194 / 211 / 226 MiB for 4 / 16 /
   32 MiB bundles — but those are high-water marks from 5–7 samples across separate runs, which is
   not enough to attribute a 30 MiB spread to bundle size.
3. The question was then settled two ways that agree.

**The code:** `put_chunk` decodes one chunk, seeks to `index * chunk_bytes` in the blob file, and
writes it. Nothing accumulates; the only in-memory copy is the chunk in flight.

**The counterexample run:** five consecutive 32 MiB uploads against one container, reading `anon`
from `memory.stat` between each:

| after | idle | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- | --- |
| anon (MiB) | 140.1 | 190.3 | 235.6 | 223.7 | 192.6 | 180.5 |

A receiver retaining bundles would climb monotonically toward +160 MiB. This rises, peaks at
round 2, and **comes back down** — allocator high-water and GC, in a band roughly 40–95 MiB above
idle, ending 40 MiB above it and trending down. Page cache is not the explanation either and was
checked: `file` went 164 KiB → 68 KiB across a 32 MiB upload, i.e. nothing.

So the conclusion the gate wanted holds — memory is bounded by chunk size and concurrency, not by
bundle size — but it rests on the code plus the five-round trend, **not** on any single peak
figure. Peaks from these runs should not be quoted: the default soak collects 4–8 samples and the
script now prints a warning saying so.

**Staging bytes come back.** Peak 16.8 MB — one bundle, not four, confirming uploads do not
accumulate — settling to 24 KB afterwards. That residue is 16 `record.json` files of ~1,508 bytes
each, one per upload, and **no payload bytes at all**: the one-hour terminal retention holding
records, exactly as designed, not a leak. The distinction was checked by listing the directory
rather than inferred from the byte count.

## Telemetry redaction, verified against this run's own rows

304 snapshot-tool rows were produced (256 of them chunk calls). Every chunk row's preview:

```json
{"declared_len": 1048576, "digest": "[redacted]", "index": 0, "upload_id": "[redacted]"}
```

A scan of **all 609 rows** for any 200+ character base64-like run found **zero**. Sizes and the
chunk index are in the clear; everything else is masked. Invariant 5 holds under real load, not
just in unit tests.

Two things worth noting from that output:

- `upload_id` and `digest` are redacted too, which is stricter than the tool's `call_payload`
  intends — `_preview_of` masks any string that is not allowlisted AND identifier-shaped. The
  consequence is operational, not a defect: **telemetry cannot correlate rows belonging to one
  upload.** Worth a deliberate allowlist decision in P2B rather than discovering it during an
  incident.
- `graph_operations` held **0 rows** after 16 completed uploads. The graph-inertness of the
  staging path is pinned by an AST test; this is the same claim confirmed from live evidence.

## What this still does NOT establish

- **Disk-budget refusal under real pressure.** `test_a_begin_is_refused_before_the_disk_budget_is_exhausted`
  covers it with a 32-byte budget and a fake clock. Nothing has filled a real disk.
- **Restart mid-upload against a real stack.** `test_an_upload_resumes_across_a_restart` covers the
  receiver's logic; the container was never killed mid-bundle.
- **Concurrency.** Every upload here was sequential. The per-principal cap of 2 and per-project cap
  of 8 are unit-tested, but no two uploads have ever actually raced.
- **Any deployed environment.** The origin was a local container throughout. A production zone may
  carry WAF rules, a proxy, or a body limit this throwaway hostname did not.
- **Sustained duration.** The soak is ~30 seconds per path. It says nothing about an hour.

The measurement items the gate names are now recorded. The `PROVISIONAL` marker is a separate
judgement — see the gate line in the plan — and the list above is what an honest reading of
"soak tests" still leaves open.

## Reproducing

```bash
docker compose -f docker-compose.remote-sim.yml up -d --build
python scripts/probe/p2a_transport_probe.py --label local
docker compose -f docker-compose.remote-sim.yml down -v
```

Add `--base-url https://<host>` to measure a real ingress. The script takes the URL as its only
path-dependent input, so the same run compares any two paths.
