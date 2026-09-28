"""Keep operator deployment material out of the public Menhir checkout."""

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


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


def test_public_deploy_files_contain_no_operator_host_address() -> None:
    windows_home = re.compile(rb"[A-Za-z]:\\Users\\[^\\\s]+", re.I)
    tracked = subprocess.check_output(["git", "ls-files", "-z", "deploy"], cwd=ROOT)
    for relative in (part.decode("utf-8") for part in tracked.split(b"\0") if part):
        path = ROOT / relative
        assert windows_home.search(path.read_bytes()) is None, relative
