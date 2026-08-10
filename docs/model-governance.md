# Model & Version Governance Record (AI-G01)

**Purpose:** a durable record of every LLM/embedding model Menhir invokes, which provider serves it,
where it is configured, and the governance stance (pinned, operator-selected, no silent auto-upgrade).
This is the AI-G01 governance artifact required for launch. Update it whenever a default model,
provider, or the production `.env` model selection changes.

**Last updated:** 2026-07-10 (menhir `main`). Package version `0.2.0`, `requires-python >=3.12`.

## Governance stance

- **Models are explicit configuration, never auto-selected.** Every model is a named default in
  `src/menhir/config/settings.py` overridable by an environment variable (below). Menhir does not
  query a provider for "latest" or auto-upgrade a model version; a model changes only when an operator
  edits config/`.env`.
- **Provider is selected per role.** Chat, Graphiti extraction, Graphiti embedding, and Graphiti
  reranking each resolve a provider independently (`local` / `openai` / `gemini`), so extraction can
  run on a different model than the chat path (this separation exists because a past misconfig ran
  extraction on the wrong model — see `settings.py` provider-alias notes).
- **No token/model identifiers are secrets**; API keys are (handled per `docs/security-posture.md`).
- **Determinism knobs are pinned, not model-selected:** Phase 3 consolidation pins all bias guards on
  (`cross_check`/`coref`/`verify`) regardless of model; SUM grounding default-on.

## Models by role (code defaults + production `.env` selection)

| Role | Setting (default) | Env override | Code default | Production `.env` (2026-07-10) |
|---|---|---|---|---|
| Chat provider | `chat_provider` (`local`) | `LLM_CHAT_PROVIDER` / `MEMORY_CHAT_PROVIDER` | `local` | `openai` |
| Chat model (local) | `local_llm_chat_model` | `LOCAL_LLM_CHAT_MODEL` | `qwen3.5-35b-a3b` | (unused; provider=openai) |
| Chat model (OpenAI) | `openai_chat_model` | `OPENAI_CHAT_MODEL` | `gpt-4.1-mini` | `gpt-4.1-nano` |
| Chat model (Gemini) | `gemini_chat_model` | `GEMINI_CHAT_MODEL` | `gemini-2.5-flash` | (unused) |
| Embedding (OpenAI) | `openai_embed_model` | `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | `text-embedding-3-small` |
| Embedding (local) | `local_llm_embed_model` | `LOCAL_LLM_EMBED_MODEL` | (empty) | `nomic-embed-text-v1.5.Q4_K_M.gguf` |
| Graphiti extraction provider | `graphiti_provider` (`local`) | `GRAPHITI_LLM_PROVIDER` / `GRAPHITI_PROVIDER` | `local` | `openai` |
| Graphiti embed provider | `graphiti_embed_provider` (inherits) | `GRAPHITI_EMBED_PROVIDER` | inherits `graphiti_provider` | `openai` |
| Graphiti reranker provider | `graphiti_reranker_provider` (inherits) | `GRAPHITI_RERANKER_PROVIDER` | inherits `graphiti_provider` | inherits |
| Phase 3 consolidation model | `personal_memory_consolidation_chat_model` (empty=global) | `MENHIR_PERSONAL_MEMORY_CHAT_MODEL` | (empty → global chat) | `gpt-4o-mini` |

Notes:
- **Production runs OpenAI** for chat + Graphiti (`gpt-4.1-nano` chat, `text-embedding-3-small`
  embeddings, `gpt-4o-mini` for Phase 3 consolidation). The local `qwen`/`nomic` models are the
  default-config fallback for a fully-local deployment, not what the live `:8090` server uses.
- **Phase 3 uses a dedicated model** (`gpt-4o-mini`) distinct from the global chat model
  (`gpt-4.1-nano`) so the cheaper model handles Graphiti enrichment while consolidation gets the
  stronger extractor (recovers measures `gpt-4.1-nano` abstains on — see
  `.agent/plans/phase3-extractor-matrix-results.md`).

## Provider SDK / library versions

Pinned dependency versions are inventoried in the repo-root **`sbom.json`** (CycloneDX 1.6). The
AI-relevant client libraries at this record's date:

| Library | Version |
|---|---|
| `openai` | 2.31.0 |
| `graphiti-core` | 0.28.2 |
| `joserfc` (OAuth JOSE) | 1.6.4 |
| `neo4j` (driver) | 5.28.3 |
| `pydantic` | 2.13.3 |

## How to regenerate / audit

- Model selection is authoritative in `src/menhir/config/settings.py` (`MemorySettings`) + the
  production `.env`. Diff those against this table on any model change.
- Dependency versions: regenerate `sbom.json` (see repo `sbom.json` header / `docs/` SBOM note).
