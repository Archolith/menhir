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
    proves_self_subject,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AmbiguousSelfBindingError",
    "SelfBindMode",
    "SelfBindOutcome",
    "SelfBindResult",
    "bind_canonical_self",
    "resolve_bind_mode",
]


class SelfBindMode(StrEnum):
    """Rollout control. Default ``OFF`` until the plan's acceptance gates pass."""

    #: Pre-change behavior exactly: binding is not evaluated at all.
    OFF = "off"
    #: Evaluate and record the decision, but leave the payload untouched.
    OBSERVE = "observe"
    #: Evaluate and apply.
    ENFORCE = "enforce"


def resolve_bind_mode(value: Any) -> SelfBindMode:
    """Parse a configured mode, failing safe to ``OFF`` on anything unrecognized.

    A typo in configuration must not silently enable a durable-write-semantics change.
    """
    try:
        return SelfBindMode(str(value or "").strip().lower())
    except ValueError:
        logger.warning(
            "Unrecognized canonical_self_binding_mode %r; falling back to 'off'", value
        )
        return SelfBindMode.OFF


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
    #: The payload held self-LIKE entities, the episode's author was proven, and none of those
    #: entities carried node-level subject authority -- so they are ordinary entities and take the
    #: ordinary Graphiti path. Distinct from NO_SELF_CANDIDATE, which saw no self-like name at
    #: all: this is the count that says how often a trusted turn mentions a third-person `user`
    #: that binding deliberately declines to claim.
    ORDINARY_USER_ENTITY = "ordinary_user_entity"


@dataclass(frozen=True, slots=True)
class SelfBindResult:
    """Outcome of one binding attempt, safe to log: enums, counts and UUIDs only."""

    outcome: SelfBindOutcome
    #: The mode this decision was made under. ``OBSERVE`` outcomes report what *would* have
    #: happened; nothing was rewritten and the resolver must not treat them as authoritative.
    mode: SelfBindMode = SelfBindMode.ENFORCE
    self_uuid: str | None = None
    #: UUIDs the extractor minted for the human, now rewritten to :attr:`self_uuid`.
    rewritten_node_uuids: tuple[str, ...] = ()
    #: Always 0 since alias collapse was removed; kept so the telemetry schema is stable across
    #: the change and a dashboard reading it does not break.
    nodes_collapsed: int = 0
    edge_endpoints_rewritten: int = 0
    index_map_keys_merged: int = 0
    #: Self-alias nodes present in a payload that did NOT bind. Activation requires this to be
    #: understood by producer and source: a persistently non-zero count means self-like entities
    #: are still being minted outside the trusted path, and are still fragmenting recall.
    self_like_without_evidence: int = 0

    def telemetry_details(self, identity: SelfIdentityContext | None) -> dict[str, Any]:
        """Structured, privacy-safe record of one binding decision.

        Deliberately carries no memory text and no arbitrary entity names -- only enums, counts,
        UUIDs and the logical namespace. An entity name here would leak episode content into
        telemetry, and the names in question are exactly the ones a user typed.
        """
        details: dict[str, Any] = {
            "outcome": str(self.outcome),
            "mode": str(self.mode),
            "self_uuid": self.self_uuid,
            "rewritten_node_count": len(self.rewritten_node_uuids),
            "nodes_collapsed": self.nodes_collapsed,
            "edge_endpoints_rewritten": self.edge_endpoints_rewritten,
            "index_map_keys_merged": self.index_map_keys_merged,
            "self_like_without_evidence": self.self_like_without_evidence,
        }
        if identity is not None:
            details.update(
                {
                    "namespace": identity.namespace,
                    "speaker_role": str(identity.speaker_role),
                    "evidence_kind": str(identity.evidence_kind or ""),
                    "source_kind": identity.source_kind,
                }
            )
        return details

    @property
    def bound(self) -> bool:
        """True only when the payload was actually rewritten.

        Observe mode never returns True here: the resolver keys its dedup bypass on this, and a
        node that was not rewritten still carries the extractor's uuid, so bypassing search for
        it would strand it with no candidates and no resolution.
        """
        return self.outcome is SelfBindOutcome.BOUND and self.mode is SelfBindMode.ENFORCE


def _node_uuid(node: Any) -> str:
    return str(getattr(node, "uuid", "") or "")


