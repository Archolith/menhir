#!/usr/bin/env python3
"""Coordinate a Menhir release without bypassing review or deployment approval."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_NAME = "release-flow.json"
SPEC_NAME = "release-spec.json"
NOTES_JSON_NAME = "release-notes.json"
NOTES_MARKDOWN_NAME = "release-notes.md"
REVIEW_REQUEST_NAME = "security-review-request.json"
RELEASE_NAME = "release.json"
BUNDLE_NAME = "install-bundle"
DEFAULT_WRAPPER = SCRIPT_DIR.parents[3] / "scripts" / "deploy-menhir.ps1"
KIND = "menhir-release-flow"
SCHEMA = 1
PHASES = ("review_requested", "bundled", "deployed")
REPOSITORIES = frozenset({"menhir", "archolith_oauth", "yawn_deploy", "yawn_vps"})
CLASS_ORDER = {"app-only": 0, "security-config": 1, "maintenance": 2}
APP_ONLY_FORBIDDEN = tuple(re.compile(pattern) for pattern in (
    r"^deploy/",
    r"^\.github/",
    r"^(pyproject\.toml|uv\.lock|poetry\.lock|requirements[^/]*)$",
    r"^src/menhir/config/",
    r"^src/menhir/api/(auth|client_policy|oauth[^/]*)\.py$",
    r"^src/menhir/core/(bootstrap|runtime|runtime_preflight)\.py$",
    r"^src/menhir/infrastructure/(schema|migration_batches|embedding_dimensions)\.py$",
    r"^src/menhir/infrastructure/telemetry/schema_migrations\.py$",
))
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")
STATE_KEYS = frozenset({
    "schema", "kind", "phase", "release_id", "release_author", "workspace",
    "deployment_class", "inputs_sha256", "spec_sha256", "notes_json_sha256",
    "notes_markdown_sha256", "review_request_sha256", "security_review_sha256",
    "release_sha256", "bundle_manifest_sha256", "bundle_sha256",
})


class ReleaseFlowError(ValueError):
    """A release-flow input, state, or transition is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ReleaseFlowError(f"bundle does not exist: {root}") from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ReleaseFlowError("install bundle must be a non-symlink directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseFlowError(f"install bundle contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        elif stat.S_ISREG(info.st_mode):
            payload_digest = _sha256(path)
        else:
            raise ReleaseFlowError(f"install bundle contains special entry: {relative}")
        record = f"{relative}\0{payload_digest}\n"
        digest.update(record.encode("utf-8"))
    return digest.hexdigest()


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseFlowError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_json(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseFlowError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseFlowError(f"{label} must be a JSON object")
    return value


def _regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ReleaseFlowError(f"{label} must be an absolute path")
    try:
        path.lstat()
    except OSError as exc:
        raise ReleaseFlowError(f"{label} does not exist: {path}") from exc
    if not path.is_file() or path.is_symlink():
        raise ReleaseFlowError(f"{label} must be a regular non-symlink file")
    return path


def _workspace(path: Path, *, create: bool = False) -> Path:
    if not path.is_absolute():
        raise ReleaseFlowError("workspace must be an absolute path")
    if create:
        path.mkdir(parents=False, exist_ok=False)
    try:
        path.lstat()
    except OSError as exc:
        raise ReleaseFlowError(f"workspace does not exist: {path}") from exc
    if not path.is_dir() or path.is_symlink():
        raise ReleaseFlowError("workspace must be a non-symlink directory")
    return path.resolve()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_text(path: Path, value: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_private_tree(path: Path) -> None:
    """Remove a coordinator-owned tree, including read-only Windows files."""
    if path.is_symlink() or not path.is_dir():
        raise ReleaseFlowError(f"managed release path is unsafe: {path.name}")
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_symlink():
            raise ReleaseFlowError(f"managed release tree contains a symlink: {path.name}")
        try:
            child.chmod(0o700 if child.is_dir() else 0o600)
        except OSError:
            pass
    try:
        path.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(path)


def _remove_managed_path(workspace: Path, path: Path) -> None:
    if path.parent != workspace:
        raise ReleaseFlowError("managed release path escaped its workspace")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink():
        raise ReleaseFlowError(f"managed release path is a symlink: {path.name}")
    if path.is_dir():
        _remove_private_tree(path)
        return
    if not path.is_file():
        raise ReleaseFlowError(f"managed release path is unsafe: {path.name}")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    path.unlink()


def _load_local_module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise ReleaseFlowError(f"cannot load release helper: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fragment_value(fragment: Any, name: str) -> Any:
    if isinstance(fragment, dict):
        return fragment.get(name)
    return getattr(fragment, name, None)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _verify_fragment_coverage(
    fragments: list[Any],
    spec: dict[str, Any],
) -> None:
    repository_paths = spec.get("repositories")
    if not isinstance(repository_paths, dict) or set(repository_paths) != REPOSITORIES:
        raise ReleaseFlowError("release spec repositories are invalid")
    prior_path_value = spec.get("prior_release")
    if not isinstance(prior_path_value, str):
        raise ReleaseFlowError("release flow currently requires a prior release")
    prior = _load_json(Path(prior_path_value), "prior release")
    prior_repos = prior.get("repos")
    if not isinstance(prior_repos, dict) or set(prior_repos) != REPOSITORIES:
        raise ReleaseFlowError("prior release repositories are invalid")

    claims: dict[str, set[str]] = {name: set() for name in REPOSITORIES}
    for fragment in fragments:
        rows = _fragment_value(fragment, "repositories")
        if not isinstance(rows, Mapping):
            raise ReleaseFlowError("release-note fragment repositories are invalid")
        for name, commits in rows.items():
            if name not in REPOSITORIES or not isinstance(commits, (list, tuple)):
                raise ReleaseFlowError("release-note fragment repository claims are invalid")
            for commit in commits:
                if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
                    raise ReleaseFlowError("release-note fragment commit is invalid")
                claims[name].add(commit)

    for name in sorted(REPOSITORIES):
        repo_value = repository_paths[name]
        if not isinstance(repo_value, str):
            raise ReleaseFlowError(f"repository path is invalid: {name}")
        repo = Path(repo_value)
        if not repo.is_absolute() or not repo.is_dir() or repo.is_symlink():
            raise ReleaseFlowError(f"repository path is unsafe: {name}")
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        base = prior_repos.get(name)
        if not isinstance(base, str) or not COMMIT_RE.fullmatch(base):
            raise ReleaseFlowError(f"prior release commit is invalid: {name}")
        if _git(repo, "merge-base", "--is-ancestor", base, head, check=False).returncode != 0:
            raise ReleaseFlowError(f"candidate {name} HEAD does not descend from the prior release")
        changed = base != head
        if changed and not claims[name]:
            raise ReleaseFlowError(f"changed repository has no release-note fragment: {name}")
        if not changed and claims[name]:
            raise ReleaseFlowError(f"unchanged repository has release-note commit claims: {name}")
        for commit in claims[name]:
            if commit == base:
                raise ReleaseFlowError(f"release-note commit is not newer than prior {name}: {commit}")
            if _git(repo, "merge-base", "--is-ancestor", base, commit, check=False).returncode != 0 \
                    or _git(repo, "merge-base", "--is-ancestor", commit, head, check=False).returncode != 0:
                raise ReleaseFlowError(
                    f"release-note commit is outside the {name} candidate range: {commit}"
                )


def _candidate_deployment_class(spec: dict[str, Any]) -> str:
    repository_paths = spec.get("repositories")
    prior_value = spec.get("prior_release")
    if not isinstance(repository_paths, dict) or set(repository_paths) != REPOSITORIES \
            or not isinstance(prior_value, str):
        raise ReleaseFlowError("release spec cannot be classified")
    prior_repos = _load_json(Path(prior_value), "prior release").get("repos")
    if not isinstance(prior_repos, dict) or set(prior_repos) != REPOSITORIES:
        raise ReleaseFlowError("prior release repositories are invalid")

    heads: dict[str, str] = {}
    for name in sorted(REPOSITORIES):
        repo = Path(repository_paths[name])
        heads[name] = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if name != "menhir" and heads[name] != prior_repos[name]:
            return "maintenance"

    menhir_base = prior_repos["menhir"]
    menhir_head = heads["menhir"]
    if menhir_base == menhir_head:
        return "maintenance"
    changed = _git(
        Path(repository_paths["menhir"]),
        "diff", "--name-only", "--diff-filter=ACDMRTUXB",
        menhir_base, menhir_head,
    ).stdout.splitlines()
    if not changed or any(
        not path.startswith("src/")
        or any(pattern.search(path) for pattern in APP_ONLY_FORBIDDEN)
        for path in changed
    ):
        return "maintenance"
    return "app-only"


def _deployment_class(fragments: list[Any], spec: dict[str, Any]) -> str:
    classes = [_fragment_value(fragment, "deployment_class") for fragment in fragments]
    if not classes or any(value not in CLASS_ORDER for value in classes):
        raise ReleaseFlowError("release-note fragments do not declare valid deployment classes")
    return max(
        [*classes, _candidate_deployment_class(spec)],
        key=CLASS_ORDER.__getitem__,
    )


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _run_release_author(
    spec_path: Path,
    destination: Path,
    security_review: Path | None = None,
) -> None:
    command = [sys.executable, str(SCRIPT_DIR / "release-author.py"), "--spec", str(spec_path)]
    if security_review is None:
        command.extend(["--review-request", str(destination)])
    else:
        command.extend([
            "--security-review", str(security_review), "--output", str(destination),
        ])
    _run(command)


def _state_path(workspace: Path) -> Path:
    return workspace / STATE_NAME


def _load_state(workspace: Path) -> dict[str, Any]:
    state = _load_json(_state_path(workspace), "release flow state")
    if set(state) != STATE_KEYS or state.get("schema") != SCHEMA or state.get("kind") != KIND:
        raise ReleaseFlowError("release flow state schema is invalid")
    if state.get("phase") not in PHASES:
        raise ReleaseFlowError("release flow phase is invalid")
    if state.get("workspace") != str(workspace):
        raise ReleaseFlowError("release flow state is bound to another workspace")
    if state.get("deployment_class") not in CLASS_ORDER:
        raise ReleaseFlowError("release flow deployment class is invalid")
    if not isinstance(state.get("release_id"), str) \
            or not RELEASE_ID_RE.fullmatch(state["release_id"]):
        raise ReleaseFlowError("release flow release_id is invalid")
    for key in STATE_KEYS:
        if key.endswith("_sha256"):
            value = state.get(key)
            if value is not None and (not isinstance(value, str) or not SHA256_RE.fullmatch(value)):
                raise ReleaseFlowError(f"release flow digest is invalid: {key}")
    return state


def _verify_staged_files(workspace: Path, state: dict[str, Any]) -> None:
    bindings = {
        "spec_sha256": workspace / SPEC_NAME,
        "notes_json_sha256": workspace / NOTES_JSON_NAME,
        "notes_markdown_sha256": workspace / NOTES_MARKDOWN_NAME,
        "review_request_sha256": workspace / REVIEW_REQUEST_NAME,
    }
    if state["phase"] in {"bundled", "deployed"}:
        bindings.update({
            "security_review_sha256": workspace / "security-review.json",
            "release_sha256": workspace / RELEASE_NAME,
            "bundle_manifest_sha256": workspace / BUNDLE_NAME / "bundle-manifest.json",
        })
    for key, path in bindings.items():
        expected = state.get(key)
        if expected is None or _sha256(_regular_file(path, key)) != expected:
            raise ReleaseFlowError(f"staged release artifact changed: {path.name}")
    if state["phase"] in {"bundled", "deployed"}:
        if _tree_sha256(workspace / BUNDLE_NAME) != state.get("bundle_sha256"):
            raise ReleaseFlowError("staged install bundle changed")


def prepare_flow(inputs_path: Path, workspace: Path, fragments_dir: Path) -> dict[str, Any]:
    inputs_path = _regular_file(inputs_path, "release inputs")
    workspace = _workspace(workspace)
    if _state_path(workspace).exists():
        state = status_flow(workspace)
        if state["inputs_sha256"] != _sha256(inputs_path):
            raise ReleaseFlowError("existing release flow is bound to different inputs")
        return state
    if any(workspace.iterdir()):
        raise ReleaseFlowError("release workspace must be empty")
    if not fragments_dir.is_absolute() or not fragments_dir.is_dir() or fragments_dir.is_symlink():
        raise ReleaseFlowError("fragments directory must be an absolute non-symlink directory")

    generated = [
        workspace / SPEC_NAME,
        workspace / "release-spec-inputs",
        workspace / NOTES_MARKDOWN_NAME,
        workspace / NOTES_JSON_NAME,
        workspace / REVIEW_REQUEST_NAME,
    ]
    try:
        release_spec = _load_local_module("menhir_release_spec", "release_spec.py")
        release_notes = _load_local_module("menhir_release_notes", "release_notes.py")
        spec_path = workspace / SPEC_NAME
        release_spec.prepare_release_spec(inputs_path, spec_path)
        spec = _load_json(spec_path, "release spec")
        fragments = list(release_notes.collect_fragments(fragments_dir))
        _verify_fragment_coverage(fragments, spec)

        notes_markdown = release_notes.render_markdown(fragments, spec["release_id"])
        notes_json = release_notes.render_json(fragments, spec["release_id"])
        if not isinstance(notes_markdown, str) or not isinstance(notes_json, str):
            raise ReleaseFlowError("release-note renderers must return text")
        _atomic_text(workspace / NOTES_MARKDOWN_NAME, notes_markdown)
        _atomic_text(workspace / NOTES_JSON_NAME, notes_json)

        review_request = workspace / REVIEW_REQUEST_NAME
        _run_release_author(spec_path, review_request)
        request = _load_json(review_request, "security review request")
        release = request.get("release")
        if not isinstance(release, dict):
            raise ReleaseFlowError("security review request has no release authority")

        state = {
            "schema": SCHEMA,
            "kind": KIND,
            "phase": "review_requested",
            "release_id": release.get("release_id"),
            "release_author": release.get("release_author"),
            "workspace": str(workspace),
            "deployment_class": _deployment_class(fragments, spec),
            "inputs_sha256": _sha256(inputs_path),
            "spec_sha256": _sha256(spec_path),
            "notes_json_sha256": _sha256(workspace / NOTES_JSON_NAME),
            "notes_markdown_sha256": _sha256(workspace / NOTES_MARKDOWN_NAME),
            "review_request_sha256": _sha256(review_request),
            "security_review_sha256": None,
            "release_sha256": None,
            "bundle_manifest_sha256": None,
            "bundle_sha256": None,
        }
        if not isinstance(state["release_id"], str) or not RELEASE_ID_RE.fullmatch(state["release_id"]):
            raise ReleaseFlowError("authored review request release_id is invalid")
        _atomic_json(_state_path(workspace), state)
        return state
    except Exception:
        if not _state_path(workspace).exists():
            for path in reversed(generated):
                _remove_managed_path(workspace, path)
        raise


def finalize_flow(workspace: Path, security_review: Path) -> dict[str, Any]:
    workspace = _workspace(workspace)
    state = _load_state(workspace)
    security_review = _regular_file(security_review, "security review")
    if state["phase"] in {"bundled", "deployed"}:
        _verify_staged_files(workspace, state)
        if state["security_review_sha256"] != _sha256(security_review):
            raise ReleaseFlowError("existing release flow is bound to a different security review")
        return state
    if state["phase"] != "review_requested":
        raise ReleaseFlowError("only a review-requested release can be finalized")
    _verify_staged_files(workspace, state)
    review_copy = workspace / "security-review.json"
    release_path = workspace / RELEASE_NAME
    bundle_path = workspace / BUNDLE_NAME
    for path in (review_copy, release_path, bundle_path):
        _remove_managed_path(workspace, path)
    stage = Path(tempfile.mkdtemp(prefix=".release-finalize.", dir=workspace))
    staged_review = stage / review_copy.name
    staged_release = stage / release_path.name
    staged_bundle = stage / bundle_path.name
    try:
        shutil.copyfile(security_review, staged_review)
        staged_review.chmod(0o400)
        _run_release_author(workspace / SPEC_NAME, staged_release, staged_review)
        bundle_builder = _load_local_module(
            "menhir_build_install_bundle", "build_install_bundle.py"
        )
        bundle_builder.build_install_bundle(
            staged_release,
            workspace / SPEC_NAME,
            staged_bundle,
        )
        manifest = _load_json(staged_bundle / "bundle-manifest.json", "bundle manifest")
        if manifest.get("release_id") != state["release_id"]:
            raise ReleaseFlowError("install bundle release_id mismatch")
        os.replace(staged_review, review_copy)
        os.replace(staged_release, release_path)
        os.replace(staged_bundle, bundle_path)
    finally:
        if stage.exists():
            _remove_private_tree(stage)

    state.update({
        "phase": "bundled",
        "security_review_sha256": _sha256(review_copy),
        "release_sha256": _sha256(release_path),
        "bundle_manifest_sha256": _sha256(bundle_path / "bundle-manifest.json"),
        "bundle_sha256": _tree_sha256(bundle_path),
    })
    _atomic_json(_state_path(workspace), state)
    return state


def deployment_command(
    workspace: Path,
    state: dict[str, Any],
) -> list[str]:
    wrapper = _regular_file(DEFAULT_WRAPPER, "deployment wrapper")
    mode = "AppOnly" if state["deployment_class"] == "app-only" else "Maintenance"
    return [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(wrapper), "-Mode", mode,
        "-BundlePath", str(workspace / BUNDLE_NAME),
        "-ExpectedBundleSha256", state["bundle_sha256"],
        "-Release", state["release_id"],
        "-SourceRepository", str(SCRIPT_DIR.parent),
    ]


def deploy_flow(
    workspace: Path,
    confirmation: str,
    *,
    execute: bool,
    runner: Callable[[list[str]], None] = _run,
) -> dict[str, Any] | list[str]:
    workspace = _workspace(workspace)
    state = _load_state(workspace)
    if state["phase"] == "deployed":
        _verify_staged_files(workspace, state)
        if confirmation != state["release_id"]:
            raise ReleaseFlowError("deployment confirmation must exactly match the release_id")
        return state
    if state["phase"] != "bundled":
        raise ReleaseFlowError("only a bundled release can be deployed")
    _verify_staged_files(workspace, state)
    if confirmation != state["release_id"]:
        raise ReleaseFlowError("deployment confirmation must exactly match the release_id")
    command = deployment_command(workspace, state)
    if not execute:
        return command
    runner(command)
    state["phase"] = "deployed"
    _atomic_json(_state_path(workspace), state)
    return state


def status_flow(workspace: Path) -> dict[str, Any]:
    workspace = _workspace(workspace)
    state = _load_state(workspace)
    _verify_staged_files(workspace, state)
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--inputs", type=Path, required=True)
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument(
        "--fragments",
        type=Path,
        default=SCRIPT_DIR / "changes" / "unreleased",
    )
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--workspace", type=Path, required=True)
    finalize.add_argument("--security-review", type=Path, required=True)
    deploy = commands.add_parser("deploy")
    deploy.add_argument("--workspace", type=Path, required=True)
    deploy.add_argument("--confirm-release-id", required=True)
    deploy.add_argument("--execute", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--workspace", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result: Any = prepare_flow(args.inputs, args.workspace, args.fragments)
        elif args.command == "finalize":
            result = finalize_flow(args.workspace, args.security_review)
        elif args.command == "deploy":
            result = deploy_flow(
                args.workspace,
                args.confirm_release_id,
                execute=args.execute,
            )
        else:
            result = status_flow(args.workspace)
    except (OSError, subprocess.CalledProcessError, ReleaseFlowError, ValueError) as exc:
        print(f"release flow failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
