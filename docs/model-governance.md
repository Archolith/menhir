# Model and version governance record (AI-G01)

**Purpose:** record every configurable LLM and embedding role Menhir invokes, where its
code default is defined, and how an operator changes it without a silent model upgrade.

**Verified against source:** 2026-08-30, package version `0.2.0`, Python `>=3.12`.

This document records source behavior. It does not claim which models a live deployment
currently uses. Deployment selection belongs in that deployment's protected environment
and release evidence, not in the public repository.

## Governance stance

- Model identifiers and providers are explicit configuration. Menhir does not query a
  provider for a `latest` model or silently rewrite configured identifiers.
- Chat, Graphiti extraction, embedding, and reranking resolve independently. This allows
  an operator to separate generation from extraction or embedding without changing code.
- Empty role-specific settings inherit the documented parent provider or model; they do
  not invoke automatic model discovery.
- API keys and operator credentials are secrets. Model identifiers are not.
- Experimental paths remain subject to the repository's
  [activation ledger](../.agent/default-off-features.md), regardless of model choice.

## Models by role

Defaults are defined in `src/menhir/config/settings_model.py` and can be overridden by the
listed environment variables.

| Role | Setting | Environment override | Code default |
|---|---|---|---|
| Chat provider | `chat_provider` | `LLM_CHAT_PROVIDER` / `MEMORY_CHAT_PROVIDER` | `local` |
| Local chat model | `local_llm_chat_model` | `LOCAL_LLM_CHAT_MODEL` | `qwen3.5-35b-a3b` |
| Local embedding model | `local_llm_embed_model` | `LOCAL_LLM_EMBED_MODEL` | empty |
| OpenAI chat model | `openai_chat_model` | `OPENAI_CHAT_MODEL` | `gpt-4o-mini` |
| OpenAI embedding model | `openai_embed_model` | `OPENAI_EMBED_MODEL` | `text-embedding-3-small` |
| Gemini chat model | `gemini_chat_model` | `GEMINI_CHAT_MODEL` | `gemini-2.5-flash` |
| Graphiti extraction provider | `graphiti_provider` | `GRAPHITI_LLM_PROVIDER` / `MEMORY_GRAPHITI_PROVIDER` / `GRAPHITI_PROVIDER` | `local` |
| Graphiti embedding provider | `graphiti_embed_provider` | `GRAPHITI_EMBED_PROVIDER` / `MEMORY_GRAPHITI_EMBED_PROVIDER` | inherits extraction provider |
| Graphiti reranker provider | `graphiti_reranker_provider` | `GRAPHITI_RERANKER_PROVIDER` | inherits extraction provider |
| Personal-memory consolidation model | `personal_memory_consolidation_chat_model` | `MENHIR_PERSONAL_MEMORY_CHAT_MODEL` | empty; inherits global chat model |

Provider-specific base URLs and credentials are configured separately. See
[`.env.example`](../.env.example) for the complete operator-facing contract.

## Provider libraries

The checked-in [`sbom.json`](../sbom.json) inventories the installed environment used for
its generation. AI and graph client versions in that artifact are:

| Library | Version |
|---|---:|
| `openai` | 2.45.0 |
| `graphiti-core` | 0.29.2 |
| `joserfc` | 1.7.3 |
| `neo4j` | 6.2.0 |
| `pydantic` | 2.13.4 |

Those are SBOM observations, not substitutes for source dependency pins or a live release
manifest. Review `pyproject.toml`, regenerate the SBOM from the release build environment,
and bind both into release authority when dependencies or models change.

## Audit procedure

1. Compare the `MemorySettings` defaults and environment aliases in
   `src/menhir/config/settings_model.py` with the table above.
2. Check `.env.example` for every public operator-facing setting. Do not read or publish a
   real `.env` as repository documentation.
3. Regenerate `sbom.json` from the clean release build environment using the procedure in
   [`governance.md`](governance.md).
4. Record live model choices only in protected deployment and release evidence.
