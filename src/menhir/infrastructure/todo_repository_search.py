"""Keyword search for the todo repository.

Split out of ``todo_repository.py`` (file-size refactor). ``TodoRepository``
composes this mixin so ``search_by_query`` stays available on the facade
unchanged. Methods run against the facade's ``self.neo4j``.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.todo_repository_constants import _query_words


class TodoSearchMixin:
    """Content keyword search over open todos."""

    def search_by_query(
        self,
        query: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Find open todos whose content matches the query (keyword-based).

        Matches if the full query string appears in content, or if any
        significant word (>= 5 chars) from the query appears in content.
        Returns priority-sorted results.
        """
        safe_limit = max(1, min(limit, 50))
        words = _query_words(query)
        return self.neo4j.execute(
            """
            WITH toLower($query) AS q, $words AS words
            MATCH (t:Todo {status: 'open'})
            WHERE toLower(t.content) CONTAINS q
              OR any(word IN words WHERE toLower(t.content) CONTAINS word)
            RETURN
                t.uuid     AS uuid,
                t.content  AS content,
                t.code_ref AS code_ref,
                t.priority AS priority
            ORDER BY
                CASE t.priority
                    WHEN 'high'   THEN 0
                    WHEN 'normal' THEN 1
                    ELSE               2
                END
            LIMIT $limit
            """,
            {"query": query.lower(), "words": words, "limit": safe_limit},
        )
