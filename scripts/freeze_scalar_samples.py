#!/usr/bin/env python3
"""Freeze k typed-scalar extraction samples to JSON (measurement only).

The preferred input is a pre-registered static episode file.  It makes the first held-out
measurement independent of whether a graph was populated with legacy ``Episodic`` nodes or raw
``TurnEvidence``.  The graph path remains available for compatibility, but follows the same
evidence-first source switch and exact namespace lookup as the scheduler.

No result is written when the input contract is incomplete.  In particular, a missing namespace
is an error rather than a warning followed by a partial capture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI

from menhir.services.perception import Episode
from menhir.services.typed_scalar_perception import extract_typed_scalars_once

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from measure_absence_band import build_episodes  # noqa: E402


STATIC_INPUT_SCHEMA_VERSION = 1
STATIC_INPUT_REQUIRED_KEYS = frozenset({"schema_version", "fixture_id", "non_lme", "namespaces"})
STATIC_NAMESPACE_REQUIRED_KEYS = frozenset({"purpose", "episodes"})
STATIC_NAMESPACE_PURPOSES = frozenset({"fully_covered", "fallback_adversarial"})

# These queries deliberately mirror the scheduler's two loaders.  The source choice is global,
# just like MemoryGraphAdapter.{load_user_episodes,load_next_scalar_batch}: any user TurnEvidence
# switches the box to evidence-first; otherwise the legacy user:-prefixed Episodic path is used.
TURN_EVIDENCE_EXISTS_Q = (
    "MATCH (t:TurnEvidence {role: 'user'}) RETURN count(t) AS c LIMIT 1"
)
TURN_EVIDENCE_Q = """
MATCH (t:TurnEvidence {namespace: $ns})
WHERE t.role = 'user' AND t.declarant = 'user' AND t.text IS NOT NULL AND t.text <> ''
RETURN t.turn_id AS uuid,
       toString(coalesce(t.occurred_at, t.recorded_at, datetime())) AS valid_at,
       t.text AS content
