#!/usr/bin/env python3
"""Shared, client-agnostic TurnEvidence producer core (ADR 0001).

Single source of truth for every TurnEvidence producer -- Claude Code, OpenCode, Codex, and any future
client. The per-client hooks are THIN ADAPTERS that supply only their identity constants
(`source_client`/`source_kind`/`hook_version`/`triage_version`) and a small input-normaliser; all of
the durable behavior -- deterministic triage, git provenance, the POST, fail-open handling, dry-run,
and health/debug -- lives here so the producers cannot drift. A cross-client parity test asserts every
producer shares this exact triage.

Contract (identical for every producer):
- **Observe every prompt, store only candidates.** Non-candidates return None / exit 0, nothing stored.
- **No LLM.** Triage is pure regex / substring rules.
- **Never block the host agent.** Menhir down or erroring => log locally + exit 0.
- **Silent stdout on the normal path.** (`--dry-run` / `--health` print deliberately, on manual runs.)
- **Stdlib only.** Runs anywhere `python` runs; no menhir install required.
- **Redact on failure.** The failure log records the error + prompt length, never the prompt text.

The server derives `prompt_hash` and `recorded_at`; clients never become hash authorities (the
client-side `prompt_hash` here is provenance/correlation only and mirrors the server's derivation).

Config (env), consistent across producers:
  MENHIR_TURNS_URL                default http://127.0.0.1:8100/api/turn-evidence
  MENHIR_AGENT_KEY                bearer token (agent tier); unset => unauthenticated POST attempt
  MENHIR_TURN_NAMESPACE           optional namespace override; else inferred from cwd basename
  MENHIR_TURN_HOOK_LOG            optional failure-log path; else <home>/.claude/menhir-turn-hook.log
  MENHIR_TURN_EVIDENCE_ENABLED    set to a falsey value (0/false/no/off) to disable capture (fail-open)
  MENHIR_TURN_EVIDENCE_DRY_RUN    set truthy to force dry-run (triage only, never POST)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8100/api/turn-evidence"

# --- deterministic triage --------------------------------------------------------------------------
# The ONE definition of what counts as durable evidence. Every producer imports these; a parity test
# pins the producers to this table so they can never diverge on capture semantics.

_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_MONEY_RE = re.compile(r"\$\s*\d+|\b\d+\s?(?:usd|dollars|bucks|cents)\b")
_DATE_RE = re.compile(
    r"\b(?:today|yesterday|tomorrow|last\s+week|next\s+week|last\s+month|this\s+year|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)

#: deterministic phrase signals -> reason label. Kept intentionally small and local; tune as needed.
_PHRASE_RULES: dict[str, tuple[str, ...]] = {
    "i_have": ("i have", "i've got", "i own", "i keep"),
    "i_bought": ("i bought", "i purchased", "i got ", "i acquired"),
    "i_use": ("i use", "i'm using", "i am using"),
    "preference": ("i prefer", "i like", "i don't like", "i do not like", "i hate", "i love", "i enjoy"),
    "cessation": ("i no longer", "i stopped", "i quit", "cancelled", "canceled", "closed",
                  "retired", "deprecated", "archived", "shut down"),
    "remember": ("remember", "save this", "note that", "keep in mind", "for the record"),
    "change": ("changed from", "changed to", "now it is", "now it's", "current is", "currently",
               "the current", "switched to", "moved to"),
    "decision": ("we decided", "we agreed", "i decided", "decision:", "let's go with", "going with"),
    "correction": ("actually", "correction", "not anymore", "no longer", "to be clear", "i meant"),
    "possession_state": ("my ", "our "),
}


#: Wrapper tags the HOST injects into the `prompt` field. What is inside them is machine-generated --
#: background-task completions, slash-command envelopes, re-injected context -- and the user never
#: typed a character of it. It arrives on the same field as their words, so without this it is stored
#: `role='user', declarant='user'`: a claim about who said it that is simply false. ADR 0001 makes
#: declarant load-bearing precisely because downstream consumers may not second-guess it, so the lie
#: does not stay contained -- `create_evidence_projection` is fail-closed on `declarant='user'` and a
#: task notification walks straight through it into an `:Episodic` stamped `source='user'`.
_HARNESS_TAGS = (
    "task-notification",
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
)

#: Balanced block. The backreference pins the close tag to the tag that opened it, so a stray
#: `</command-args>` cannot terminate a `<system-reminder>` and swallow the user's words with it.
_HARNESS_BLOCK_RE = re.compile(
    r"<(" + "|".join(_HARNESS_TAGS) + r")\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)

#: An opened-but-never-closed harness block, stripped to end-of-prompt. Observed in the wild: 6 of 469
#: captured notifications arrived truncated mid-block. Treating the remainder as harness text can cost
#: a turn if a user ever literally types `<system-reminder>`; treating it as user text costs a stored
#: falsehood. Dropping a turn is recoverable, wrong provenance is not, so this errs toward dropping.
_HARNESS_UNCLOSED_RE = re.compile(
    r"<(?:" + "|".join(_HARNESS_TAGS) + r")\b[^>]*>.*\Z",
    re.DOTALL | re.IGNORECASE,
)


def strip_harness_blocks(text: str) -> str:
    """Return only what the USER typed, with host-injected blocks removed.

    Runs BEFORE triage, which matters twice over. The obvious effect is that a prompt which was
    nothing but a notification now has no text left and is dropped instead of stored as a user
    statement. The less obvious one is that triage stops reading machine text: 468 of the 469
    notifications captured in production qualified on reason `number`, matching the hex digits in
    their own `task-id` and `tool-use-id`. Stripping after triage would fix neither.

    Substitutes a space rather than empty string so removing a mid-sentence block cannot fuse the
    words on either side of it into one.
    """
    if not text:
        return ""
    out = _HARNESS_BLOCK_RE.sub(" ", text)
    out = _HARNESS_UNCLOSED_RE.sub(" ", out)
    return out.strip()


def triage_user_prompt(text: str) -> tuple[bool, list[str]]:
    """Deterministic, LLM-free triage. Returns (is_candidate, sorted_reasons). A prompt is a candidate
    if it matches any evidence signal; a boring instruction to the agent matches nothing and is
    dropped. This is the SINGLE triage every producer shares."""
    t = (text or "").lower()
    reasons: list[str] = []
    if _NUMBER_RE.search(t):
        reasons.append("number")
    if _MONEY_RE.search(t):
        reasons.append("money")
    if _DATE_RE.search(t):
        reasons.append("date_time")
    for reason, phrases in _PHRASE_RULES.items():
        if any(p in t for p in phrases):
            reasons.append(reason)
    return (bool(reasons), sorted(set(reasons)))


# --- provenance ------------------------------------------------------------------------------------


def infer_namespace(hook_input: dict) -> str:
    override = (os.environ.get("MENHIR_TURN_NAMESPACE") or "").strip()
    if override:
        return override
    cwd = hook_input.get("cwd") or ""
    base = os.path.basename(cwd.rstrip("/\\")) if cwd else ""
    return base or "default"


def prompt_hash(text: str) -> str:
    """Deterministic content fingerprint (sha256) of the prompt -- provenance, not the raw text.

    Mirrors Menhir's server-side ``derive_prompt_hash`` so the client can log/correlate which prompt
    it sent without keeping the text around. The server derives its own authoritative copy; this is
    correlation only."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


