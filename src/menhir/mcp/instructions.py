"""The MCP server instructions every Menhir client receives, shared by the stdio and remote servers.

This is the only guidance an MCP client gets without a pasted AGENTS.md block, so it says when
memory is worth consulting and how to read it, not just what Menhir is. In a matched agent
evaluation (same tasks and model, only this text and the recall/provenance/structure tool
descriptions changed) agents given the previous one-line positioning text skipped recall in half
the runs and used Menhir only as a code index; with this text every run recalled. Keep it short
and keep `docs/agent-usage.md` and `docs/templates/AGENTS.menhir.md` consistent with it.
"""

from __future__ import annotations

SERVER_INSTRUCTIONS = """\
Menhir provides recorded project memory and an indexed code graph. Use recall_memories for prior \
decisions, rejected alternatives, incidents, preferences, or constraints when they could change \
the answer or planned change, especially when repository history cannot supply the rationale. \
Memory contains recorded claims, not guaranteed truth.

Use configured identifiers: namespace scopes memory, project identifies a structural index, and \
workspace selects startup pins. At session start, call read_flagged_memories then \
recall_context_memories with the same reader_id, workspace and namespace. Do not invent \
identifiers.

Start with one focused recall naming the component and decision. For code-linked questions, pass \
file_context and file_context_project. Rephrase only if evidence is missing. \
include_invalidated=true adds superseded facts attached to returned memories; it is not \
exhaustive history search.

Recall normally returns summaries and facts. When wording or rationale matters, use \
get_provenance(node_uuid=...) for linked source excerpts; increase content_chars if truncated, up \
to 5000. Cite the source and distinguish recorded reasons from inference.

Use query_structure for layout and impact; check projects and index warnings. Verify current \
behaviour against current code. Inspect dates, supersession, conflicts and stale anchors before \
applying memory.

When relevant, inspect list_todos/get_todo, list_artifacts/get_artifact, list_artifact_questions \
or list_conflicts. Artifacts provide document locations. Discover unlisted tools through the \
client's supported mechanism.

Empty results do not prove no history exists. Report errors or degraded results; continue from \
local evidence only when sufficient. Use authorized tools only. Store durable, verified lessons \
when permitted; never secrets."""
