"""Deterministic canonical-self binding, applied before Graphiti deduplication.

Graphiti resolves an extracted entity by cosine candidate search plus, when several candidates
share a normalized name, an LLM. For the human-self entity that boundary is probabilistic, and in
production it fragmented one identity across dozens of nodes: the candidate window saturates with
exact-name ``user`` matches, so the deterministic single-match branch became arithmetically
unreachable and every extraction fell through to the LLM.

This module removes the human from that boundary entirely. When -- and only when -- the ingestion
boundary proved the episode's author, the self node is rewritten to the deterministic
per-namespace UUID *before* candidate acquisition, so it never enters similarity search, the dedup
prompt, or Menhir's identity gate.

What it is not: a name rule. An entity called ``user`` with no trusted evidence stays an ordinary
semantic entity and takes the ordinary Graphiti path. See ``domain/self_identity`` for the
evidence contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from menhir.domain.self_identity import (
    SelfIdentityContext,
    eligible_self_evidence,
    is_self_alias,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AmbiguousSelfBindingError",
    "SelfBindOutcome",
    "SelfBindResult",
    "bind_canonical_self",
]


class AmbiguousSelfBindingError(RuntimeError):
    """Raised when a payload cannot be bound without guessing.

    Deliberately fatal to the extraction attempt rather than resolved by picking one. The pending
    episode stays retryable with its raw text intact, so no graph write happens and nothing is
    lost -- the opposite of silently folding two identities together.
    """


class SelfBindOutcome(StrEnum):
    """Why a payload did or did not bind. Recorded for observability; no free text."""

    BOUND = "bound"
    NOT_ELIGIBLE = "not_eligible"
    NO_SELF_CANDIDATE = "no_self_candidate"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class SelfBindResult:
    """Outcome of one binding attempt, safe to log: enums, counts and UUIDs only."""

    outcome: SelfBindOutcome
    self_uuid: str | None = None
    #: UUIDs the extractor minted for the human, now rewritten to :attr:`self_uuid`.
    rewritten_node_uuids: tuple[str, ...] = ()
    nodes_collapsed: int = 0
    edge_endpoints_rewritten: int = 0
    index_map_keys_merged: int = 0

    @property
    def bound(self) -> bool:
        return self.outcome is SelfBindOutcome.BOUND


def _node_uuid(node: Any) -> str:
    return str(getattr(node, "uuid", "") or "")


def bind_canonical_self(
    nodes: list[Any],
    edges: list[Any],
    index_map: dict[str, list[int]],
    identity: SelfIdentityContext | None,
) -> SelfBindResult:
    """Rewrite the proven human to its deterministic UUID, in place, across the whole payload.

    Mutates ``nodes``, ``edges`` and ``index_map`` together. Partial application is the failure
    this guards against: rewriting a node UUID without following both edge directions and the
    episode index map would orphan the very facts the episode carried, which is worse than the
    fork it fixes.

    Returns a :class:`SelfBindResult` describing what happened. Raises
    :class:`AmbiguousSelfBindingError` only when binding would require a guess.
    """
    if not eligible_self_evidence(identity):
        return SelfBindResult(SelfBindOutcome.NOT_ELIGIBLE)

    assert identity is not None  # narrowed by eligible_self_evidence
    canonical_uuid = identity.self_uuid

    self_nodes = [n for n in nodes if is_self_alias(getattr(n, "name", None))]
    if not self_nodes:
        return SelfBindResult(SelfBindOutcome.NO_SELF_CANDIDATE)

    self_uuids = {_node_uuid(n) for n in self_nodes if _node_uuid(n)}

    # A non-self node already sitting on the canonical UUID would be silently absorbed into the
    # human by the rewrite below. That is a real contradiction, not an alias collapse.
    for node in nodes:
        if _node_uuid(node) == canonical_uuid and not is_self_alias(getattr(node, "name", None)):
            raise AmbiguousSelfBindingError(
                f"canonical self uuid {canonical_uuid} is already held by non-self entity "
                f"in namespace {identity.namespace!r}; refusing to bind"
            )

    # Several aliases in one payload ("user" and "I") denote the same proven human, so they
    # collapse deterministically rather than competing. Keep the first in extraction order.
    keeper = self_nodes[0]
    collapsed = self_nodes[1:]

    # The rewrite spans three structures and is only correct as a unit: a node rewritten while
    # its edges still point at the discarded UUID orphans every fact the episode carried. The
    # payload objects are externally supplied and validated, so an assignment can fail partway.
    # Snapshot enough to restore, and put the whole application behind one rollback.
    original_nodes = list(nodes)
    original_keeper_uuid = _node_uuid(keeper)
    original_endpoints = [
        (e, str(getattr(e, "source_node_uuid", "") or ""), str(getattr(e, "target_node_uuid", "") or ""))
        for e in edges
    ]
    original_index_map = {k: list(v) for k, v in index_map.items()}

    def _rollback() -> None:
        try:
            keeper.uuid = original_keeper_uuid
        except Exception:  # noqa: BLE001 - best effort; the raise below is what callers act on
            logger.exception("Self-binding rollback could not restore node uuid")
        nodes[:] = original_nodes
        for edge, source, target in original_endpoints:
            try:
                edge.source_node_uuid = source
                edge.target_node_uuid = target
            except Exception:  # noqa: BLE001
                logger.exception("Self-binding rollback could not restore an edge endpoint")
        index_map.clear()
        index_map.update(original_index_map)

    try:
        keeper.uuid = canonical_uuid
        for node in collapsed:
            nodes.remove(node)

        # Both endpoint directions, or an edge survives pointing at a UUID no node carries.
        endpoints_rewritten = 0
        for edge in edges:
            for attr in ("source_node_uuid", "target_node_uuid"):
                if str(getattr(edge, attr, "") or "") in self_uuids:
                    setattr(edge, attr, canonical_uuid)
                    endpoints_rewritten += 1

        # Episode attribution follows the identity, merged rather than overwritten so no episode
        # index is dropped when two aliases collapse.
        merged: list[int] = []
        keys_merged = 0
        for stale in self_uuids | {canonical_uuid}:
            indices = index_map.pop(stale, None)
            if indices is None:
                continue
            keys_merged += 1
            for idx in indices:
                if idx not in merged:
                    merged.append(idx)
        if merged:
            index_map[canonical_uuid] = sorted(merged)
    except Exception as exc:
        _rollback()
        raise AmbiguousSelfBindingError(
            f"self binding failed partway for namespace {identity.namespace!r} and was rolled "
            f"back: {exc}"
        ) from exc

    result = SelfBindResult(
        outcome=SelfBindOutcome.BOUND,
        self_uuid=canonical_uuid,
        rewritten_node_uuids=tuple(sorted(self_uuids)),
        nodes_collapsed=len(collapsed),
        edge_endpoints_rewritten=endpoints_rewritten,
        index_map_keys_merged=keys_merged,
    )
    logger.info(
        "Canonical self bound namespace=%s episode=%s uuid=%s collapsed=%d endpoints=%d "
        "index_keys=%d evidence=%s role=%s",
        identity.namespace,
        identity.episode_uuid,
        canonical_uuid,
        result.nodes_collapsed,
        result.edge_endpoints_rewritten,
        result.index_map_keys_merged,
        identity.evidence_kind,
        identity.speaker_role,
    )
    return result
