"""Bring a deployment client policy up to this build's MCP tool catalog.

Production startup refuses a v1/v2 policy unless every client decides every catalog tool
(allowed or denied) and an operator product client allows exactly ``catalog - denied``. A v3
policy loads with an older ``profile_catalog`` but withholds the newer tools. This script:

- v1/v2: adds each tool a client has not decided. Operator clients allow it unless it is in
  OPERATOR_DENIED_TOOLS; agent clients allow it only if it is in AGENT_ALLOWED_TOOLS; readonly
  clients deny it. Existing decisions are never changed.
- v3: with ``--profile-catalog``, re-pins the policy to that catalog version.
- Recomputes ``canonical_digest`` and loads the result with the same loader and catalog that
  production startup uses, so a policy it writes is one startup accepts.

Default is a dry run that prints the changes. ``--out`` writes the new policy; set
MENHIR_CLIENT_POLICY_DIGEST to the printed digest when deploying it.

    python scripts/update_client_policy.py /path/client-policy.json
    python scripts/update_client_policy.py /path/client-policy.json --out /path/new.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from menhir.access_contract import AGENT_ALLOWED_TOOLS, OPERATOR_DENIED_TOOLS
from menhir.api.client_policy import (
    _canonical_payload,
    _reject_duplicate_keys,
    load_client_policy,
)
from menhir.api.mcp_remote import production_tool_catalog
from menhir.mcp.tool_profiles import CATALOG_VERSION


def _decide(tool: str, maximum_tier: str) -> str:
    if maximum_tier == "operator":
        return "denied_tools" if tool in OPERATOR_DENIED_TOOLS else "allowed_tools"
    if maximum_tier == "agent":
        return "allowed_tools" if tool in AGENT_ALLOWED_TOOLS else "denied_tools"
    return "denied_tools"


def _insert(values: list[str], tool: str) -> list[str]:
    was_sorted = values == sorted(values)
    values = [*values, tool]
    return sorted(values) if was_sorted else values


def update_policy(
    payload: dict[str, Any],
    catalog: frozenset[str],
    *,
    profile_catalog: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return the updated policy (digest recomputed) and a line per change. Never mutates input."""

    policy = json.loads(json.dumps(payload))
    changes: list[str] = []
    version = policy.get("version")
    if version in (1, 2):
        if profile_catalog is not None:
            raise ValueError("--profile-catalog applies only to version 3 policies")
        for client_id, client in policy["clients"].items():
            label = client.get("label") or client_id
            allowed = list(client.get("allowed_tools", []))
            denied = list(client.get("denied_tools", []))
            unknown = sorted((set(allowed) | set(denied)) - catalog)
            if unknown:
                raise ValueError(
                    f"client {label} names tools this build does not have: {unknown}; "
                    "remove them by hand after confirming they were retired"
                )
            lists = {"allowed_tools": allowed, "denied_tools": denied}
            for tool in sorted(catalog - set(allowed) - set(denied)):
                key = _decide(tool, str(client.get("maximum_tier", "")))
                lists[key] = _insert(lists[key], tool)
                changes.append(f"{label}: {tool} -> {key.split('_')[0]}")
            client["allowed_tools"], client["denied_tools"] = lists["allowed_tools"], lists["denied_tools"]
    elif version == 3:
        if profile_catalog is not None:
            if not 1 <= profile_catalog <= CATALOG_VERSION:
                raise ValueError(
                    f"--profile-catalog must be between 1 and this build's {CATALOG_VERSION}"
                )
            old = policy.get("profile_catalog")
            if old != profile_catalog:
                policy["profile_catalog"] = profile_catalog
                changes.append(f"profile_catalog: {old} -> {profile_catalog}")
    else:
        raise ValueError(f"unsupported policy version {version!r}")
    policy["canonical_digest"] = hashlib.sha256(_canonical_payload(policy)).hexdigest()
    return policy, changes


def render(policy: dict[str, Any]) -> str:
    return json.dumps(policy, indent=2, ensure_ascii=True) + "\n"


def verify(text: str, digest: str, catalog: frozenset[str]) -> None:
    """Load ``text`` exactly as production startup would; raises ValueError if it would refuse."""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "client-policy.json"
        path.write_text(text, encoding="utf-8")
        load_client_policy(str(path.resolve()), digest, tool_catalog=catalog)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("policy", type=Path, help="current deployment client-policy.json")
    parser.add_argument("--out", type=Path, help="write the updated policy here")
    parser.add_argument(
        "--profile-catalog",
        type=int,
        help=f"v3 only: re-pin profile_catalog (this build is {CATALOG_VERSION})",
    )
    args = parser.parse_args(argv)

    payload = json.loads(
        args.policy.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
    )
    catalog = production_tool_catalog()
    try:
        policy, changes = update_policy(payload, catalog, profile_catalog=args.profile_catalog)
        text = render(policy)
        verify(text, policy["canonical_digest"], catalog)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    for line in changes or ["no changes: the policy already matches this build's catalog"]:
        print(line)
    if policy.get("version") == 3 and policy.get("profile_catalog", 0) < CATALOG_VERSION:
        print(
            f"note: profile_catalog {policy.get('profile_catalog')} < {CATALOG_VERSION}; "
            "newer profile tools stay withheld (pass --profile-catalog to grant them)"
        )
    print(f"MENHIR_CLIENT_POLICY_DIGEST={policy['canonical_digest']}")
    if args.out is None:
        print("dry run: nothing written (pass --out to write)")
        return 0
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