#: Back-compat alias: existing hook tests reference ``hook._prompt_hash``.
_prompt_hash = prompt_hash


def git_probe(args: list[str], cwd: str | None) -> str | None:
    """Run a read-only git command in ``cwd``; return stripped stdout or None. NEVER raises.

    Fail-open by design: no git, not a repo, timeout, or any error -> None. Git runs CLIENT-SIDE in
    the hook; Menhir never shells out to git, so the server has no git dependency."""
    if not cwd:
        return None
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if out.returncode != 0:
            return None
        value = (out.stdout or "").strip()
        return value or None
    except Exception:
        return None


def collect_provenance(hook_input: dict, prompt: str, *, git_fn, default_event: str | None = None
                       ) -> tuple[str | None, dict]:
    """Collect cheap, safe provenance for the metadata envelope. Returns (project_root, metadata).

    ``git_fn`` is injected (not a hard import) so each producer keeps a monkeypatchable module-level
    ``_git`` seam. All git lookups are fail-open; when git is unavailable the fields are None and
    capture proceeds unchanged. ``default_event`` supplies the client's native event name when the
    input omits ``hook_event_name`` (e.g. OpenCode 'chat.message')."""
    cwd = hook_input.get("cwd")
    project_root = git_fn(["rev-parse", "--show-toplevel"], cwd)
    # If cwd is not a git repo (or git is absent), the branch/commit lookups would fail too -- skip
    # them so a non-repo directory costs one git probe, not three.
    if project_root:
        git_branch = git_fn(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        git_commit = git_fn(["rev-parse", "--short", "HEAD"], cwd)
    else:
        git_branch = git_commit = None
    metadata = {
        "hook_event_name": hook_input.get("hook_event_name") or default_event,
        "permission_mode": hook_input.get("permission_mode"),
        "project_root": project_root,
        "git_branch": git_branch,
        "git_commit": git_commit,
        "prompt_hash": prompt_hash(prompt),
    }
    return project_root, metadata


def build_payload(hook_input: dict, *, source_kind: str, source_client: str, hook_version: str,
                  triage_version: str, git_fn, default_event: str | None = None) -> dict | None:
    """Map a normalised producer envelope to a /turn-evidence body IFF the prompt passes triage.
    Returns None when there is no prompt OR the prompt is a non-candidate (nothing to store).

    Every producer's ``build_evidence_payload`` is a one-line call to this with its own constants and
    module-level ``git_fn`` (so ``monkeypatch.setattr(hook, "_git", ...)`` still works)."""
    prompt = hook_input.get("prompt")
    if not prompt or not str(prompt).strip():
        return None
    # Strip BEFORE triage, and store the stripped text: everything below stamps this record
    # `declarant='user'`, so whatever survives here is what Menhir will assert the user said.
    prompt = strip_harness_blocks(str(prompt))
    if not prompt:
        return None  # the turn was entirely host-injected -- the user said nothing to record
    is_candidate, reasons = triage_user_prompt(prompt)
    if not is_candidate:
        return None
    session_id = hook_input.get("session_id")
    _project_root, metadata = collect_provenance(
        hook_input, prompt, git_fn=git_fn, default_event=default_event)
    return {
        "text": prompt,
        "role": "user",
        "declarant": "user",
        "session_id": session_id,
        "namespace": infer_namespace(hook_input),
        "source_kind": source_kind,
        "source_client": source_client,
        "hook_version": hook_version,
        "source_id": session_id or hook_input.get("transcript_path"),
        "cwd": hook_input.get("cwd"),
        "transcript_path": hook_input.get("transcript_path"),
        # Stable per-prompt id (Claude Code `prompt_id` UUID, CC v2.1.196+): unique per genuine turn,
        # stable across a double-fired retry -- the server keys idempotency on it so genuine repetitions
        # stay distinct evidence sources (G18). None on older CC / producers that omit it (server then
        # falls back to text-keying).
        "prompt_id": hook_input.get("prompt_id"),
        "triage_reason": reasons,
        "triage_version": triage_version,
        "metadata": metadata,
    }


# --- transport (fail-open) -------------------------------------------------------------------------


def log_failure(payload: dict, exc: Exception) -> None:
    """Record a capture failure locally WITHOUT storing the raw prompt text (privacy)."""
    path = (os.environ.get("MENHIR_TURN_HOOK_LOG") or "").strip()
    if not path:
        path = os.path.join(os.path.expanduser("~"), ".claude", "menhir-turn-hook.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "error": f"{type(exc).__name__}: {exc}",
                "source_client": payload.get("source_client"),
                "namespace": payload.get("namespace"),
                "session_id": payload.get("session_id"),
                "triage_reason": payload.get("triage_reason"),
                "prompt_len": len(payload.get("text") or ""),
            }) + "\n")
    except Exception:
        pass  # a failing failure-logger must never block the host agent


