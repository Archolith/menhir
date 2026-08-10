/**
 * Menhir TurnEvidence producer plugin for OpenCode (ADR 0001).
 *
 * OpenCode has no native shell-command prompt hook (unlike Claude Code's `UserPromptSubmit`), so this
 * thin plugin is the WIRING layer: it observes every `chat.message`, extracts the user's prompt, and
 * pipes a JSON envelope to the Python producer (`scripts/hooks/menhir_opencode_turn_evidence.py`) on
 * stdin. All evidence semantics -- deterministic triage, junk-drop, provenance, fail-open -- live in
 * the Python producer, exactly as they do for the Claude hook. This file does NOT triage and does NOT
 * decide what is stored; it only forwards the raw prompt.
 *
 * Contract, matching the Claude producer:
 *   - Observe every prompt; the producer stores only triage candidates.
 *   - Never block OpenCode: spawn is fire-and-forget and every error is swallowed.
 *   - No LLM here; no network here (the producer owns the POST).
 *
 * Install (not auto-wired, like the Claude settings.json registration):
 *   1. Copy or symlink this file into `~/.config/opencode/plugin/` (global) or a project's
 *      `.opencode/plugin/` directory. OpenCode auto-loads `*.js` plugins from those dirs.
 *   2. Point the plugin at the menhir venv python and producer script via env, or edit the defaults
 *      below:
 *        MENHIR_OPENCODE_PYTHON   absolute path to a python that can run stdlib scripts
 *                                 (default: the menhir venv python)
 *        MENHIR_OPENCODE_PRODUCER absolute path to menhir_opencode_turn_evidence.py
 *   3. Optional producer env (read by the Python side, not here):
 *        MENHIR_TURNS_URL, MENHIR_AGENT_KEY, MENHIR_TURN_NAMESPACE, MENHIR_TURN_HOOK_LOG
 *
 * See scripts/opencode-plugin/README.md for details.
 */

import { spawn } from "node:child_process";
import path from "node:path";

// Repo layout: this file lives at <menhir>/scripts/opencode-plugin/menhir-turn-evidence.js, and the
// producer at <menhir>/scripts/hooks/menhir_opencode_turn_evidence.py. Resolve relative to this file
// so a symlinked/global install still finds the producer via env override.
const HERE = path.dirname(new URL(import.meta.url).pathname);
const DEFAULT_PRODUCER = path.resolve(HERE, "..", "hooks", "menhir_opencode_turn_evidence.py");
const DEFAULT_PYTHON = path.resolve(HERE, "..", "..", ".venv", "Scripts", "python.exe");

/** Concatenate the user-authored text parts of an OpenCode message. Synthetic/ignored parts and
 *  non-text parts (reasoning, tool calls, files) are skipped -- we only want what the user typed. */
function extractPromptText(parts) {
  if (!Array.isArray(parts)) return "";
  const chunks = [];
  for (const part of parts) {
    if (!part || part.type !== "text" || part.synthetic || part.ignored) continue;
    if (typeof part.text === "string" && part.text.trim()) chunks.push(part.text);
  }
  return chunks.join("\n").trim();
}

export const MenhirTurnEvidencePlugin = async ({ directory, worktree }) => {
  const python = process.env.MENHIR_OPENCODE_PYTHON || DEFAULT_PYTHON;
  const producer = process.env.MENHIR_OPENCODE_PRODUCER || DEFAULT_PRODUCER;

  return {
    /** Observe every user message; forward the raw prompt to the Python producer for triage. */
    "chat.message": async (input, output) => {
      try {
        const prompt = extractPromptText(output && output.parts);
        if (!prompt) return; // nothing the user typed -> nothing to observe

        const envelope = JSON.stringify({
          prompt,
          session_id: input && input.sessionID,
          cwd: worktree || directory || process.cwd(),
          hook_event_name: "chat.message",
        });

        // Fire-and-forget: detach from OpenCode's flow. Any spawn/pipe error is swallowed so the
        // producer can NEVER block or crash the chat turn (fail-open, matching the Claude hook).
        const child = spawn(python, [producer], {
          stdio: ["pipe", "ignore", "ignore"],
          windowsHide: true,
        });
        child.on("error", () => {}); // e.g. python not found -> ignore
        child.stdin.on("error", () => {}); // e.g. EPIPE if the child exits early -> ignore
        child.stdin.write(envelope);
        child.stdin.end();
      } catch {
        // Producer wiring must never disrupt OpenCode. Swallow everything.
      }
    },
  };
};

export default MenhirTurnEvidencePlugin;
