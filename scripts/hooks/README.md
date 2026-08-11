# Menhir hooks

Client-side hooks for two independent systems:

| System | Hooks | Purpose |
|--------|-------|---------|
| **TurnEvidence** | `menhir_turn_evidence*.py` | Feed candidate **user prompts** to `:TurnEvidence` capture (ADR 0001) |
| **Hook Center** | `menhir_file_event.py` | Feed **file-change events** to `/api/tool-events` for stale-reference detection |

---

# Part 1: TurnEvidence producers

Full producer guide: [`docs/turn-evidence-producers.md`](../../docs/turn-evidence-producers.md).

## The invariant (every producer)

```
Hooks observe user prompts.
Hooks do NOT store every prompt.
Hooks do NOT call an LLM.
Only triage-accepted prompts become TurnEvidence.
```

Boring instructions ("rewrite this", "continue", "explain this error") match no triage signal and never
leave the machine. Producers **fail open**: if Menhir is unreachable, git is absent, or the input is
malformed, they log locally (never the prompt text) and exit 0 without blocking the host agent.

## Files

| File | Role |
|------|------|
| `menhir_turn_evidence_common.py` | **Shared core** — the one triage, provenance, POST, fail-open, dry-run, and health implementation. |
| `menhir_turn_evidence.py` | Claude Code producer (`UserPromptSubmit` hook, `source_client="claude_code"`). |
| `menhir_opencode_turn_evidence.py` | OpenCode producer (driven by the `chat.message` plugin, `source_client="opencode"`). |
| `menhir_codex_turn_evidence.py` | Codex producer (`UserPromptSubmit` hook, `source_client="codex"`). |
| `menhir_memory_admission.py` | **`PostToolUse` companion** — joins a written memory to the turn that prompted it. See "Part 1b". |
| `codex-hooks.example.json` | Example Codex `hooks.json` registration. |

The three producers are thin adapters over the shared core; a cross-client **parity test**
(`tests/test_producer_pack.py`) asserts they share byte-for-byte identical triage, so they cannot drift.

## Install

### Claude Code
Register the `UserPromptSubmit` hook in project or user `.claude/settings.local.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "C:\\...\\menhir\\.venv\\Scripts\\python.exe",
        "args": [ "C:\\...\\menhir\\scripts\\hooks\\menhir_turn_evidence.py" ],
        "timeout": 8 } ] }
    ]
  }
}
```

### OpenCode
Install the plugin from [`scripts/opencode-plugin/`](../opencode-plugin/README.md) — it observes each
`chat.message` and pipes the prompt to `menhir_opencode_turn_evidence.py`.

### Codex
Merge `codex-hooks.example.json` into your Codex `hooks.json` (Codex exposes a Claude-compatible
`UserPromptSubmit` event). Codex pipes the event JSON to the script on stdin, exactly like Claude.

## Dry-run and health (all producers)

Neither command POSTs, and neither prints the API key or (by default) the prompt text.

```bash
# Would this prompt be captured? (triage only, no POST)
echo '{"prompt":"I have 25 movies"}' | python menhir_codex_turn_evidence.py --dry-run
# -> would_capture: true / triage_reasons: ["i_have","number"] / source_client: "codex" ...

# Local producer config check (no POST, no secrets)
python menhir_codex_turn_evidence.py --health
# -> menhir_url / api_key_configured: yes|no / source_client / versions / git_available / cwd ...
```

## Configuration (env, shared by all producers)

| Var | Meaning |
|-----|---------|
| `MENHIR_TURNS_URL` | Endpoint. Default `http://127.0.0.1:8100/api/turn-evidence`. |
| `MENHIR_AGENT_KEY` | Bearer token (agent tier). Unset => unauthenticated POST attempt. |
| `MENHIR_TURN_NAMESPACE` | Namespace override; else inferred from the cwd/project basename. |
| `MENHIR_TURN_HOOK_LOG` | Failure-log path; else `<home>/.claude/menhir-turn-hook.log`. |
| `MENHIR_TURN_EVIDENCE_ENABLED` | Set falsey (`0`/`false`/`no`/`off`) to disable capture (fail-open no-op). Unset => enabled. |
| `MENHIR_TURN_EVIDENCE_DRY_RUN` | Set truthy to force dry-run everywhere (never POST). |

## What is NOT captured

No assistant turns, no tool turns, no full transcripts, no LLM calls, no raw evidence in normal recall.
Only user prompts that pass deterministic triage. To disable a producer, set
`MENHIR_TURN_EVIDENCE_ENABLED=0` or remove its hook registration.

---

# Part 1b: memory admission (`PostToolUse`)

`menhir_memory_admission.py` closes the provenance loop the `UserPromptSubmit` producer opens.

```
UserPromptSubmit  ->  capture the turn, stash the server's turn_id (keyed by the HOST session_id)
add_memory        ->  a memory is written
PostToolUse       ->  read episode_id from the tool result, pair it with the stashed turn,
                      POST /api/episode-admission
```

Menhir then draws `(:Episodic)-[:ADMITTED_ON]->(:TurnEvidence)` and mints an **evidence projection** —
a non-recallable `:Episodic` holding the turn's verbatim text — so a typed scalar assertion extracted
from the user's words can bind to entities extracted from those same words, instead of from the
agent's paraphrase of them.

**Why a second hook and not an argument.** `PostToolUse` fires *after* the tool call, so it cannot add
`turn_evidence_uuid` to an `add_memory` that already ran — it can only report the pairing afterwards.
And the pairing cannot be made server-side: Menhir's own MCP `session_id` is a derived constant,
identical across every window, so the server cannot tell two conversations apart. The host's
`session_id` can, and both hooks see it.

