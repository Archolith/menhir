from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bump", ROOT / "scripts" / "bump.py")
assert SPEC is not None and SPEC.loader is not None
bump = importlib.util.module_from_spec(SPEC)
sys.modules["bump"] = bump
SPEC.loader.exec_module(bump)

COMMIT = "a" * 40
DATE = "2030-01-02"
#: Tracked files that may name the current version without being bumped: history, generated
#: inventories, other packages that share the number, and this repo's test fixtures. Add a file
#: here only if its mention must NOT change on the next release; otherwise add it to VERSION_LINES.
VERSION_MENTION_EXEMPT = (
    ".agent/", "CHANGELOG", "THIRD-PARTY-LICENSES.txt", "tests/test_release_pipeline.py",
)


def _next_patch(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def _next_fork(version: str) -> str:
    base, _, post = version.rpartition(".post")
    return f"{base}.post{int(post) + 1}"


def _release(version: str) -> bump.ForkRelease:
    base = "https://files.pythonhosted.org/packages/aa/bb/ccc"
    return bump.ForkRelease(
        bump.FileRelease(f"{base}/archolith_graphiti_core-{version}.tar.gz", "1" * 64, 11,
                         "2030-01-02T03:04:05.006Z"),
        bump.FileRelease(f"{base}/archolith_graphiti_core-{version}-py3-none-any.whl", "2" * 64, 22,
                         "2030-01-02T03:04:05.007Z"),
    )


def _copy_tree(tmp_path: Path, paths: set[str]) -> Path:
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def _version_tree(tmp_path: Path) -> Path:
    paths = {path for path, _ in bump.VERSION_LINES}
    return _copy_tree(tmp_path, paths | {"CHANGELOG.md", "docs/model-governance.md"})


def _fork_tree(tmp_path: Path) -> Path:
    return _copy_tree(tmp_path, set(bump.FORK_VERSION_FILES) | {"CHANGELOG.md", "uv.lock"})


class FakeRun:
    def __init__(self, *, failing: str | None = None, outputs: dict[str, str] | None = None) -> None:
        self.failing = failing
        self.outputs = outputs or {}
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        text = " ".join(command)
        code = 1 if self.failing and text.startswith(self.failing) else 0
        return subprocess.CompletedProcess(command, code, self.outputs.get(text, ""), "boom")


# ---- the real repository ----------------------------------------------------


def test_version_edits_match_the_repository_today() -> None:
    version = bump.current_version(ROOT)
    edits = bump.version_edits(ROOT, _next_patch(version), summary="s", date=DATE)
    bump.apply_edits(ROOT, edits, write=False)


def test_fork_edits_match_the_repository_today() -> None:
    new = _next_fork(bump.current_fork_version(ROOT))
    edits = bump.fork_edits(ROOT, new, commit=COMMIT, release=_release(new), summary="s", date=DATE)
    bump.apply_edits(ROOT, edits, write=False)


def _tracked_text() -> dict[str, bytes]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if listed.returncode != 0:
        pytest.skip("needs a git checkout")
    files: dict[str, bytes] = {}
    for relative in listed.stdout.splitlines():
        path = ROOT / relative
        if path.is_file():
            data = path.read_bytes()
            if b"\0" not in data[:4096]:
                files[relative] = data
    return files


def test_every_mention_of_the_current_version_is_bumped_or_exempt() -> None:
    # Fails when someone adds a new line stating the current version that the bump would miss.
    version = re.escape(bump.current_version(ROOT))
    pattern = re.compile(rf"(?<![0-9.]){version}(?![0-9]|\.[0-9])".encode())
    covered = {path for path, _ in bump.VERSION_LINES} | {"docs/model-governance.md", "uv.lock"}
    missed = [
        relative for relative, data in _tracked_text().items()
        if pattern.search(data) and relative not in covered
        and not relative.startswith(VERSION_MENTION_EXEMPT)
    ]
    assert missed == []


def test_the_script_names_no_real_version() -> None:
    # An example version in bump.py would fail the two mention checks at that very release.
    source = (ROOT / "scripts" / "bump.py").read_text(encoding="utf-8")
    assert re.findall(r"[0-9]+\.[0-9]+\.[0-9]+", source) == []


def test_fork_file_table_is_complete() -> None:
    # A new mention of the pin, or a removed one, must fail here rather than at bump time.
    version = bump.current_fork_version(ROOT).encode()
    actual = {
        relative: data.count(version) for relative, data in _tracked_text().items()
        if version in data and relative != "uv.lock" and not relative.startswith("CHANGELOG")
    }
    assert actual == bump.FORK_VERSION_FILES


# ---- version bump -----------------------------------------------------------


def test_version_bump_updates_every_line(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)
    old = bump.current_version(root)
    new = _next_patch(old)
    bump.apply_edits(root, bump.version_edits(root, new, summary="a fix", date=DATE))
    assert bump.current_version(root) == new
    for path, line in bump.VERSION_LINES:
        assert line.format(v=new) in bump.read_text(root, path)
        assert line.format(v=old) not in bump.read_text(root, path)
    assert f"{DATE}, package version `{new}`" in bump.read_text(root, "docs/model-governance.md")
    assert bump.read_text(root, "CHANGELOG.md").startswith(f"## {DATE} - v{new}: a fix\n")


def test_older_changelog_entries_are_kept(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)
    before = bump.read_text(root, "CHANGELOG.md")
    new = _next_patch(bump.current_version(root))
    bump.apply_edits(root, bump.version_edits(root, new, summary="x", date=DATE))
    assert bump.read_text(root, "CHANGELOG.md").endswith(before)


@pytest.mark.parametrize("new", ["0.0.1", "1.2", "v1.2.3", "1.2.3rc1", "1.2.3.post1", ""])
def test_old_or_malformed_version_is_refused(tmp_path: Path, new: str) -> None:
    root = _version_tree(tmp_path)
    with pytest.raises(bump.BumpError):
        bump.version_edits(root, new, summary="x", date=DATE)


def test_same_version_is_refused(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)
    with pytest.raises(bump.BumpError, match="not newer"):
        bump.version_edits(root, bump.current_version(root), summary="x", date=DATE)


def test_one_missing_line_writes_nothing(tmp_path: Path) -> None:
    # Counterexample: a partial bump would ship a package whose docs state the old version.
    root = _version_tree(tmp_path)
    old = bump.current_version(root)
    edits = bump.version_edits(root, _next_patch(old), summary="x", date=DATE)
    readme = bump.read_text(root, "README.md").replace(
        f"known gaps in {old},", "known gaps in this release,"
    )
    (root / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    before = (root / "pyproject.toml").read_bytes()
    with pytest.raises(bump.BumpError, match="nothing written"):
        bump.apply_edits(root, edits)
    assert (root / "pyproject.toml").read_bytes() == before


def test_a_line_that_appears_twice_writes_nothing(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)
    old = bump.current_version(root)
    edits = bump.version_edits(root, _next_patch(old), summary="x", date=DATE)
    with (root / "README.md").open("a", encoding="utf-8", newline="\n") as readme:
        readme.write(f"\nThe current package version is `{old}`.\n")
    with pytest.raises(bump.BumpError, match="found 2"):
        bump.apply_edits(root, edits)


def test_crlf_files_stay_crlf(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\r\ntwo\r\n")
    bump.apply_edits(tmp_path, [bump.Edit("a.txt", "one\ntwo", "1\n2")])
    assert path.read_bytes() == b"1\r\n2\r\n"


# ---- fork bump --------------------------------------------------------------


def test_fork_bump_replaces_version_commit_hash_and_lock_lines(tmp_path: Path) -> None:
    root = _fork_tree(tmp_path)
    old = bump.current_fork_version(root)
    new = _next_fork(old)
    release = _release(new)
    bump.apply_edits(root, bump.fork_edits(
        root, new, commit=COMMIT, release=release, summary="fix", date=DATE,
    ))
    assert bump.current_fork_version(root) == new
    lock = bump.read_text(root, "uv.lock")
    assert old not in lock
    assert f'sdist = {{ url = "{release.sdist.url}", hash = "sha256:{"1" * 64}", size = 11, ' in lock
    assert f'{{ url = "{release.wheel.url}", hash = "sha256:{"2" * 64}", size = 22, ' in lock
    architecture = bump.read_text(root, ".agent/architecture.md")
    assert COMMIT in architecture
    assert "2" * 64 in architecture
    assert "2" * 64 in bump.read_text(root, "tests/e2e/test_e2e_06_graphiti_fork.py")


def test_fork_bump_keeps_other_packages_lock_lines(tmp_path: Path) -> None:
    root = _fork_tree(tmp_path)
    before = bump.read_text(root, "uv.lock").splitlines()
    new = _next_fork(bump.current_fork_version(root))
    bump.apply_edits(root, bump.fork_edits(
        root, new, commit=COMMIT, release=_release(new), summary="x", date=DATE,
    ))
    after = bump.read_text(root, "uv.lock").splitlines()
    assert len(after) == len(before)
    assert sum(1 for a, b in zip(after, before) if a != b) == 4


@pytest.mark.parametrize("commit", ["abc", "A" * 40, ""])
def test_short_or_malformed_commit_is_refused(tmp_path: Path, commit: str) -> None:
    root = _fork_tree(tmp_path)
    new = _next_fork(bump.current_fork_version(root))
    with pytest.raises(bump.BumpError, match="40-character"):
        bump.fork_edits(root, new, commit=commit, release=_release(new), summary="x", date=DATE)


def _pypi_entry(name: str, **overrides) -> dict:
    entry = {
        "filename": name,
        "url": f"https://files.pythonhosted.org/packages/a/b/c/{name}",
        "digests": {"sha256": "3" * 64},
        "size": 5,
        "upload_time_iso_8601": "2026-11-02T19:35:33.018251Z",
    }
    entry.update(overrides)
    return entry


def test_pypi_payload_becomes_lock_values() -> None:
    wheel = "archolith_graphiti_core-1.2.3.post4-py3-none-any.whl"
    sdist = "archolith_graphiti_core-1.2.3.post4.tar.gz"
    release = bump.parse_fork_release(
        "1.2.3.post4", {"urls": [_pypi_entry(wheel), _pypi_entry(sdist)]},
    )
    assert release.wheel.upload_time == "2026-11-02T19:35:33.018Z"
    assert release.sdist.sha256 == "3" * 64


def test_unexpected_pypi_file_set_is_refused() -> None:
    with pytest.raises(bump.BumpError, match="one sdist and one wheel"):
        bump.parse_fork_release("1.2.3", {"urls": [_pypi_entry("archolith_graphiti_core-1.2.3.tar.gz")]})


def test_download_url_on_another_host_is_refused() -> None:
    wheel = "archolith_graphiti_core-1.2.3-py3-none-any.whl"
    sdist = "archolith_graphiti_core-1.2.3.tar.gz"
    payload = {"urls": [
        _pypi_entry(wheel, url=f"https://example.com/{wheel}"), _pypi_entry(sdist),
    ]}
    with pytest.raises(bump.BumpError, match="unexpected download"):
        bump.parse_fork_release("1.2.3", payload)


# ---- finishing --------------------------------------------------------------


def test_failed_lock_check_restores_every_file(tmp_path: Path) -> None:
    # Counterexample: a new fork with changed dependencies needs a real re-lock, not this edit.
    root = _fork_tree(tmp_path)
    new = _next_fork(bump.current_fork_version(root))
    edits = bump.fork_edits(root, new, commit=COMMIT, release=_release(new), summary="x", date=DATE)
    before = {path: (root / path).read_bytes() for path in {edit.path for edit in edits}}
    run = FakeRun(failing="uv lock")
    with pytest.raises(bump.BumpError, match="restored"):
        bump.finish(root, edits, run=run, pr=True, branch="b", title="t", body="x")
    assert {path: (root / path).read_bytes() for path in before} == before
    assert not any(command[0] == "git" for command in run.commands)


def test_pr_stages_only_the_edited_files_by_name(tmp_path: Path) -> None:
    root = _version_tree(tmp_path)
    new = _next_patch(bump.current_version(root))
    edits = bump.version_edits(root, new, summary="x", date=DATE)
    run = FakeRun()
    bump.finish(root, edits, run=run, pr=True, branch="chore/release-x", title="t", body="b")
    assert run.commands[0] == ["uv", "lock", "--check"]
    add = next(command for command in run.commands if command[:2] == ["git", "add"])
    assert add[2] == "--"
    assert set(add[3:]) == {edit.path for edit in edits}
    assert not any(command[:2] == ["git", "tag"] for command in run.commands)


@pytest.mark.parametrize("outputs, match", [
    ({"git status --porcelain": " M README.md"}, "not clean"),
    ({"git rev-parse --abbrev-ref HEAD": "feature"}, "from main"),
    ({"git rev-parse --abbrev-ref HEAD": "main", "git rev-parse HEAD": "1",
      "git rev-parse origin/main": "2"}, "origin/main"),
])
def test_pr_needs_a_clean_up_to_date_main(outputs: dict[str, str], match: str) -> None:
    with pytest.raises(bump.BumpError, match=match):
        bump.require_clean_main(FakeRun(outputs=outputs))


def test_cli_reports_a_refusal_without_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    code = bump.main(["version", "0.0.1", "--summary", "x"], run=FakeRun())
    assert code == 1
    assert "not newer" in capsys.readouterr().err
