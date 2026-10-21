"""Shared hard bounds for memory-write payloads."""

from __future__ import annotations


#: Matches the default enrichment preflight: estimate_episode_tokens() counts about four
#: characters per token and graphiti_episode_max_estimated_tokens defaults to 12,000.
MAX_EPISODE_CHARS = 48_000

#: Matches the compose path's existing diff truncation boundary.
MAX_DIFF_CHARS = 50_000


def validate_memory_payload(text: str, diff: str | None = None) -> None:
    """Reject oversized memory input before any write-side effect."""

    if len(text) > MAX_EPISODE_CHARS:
        raise ValueError(
            f"Memory text exceeds the {MAX_EPISODE_CHARS}-character limit."
        )
    if diff is not None and len(diff) > MAX_DIFF_CHARS:
        raise ValueError(f"Memory diff exceeds the {MAX_DIFF_CHARS}-character limit.")
