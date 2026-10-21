# Menhir provenance, governance, and code-context rules

Use the configured Menhir MCP access surface for recorded project memory, provenance-linked context,
governed knowledge, and structural code analysis. The code shows what the project does; Menhir may hold
the recorded reasons: decisions, rejected alternatives, incidents, preferences, and constraints.

1. At session startup, call `read_flagged_memories` and then `recall_context_memories` with the same stable
   `reader_id`, the registered workspace key `<workspace-key>`, and the same namespace.
2. Use targeted `recall_memories` for prior decisions, rejected alternatives, incidents, preferences, or
   constraints when they could change the answer or the planned change, especially when the repository and
   its Git history do not record the rationale. Start with one focused query naming the component and the
   decision; rephrase only if evidence is missing. Pass `file_context` and `file_context_project` when the
   question concerns code.
3. Recall returns summaries and facts. When wording or rationale matters, read the linked source excerpts
   with `get_provenance(node_uuid=...)`. Cite what you use and distinguish recorded reasons from inference.
4. For structural questions, call `query_structure(query_type="projects")` first. The structural project key
   for this repository is `<project-key>`. If it is absent, run `ingest_project` before trusting empty results.
5. Before editing a file, call `query_structure(query_type="blast_radius", project="<project-key>",
   path="<repo-relative-path>")` once. Use `affected_tests` to choose focused verification.
6. Treat stale anchors and incomplete indexes as warnings to inspect current code, and check dates,
   supersession, and conflicts before applying a memory. Never turn "not indexed" or an empty result into
   "no impact" or "no history."
7. If your client exposes `rate_recall`, call it after using recall output with an honest usefulness rating.
8. Store only durable, verified lessons; never secrets. Use `add_memory_and_track` as the original write
   when immediate use matters; otherwise use `add_memory`. Attach a bounded Git diff when useful.
   Preserve the returned episode ID. After `PENDING` or timeout, observe that same ID with an available,
   authorized status tool (`get_enrichment_status` uses `episode_uuid`), not another write. `READY` is
   enrichment state, not guaranteed retrieval. Do not broaden client permissions to obtain status.
9. If Menhir fails, report that memory/structure context was unavailable. Continue only when current local
   evidence is sufficient for the task.

Workspace bootstrap keys, semantic namespaces, and structural project keys are distinct. Use the explicit
values above; do not infer them from the current directory.
