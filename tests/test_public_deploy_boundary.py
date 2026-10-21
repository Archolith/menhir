"""Keep operator deployment material out of the public Menhir checkout."""

import ipaddress
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


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
    windows_home = re.compile(rb"[A-Za-z]:\\Users\\[^\\\s]+", re.I)
    ipv4 = re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    for relative in _tracked_deploy_paths():
        path = ROOT / relative
        contents = path.read_bytes()
        assert windows_home.search(contents) is None, relative
        for candidate in ipv4.findall(contents):
            try:
                address = ipaddress.ip_address(candidate.decode("ascii"))
            except ValueError:
                continue
            assert not address.is_global, relative
