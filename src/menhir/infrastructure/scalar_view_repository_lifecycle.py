"""Retire-in-place operations for the scalar View repository.

Extracted verbatim from ``scalar_view_repository.py``; the composed ``ScalarViewRepositoryMixin``
picks these operations up through inheritance, so every existing import site is unchanged.
"""

from __future__ import annotations


class ScalarViewRetireOpsMixin:
    """Expire current Views that have no replacement, keeping the version for audit."""

    def retire_scalar_state(self, *, view_key: str) -> bool:
        """Expire a current scalar_state View with NO replacement (a slot that vanished or now
        abstains). Retires-in-place (view_current=false, expired_at) — the version is kept for
        audit, not deleted. Returns True if a current version was retired."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {view_kind:'scalar_state', view_key:$k})
            WHERE coalesce(n.view_current, true)
            SET n.view_current = false, n.qs_current = false, n.expired_at = datetime(),
                n.retired = true, n.last_accessed = datetime()
            REMOVE n.ss_view_key_current
            RETURN count(n) AS retired
            """,
            {"k": view_key},
        )
        return bool(rows and int(rows[0].get("retired") or 0) > 0)

    def retire_scalar_history(self, *, view_key: str) -> bool:
        """Expire a current scalar_history View with NO replacement. Returns True if retired."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {view_kind:'scalar_history', view_key:$k})
            WHERE coalesce(n.view_current, true)
            SET n.view_current = false, n.qs_current = false, n.expired_at = datetime(),
                n.retired = true, n.last_accessed = datetime()
            RETURN count(n) AS retired
            """,
            {"k": view_key},
        )
        return bool(rows and int(rows[0].get("retired") or 0) > 0)
