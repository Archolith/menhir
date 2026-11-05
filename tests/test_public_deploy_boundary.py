"""Keep operator deployment material out of the public Menhir checkout."""

import ipaddress
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
AGENT_BASELINE = ROOT / "tests" / "fixtures" / "agent-boundary-baseline.json"
# Backslash, forward-slash, escaped and Git Bash (/c/Users/...) spellings.
_WINDOWS_HOME = re.compile(rb"(?:[A-Za-z]:[\\/]+|/[A-Za-z]/)Users[\\/]+[^\\/\s]+", re.I)
_IPV4 = re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _tracked_deploy_paths() -> list[str]:
    tracked = subprocess.check_output(["git", "ls-files", "-z", "deploy"], cwd=ROOT)
    return [part.decode("utf-8") for part in tracked.split(b"\0") if part]


def test_operator_deployment_material_is_not_in_public_tree() -> None:
    assert not (ROOT / "pipeline").exists()
    forbidden = (
        "deploy/operator-host.json",
        "deploy/client-policy.production.json",
        "deploy/docker-compose.production.yml",
        "deploy/RUNBOOK.md",
        "deploy/PRODUCTION.md",
        "deploy/LIVE_VPS_PLAYBOOK.md",
        "deploy/RELEASE_AUTOMATION.md",
        "deploy/personal_deploy.py",
        "deploy/personal_stage.ps1",
        "deploy/personal_promote.ps1",
        "deploy/scaffold/install.sh",
        "deploy/ansible/playbook.yml",
    )
    assert not [relative for relative in forbidden if (ROOT / relative).exists()]
    operator_prefixes = (
        "deploy/ansible/",
        "deploy/changes/releases/",
        "deploy/personal_",
        "deploy/scaffold/",
    )
    assert not [path for path in _tracked_deploy_paths() if path.startswith(operator_prefixes)]


def test_public_deploy_files_contain_no_operator_host_address() -> None:
    for relative in _tracked_deploy_paths():
        path = ROOT / relative
        contents = path.read_bytes()
        assert _WINDOWS_HOME.search(contents) is None, relative
        for candidate in _IPV4.findall(contents):
            try:
                address = ipaddress.ip_address(candidate.decode("ascii"))
            except ValueError:
                continue
            assert not address.is_global, relative


def _tracked_agent_paths() -> list[str]:
    tracked = subprocess.check_output(["git", "ls-files", "-z", ".agent"], cwd=ROOT)
    return [part.decode("utf-8") for part in tracked.split(b"\0") if part]


def boundary_counts(contents: bytes) -> dict[str, int]:
    """Count Windows home paths and public IPv4 addresses, the two things deploy/ forbids."""

    public = 0
    for candidate in _IPV4.findall(contents):
        try:
            public += ipaddress.ip_address(candidate.decode("ascii")).is_global
        except ValueError:
            continue
    return {"home_paths": len(_WINDOWS_HOME.findall(contents)), "global_ipv4": public}


def test_agent_docs_gain_no_home_paths_or_public_addresses() -> None:
    """Older .agent/ docs predate this check, so their counts are a ceiling, not a pass.

    A new file must have none, and a baselined file may not gain any.
    """

    baseline = json.loads(AGENT_BASELINE.read_text(encoding="utf-8"))["files"]
    grown = []
    for relative in _tracked_agent_paths():
        allowed = baseline.get(relative, {})
        for kind, count in boundary_counts((ROOT / relative).read_bytes()).items():
            if count > allowed.get(kind, 0):
                grown.append(f"{relative}: {kind} {count}, baseline {allowed.get(kind, 0)}")
    assert not grown, (
        "Remove the home path or public address instead of raising the baseline. If a file was "
        "only moved or renamed, move its entry in tests/fixtures/agent-boundary-baseline.json.\n"
        + "\n".join(grown)
    )


def test_the_agent_baseline_lists_only_tracked_files() -> None:
    baseline = json.loads(AGENT_BASELINE.read_text(encoding="utf-8"))["files"]
    assert not sorted(set(baseline) - set(_tracked_agent_paths()))
