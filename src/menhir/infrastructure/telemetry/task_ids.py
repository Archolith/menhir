"""Stable per-episode task identifiers recorded in telemetry rows."""

from __future__ import annotations


def build_episode_task_id(*, episode_uuid: str, provider: str, action: str) -> str:
    """Return the ``memory-<uuid>--<provider>-<action>`` id stored on episode task events."""

    normalized_episode = "".join(ch for ch in (episode_uuid or "unknown") if ch.isalnum()) or "unknown"
    normalized_provider = "".join(ch if ch.isalnum() else "-" for ch in provider).strip("-") or "unknown"
    normalized_action = "".join(ch if ch.isalnum() else "-" for ch in action).strip("-") or "unknown"
    return f"memory-{normalized_episode}--{normalized_provider}-{normalized_action}"
