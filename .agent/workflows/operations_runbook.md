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

For MCP connection details, see [backend-first-mcp.md](backend-first-mcp.md).
For log files, request IDs, and error envelopes, see
[logging-and-troubleshooting.md](logging-and-troubleshooting.md). For
enrichment-specific stall diagnosis, see
[troubleshoot_enrichment_stalls.md](troubleshoot_enrichment_stalls.md).
