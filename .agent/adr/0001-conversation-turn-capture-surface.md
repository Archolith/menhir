# ADR 0001 — Conversation Turn Capture Surface

- **Status:** ACCEPTED as target architecture (2026-07-07). **Implementation deferred** — gated on a
  real Turn producer (see below). The immediate Phase 3 patch ships without it.
- **Date:** 2026-07-07
- **Deciders:** ctharvey (accepted via handoff 2026-07-07)
- **Related:** `.agent/plans/phase3-canonicalization-guards.md`,
  `.agent/plans/phase3-extractor-matrix-results.md`,
  `.agent/archive/plans/perception-consolidation-prod-wiring.md`

## Context

Phase 3 (`consolidate_personal_memory`) folds count/amount Views ("bike spend", "movies on my watch
list") from **user-authored conversational turns**. Verified this session:

- The live graph has **1,718 `Episodic` nodes and 0** with any of `role` / `speaker` / `source_kind`
  / `actor` / `declarant`. Every `source` is an agent/tool id (`claude-code` 970, `codex` 374,
  `opencode` 112, `claude-chat` 27, ...). There is **no user-vs-assistant dimension** in the model.
- `add_memory` stores `text` verbatim; nothing prepends a role. The `user:` prefix the dirty
  detector currently keys on is **LME-benchmark conversational-turn format only** — never in real
  ingest.

So Menhir has **no conversational-turn capture surface**. The Phase 3 extractor/gate/canonicalization
work is sound and the matrix picked gpt-4o-mini @ k=3, but **a correct extractor folds nothing if no
user-authored evidence is captured.** This ADR records the accepted target for that capture.

## Decision drivers

- Perception is **precision-first** and must know the **declarant** — a stated total from the *user*
  is evidence; the same words in an *assistant* turn ("You said you have 25 movies...") must not be
  folded. Role is load-bearing.
- Must **not re-shape existing memory ingest** — turns are a different lifecycle class from curated
  memory.
- Must carry enough **provenance to audit** a later View: the exact span, message/session, when
  recorded, when asserted to hold.
- Cheap on the hot path — capture must add no LLM cost per turn.
- Phase 3's evidence filter must key on **metadata**, not a text prefix.

## Claude MVP clarification (2026-07-07)

Turn capture is **selective**, not transcript logging. The Claude producer stores `:TurnEvidence`
records (not every turn):
- The hook **observes every user prompt** but stores only prompts that pass **deterministic, local,
  LLM-free triage** (a number, money, a date, a possession/preference/decision/correction signal).
- **Non-candidate prompts are discarded by default** ("rewrite this", "continue"): never stored,
  never sent anywhere.
- **No LLM runs during triage or capture.** The model never decides whether the raw turn is captured.
- Raw `:TurnEvidence` is **not default recall material**; only Phase 3 reads it.
- A full-transcript mode is possible only as an **explicit future opt-in**, never the default.

## Producer Pack v1: OpenCode + Codex + shared core (2026-07-08)

The producer hose widened from one faucet (Claude) to three, all feeding the **same**
`/api/turn-evidence` contract with no change to triage semantics, Phase 3, View logic, or any consumer
behavior. Full guide: `docs/turn-evidence-producers.md`.

- **OpenCode** (`source_client="opencode"`): no native shell-command prompt hook, so a thin JS plugin
  (`scripts/opencode-plugin/menhir-turn-evidence.js`) observes each `chat.message` and pipes the prompt
  to `scripts/hooks/menhir_opencode_turn_evidence.py` on stdin.
- **Codex** (`source_client="codex"`): exposes a Claude-compatible `UserPromptSubmit` event in
  `hooks.json`, so `scripts/hooks/menhir_codex_turn_evidence.py` is registered exactly like the Claude
  hook (event JSON on stdin). A small normaliser maps Codex's `user_prompt`/`workspace_root` aliases.
- **Shared core** (`scripts/hooks/menhir_turn_evidence_common.py`): the ONE implementation of triage,
  provenance, POST, fail-open, dry-run, and health. All three producers are thin adapters that supply
  only their identity constants; a cross-client parity test asserts their triage is byte-for-byte
  identical (behavioral + structural — they share the same triage objects), so producers cannot drift.
- **Ergonomics**: every producer supports `--dry-run` (print would-capture/drop, never POST) and
  `--health` (print local config, never POST, never print the API key or prompt text), plus
  `MENHIR_TURN_EVIDENCE_ENABLED` (falsey => disable, fail-open) and `MENHIR_TURN_EVIDENCE_DRY_RUN`.

The pack is producer-side only. Still user-prompt-only: no assistant turns, no tool turns, no transcript
mode, no new triage categories, no Phase 3/View/consumer changes. The Claude producer's capture behavior
and identity labels are unchanged (verified by its existing suite + the parity test).

## Decision

**Accepted: Option B — a first-class evidence record type, separate from `:Episodic` memory.** The
node label is **`:TurnEvidence`** (renamed from `:Turn` to signal it is selective captured evidence,
not a transcript). `TurnEvidence ≠ Memory`: it is *raw evidence*; a Memory/View is a *promoted or
derived product*.

### Considered options
- **A — bolt role/source fields onto `Episodic`.** Rejected: mixes two lifecycle classes (raw
  turns: many/cheap/noisy/disposable/not recallable; curated memories: fewer/enriched/durable/
  recallable). Raw chat would pollute recall and decay, duplicate evidence, and let agent summaries
  be mistaken for user statements.
- **B — separate `:Turn` type (ACCEPTED).** Clean separation; turns don't distort memory recall/
  decay; Phase 3 keys on `:Turn {role:'user'}` metadata; existing ingest untouched. Cost: a new type
  + a small ingest surface + a producer.
- **C — reinterpret existing sources (`claude-chat`/`claude-code`/`codex`/`opencode`) as
  user-authored.** Rejected: agent/tool-written records that may *describe* what the user said but
  are not user statements. Would poison the precision boundary.
- **D — status quo (benchmark `user:` prefix).** Rejected as an end state; fixtures only.

### Target flow
```
conversation source -> :Turn records -> Phase 3 reads role=user turns
  -> perception extracts base events / stated measures -> deterministic folds derive Views
  -> normal recall sees Views/memories, NOT raw chat
```

### Minimal `:Turn` shape
```
turn_id, session_id, namespace,
role: user | assistant | tool | agent,   # REQUIRED — the declarant class
speaker / declarant, text, occurred_at?, recorded_at, source_kind, source_id, metadata
```
Optional/future: `message_index, thread_id, conversation_id, tool_name, model_name, provider,
asserted_at` beyond the source-message time, `anchor_relation, redaction_state`.

`recorded_at` is the server receive time and monotonic processing cursor. `occurred_at` is optional
source/world time for replay and import producers; when supplied it becomes the assertion/view
validity basis. Live hooks omit it and therefore fall back to `recorded_at`.

## Semantics (load-bearing)

### Declarant is captured, never inferred from prose
- Good: `Turn.role=user`, `Turn.text="I like X"` -> declarant = user.
- Bad: read a memory "The user likes X" and *assume* declarant = user.

### Legacy agent-authored records stay legacy
Existing `Episodic` records are treated as `recorded_by = agent/tool source`, `declarant =
unknown/agent`, `basis = legacy_agent_memory`. They are **not** retroactively upgraded into user
statements unless they contain a preserved raw user span with reliable metadata.

### The evidence ladder
```
Turn               raw transcript/evidence
ExtractedBaseEvent span-grounded typed event from a Turn (or other admitted source)
StatedMeasure      a value explicitly stated in a Turn/source span
FoldDerivedView    deterministic aggregate/current state produced by a fold
```
- "I bought one bike for $50 and another for $75" -> two purchase base events -> fold ->
  `bike_spend_total = 125`.
- "I have 25 movies on my watch list" -> `StatedMeasure watchlist.item_count = 25`.
- "I bought an iPhone" -> `iphone_count = 1` is INVALID direct perception output; reject/quarantine
  unless a deterministic fold over base events produces it. (Enforced today by count-floor + the
  stated-span guard.)

## The producer dependency (the real gate)

**Adding `:Turn` is not enough. We must commit to wiring at least one producer** that actually writes
Turns — an agent wrapper, MCP adapter, chat-client hook, Claude/OpenAI/Gemini export importer, local
conversation middleware, browser extension, or IDE agent bridge. Without a producer, `:Turn` stays
empty and Phase 3 still has no user-authored evidence. The schema is the easy part; the producer is
the decision that makes this real.

## Topics the build (follow-up ADR/plan) must resolve
- Why Turn is separate from Memory (settled above).
- Turn schema + how Turns link to Session/Namespace.
- Whether raw Turns are ever recallable by default (default: **no**).
- Retention / decay policy for raw Turns.
- Privacy / redaction policy (`redaction_state`).
- Migration / backfill policy (default: **no backfill**; legacy stays `legacy_agent_memory`).
- Producer requirements and which producer ships first.
- How Phase 3 consumes Turns: dirty query on `:Turn {role:'user'}` replaces `content STARTS WITH
  'user:'`; `is_user_authored_evidence` keys on `role`/`source_kind`, prefix demoted to fixture-only
  fallback.

## Consequences
- **Positive:** Phase 3 becomes exercisable on real data; the precision guarantee is enforceable
  (declarant known); memory recall/decay is not polluted by raw turns.
- **Cost/work:** new node type + ingest surface + a real producer (the gating dependency).
- **Migration:** none required; additive. Benchmark path keeps working via the prefix fallback.

## Non-goals (unchanged, deferred)
Full Turn ingest API now; role backfill onto legacy memories; treating `claude-chat` as user; moving
raw turns into `Episodic`; a full evidence-law admission layer; global STATEMENT-only perception;
changing `fold_algebra` semantics; IdentityView; crossdating; View nucleation.
