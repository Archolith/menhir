# TurnEvidence producers

How Menhir captures candidate **user prompts** as `:TurnEvidence`
([ADR 0001](../.agent/adr/0001-conversation-turn-capture-surface.md)), across every host agent.

A **producer** is a thin client-side hook that observes user prompts in a host agent (Claude Code,
OpenCode, Codex), runs a cheap deterministic triage, and POSTs only the prompts that look like durable
memory evidence to `POST /api/turn-evidence`. Producers are the *faucets*; they all feed the same narrow
hose. Phase 3 (the consumer) later consolidates that evidence into durable Views.

## The invariant

```
Hooks observe user prompts.
Hooks do NOT store every prompt.
Hooks do NOT call an LLM.
Only triage-accepted prompts become TurnEvidence.
```

Non-candidate prompts ("rewrite this", "continue", "explain this error") match no signal and are dropped
before anything leaves the machine. This is **not** transcript logging.

## What is and isn't captured

| Captured | NOT captured |
|----------|--------------|
| User prompts that pass deterministic triage | Assistant turns |
| A small provenance envelope (see below) | Tool turns |
| | Full transcripts |
| | Anything decided by an LLM |

Raw `:TurnEvidence` never enters normal recall; only Phase 3 reads it.

## Producers

| Client | Producer | Surface | `source_client` |
|--------|----------|---------|-----------------|
| Claude Code | `scripts/hooks/menhir_turn_evidence.py` | `UserPromptSubmit` command hook | `claude_code` |
| OpenCode | `scripts/hooks/menhir_opencode_turn_evidence.py` | `chat.message` JS plugin -> stdin | `opencode` |
| Codex | `scripts/hooks/menhir_codex_turn_evidence.py` | `UserPromptSubmit` command hook | `codex` |

All three are **thin adapters** over one shared core, `scripts/hooks/menhir_turn_evidence_common.py`,
which owns triage, provenance, the POST, fail-open handling, dry-run, and health. Each adapter supplies
only its identity constants (`source_client` / `source_kind` / `hook_version` / `triage_version`) and,
for Codex, a small normaliser for its field aliases. A cross-client **parity test**
(`tests/test_producer_pack.py`) asserts the producers share byte-for-byte identical triage — they
cannot drift.

### Adding a new producer

1. Create `scripts/hooks/menhir_<client>_turn_evidence.py` as a thin adapter: import the shared core,
   set the four identity constants, define `build_evidence_payload` and `main` as one-line delegations,
   and (if the client's event shape differs) a `normalize(raw)->dict` mapping to the common envelope
   (`prompt` / `session_id` / `cwd` / `transcript_path` / `hook_event_name`).
2. Wire the client's user-prompt event to pipe JSON to the script on stdin.
3. Add the client to the parity test corpus and add a `test_<client>_turn_evidence.py`.

Do **not** copy the triage rules into the new producer — import them, so parity holds by construction.

## The contract

Every producer POSTs the same conceptual payload:

```json
{
  "namespace": "...",
  "session_id": "...",
  "role": "user",
  "declarant": "user",
  "text": "...",
  "source_kind": "<client>_hook",
  "source_client": "claude_code | opencode | codex",
  "source_id": "...",
  "hook_version": "...",
  "triage_version": "...",
  "triage_reason": ["..."],
  "metadata": {
    "hook_event_name": "...",
    "permission_mode": "...",
    "project_root": "...",
    "git_branch": "...",
    "git_commit": "...",
    "prompt_hash": "..."
  }
}
```

The **server derives** `prompt_hash` and `recorded_at`; clients never become hash authorities (the
client-side `prompt_hash` in the envelope is provenance/correlation only and mirrors the server's
derivation). `source_client` / `hook_version` / `prompt_hash` are queryable node properties; the rest of
`metadata` is stored verbatim. All provenance is optional and **non-fatal**: missing git or metadata
never blocks capture, and the old (pre-provenance) payload shape is still accepted.

## Install

See [`scripts/hooks/README.md`](../scripts/hooks/README.md) for per-client registration
(Claude `.claude/settings.local.json`, the OpenCode plugin, and `codex-hooks.example.json`).

## Dry-run and health

Every producer supports two manual, POST-free commands:

```bash
# Would this prompt be captured? Triage only — never POSTs.
echo '{"prompt":"I have 25 movies"}' | python scripts/hooks/menhir_codex_turn_evidence.py --dry-run
# would_capture: true
# triage_reasons: ["i_have", "number"]
# source_client: "codex"
# triage_version: "codex-hook-v1"
# prompt_length: 15

# Local producer config — never POSTs, never prints the API key or prompt text.
python scripts/hooks/menhir_codex_turn_evidence.py --health
# menhir_url / api_key_configured: yes|no / capture_enabled / source_client / versions / git_available / cwd
```

## Configuration (env)

| Var | Meaning |
|-----|---------|
| `MENHIR_TURNS_URL` | Endpoint. Default `http://127.0.0.1:8090/api/turn-evidence`. |
| `MENHIR_AGENT_KEY` | Bearer token (agent tier). Unset => unauthenticated POST attempt. |
| `MENHIR_TURN_NAMESPACE` | Namespace override; else inferred from the cwd/project basename. |
| `MENHIR_TURN_HOOK_LOG` | Failure-log path; else `<home>/.claude/menhir-turn-hook.log`. |
| `MENHIR_TURN_EVIDENCE_ENABLED` | Set falsey (`0`/`false`/`no`/`off`) to disable capture (fail-open no-op). Unset => enabled. |
| `MENHIR_TURN_EVIDENCE_DRY_RUN` | Set truthy to force dry-run everywhere (never POST). |

## Failure behavior

Producers never block the host agent. Menhir unreachable, a non-2xx response, a timeout, missing git, or
malformed input all result in a local log entry (error + prompt length, **never** the prompt text) and a
clean exit. Disabling capture (`MENHIR_TURN_EVIDENCE_ENABLED=0`) is a silent no-op.

## How to disable

- Per session/shell: `MENHIR_TURN_EVIDENCE_ENABLED=0`.
- Permanently: remove the client's hook registration (Claude/Codex `hooks.json`, or the OpenCode plugin).
