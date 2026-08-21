"""The callable seam types services share with their injected LLM/embedding collaborators.

CF-77: `LlmComplete` and `Embed` were re-declared in eight modules -- ten declarations for two
types -- and one had already drifted. `quantstate_consolidator.py:22` read
``Callable[[str], list[float]]`` while every other declaration read
``Callable[[str], list[float] | None]``, so the module that consumed it was the one module whose
type said an embedder may never return ``None``.

That is the whole argument for one declaration: the copies did not stay identical, and the one
that drifted dropped exactly the case a caller has to handle.

These live in `services/` rather than `domain/` on purpose -- they describe how a service is
handed its collaborators, which is a service-layer concern. Domain does not import them.
"""

from __future__ import annotations

from collections.abc import Callable

#: Chat completion: ``(system_prompt, user_prompt) -> text``.
LlmComplete = Callable[[str, str], str]

#: Embedding: ``text -> vector``, or ``None`` when the embedder is unavailable or declines.
#:
#: The ``| None`` is load-bearing and is what the diverged copy dropped. Every real embedding seam
#: in this codebase can yield ``None`` -- a down endpoint, a circuit-breaker refusal, a short
#: upstream response (CF-156) -- and a signature promising otherwise invites a caller to skip the
#: check.
Embed = Callable[[str], "list[float] | None"]

__all__ = ["Embed", "LlmComplete"]
