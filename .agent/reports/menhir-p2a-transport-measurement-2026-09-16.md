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

**Not yet applied.** `SnapshotLimits.chunk_bytes` is unchanged pending sign-off, because it is a
frozen-protocol constant and the plan gates the provisional marker on this record existing.

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

## What this does NOT establish

The P2A gate asks for more than a size ceiling. Measured here: encoded call size, success rate at
each rung, median latency, and failure behaviour at the first rejected rung. **Not measured:**

- **p95 latency** — three trials per rung gives a median, not a tail.
- **Peak process memory** under sustained chunking.
- **Staging growth and TTL/disk-budget behaviour** — the soak the gate requires, including the
  restart and disk-pressure tests.
- **Telemetry redaction under real load** — `call_payload` is overridden and unit-tested, but was
  not re-verified against rows produced by this run.
- **Any deployed environment.** The origin was a local container. A production zone may carry WAF
  rules, a proxy, or a body limit this throwaway hostname did not.

The chunk-size question is answered. The rest of the P2A gate is not, and the provisional marker
on `SnapshotLimits` should stay until it is.

## Reproducing

```bash
docker compose -f docker-compose.remote-sim.yml up -d --build
python scripts/probe/p2a_transport_probe.py --label local
docker compose -f docker-compose.remote-sim.yml down -v
```

Add `--base-url https://<host>` to measure a real ingress. The script takes the URL as its only
path-dependent input, so the same run compares any two paths.
