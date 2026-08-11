# menhir — rich console dashboard + memory-content privacy redaction

> **Archived 2026-08-11.** The console dashboard, central display-time privacy policy, runtime
> settings, Explorer integration, and regression coverage are implemented.

Status: IN PROGRESS
Author: Claude Code (Opus 4.8)
Date: 2026-07-12
Decisions (owner-approved): console = `rich` live dashboard (rich already installed via
typer, no new dep); privacy scope = console + log tail + explorer web UI, redacted at
DISPLAY time (log files unchanged); toggle = `MENHIR_PRIVACY_REDACT` env (default off) +
live `p` key in the console.

## Goal

1. Make the console launch a genuinely good live operator view: a `rich` dashboard showing
   server/neo4j/ready state, queue + enrichment + scheduler metrics (from `/api/ready` +
   `/api/stats`), and a live tail of `logs/server.log`.
2. Add a privacy mode that hides memory *contents* (free text) wherever they surface —
   the console log tail and the explorer web UI — for screen-sharing/demos. Redaction is
   at display time; log files and Neo4j are untouched.

## Design

### Central redaction (single source of truth)
`src/menhir/privacy.py`:
- `MASK = "[hidden]"` (or `"•••"`), length-agnostic.
- `redact_text(value: str | None, *, reveal: bool) -> str | None` — returns MASK when not
  revealing and value is non-empty; passthrough when `reveal`.
- `redact_log_line(line: str, *, reveal: bool) -> str` — best-effort masking of memory
  content embedded in a log line. Strategy: mask quoted strings and known content markers
  (entity/episode names, content previews) via targeted regexes; keep timestamp, logger,
  level, and structural tokens (uuids, counts, `key=value` metrics). When `reveal`, return
  the line unchanged. Conservative: better to over-mask a log line than leak.
- Redacted memory FIELDS across surfaces = `content`, `summary`, `preview`, candidate
  `notes`/`content`, episode `content`/`preview`, and node display `name`. Structural
  fields (uuid, labels, scope, session_id, timestamps, counts, kinds) are NEVER redacted —
  the graph stays navigable, only the text is hidden.

### Settings
`MemorySettings.privacy_redact: bool = False` ← `MENHIR_PRIVACY_REDACT` (parse_bool_env).
This is the initial state for both the console and the explorer.

### Console dashboard — `src/menhir/cli/console.py`
- Rich `Live` layout, refresh ~2 Hz:
  - Header: `menhir ● ready|starting|down  bind host:port  neo4j up|down  mode <startup>`
  - Metrics: queue_depth, enriching, scheduler state, enrichment rate, ops p50 / calls,
    capabilities summary — parsed from `GET /api/ready` + `GET /api/stats` (loopback,
    AuthMode.NONE → no token needed).
  - Recent: last N lines of `logs/server.log`, each passed through `redact_log_line`.
  - Footer: key hints — `p` toggle privacy (● PRIVACY ON/OFF), `q`/Ctrl+C quit.
- Non-blocking keypress on Windows via `msvcrt.kbhit()/getwch()`; POSIX via `select` on
  stdin (best-effort — primary target is Windows). Polling loop is async (httpx) so a slow
  server never freezes the UI; on connection error, show "server unreachable" and keep
  retrying.
- Command: `menhir console` (Typer) with `--host/--port/--interval/--log-file/--redact`.
  `--redact/--no-redact` overrides the env default; live `p` toggles thereafter.

### Launcher wiring — `scripts/start-server.ps1`
`console` action becomes: ensure Docker/Neo4j + a running server (start in background if not
already up; remember whether WE started it), then run `menhir console` (the dashboard).
On dashboard exit: if we started the server this invocation, leave it running but print the
stop hint (a monitor should not kill a pre-existing server); do not auto-stop. Raw
foreground `serve` remains documented as the low-level fallback.

### Explorer redaction (web UI)
- Explorer honors `settings.privacy_redact` server-side: redact memory fields in the row
  builders (`_recent_episodes`, `_queued/_failed/_successful/_recovered_episodes`,
  `_search_entities`, `_candidates`, `_node_detail`, `_session_detail`) before templates
  render — so redacted content never reaches the browser (true privacy for screen-share).
- Per-browser live toggle: a header button posts/gets `?reveal=1` which the server honors
  via a signed/session cookie override of the env default (cookie can only REVEAL on
  loopback; ignored on non-loopback binds where privacy should not be defeatable by a
  cookie). Keep this bounded — if it grows, ship server-side redaction first and the cookie
  toggle second.

## Files
- NEW `src/menhir/privacy.py`
- NEW `src/menhir/cli/console.py`
- `src/menhir/cli/__init__.py` — `console` command
- `src/menhir/config/settings.py` — `privacy_redact`
- `src/menhir/explorer/app.py` — apply redaction in row builders (+ reveal cookie)
- `scripts/start-server.ps1` — `console` → ensure server + dashboard
- tests: `tests/test_privacy.py` (redaction), console parse/render smoke, explorer redaction
- docs: operations_runbook.md, security-posture.md, CHANGELOG.md

## Verification
- `pytest tests/test_privacy.py tests/test_explorer_*.py` green.
- Console renders against the live loopback server; `p` toggles the PRIVACY badge and masks
  the log tail; `q` exits leaving the server up.
- Explorer with `MENHIR_PRIVACY_REDACT=true`: content/summary/preview render as `[hidden]`;
  structure/graph still navigable; reveal cookie flips it on loopback only.

## Risks
- `redact_log_line` is heuristic — calibrate regexes against real `server.log` samples; err
  toward over-masking. Never claim it is a hard guarantee for arbitrary third-party log text.
- Keypress handling is platform-specific; Windows (msvcrt) is the supported path, POSIX is
  best-effort.
- Cookie reveal must be loopback-only so privacy can't be turned off remotely.
