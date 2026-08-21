"""Graph-native verifiers — keep derivable beliefs fresh against their source of truth.

The problem this solves: supersession/contradiction only fire when a *new* memory is written.
If the world changes and nothing writes a correcting memory, a belief silently stays "current"
and wrong. The cure is scheduled RE-OBSERVATION wired to exactly the nodes it governs.

Design (see .agent/plans/menhir-graph-verifiers.md):
  - A Verifier is a graph node holding a *binding*, not code: `verifier_kind` + `verifier_params`
    + the register coordinates (subject, counter) it maintains. Executable logic lives in a trusted
    in-code registry (`DEFAULT_EXECUTORS`); an unknown kind is skipped, never executed — the graph
    never carries runnable code.
  - The register it confirms is a supersedable counter View: `(register)-[:VERIFIED_BY]->(verifier)`.
    Re-deriving simply re-records the counter, which self-supersedes on value change.
  - Free-text beliefs `(belief)-[:REFERENCES]->(register)` fan in; on a value change they are flagged
    for review so recall can down-rank prose that now restates a stale value.

`sync_verifiers` is the graph-driven loop: enumerate verifiers, run each executor against its live
source, refresh the register, and on change flag the referencing beliefs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from menhir.infrastructure.view_repository import ViewRepository
from menhir.services.seam_types import Embed

logger = logging.getLogger(__name__)



@dataclass(frozen=True)
class VerifierResult:
    """One reading from a source of truth.

    `value` is the numeric register value used for deterministic supersession (bool -> 1.0/0.0,
    int as-is). `display` is the human-readable form kept for provenance. `ok=False` means the
    source could not be read this cycle — the register is then left untouched (absence of evidence
    is not evidence the value changed), so a transient read failure never supersedes a good value.
    """

    value: float
    display: str
    ok: bool = True


@dataclass
class VerifierContext:
    """Ambient inputs an executor may read from (extensible). `settings` is the parsed env config;
    add more providers (e.g. a live scheduler snapshot) as new verifier kinds need them."""

    settings: Any = None
    extra: Mapping[str, Any] | None = None


# --------------------------------------------------------------------------- executors (trusted)

Executor = Callable[[Mapping[str, Any], VerifierContext], VerifierResult]


def _coerce(raw: str, as_type: str) -> VerifierResult:
    raw = raw.strip()
    if as_type == "int":
        try:
            return VerifierResult(value=float(int(raw)), display=raw)
        except ValueError:
            return VerifierResult(value=0.0, display=raw, ok=False)
    # default: boolean
    truthy = raw.lower() in ("1", "true", "yes", "on")
    return VerifierResult(value=1.0 if truthy else 0.0, display=raw)


def env_key_executor(params: Mapping[str, Any], ctx: VerifierContext) -> VerifierResult:
    """Read the current truth from an environment variable (falling back to a settings attribute).
    params: {"key": "MENHIR_...", "as": "bool"|"int" (default bool), "settings_attr": "..."?}."""
    key = str(params["key"])
    as_type = str(params.get("as", "bool"))
    raw = os.getenv(key)
    if raw is None and ctx.settings is not None and params.get("settings_attr"):
        attr = getattr(ctx.settings, str(params["settings_attr"]), None)
        if attr is not None:
            raw = str(attr)
    if raw is None:
        return VerifierResult(value=0.0, display="(unset)", ok=False)
    return _coerce(raw, as_type)


DEFAULT_EXECUTORS: dict[str, Executor] = {
    "env_key": env_key_executor,
}


# --------------------------------------------------------------------------- repository protocol


class VerifierRepositoryProtocol(Protocol):
    def list_verifiers(self) -> list[dict[str, Any]]: ...
    def stamp_verifier(self, *, verifier_uuid: str, value: float, display: str, at: str) -> None: ...
    def ensure_verified_edge(self, *, register_uuid: str, verifier_uuid: str) -> None: ...
    def flag_referencing_beliefs(
        self, *, verifier_uuid: str, new_value: float, display: str, at: str
    ) -> int: ...


class CounterGraphProtocol(Protocol):
    def record_counter(self, **kwargs: Any) -> dict[str, Any]: ...
    def fetch_counter(
        self, *, subject: str, counter: str, namespace: str | None = None
    ) -> dict[str, Any] | None: ...


# --------------------------------------------------------------------------- graph-driven sync


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def sync_verifiers(
    *,
    repo: VerifierRepositoryProtocol,
    graph_adapter: CounterGraphProtocol,
    context: VerifierContext,
    executors: Mapping[str, Executor] = DEFAULT_EXECUTORS,
    namespace: str = "agent-status",
    embed: Embed | None = None,
) -> list[dict[str, Any]]:
    """Enumerate verifier nodes, re-derive each from its live source, refresh the register it
    confirms (self-superseding counter), and flag referencing beliefs on change. Returns one
    result row per verifier describing the outcome (refreshed / changed / skipped / error)."""
    results: list[dict[str, Any]] = []
    now = _utc_now_iso()
    for v in repo.list_verifiers():
        vid = str(v["uuid"])
        kind = str(v.get("verifier_kind") or "")
        subject = str(v.get("register_subject") or "")
        counter = str(v.get("register_counter") or "")
        executor = executors.get(kind)
        if executor is None:
            # Unknown kind: the graph is data, execution is code — never run something untrusted.
            results.append({"verifier": vid, "kind": kind, "status": "skipped_unknown_kind"})
            continue
        try:
            params = _parse_params(v.get("verifier_params"))
            res = executor(params, context)
        except Exception as exc:  # a broken probe must not abort the rest of the sync
            logger.warning("Verifier %s (%s) executor failed: %s", vid, kind, exc)
            results.append({"verifier": vid, "kind": kind, "status": "error", "error": str(exc)})
            continue
        if not res.ok:
            results.append({"verifier": vid, "kind": kind, "status": "source_unavailable"})
            continue

        prev = None
        if hasattr(graph_adapter, "fetch_counter"):
            try:
                prev = graph_adapter.fetch_counter(subject=subject, counter=counter, namespace=namespace)
            except Exception:
                prev = None
        changed = not (prev and prev.get("value") is not None and float(prev["value"]) == res.value)

        name_embedding = None
        if embed is not None and changed:
            try:
                name_embedding = embed(ViewRepository.retrieval_text(subject, counter, res.value))
            except Exception:
                name_embedding = None  # BM25-only; never block the write

        # Flag BEFORE the new value becomes durable. `changed` is an edge-triggered signal
        # computed from `prev`, and `record_counter` supersedes `prev` -- so once it lands, the
        # condition that produced this signal is gone and no later sync can recompute it. Any
        # failure between the record and the flag (process death, a Neo4j error from
        # `ensure_verified_edge` or `stamp_verifier`, a lost lease) lost the flag PERMANENTLY:
        # next sync sees prev["value"] == res.value, `changed` is False, and nothing retries.
        #
        # Flagging first inverts that. `flag_referencing_beliefs` needs only `vid` -- it
        # traverses the ALREADY-DURABLE (reg)-[:VERIFIED_BY]->(v) edge and never reads the
        # register's value -- so it does not depend on this sync's write. If anything below
        # fails, `prev` is untouched, the next sync recomputes `changed` as True, and the flag
        # is reissued. The SET is idempotent, so a retry costs nothing.
        #
        # The transient state this admits is beliefs flagged for review slightly before the
        # register shows the new value. That is the fail-safe direction: a belief flagged early
        # is corrected on read, a belief never flagged is silently stale forever.
        flagged = 0
        if changed:
            flagged = repo.flag_referencing_beliefs(
                verifier_uuid=vid, new_value=res.value, display=res.display, at=now
            )

        rec = graph_adapter.record_counter(
            subject=subject,
            counter=counter,
            value=res.value,
            namespace=namespace,
            source="verifier-sync",
            name_embedding=name_embedding,
        )
        register_uuid = str(rec.get("uuid") or "")
        if register_uuid:
            repo.ensure_verified_edge(register_uuid=register_uuid, verifier_uuid=vid)
        repo.stamp_verifier(verifier_uuid=vid, value=res.value, display=res.display, at=now)
        results.append({
            "verifier": vid, "kind": kind, "subject": subject, "counter": counter,
            "value": res.value, "display": res.display, "changed": changed,
            "beliefs_flagged": flagged, "status": "refreshed",
        })
    return results


# --------------------------------------------------------------------------- seeding


# The standard config/status verifiers maintained on this box. Each is an env_key binding whose
# register is re-derived from the live environment on every sync. Kept small and declarative; add
# rows here (or new executor kinds) as more derivable state should self-heal.
DEFAULT_VERIFIER_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "kind": "env_key",
        "params": {"key": "MENHIR_EXPERIENCE_COUNTER_ENABLED", "settings_attr": "experience_counter_enabled"},
        "register_subject": "menhir-config",
        "register_counter": "experience_counter_enabled",
    },
    {
        "kind": "env_key",
        "params": {"key": "MENHIR_STRUCTURE_WATCHER_ENABLED", "settings_attr": "structure_watcher_enabled"},
        "register_subject": "menhir-config",
        "register_counter": "structure_watcher_enabled",
    },
    {
        "kind": "env_key",
        "params": {"key": "MENHIR_API_PORT", "as": "int", "settings_attr": "api_port"},
        "register_subject": "menhir-config",
        "register_counter": "api_port",
    },
)


class _SeedRepo(Protocol):
    def upsert_verifier(
        self, *, kind: str, params: dict[str, Any], register_subject: str,
        register_counter: str, namespace: str = ...,
    ) -> str: ...


def seed_default_verifiers(
    repo: _SeedRepo, *, seeds: "tuple[dict[str, Any], ...]" = DEFAULT_VERIFIER_SEEDS,
    namespace: str = "agent-status",
) -> list[str]:
    """Idempotently upsert the standard verifier bindings. Safe to call on every startup — the
    repository MERGEs on a deterministic key, so re-seeding never duplicates. Returns their uuids."""
    uuids: list[str] = []
    for s in seeds:
        uuids.append(
            repo.upsert_verifier(
                kind=s["kind"], params=s["params"],
                register_subject=s["register_subject"], register_counter=s["register_counter"],
                namespace=namespace,
            )
        )
    return uuids


def _parse_params(raw: Any) -> dict[str, Any]:
    import json

    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(raw)
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    except (ValueError, TypeError):
        return {}
