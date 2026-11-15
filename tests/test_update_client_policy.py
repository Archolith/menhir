"""scripts/update_client_policy.py: a policy it writes is one production startup accepts (#476)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from menhir.api.client_policy import _canonical_payload
from menhir.api.mcp_remote import production_tool_catalog

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "update_client_policy", ROOT / "scripts" / "update_client_policy.py"
)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)

V2 = ROOT / "tests" / "fixtures" / "client-policy.synthetic.json"
V3 = ROOT / "tests" / "fixtures" / "client-policy.v3.synthetic.json"
NEW = ("delete_memories", "list_namespace_memories")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _redigest(policy: dict) -> dict:
    policy["canonical_digest"] = hashlib.sha256(_canonical_payload(policy)).hexdigest()
    return policy


def _pre_476_v2() -> dict:
    """The v2 fixture as a deployment written before #476 would have it."""
    policy = _load(V2)
    for client in policy["clients"].values():
        for key in ("allowed_tools", "denied_tools"):
            client[key] = [t for t in client[key] if t not in NEW]
    return _redigest(policy)


def _startup_refuses(policy: dict) -> bool:
    try:
        script.verify(script.render(policy), policy["canonical_digest"], production_tool_catalog())
    except ValueError:
        return True
    return False


def test_pre_476_v2_policy_is_refused_and_the_update_is_accepted() -> None:
    """Fails if the old policy would start (no gate) or the updated one would not."""
    old = _pre_476_v2()
    assert _startup_refuses(old)
    new, changes = script.update_policy(old, production_tool_catalog())
    assert not _startup_refuses(new)
    assert new["canonical_digest"] != old["canonical_digest"]
    assert len(changes) == len(NEW) * len(old["clients"])


def test_update_reproduces_the_reviewed_fixture_decisions() -> None:
    """The per-tier rule gives the same lists the hand-reviewed fixture carries."""
    new, _ = script.update_policy(_pre_476_v2(), production_tool_catalog())
    assert new == _load(V2)


def test_tier_rule_for_new_tools() -> None:
    new, _ = script.update_policy(_pre_476_v2(), production_tool_catalog())
    by_tier = {c["maximum_tier"]: c for c in new["clients"].values()}
    assert set(NEW) <= set(by_tier["operator"]["allowed_tools"])
    assert set(NEW) <= set(by_tier["agent"]["denied_tools"])
    assert set(NEW) <= set(by_tier["readonly"]["denied_tools"])


def test_existing_decisions_are_never_changed() -> None:
    """Fails if the script re-decides a tool the deployment already chose (e.g. denies delete_memory)."""
    old = _pre_476_v2()
    operator = next(c for c in old["clients"].values() if c["maximum_tier"] == "operator")
    operator["allowed_tools"].remove("delete_memory")
    operator["denied_tools"] = sorted([*operator["denied_tools"], "delete_memory"])
    new, _ = script.update_policy(_redigest(old), production_tool_catalog())
    label = operator["label"]
    updated = next(c for c in new["clients"].values() if c["label"] == label)
    assert "delete_memory" in updated["denied_tools"]


def test_current_policy_is_a_no_op() -> None:
    current = _load(V2)
    new, changes = script.update_policy(current, production_tool_catalog())
    assert changes == [] and new == current


def test_unknown_tool_is_refused() -> None:
    policy = _load(V2)
    next(iter(policy["clients"].values()))["denied_tools"].append("retired_tool")
    with pytest.raises(ValueError, match="retired_tool"):
        script.update_policy(policy, production_tool_catalog())


def test_v3_repin_grants_and_default_leaves_withheld() -> None:
    old = _load(V3)
    old["profile_catalog"] = 3
    old = _redigest(old)
    same, changes = script.update_policy(old, production_tool_catalog())
    assert changes == [] and same == old and not _startup_refuses(same)
    new, changes = script.update_policy(old, production_tool_catalog(), profile_catalog=4)
    assert changes == ["profile_catalog: 3 -> 4"] and new == _load(V3)
    assert not _startup_refuses(new)


def test_main_dry_run_writes_nothing_and_out_writes_a_loadable_file(tmp_path, capsys) -> None:
    src = tmp_path / "policy.json"
    src.write_text(json.dumps(_pre_476_v2()), encoding="utf-8")
    out = tmp_path / "new.json"
    assert script.main([str(src)]) == 0
    assert not out.exists() and "dry run" in capsys.readouterr().out
    assert script.main([str(src), "--out", str(out)]) == 0
    digest = capsys.readouterr().out.split("MENHIR_CLIENT_POLICY_DIGEST=")[1].split()[0]
    policy = json.loads(out.read_text(encoding="utf-8"))
    assert policy["canonical_digest"] == digest and not _startup_refuses(policy)