def bind_canonical_self(
    nodes: list[Any],
    edges: list[Any],
    index_map: dict[str, list[int]],
    identity: SelfIdentityContext | None,
    mode: SelfBindMode = SelfBindMode.ENFORCE,
) -> SelfBindResult:
    """Rewrite the proven human to its deterministic UUID, in place, across the whole payload.

    Mutates ``nodes``, ``edges`` and ``index_map`` together. Partial application is the failure
    this guards against: rewriting a node UUID without following both edge directions and the
    episode index map would orphan the very facts the episode carried, which is worse than the
    fork it fixes.

    Returns a :class:`SelfBindResult` describing what happened. Raises
    :class:`AmbiguousSelfBindingError` only when binding would require a guess.
    """
    if mode is SelfBindMode.OFF:
        return SelfBindResult(SelfBindOutcome.NOT_ELIGIBLE, mode=mode)

    if not eligible_self_evidence(identity):
        # Count self-like emissions that did not bind. This is the signal that says whether the
        # trusted-evidence contract actually covers production traffic: if entities named "user"
        # keep appearing from untrusted producers, they keep fragmenting recall and activation is
        # not yet safe. Counting names is fine; recording them is not.
        return SelfBindResult(
            SelfBindOutcome.NOT_ELIGIBLE,
            mode=mode,
            self_like_without_evidence=sum(
                1 for n in nodes if is_self_alias(getattr(n, "name", None))
            ),
        )

    assert identity is not None  # narrowed by eligible_self_evidence
    canonical_uuid = identity.self_uuid

    # Two questions, deliberately separated. `self_like` is "could this be the human" -- a name
    # test, never authority. `self_nodes` is "does THIS NODE carry subject authority", which is
    # what binding requires. The trusted episode signal proves who AUTHORED the episode and
    # nothing more; a lone third-person `user` in a human turn is an ordinary entity (an RBAC
    # role, a `users` table, the customer the turn is about), and rewriting it to the canonical
    # UUID would fold a foreign subject into the human identity -- the false-positive bind this
    # module exists to prevent, and the one no later migration can separate again.
    self_like = [n for n in nodes if is_self_alias(getattr(n, "name", None))]
    self_nodes = [n for n in nodes if proves_self_subject(getattr(n, "name", None), identity)]
    if not self_nodes:
        # Self-like names present but none proving: they stay ordinary entities. Counted, because
        # a persistently high count is what says whether per-node subject authority for
        # third-person references is worth building.
        if self_like:
            return SelfBindResult(
                SelfBindOutcome.ORDINARY_USER_ENTITY,
                mode=mode,
                self_like_without_evidence=len(self_like),
            )
        return SelfBindResult(SelfBindOutcome.NO_SELF_CANDIDATE, mode=mode)

    # Even with node-level authority, two proving nodes cannot both be the author, and episode
    # evidence cannot say which. Fail closed and visibly: the pending episode stays retryable
    # with its raw text intact and nothing is written.
    if len(self_nodes) > 1:
        raise AmbiguousSelfBindingError(
            f"{len(self_nodes)} nodes claim self-subject authority in one payload for namespace "
            f"{identity.namespace!r}; episode-level evidence cannot prove they are the same "
            f"subject, so refusing to bind"
        )

    self_uuids = {_node_uuid(n) for n in self_nodes if _node_uuid(n)}

    # Any node OTHER than the proving one already sitting on the canonical UUID would be
    # silently absorbed into the human by the rewrite below. Identity by name is not enough to
    # excuse that: a third-person `user` holding the canonical uuid is exactly the fork this
    # change exists to stop being created.
    proving = {id(n) for n in self_nodes}
    for node in nodes:
        if _node_uuid(node) == canonical_uuid and id(node) not in proving:
            raise AmbiguousSelfBindingError(
                f"canonical self uuid {canonical_uuid} is already held by an entity with no "
                f"subject authority in namespace {identity.namespace!r}; refusing to bind"
            )

    if mode is SelfBindMode.OBSERVE:
        # Report what enforce would have done, having mutated nothing. Both refusal checks above
        # still run, so observe surfaces an ambiguous payload before enforce could act on it.
        return SelfBindResult(
            outcome=SelfBindOutcome.BOUND,
            mode=mode,
            self_uuid=canonical_uuid,
            rewritten_node_uuids=tuple(sorted(self_uuids)),
            self_like_without_evidence=len(self_like) - len(self_nodes),
        )

    # Exactly one, guaranteed by the authority check above.
    keeper = self_nodes[0]

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
        mode=mode,
        self_uuid=canonical_uuid,
        rewritten_node_uuids=tuple(sorted(self_uuids)),
        edge_endpoints_rewritten=endpoints_rewritten,
        index_map_keys_merged=keys_merged,
        self_like_without_evidence=len(self_like) - len(self_nodes),
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
