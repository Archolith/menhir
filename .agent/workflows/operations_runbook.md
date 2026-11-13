# Local operations runbook

This runbook covers a self-hosted development instance of Menhir. Start with
the [installation guide](../../README.md) and use a disposable local Neo4j
database when testing memory features. Production host setup, release
promotion, recovery, and private client policy are maintained in the private
`Archolith/menhir-deploy` repository.

## Start and check

From the Menhir repository, install dependencies as described in the root
README, then:

```bash
menhir up --compose-neo4j
menhir check
menhir diagnostics
menhir serve
```

`menhir diagnostics --json` reports a redacted local security posture without
printing secrets or connecting to the network or graph. Once the server has
started, check its process and dependencies:

```bash
curl -fsS http://127.0.0.1:8100/api/health
curl -fsS http://127.0.0.1:8100/api/ready
```

Use `menhir console` to inspect a running local instance, and `menhir
serve-watch` if a local restart watchdog is desired. If readiness fails, run
`menhir diagnostics` and inspect the configured Neo4j URI, provider settings,
and server logs before retrying. The local Docker test-stack guidance is in
[deploy/README.md](../../deploy/README.md).

## Stranded saga rows

A merge, unmerge, delete, erasure or metric write that failed ambiguously (the connection dropped
mid-statement) stays `PREPARED` with its participants fenced, and in `live` startup mode it blocks
boot. A failure the server itself rolled back closes `FAILED` on its own (#427). To inspect and
close what is left:

```bash
menhir saga list                       # PREPARED and NEEDS_REVIEW rows, with ownership
menhir saga show OP_ID                 # row, claim history, ownership, graph classification
menhir saga attest OP_ID --owner-token TOKEN --by NAME
menhir saga resolve OP_ID --by NAME            # dry run: prints the planned close
menhir saga resolve OP_ID --by NAME --apply    # COMMITTED / FAILED / NEEDS_REVIEW from the graph
menhir saga clear-review OP_ID --to FAILED|COMMITTED|REVERSED --by NAME
```

`attest` states that the writer process named by `TOKEN` has stopped, so it is true only once that
process is gone: a container restarted after the row's `created_at`, or the host rebooted. If the
row was written by the server that is still running, restart the server first, then attest the old
token, then `resolve`. On a single-PID-namespace host, `MENHIR_HOST_PID_NAMESPACE_VERIFIABLE` lets
a dead PID prove this without an attestation. `resolve` takes the reconciliation gate (briefly
pausing new saga writes), refuses a row that is not abandoned, and never closes an erasure:
complete a partial erasure with live replay instead. Rows with no owner token need direct repair.

For MCP connection details, see [backend-first-mcp.md](backend-first-mcp.md).
For log files, request IDs, and error envelopes, see
[logging-and-troubleshooting.md](logging-and-troubleshooting.md). For
enrichment-specific stall diagnosis, see
[troubleshoot_enrichment_stalls.md](troubleshoot_enrichment_stalls.md).
