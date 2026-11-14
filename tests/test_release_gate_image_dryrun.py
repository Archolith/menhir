"""#614: scripts/release.py refuses `--tag` unless the image dry run is green on HEAD's SHA."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("release_gate", ROOT / "scripts" / "release.py")
assert _SPEC is not None and _SPEC.loader is not None
release = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = release  # dataclasses resolve annotations through sys.modules
_SPEC.loader.exec_module(release)

SHA = "a" * 40
URL = "https://github.com/Archolith/menhir/actions/runs/7"


def _run(run_id=7, status="completed", conclusion="success", event="workflow_dispatch",
         created="2026-11-13T10:00:00Z"):
    return {"databaseId": run_id, "status": status, "conclusion": conclusion, "url": URL,
            "event": event, "createdAt": created}


def _jobs(conclusion="success", *, drop=None):
    return [
        {"name": f"dry run {runner} / {stage}", "conclusion": conclusion}
        for runner in release.DRYRUN_RUNNERS
        for stage in release.DRYRUN_STAGES
        if (runner, stage) != drop
    ]


class FakeGitHub:
    """Serves `gh run list` from a queue of snapshots and `gh run view` per run id."""

    def __init__(self, snapshots, jobs):
        self.snapshots = list(snapshots)
        self.jobs = jobs
        self.list_calls = []

    def gh_json(self, *args):
        self.list_calls.append(args)
        assert args[:2] == ("run", "list")
        assert args[args.index("--commit") + 1] == SHA
        assert args[args.index("--workflow") + 1] == "release-image-dryrun.yml"
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def run(self, cmd, **_):
        assert cmd[:3] == ["gh", "run", "view"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"jobs": self.jobs[int(cmd[3])]}), "")


@pytest.fixture
def github(monkeypatch):
    def install(snapshots, jobs=None):
        fake = FakeGitHub(snapshots, jobs if jobs is not None else {7: _jobs()})
        monkeypatch.setattr(release, "gh_json", fake.gh_json)
        monkeypatch.setattr(release, "run", fake.run)
        monkeypatch.setattr(release.time, "sleep", lambda _s: None)
        return fake
    return install


def _ctx():
    return release.Context(tag="v9.9.9", version="9.9.9", head=SHA)


def test_a_green_dispatched_run_on_both_runners_passes(github) -> None:
    github([[_run()]])
    detail = release.check_image(_ctx())
    assert "ubuntu-24.04" in detail and "ubuntu-26.04" in detail and URL in detail


def test_no_run_refuses_and_prints_the_dispatch_command(github) -> None:
    github([[]])
    with pytest.raises(release.CheckFailed) as excinfo:
        release.check_image(_ctx())
    assert "gh workflow run release-image-dryrun.yml --ref main -R Archolith/menhir" in str(excinfo.value)


def test_a_pull_request_run_does_not_count(github) -> None:
    """A PR run builds the merge commit, not HEAD."""
    github([[_run(event="pull_request")]])
    with pytest.raises(release.CheckFailed, match="no release image dry run exists"):
        release.check_image(_ctx())


def test_a_red_run_refuses(github) -> None:
    github([[_run(conclusion="failure")]])
    with pytest.raises(release.CheckFailed, match="not green"):
        release.check_image(_ctx())


def test_the_newest_run_decides(github) -> None:
    github([[_run(run_id=7, created="2026-11-13T10:00:00Z"),
             _run(run_id=8, conclusion="failure", created="2026-11-13T11:00:00Z", event="schedule")]])
    with pytest.raises(release.CheckFailed, match="not green"):
        release.check_image(_ctx())


def test_it_waits_for_a_running_dry_run(github) -> None:
    fake = github([[_run(status="in_progress", conclusion="")], [_run()]])
    release.check_image(_ctx())
    assert len(fake.list_calls) == 2


@pytest.mark.parametrize("jobs", [
    _jobs(drop=("ubuntu-26.04", "Verify sealed candidate without publishing")),
    _jobs(conclusion="skipped"),
])
def test_a_green_run_missing_a_runner_or_stage_refuses(github, jobs) -> None:
    """A run can conclude success with a job skipped; that is not a dry run."""
    github([[_run()]], {7: jobs})
    with pytest.raises(release.CheckFailed, match="did not build and verify on every runner"):
        release.check_image(_ctx())


def test_image_runs_after_ci_and_tag_refuses_before_tagging(monkeypatch) -> None:
    assert release.CHECK_ORDER.index("image") == release.CHECK_ORDER.index("ci") + 1
    for name in release.CHECK_ORDER:
        monkeypatch.setitem(release.CHECKS, name, lambda ctx: "ok")

    def red(ctx):
        raise release.CheckFailed("the image dry run is not green")

    monkeypatch.setitem(release.CHECKS, "image", red)
    tagged = []
    monkeypatch.setattr(release, "do_tag", lambda ctx: tagged.append(ctx.tag))
    monkeypatch.setattr(release, "watch_publish", lambda ctx: None)
    assert release.main(["v9.9.9", "--tag"]) == 1
    assert tagged == []


def test_there_is_no_flag_that_skips_the_image_check() -> None:
    options = {
        opt for line in release.__doc__.splitlines() for opt in line.split() if opt.startswith("--")
    }
    assert not any("skip" in opt or "image" in opt for opt in options)
    with pytest.raises(SystemExit):
        release.main(["v9.9.9", "--tag", "--skip-image"])
    assert release.main(["v9.9.9", "--tag", "--only", "tree,ci"]) == 2
