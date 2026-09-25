"""Shared current-version reads for the scalar View repository.

Extracted verbatim from ``scalar_view_repository.py``; the composed ``ScalarViewRepositoryMixin``
picks these operations up through inheritance, so every existing import site is unchanged.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.recall_visibility import default_recall_visibility_cypher
from menhir.infrastructure.view_models import ViewClass, _label_for


class ScalarViewReadOpsMixin:
    """Current-version fetch operations for the scalar_state and scalar_history APIs."""

    def _fetch_current(
        self, kind_name: str, key: str, *, view_class: ViewClass = ViewClass.FACT
    ) -> dict[str, Any] | None:
        """Current version for a (kind, key), parsed by the kind. Read projection lives in the
        kind (`read_fields`), so the value-slot definition is SSOT across write and read.
        Label-scoped so a FACT read never returns a METRIC of the same key, and vice versa.

        Direct getters remain authoritative inspection surfaces: an ineligible row is returned, not
        hidden. ``recall_eligible`` is an attached read-side decision for context callers; it is false
        for OPERATOR, retired, unstamped, noncurrent, or provenance-invalid Views.
        """
        kind = self.KINDS[kind_name]
        label = _label_for(view_class)
        rows = self.neo4j.execute(
            f"MATCH (n:{label} {{view_kind:$kind}}) WHERE n.view_key=$k AND coalesce(n.view_current, true) "
            f"RETURN {kind.read_fields}, "
            f"CASE WHEN {default_recall_visibility_cypher('n')} "
            "THEN true ELSE false END AS recall_eligible LIMIT 1",
            {"k": key, "kind": kind_name},
        )
        if not rows:
            return None
        row = dict(rows[0])
        parsed = kind.parse(row)
        parsed["recall_eligible"] = bool(row.get("recall_eligible"))
        return parsed

    def fetch_scalar_state(
        self, *, subject_uuid: str, attribute: str, scope: str, value_kind: str, unit: str,
        namespace: str | None = None,
    ) -> dict[str, Any] | None:
        kind = self.KINDS["scalar_state"]
        disc = kind.key_discriminator(
            {"attribute": attribute, "scope": scope, "value_kind": value_kind, "unit": unit})
        return self._fetch_current(
            "scalar_state", self._key(namespace, subject_uuid, disc, subject_uuid=subject_uuid))

    def fetch_scalar_history(
        self, *, subject_uuid: str, attribute: str, scope: str, value_kind: str, unit: str,
        namespace: str | None = None,
    ) -> dict[str, Any] | None:
        """The CURRENT scalar_history View for one slot.

        Direct inspection is unfiltered; context callers consume the attached ``recall_eligible``.
        """
        kind = self.KINDS["scalar_history"]
        disc = kind.key_discriminator(
            {"attribute": attribute, "scope": scope, "value_kind": value_kind, "unit": unit})
        return self._fetch_current(
            "scalar_history", self._key(namespace, subject_uuid, disc, subject_uuid=subject_uuid))
