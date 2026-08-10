"""Warden label enum — replaces bare string literals across warden.py and formatters.

Previously the warden gate set ``WardenVerdict.label`` as a raw string (``"historical"``,
``"conflict"``, ``"uncertain"``, ``"suspicious_chain"``).  This enum is the SSOT for
those labels so they cannot silently drift.

Because ``WardenLabel`` is ``str, Enum`` every value compares equal to its string:
``WardenLabel.CONFLICT == "conflict"`` is ``True``, so consumers that read the label as a
string (formatters, serialisers, tests) need no changes.
"""

from __future__ import annotations

from enum import Enum


class WardenLabel(str, Enum):
    """The flag shown to LLM consumers when the warden gate admits but qualifies a claim.

    Maps to the ``label`` field on :class:`~menhir.domain.warden.WardenVerdict` and
    (when the warden gate is active) on :class:`~menhir.domain.recall.ScoredMemory`.
    """

    HISTORICAL        = "historical"         # former belief, not current
    CONFLICT          = "conflict"           # contradicted, under active conflict
    UNCERTAIN         = "uncertain"          # low belief confidence, use with hedging
    SUSPICIOUS_CHAIN  = "suspicious_chain"   # meta-memory chain too deep (Guard 3)
