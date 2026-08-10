# Hook Center Stale Anchor Lane — real-DB smoke runbook

`scripts/smoke/hook_center_stale_lane_smoke.py` proves the whole stale-file-anchor lane
against a **real** Neo4j backend, end to end:

```
file/tool event -> file marked dirty -> stale anchor detected -> recall labels stale
-> formatter/context warns -> verification receipt records outcome
-> receipt enriches recall only when path-aware and post-dirty
```

Core invariant: **a wrong current-state view is worse than a miss** — no receipt (wrong-path,
pre-dirty, or malformed) ever marks a stale memory fresh.

## What it exercises

- **Over HTTP** against a throwaway Menhir server (self-served by default): `POST /api/tool-events`,
  `GET /api/tool-events/dirty`, `GET /api/tool-events/stale`,
  `POST|GET /api/tool-events/stale-verifications`. The server mounts the **real** router over
  a **real** Neo4j-backed graph adapter (no full runtime, no embedder, no scheduler).
- **In-process** through the **real** `RecallService.recall()`,
  `ContextBuilderService.build_context()`, and `menhir.mcp.formatters` against the same
  throwaway Neo4j. Only the embedding-dependent graphiti vector search is seeded; every
  stale-labeling / advisory / receipt-matching step is the shipped code path against real Cypher.

## Prerequisites

- Python 3.12 with the menhir package importable (run from the repo root; the script adds
  `src/` to `sys.path`).
- **Docker** (for the default self-serve path — the launcher spins up and tears down a throwaway
  `neo4j:5-community` sidecar itself), OR an existing disposable Neo4j to reuse via
  `MENHIR_TEST_NEO4J_URI`. **Never point this at the shared workspace DB.**

## Running

### Default — pure self-serve (recommended)

The launcher owns everything: it starts a throwaway Menhir server *and* its own throwaway Neo4j on
ephemeral ports, runs the lane against that single shared DB, and tears both down.

```bash
# Zero DB flags. Docker required (or set MENHIR_TEST_NEO4J_URI, below).
python scripts/smoke/hook_center_stale_lane_smoke.py

# JSON-only on stdout (diagnostics go to stderr)
python scripts/smoke/hook_center_stale_lane_smoke.py --json
```

> **Do NOT pass `--neo4j-uri` / `--neo4j-password` in self-serve mode.** In self-serve, the launcher
> starts its *own* Neo4j for the HTTP server; a `--neo4j-uri` you add only redirects the *in-process*
> fixture/recall backend, so the two halves talk to **different databases** (split-brain). The
> symptom is `[1] tool_event_accepted: marked_dirty=False` and every downstream check failing. The
> Neo4j flags are ONLY for the fully-external mode below.

### Reuse an existing throwaway Neo4j (fast path — no second container)

To avoid the launcher starting a fresh container each run, point it at a disposable Neo4j you already
have up. Still zero `--neo4j` flags — the env var feeds the launcher, so both server and in-process
backend share it:

```bash
docker run -d --name menhir-smoke-neo4j -p 7688:7687 -e NEO4J_AUTH=neo4j/smokepass neo4j:5-community
export MENHIR_TEST_NEO4J_URI=bolt://localhost:7688
export MENHIR_TEST_NEO4J_USER=neo4j
export MENHIR_TEST_NEO4J_PASSWORD=smokepass
python scripts/smoke/hook_center_stale_lane_smoke.py
docker rm -f menhir-smoke-neo4j   # when finished
```

### Fully external — you own both the server and the DB

Only here do the `--neo4j-*` flags apply. `--no-self-serve` + `--url` make the smoke skip the
launcher and target a running server; the `--neo4j-*` flags must point at the **same** DB that server
uses, or you re-introduce the split-brain.

```bash
python scripts/smoke/hook_center_stale_lane_smoke.py \
    --no-self-serve --url http://127.0.0.1:8099 \
    --neo4j-uri bolt://localhost:7688 --neo4j-password smokepass
```

### Useful flags

| Flag | Effect |
|------|--------|
| `--url` | Base server URL (default `http://127.0.0.1:<port>`) |
| `--no-self-serve` | Use an already-running server instead of spawning one |
| `--project` / `--path` / `--memory-uuid` | Override the disposable fixture identifiers |
| `--json` | Emit parseable JSON only on stdout |
| `--keep-data` | Skip the post-run cleanup (leaves smoke nodes in the DB) |
| `--skip-recall` | Skip recall/formatter/enrichment checks (server-only environments) |
| `--skip-context-builder` | Skip the context-builder atomicity check |
| `--require-clean-start` | Fail if smoke data for the project already exists |
| `--agent-key` / `--readonly-key` | Bearer tokens if the target server has auth configured |

Environment fallbacks: `MENHIR_URL` / `MENHIR_TOOL_EVENTS_URL`, `MENHIR_AGENT_KEY`,
`MENHIR_READONLY_KEY`, `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` / `NEO4J_DATABASE`.

## Result states

| State | Meaning |
|-------|---------|
| `PASS` | All 12 lane checks passed |
| `PASS_WITH_SKIPS` | All evaluated checks passed; some were skipped (flags/env) and reported honestly |
| `FAIL` | One or more checks failed (exit code 1) |

## Safety

- Never uploads file content (only a synthetic provenance hash) and never captures transcripts.
- Uses a disposable project namespace; cleanup deletes only that namespace's nodes and never
  clears dirty flags globally.
- Adds no lifecycle behavior: no auto-refresh, no dirty clearing, no down-ranking, no
  deletion/expiration.

## Unit tests

```bash
python -m pytest tests/test_hook_center_stale_lane_smoke.py -q
```

These mock HTTP and the in-process backend (no Neo4j required) and assert the script's
orchestration, JSON-only output, exit codes, honest skip/fail distinction, and the
no-file-content guarantee.

See also the live component smoke: [`hook-center-live-smoke.md`](hook-center-live-smoke.md).
A dated receipt from a real run lives under [`docs/smoke/`](../smoke/).
