"""MCP tool: ingest_document — feed a doc/markdown/text file into the memory graph."""

from __future__ import annotations

import asyncio
import os

from menhir.mcp.service_access import get_mcp_session
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def ingest_document(
    path: str,
    project: str | None = None,
    document_type: str = "generic",
    namespace: str = "",
) -> str:
    """Ingest a document or markdown file into the memory graph.

    Creates a structural document entity in Neo4j (queryable via blast_radius
    and file_context) and queues a narrative episode for Graphiti semantic
    extraction, giving the memory graph full knowledge of the document's content.

    Supports any text file: .md, .txt, .rst, .adoc, plain prose, specs, READMEs.

    Args:
        path: Absolute path to the file to ingest.
        project: Project/namespace label for the document (default: parent
            directory name). Use the same value as the associated ingest_project
            call to co-locate the document with its project's structural graph.
        document_type: Type of document (generic, wiki_article, reference_article).
            Used for filtering documents in recall/queries.
        namespace: Optional silo for the QUEUED EPISODE -- the recallable memory this
            produces. Empty = default/global behavior. Note this is unrelated to `project`,
            which labels the shared structure graph and is not a tenancy boundary.

    Returns:
        Summary of the entity written and episode queue status.
    """
    return await IngestDocumentTool().execute(
        path=path, project=project, document_type=document_type, namespace=namespace
    )


class IngestDocumentTool(BaseTextTool):
    name = "ingest_document"
    # NAMESPACED, not OBJECT: the structure node this writes is deliberately shared (the
    # structure graph is an index of a codebase keyed by project, and `query_structure`
    # documents that namespace scopes only its Todo section). The EPISODE it queues is not --
    # that becomes recallable tenant memory through the same `queue_episode` call `add_memory`
    # makes. Omitting the argument sent it to the default group regardless of the caller's pin,
    # which is CF-220's escape in a third tool.
    scope = ToolScope.NAMESPACED
    description = "Ingest a doc/markdown/text file into the memory graph as a document entity + semantic episode."

    def timeout_for(
        self, path: str = "", project: str | None = None, document_type: str = "generic"
    ) -> int:
        return 30

    async def endpoint(
        self, path: str, project: str | None = None, document_type: str = "generic",
        namespace: str = "",
    ) -> str:
        if not os.path.isfile(path):
            return f"Error: not a file: {path}"

        backend = self.get_backend()
        session = get_mcp_session()

        result = await backend.ingest_document(
            path,
            project=project,
            session_id=session.session_id,
            user_id=session.user_id,
            document_type=document_type,
        )

        structure_project = result.get("structure_project", "")
        structure_path = result.get("structure_path", "")
        content_length = result.get("content_length", 0)
        doc_type = result.get("document_type", "generic")
        narrative = result.get("narrative", "")

        # Queue narrative episode for Graphiti enrichment (best-effort, non-blocking)
        episode_status = "episode queue pending"
        if narrative:
            try:
                ep_result = await asyncio.wait_for(
                    backend.queue_episode(
                        narrative,
                        user_id=session.user_id,
                        session_id=session.session_id,
                        source="document-ingest",
                        # Forwarded only when set: byte-identical call when unpinned.
                        **({"namespace": namespace} if namespace else {}),
                    ),
                    timeout=10,
                )
                episode_status = (
                    f"episode_id={ep_result.get('episode_id')}"
                    if ep_result.get("episode_id")
                    else "episode queue failed"
                )
            except asyncio.TimeoutError:
                episode_status = "episode queue deferred (enrichment pipeline busy)"
            except Exception as exc:
                episode_status = f"episode queue error: {exc}"
        else:
            episode_status = "no content (empty file)"

        filename = os.path.basename(path)
        return (
            f"Ingested {filename}: entity written. Semantic episode: {episode_status}\n"
            f"  project={structure_project}, path={structure_path}, "
            f"document_type={doc_type}, content_length={content_length} chars"
        )
