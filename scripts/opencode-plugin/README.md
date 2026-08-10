# Menhir TurnEvidence — OpenCode producer

One [`:TurnEvidence`](../../.agent/adr/0001-conversation-turn-capture-surface.md) producer (alongside
Claude Code and Codex) that feeds the **same** `/api/turn-evidence` contract. It lets OpenCode
contribute candidate user prompts to Menhir's Phase 3 personal-memory consolidation without changing
triage, Phase 3, View logic, or any consumer behavior. For the full producer system (all clients,
shared core, dry-run/health, env), see [`docs/turn-evidence-producers.md`](../../docs/turn-evidence-producers.md)
and [`scripts/hooks/README.md`](../hooks/README.md).

## What this is

Two files:

| File | Role |
|------|------|
| `menhir-turn-evidence.js` | Thin OpenCode plugin. On every `chat.message` it extracts the user prompt and pipes a JSON envelope to the Python producer on stdin. No triage, no network. |
| `../hooks/menhir_opencode_turn_evidence.py` | The producer. Runs the same deterministic, LLM-free triage as the Claude hook and POSTs only candidate prompts to `/api/turn-evidence` with `source_client="opencode"`. Fail-open. |

The plugin is only *wiring* — the equivalent of the Claude hook's registration in
`.claude/settings.local.json`. All evidence semantics live in the Python producer, which is a thin
adapter over the shared core `../hooks/menhir_turn_evidence_common.py`; a cross-client parity test
(`tests/test_producer_pack.py`) asserts every producer's triage is byte-for-byte identical, so they
cannot drift.

## Install

OpenCode auto-loads `*.js` plugins from `~/.config/opencode/plugin/` (global) or a project's
`.opencode/plugin/` directory.

1. Copy or symlink the plugin into one of those directories, e.g. (PowerShell):

   ```powershell
   New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\opencode\plugin"
   Copy-Item .\menhir-turn-evidence.js "$env:USERPROFILE\.config\opencode\plugin\"
   ```

2. Tell the plugin where the producer and a python interpreter live (defaults resolve to the menhir
   repo's venv relative to the plugin file, which only works for an in-repo/symlinked copy):

   ```
   MENHIR_OPENCODE_PYTHON     absolute path to python (default: <menhir>/.venv/Scripts/python.exe)
   MENHIR_OPENCODE_PRODUCER   absolute path to menhir_opencode_turn_evidence.py
   ```

   The producer is stdlib-only, so any Python 3 works — it does not need the menhir package installed.

3. Optional producer configuration (read by the Python side):

   ```
   MENHIR_TURNS_URL       default http://127.0.0.1:8090/api/turn-evidence
   MENHIR_AGENT_KEY       bearer token (agent tier)
   MENHIR_TURN_NAMESPACE  namespace override; else inferred from the project directory basename
   MENHIR_TURN_HOOK_LOG   failure-log path; else <home>/.claude/menhir-turn-hook.log
   ```

## Guarantees

- **Observe every prompt, store only candidates.** Boring instructions ("rewrite this", "continue")
  match no triage signal and never leave the machine.
- **Never blocks OpenCode.** The plugin spawns the producer fire-and-forget and swallows every error;
  the producer exits 0 on any failure (Menhir down, git absent, malformed input).
- **No LLM, no transcript logging, no assistant/tool turns.** Only user-authored text parts are
  forwarded, exactly as the Claude MVP producer does.
- **Provenance identifies the producer.** Captured nodes carry `source_client="opencode"`,
  `source_kind="opencode_hook"`, `triage_version="opencode-hook-v1"`.
