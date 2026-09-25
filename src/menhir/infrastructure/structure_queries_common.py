"""Shared constants for the structure graph writer.

Moved verbatim out of ``structure_queries.py`` (facade split) so the writer
mixins can import them without a circular import; the original module
re-exports everything defined here.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.utils import source_confidence_for


#: The source label every node written by the project scanner carries.
STRUCTURE_SOURCE = "project-scan"

#: Derived, never restated. This module used to name SOURCE_CONFIDENCE_STRUCTURAL directly, which is
#: how it silently drifted from `source_confidence_for` -- both were right in isolation and disagreed
#: with each other for 48,781 production entities. Deriving it means the two cannot part again.
STRUCTURE_SOURCE_CONFIDENCE = source_confidence_for(STRUCTURE_SOURCE)

_ENTITY_DEFAULTS: dict[str, Any] = {
    "type": "SEMANTIC",
    "scope": "PERSISTENT",
    "source": STRUCTURE_SOURCE,
    "source_confidence": STRUCTURE_SOURCE_CONFIDENCE,
    "user_flagged": False,
    "group_id": "",
    "summary": "",
}
