"""Effective recall-authority switches.

Authority is served only while its lane is writing. A View or event assertion the lane no longer
updates is frozen, and leading with it as the current answer would be false-current (#168). Every
RecallService construction site must read authority through these, never the raw flag.
"""

from __future__ import annotations

from menhir.config.settings_model import MemorySettings


def scalar_view_authority_effective(settings: MemorySettings) -> bool:
    return bool(
        settings.personal_memory_scalar_view_authority_enabled
        and settings.personal_memory_scalar_state_enabled
    )


def event_history_authority_effective(settings: MemorySettings) -> bool:
    return bool(
        settings.personal_memory_event_history_authority_enabled
        and settings.personal_memory_event_history_enabled
    )
