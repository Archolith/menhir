"""Which `:Episodic` node is Menhir's own queue node (one per memory).

Each memory has two `:Episodic` nodes: Menhir's queue node (`create_pending_episode`, carries
`reference_time`/`namespace`, never `valid_at`) and the Graphiti episode it resolves to (carries
`valid_at`/`group_id`, MENTIONS and fact ids). `processing_state` does NOT tell them apart: the
node-defaults migration (`infrastructure/schema.py`) stamps queue fields onto every unstamped
`:Episodic` at bootstrap, Graphiti's included (#215). Graphiti always sets `valid_at`; Menhir never
writes it on a queue node -- so `valid_at IS NULL` is the discriminator.
"""

from __future__ import annotations


def menhir_queue_episode_cypher(variable: str = "n") -> str:
    """Cypher predicate true only for Menhir's own queue `:Episodic` node (never Graphiti's)."""
    if not variable or not variable.isidentifier():
        raise ValueError(f"variable must be a Cypher identifier, got {variable!r}")
    return f"{variable}.valid_at IS NULL"