**Doing nothing is the normal outcome.** Most `add_memory` calls have no stashed turn (the prompt was
a non-candidate), and the hook draws nothing. It never falls back to an older turn or another
session's — a wrong link is worse than a missing one here.

## Install (Claude Code)

Requires the `UserPromptSubmit` producer above to be registered too; alone it has nothing to read.

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "mcp__memory__add_memory",
        "hooks": [ { "type": "command",
          "command": "C:\\...\\menhir\\.venv\\Scripts\\python.exe",
          "args": [ "C:\\...\\menhir\\scripts\\hooks\\menhir_memory_admission.py" ],
          "timeout": 8 } ] }
    ]
  }
}
```

## Verify

```bash
# what it WOULD link, without posting (pipe a PostToolUse event on stdin)
python scripts/hooks/menhir_memory_admission.py --dry-run
python scripts/hooks/menhir_memory_admission.py --health   # never prints the key
```

Then, after a triage-worthy prompt followed by an `add_memory`:

```cypher
MATCH (e:Episodic)-[:ADMITTED_ON]->(t:TurnEvidence) RETURN e.uuid, t.turn_id, t.text LIMIT 5
MATCH (p:Episodic) WHERE p.is_evidence_projection RETURN p.uuid, p.content, p.processing_state LIMIT 5
```

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `MENHIR_ADMISSION_URL` | derived from `MENHIR_TURNS_URL` | Override the endpoint. Normally unset — one host setting, not two. |
| `MENHIR_TURN_STASH_DIR` | `~/.claude/menhir-turns` | Where turn ids are parked between the two hooks. |
| `MENHIR_TURN_EVIDENCE_ENABLED` | enabled | Falsey disables this hook too. |

## What this link does NOT mean

It records that a memory was written **in the context of** a captured turn. It is **not** proof a
human said the memory's content. Every id involved is supplied by whoever holds the agent key, and
`/api/turn-evidence` accepts `role` and `declarant` from the caller — so a client can post a turn
claiming `declarant='user'` and cite it. Treat the edge as provenance, never as verification. See the
CORRECTION in `.agent/plans/menhir-evidence-projection-episodes.md`.

---

# Part 2: Hook Center / file-event hook

`menhir_file_event.py` — observe file edit/write/delete/rename events and POST normalized events to
Menhir's `/api/tool-events` endpoint, which marks the affected structure-file node dirty. This makes
stale file-anchored memories detectable without relying on an LLM to call a memory tool.

Full docs: [`docs/hook-center-tool-events.md`](../../docs/hook-center-tool-events.md).

## The invariant

```
Hooks observe file/tool events.
Hooks do NOT capture raw transcripts.
Hooks do NOT upload file contents by default.
Hooks fail open.
Menhir uses events to mark file-linked context dirty/stale.
```

## Supported clients

Claude Code and Codex (both share the `PostToolUse` hook shape). OpenCode has no clean file-event
hook surface — documented limitation.

## Quick install

Register on `PostToolUse` in your Claude or Codex config, matched to the file tools:

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [ { "type": "command",
          "command": "C:\\...\\menhir\\.venv\\Scripts\\python.exe",
          "args": [ "C:\\...\\menhir\\scripts\\hooks\\menhir_file_event.py" ],
          "timeout": 8 } ] }
    ]
  }
}
```

See `file-event-hooks.example.json` for a ready-to-merge registration block.

## Dry-run

Test without posting:

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"src/foo.py"}}' \
  | python menhir_file_event.py --dry-run
# {"would_send": true, "operation": "edit", "path": "src/foo.py", "content_uploaded": false}
```

## Configuration

| Var | Meaning |
|-----|---------|
| `MENHIR_TOOL_EVENTS_URL` | Endpoint. Default `http://127.0.0.1:8100/api/tool-events`. |
| `MENHIR_AGENT_KEY` | Bearer token (agent tier). Unset => unauthenticated attempt. |
| `MENHIR_SOURCE_CLIENT` | Override the detected client name. |
| `MENHIR_ARTIFACT_RECONCILE_REPOSITORY` | Stable graph repository identity. Preferred over repository-local Git config `menhir.artifactRepository`; never inferred from a worktree basename. |
| `MENHIR_TURN_EVIDENCE_ENABLED` | Set falsey (`0`/`false`/`no`/`off`) to disable. |
| `MENHIR_TURN_HOOK_LOG` | Failure-log path. |

Set the shared identity once per repository when the environment variable is not appropriate:

```bash
git config --local menhir.artifactRepository menhir
```

## What is NOT captured

No file contents, no assistant turns, no tool transcripts, no raw transcripts. The file hash is a
local sha256 digest, never uploaded as content.

## Live smoke

A live end-to-end smoke harness (`scripts/smoke/hook_center_live_smoke.py`) validates Hook Center
components against a throwaway Menhir instance. See the
[smoke runbook](../../docs/runbooks/hook-center-live-smoke.md) for details.

---

# Part 3: Policy Guard

`menhir_policy_guard.py` — optional `PreToolUse` hook that can warn or block edits to protected
files before the tool runs. Reads local policy from `.menhir/policy.json`. Never calls an LLM,
never uploads file contents. Fail-open: no policy file or malformed input exits 0.

Full docs: [`docs/hook-center-tool-events.md`](../../docs/hook-center-tool-events.md).

## Quick install

Register on `PreToolUse` alongside the file-event hook:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [ { "type": "command",
          "command": "C:\\...\\menhir\\.venv\\Scripts\\python.exe",
          "args": [ "C:\\...\\menhir\\scripts\\hooks\\menhir_policy_guard.py" ],
          "timeout": 5 } ] }
    ]
  }
}
```

## Config

| Var | Meaning |
|-----|---------|
| `MENHIR_POLICY_FILE` | Path to policy JSON (default `.menhir/policy.json`). |
