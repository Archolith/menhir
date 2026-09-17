# Menhir provenance, governance, and code-context rules

Use the configured Menhir MCP access surface for provenance-linked context, governed
knowledge, and structural code analysis.

1. At session startup, call `read_flagged_memories` and then `recall_context_memories` with the same stable
   `reader_id` and the registered workspace key: `<workspace-key>`.
2. Before filesystem exploration, call `query_structure(query_type="projects")`. The structural project key
   for this repository is `<project-key>`. If it is absent, run `ingest_project` before trusting empty results.
3. Before editing a file, call `query_structure(query_type="blast_radius", project="<project-key>",
   path="<repo-relative-path>")` once. Use `affected_tests` to choose focused verification.
4. Use targeted `recall_memories` for prior decisions, failures, preferences, or constraints. Pass
   `file_context` and `file_context_project` when the question concerns code.
5. Treat stale anchors and incomplete indexes as warnings to inspect current code. Never turn "not indexed"
   or an empty result into "no impact."
6. After using recall output, call `rate_recall` with an honest usefulness rating.
7. Store only durable, verified lessons; never secrets. Use `add_memory_and_track` as the original write
   when immediate use matters; otherwise use `add_memory`. Attach a bounded Git diff when useful.
   Preserve the returned episode ID. After `PENDING` or timeout, observe that same ID with an available,
   authorized status tool (`get_enrichment_status` uses `episode_uuid`), not another write. `READY` is
   enrichment state, not guaranteed retrieval. Do not broaden client permissions to obtain status.
8. If Menhir fails, report that memory/structure context was unavailable. Continue only when current local
   evidence is sufficient for the task.

Workspace bootstrap keys, semantic namespaces, and structural project keys are distinct. Use the explicit
values above; do not infer them from the current directory.