ORDER BY coalesce(t.occurred_at, t.recorded_at), t.recorded_at, t.turn_id
LIMIT $limit
"""
LEGACY_EPISODIC_Q = """
MATCH (e:Episodic {group_id: $ns})
WHERE e.content STARTS WITH 'user:'
RETURN e.uuid AS uuid, toString(e.valid_at) AS valid_at, e.content AS content
ORDER BY e.valid_at
LIMIT $limit
"""


class FreezeInputError(ValueError):
    """Raised when a frozen-capture input cannot be trusted as complete and exact."""


def _non_blank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FreezeInputError(f"{field} must be a non-blank string")
    return value


def _load_json(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    try:
        with resolved.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise FreezeInputError(f"static input does not exist: {resolved}") from exc
    except OSError as exc:
        raise FreezeInputError(f"could not read static input {resolved}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FreezeInputError(
            f"invalid static input JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise FreezeInputError("static input top-level JSON value must be an object")
    return payload


def _load_static_namespaces(path: str | Path) -> tuple[dict[str, list[Episode]], dict[str, Any]]:
    """Load the versioned, exact episode rows used by a held-out smoke capture."""
    payload = _load_json(path)
    missing = STATIC_INPUT_REQUIRED_KEYS - payload.keys()
    extra = payload.keys() - STATIC_INPUT_REQUIRED_KEYS
    if missing:
        raise FreezeInputError(f"static input missing required fields: {', '.join(sorted(missing))}")
    if extra:
        raise FreezeInputError(f"static input has unknown fields: {', '.join(sorted(extra))}")
    if payload["schema_version"] != STATIC_INPUT_SCHEMA_VERSION:
        raise FreezeInputError(
            f"static input schema_version must be {STATIC_INPUT_SCHEMA_VERSION}"
        )
    fixture_id = _non_blank(payload["fixture_id"], "static input fixture_id")
    if payload["non_lme"] is not True:
        raise FreezeInputError("static input non_lme must be true")
    raw_namespaces = payload["namespaces"]
    if not isinstance(raw_namespaces, dict) or not raw_namespaces:
        raise FreezeInputError("static input namespaces must be a non-empty object")

    namespaces: dict[str, list[Episode]] = {}
    seen_uuids: set[str] = set()
    purposes: set[str] = set()
    for namespace, raw_namespace in raw_namespaces.items():
        if not isinstance(namespace, str) or not namespace.strip():
            raise FreezeInputError("static input namespace keys must be non-blank strings")
        if not isinstance(raw_namespace, dict):
            raise FreezeInputError(f"namespace {namespace!r} must be an object")
        missing = STATIC_NAMESPACE_REQUIRED_KEYS - raw_namespace.keys()
        extra = raw_namespace.keys() - STATIC_NAMESPACE_REQUIRED_KEYS
        if missing:
            raise FreezeInputError(
                f"namespace {namespace!r} missing required fields: {', '.join(sorted(missing))}"
            )
        if extra:
            raise FreezeInputError(
                f"namespace {namespace!r} has unknown fields: {', '.join(sorted(extra))}"
            )
        purpose = raw_namespace["purpose"]
        if not isinstance(purpose, str) or purpose not in STATIC_NAMESPACE_PURPOSES:
            raise FreezeInputError(
                f"namespace {namespace!r} purpose must be one of {sorted(STATIC_NAMESPACE_PURPOSES)}"
            )
        purposes.add(purpose)
        raw_episodes = raw_namespace["episodes"]
        if not isinstance(raw_episodes, list) or not raw_episodes:
            raise FreezeInputError(f"namespace {namespace!r} episodes must be a non-empty array")

        episodes: list[Episode] = []
        for index, raw_episode in enumerate(raw_episodes):
            context = f"namespace {namespace!r}, episode {index}"
            if not isinstance(raw_episode, dict) or set(raw_episode) != {"uuid", "content"}:
                raise FreezeInputError(f"{context} must contain exactly uuid and content")
            uuid = _non_blank(raw_episode["uuid"], f"{context} uuid")
            if raw_episode["uuid"] != uuid:
                raise FreezeInputError(f"{context} uuid must not have surrounding whitespace")
            if uuid in seen_uuids:
                raise FreezeInputError(f"duplicate episode uuid {uuid!r}")
            content = _non_blank(raw_episode["content"], f"{context} content")
            if len(content) < 8:
                raise FreezeInputError(f"{context} content must contain at least 8 characters")
            seen_uuids.add(uuid)
            episodes.append(Episode(uuid=uuid, content=content))
        namespaces[namespace] = episodes

    if purposes != STATIC_NAMESPACE_PURPOSES:
        raise FreezeInputError(
            "static input must contain separate fully_covered and fallback_adversarial namespaces"
        )
    metadata = {
        "mode": "static",
        "schema_version": STATIC_INPUT_SCHEMA_VERSION,
        "fixture_id": fixture_id,
        "path": str(Path(path).expanduser().resolve()),
    }
    return namespaces, metadata


def _graph_has_turn_evidence(session: Any) -> bool:
    rows = session.run(TURN_EVIDENCE_EXISTS_Q).data()
    count = rows[0].get("c", 0) if rows else 0
    return bool(int(count or 0) > 0)


def _load_graph_namespaces(
    session: Any, namespaces: list[str], *, limit: int = 500,
) -> tuple[dict[str, list[Episode]], dict[str, Any]]:
    """Load exact namespaces using the same evidence-first source switch as the scheduler."""
    if len(set(namespaces)) != len(namespaces):
        raise FreezeInputError("--ns must not contain duplicate exact namespaces")
    use_turn_evidence = _graph_has_turn_evidence(session)
    query = TURN_EVIDENCE_Q if use_turn_evidence else LEGACY_EPISODIC_Q
    source = "turn_evidence" if use_turn_evidence else "episodic"
    loaded: dict[str, list[Episode]] = {}
    for namespace in namespaces:
        if not namespace.strip():
            raise FreezeInputError("--ns namespaces must be non-blank exact group IDs")
        rows = session.run(query, {"ns": namespace, "limit": int(limit)}).data()
        if not rows:
            raise FreezeInputError(
                f"no user episodes found for exact namespace {namespace!r} via {source}"
            )
        episodes = build_episodes(rows)
        if not episodes:
            raise FreezeInputError(
                f"exact namespace {namespace!r} has no usable user episodes via {source}"
            )
        loaded[namespace] = episodes
    return loaded, {"mode": "graph", "source": source, "namespaces": list(namespaces)}


def main() -> int:
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--ns", nargs="+", help="exact namespace IDs; graph source follows scheduler parity"
    )
    source.add_argument(
        "--episodes-json", metavar="PATH",
        help="versioned static episode JSON; preferred for held-out measurement",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--uri", default="bolt://127.0.0.1:7701")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="lmedata123")
    args = ap.parse_args()

    if args.k <= 0 or args.max_tokens <= 0:
        print("-k and --max-tokens must be positive", file=sys.stderr)
        return 2

    # Fully validate and materialize every input episode before constructing the LLM client.  A
    # missing namespace therefore cannot consume calls and leave a partial capture behind.
    try:
        if args.episodes_json:
            namespace_episodes, input_metadata = _load_static_namespaces(args.episodes_json)
        else:
            driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
            try:
                with driver.session() as session:
                    namespace_episodes, input_metadata = _load_graph_namespaces(session, args.ns)
            finally:
                driver.close()
    except FreezeInputError as exc:
        print(f"input rejected: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # graph connection/query failures are measurement blockers
        print(f"input loading failed closed: {exc}", file=sys.stderr)
        return 2

    load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    model = os.environ.get("MENHIR_PERSONAL_MEMORY_CHAT_MODEL") or "gpt-4o-mini"
    client = OpenAI(api_key=api_key)

    truncated = 0
    calls = 0

    def llm_complete(system: str, user: str) -> str:
        nonlocal truncated, calls
        calls += 1
        resp = client.chat.completions.create(
            model=model, temperature=args.temp, max_tokens=args.max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        if resp.choices[0].finish_reason == "length":
            truncated += 1
        return resp.choices[0].message.content or ""

    out: dict[str, Any] = {
        "settings": {
            "model": model, "k": args.k, "temp": args.temp,
            "max_tokens": args.max_tokens,
        },
        "input": input_metadata,
        "namespaces": {},
    }
    for namespace, episodes in namespace_episodes.items():
        samples = [
            extract_typed_scalars_once(episodes, llm_complete) for _ in range(args.k)
        ]
        out["namespaces"][namespace] = {
            "episodes": [{"uuid": e.uuid, "content": e.content} for e in episodes],
            "samples": [[asdict(p) for p in sample] for sample in samples],
        }
        print(f"  froze {namespace}: {[len(sample) for sample in samples]} proposals/sample")

    out["settings"]["truncated_completions"] = truncated
    out["settings"]["llm_calls"] = calls
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nwrote {args.out}   calls={calls}  truncated={truncated}")
    if truncated:
        print(
            "WARNING: truncated completions present -- this frozen set is CONTAMINATED, "
            "raise --max-tokens and re-freeze before using it for any comparison."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