#: Back-compat alias: existing hook tests reference ``hook._log_failure``.
_log_failure = log_failure


def post_evidence(payload: dict, *, url: str | None = None, timeout: float = 3.0) -> str | None:
    """POST one evidence record; return the server's `turn_id`, or None on any failure.

    RETURNS THE turn_id, NOT A BOOL. `/api/turn-evidence` has always answered with
    `{turn_id, created, recorded_at}` and this function has always thrown it away, which left the id
    of the turn just captured knowable ONLY to the server. Everything downstream that wants to tie a
    memory back to the words that prompted it needs that id, and the capturing hook is the only place
    it is available at the right moment.

    A missing or unparseable `turn_id` is treated as failure-to-learn-the-id, NOT as a failed POST:
    the record may well have been stored. Callers must therefore treat None as "no id available",
    never as "nothing was captured".

    Still fail-open in every case -- a capture problem must never block the host agent."""
    url = url or os.environ.get("MENHIR_TURNS_URL") or DEFAULT_URL
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = (os.environ.get("MENHIR_AGENT_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read()
    except Exception as exc:  # network down, 4xx/5xx, timeout -- never block the host agent
        log_failure(payload, exc)
        return None
    try:
        turn_id = (json.loads(raw) or {}).get("turn_id")
    except Exception:
        return None  # stored, but the id is unavailable -- not a capture failure
    return str(turn_id) if turn_id else None


# --- config / CLI (dry-run + health) ---------------------------------------------------------------

_FALSEY = {"0", "false", "no", "off", ""}
_TRUTHY = {"1", "true", "yes", "on"}


def stash_path(session_id: str) -> str | None:
    """Where this session's most recent captured turn_id is parked, or None if unusable.

    A file, because the two hooks that need to agree are SEPARATE PROCESSES: `UserPromptSubmit`
    learns the turn_id, and a later `PostToolUse` on `add_memory` is what knows a memory was written.
    Nothing else spans them -- Menhir's own MCP `session_id` is a derived constant identical across
    every window (`api/auth.py`), so a server-side join cannot tell conversations apart. The host's
    `session_id` can, and both hooks see it.

    `session_id` is sanitized into the filename rather than interpolated: it arrives from the host's
    event JSON, and a value containing a path separator would otherwise write outside the directory.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:96]
    if not safe or safe.strip(".") == "":
        return None
    base = (os.environ.get("MENHIR_TURN_STASH_DIR") or "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".claude", "menhir-turns")
    return os.path.join(base, f"{safe}.json")


def stash_turn_id(session_id: str, turn_id: str) -> bool:
    """Record the turn_id just captured for this session. Best-effort, never raises."""
    path = stash_path(session_id)
    if not path or not turn_id:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"turn_id": turn_id, "session_id": session_id,
                       "recorded_at": time.time()}, fh)
        return True
    except Exception:
        return False  # a failed stash costs provenance, never the turn capture


def read_stashed_turn_id(session_id: str, *, max_age_s: float = 3600.0) -> str | None:
    """The most recent captured turn_id for this session, or None.

    AGE-BOUNDED ON PURPOSE. The stash is a 'most recent turn' pointer, so a stale one would attach a
    memory to whatever the user happened to say hours ago -- provenance that is confidently wrong,
    which is worse than absent. Past the bound it reports nothing rather than guessing.
    """
    path = stash_path(session_id)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if max_age_s and (time.time() - float(data.get("recorded_at") or 0)) > max_age_s:
            return None
        turn_id = data.get("turn_id")
        return str(turn_id) if turn_id else None
    except Exception:
        return None


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


def capture_enabled() -> bool:
    """Capture is on unless MENHIR_TURN_EVIDENCE_ENABLED is explicitly falsey. Unset => enabled, so
    existing producers behave exactly as before."""
    raw = os.environ.get("MENHIR_TURN_EVIDENCE_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def resolve_url() -> str:
    return os.environ.get("MENHIR_TURNS_URL") or DEFAULT_URL


def load_stdin_json() -> dict:
    """Read the host's event JSON from stdin, fail-open. Malformed / non-object => {}."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def format_dry_run(hook_input: dict, *, source_client: str, triage_version: str) -> str:
    """Concise, safe verdict for a would-be capture. Never prints the prompt text or secrets."""
    raw = str(hook_input.get("prompt") or "")
    # Mirror the capture path exactly, stripping included -- a dry run that triaged the raw prompt
    # would answer a question nobody asked and report captures that will not happen.
    prompt = strip_harness_blocks(raw)
    if not prompt:
        is_candidate, reasons = False, []
    else:
        is_candidate, reasons = triage_user_prompt(prompt)
    lines = [
        f"would_capture: {str(is_candidate).lower()}",
        f"triage_reasons: {json.dumps(reasons)}",
        f"source_client: {json.dumps(source_client)}",
        f"triage_version: {json.dumps(triage_version)}",
        f"prompt_length: {len(prompt)}",
        f"harness_stripped_chars: {len(raw) - len(prompt)}",
    ]
    return "\n".join(lines)


def format_health(*, source_client: str, source_kind: str, hook_version: str, triage_version: str,
                  git_fn) -> str:
    """Local producer config for a health/debug check. Never posts, never prints the API key or any
    prompt content -- only whether a key is configured."""
    key_set = bool((os.environ.get("MENHIR_AGENT_KEY") or "").strip())
    cwd = os.getcwd()
    git_ok = git_fn(["rev-parse", "--show-toplevel"], cwd) is not None
    lines = [
        f"menhir_url: {resolve_url()}",
        f"api_key_configured: {'yes' if key_set else 'no'}",
        f"capture_enabled: {'yes' if capture_enabled() else 'no'}",
        f"dry_run_env: {'yes' if _env_truthy('MENHIR_TURN_EVIDENCE_DRY_RUN') else 'no'}",
        f"source_client: {source_client}",
        f"source_kind: {source_kind}",
        f"hook_version: {hook_version}",
        f"triage_version: {triage_version}",
        f"git_available: {'yes' if git_ok else 'no'}",
        f"cwd: {cwd}",
        "dry_run_supported: yes",
    ]
    return "\n".join(lines)


def run_cli(argv: list[str], *, source_kind: str, source_client: str, hook_version: str,
            triage_version: str, git_fn, default_event: str | None = None,
            normalize=None) -> int:
    """Shared producer entrypoint. Handles `--health` and `--dry-run` (both never POST), the
    `MENHIR_TURN_EVIDENCE_DRY_RUN`/`MENHIR_TURN_EVIDENCE_ENABLED` env toggles, and the normal capture
    path. `normalize(raw)->dict` maps a client's raw event to the common envelope (prompt/session_id/
    cwd/transcript_path/hook_event_name); omit it for clients that already speak the common shape."""
    argv = argv or []

    if "--health" in argv:
        print(format_health(source_client=source_client, source_kind=source_kind,
                            hook_version=hook_version, triage_version=triage_version, git_fn=git_fn))
        return 0

    raw = load_stdin_json()
    hook_input = normalize(raw) if normalize is not None else raw

    if "--dry-run" in argv or _env_truthy("MENHIR_TURN_EVIDENCE_DRY_RUN"):
        print(format_dry_run(hook_input, source_client=source_client, triage_version=triage_version))
        return 0

    if not capture_enabled():
        return 0  # explicitly disabled -> fail-open no-op

    payload = build_payload(
        hook_input, source_kind=source_kind, source_client=source_client, hook_version=hook_version,
        triage_version=triage_version, git_fn=git_fn, default_event=default_event)
    if payload is None:
        return 0  # no prompt, or a non-candidate prompt that triage dropped
    turn_id = post_evidence(payload)
    # Park the id for a later PostToolUse on add_memory, which is the process that knows a memory was
    # written but has no other way to learn WHICH turn prompted it. Best-effort: a missed stash costs
    # provenance on one memory, never the capture itself.
    if turn_id:
        stash_turn_id(str(payload.get("session_id") or ""), turn_id)
    return 0
