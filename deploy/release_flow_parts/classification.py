"""Release-note coverage checks and deployment-class classification."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import (
    APP_ONLY_SOURCE_PATHS,
    CLASS_ORDER,
    COMMIT_RE,
    REPOSITORIES,
    SECURITY_CONFIG_PATHS,
    ReleaseFlowError,
)
from .fragments import _fragment_value
from .fsio import _load_json, _sha256


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
    # Prior records up to 0.2.0-16 also pin yawn_vps; a superset is accepted.
    if not isinstance(prior_repos, dict) or not REPOSITORIES <= set(prior_repos):
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
    prior_release = _load_json(Path(prior_value), "prior release")
    prior_repos = prior_release.get("repos")
    if not isinstance(prior_repos, dict) or not REPOSITORIES <= set(prior_repos):
        raise ReleaseFlowError("prior release repositories are invalid")

    current_secrets = spec.get("secret_version_ids")
    prior_secrets = prior_release.get("secret_version_ids")
    if isinstance(current_secrets, dict) and isinstance(prior_secrets, dict):
        if any(
            name != "client-policy" and current_secrets.get(name) != prior_secrets.get(name)
            for name in set(current_secrets) | set(prior_secrets)
        ):
            return "maintenance"
    rendered = spec.get("rendered")
    prior_rendered = prior_release.get("rendered")
    changed_operations = False
    if isinstance(rendered, dict) and isinstance(prior_rendered, dict):
        oauth_public = rendered.get("oauth_public_key_sha256")
        if isinstance(oauth_public, str) \
                and _sha256(Path(oauth_public)) != prior_rendered.get("oauth_public_key_sha256"):
            return "maintenance"
        operations = rendered.get("operations_policy_sha256")
        changed_operations = isinstance(operations, str) and (
            _sha256(Path(operations)) != prior_rendered.get("operations_policy_sha256")
        )

    heads: dict[str, str] = {}
    changed_oauth = False
    for name in sorted(REPOSITORIES):
        repo = Path(repository_paths[name])
        heads[name] = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if name == "yawn_deploy" and heads[name] != prior_repos[name]:
            return "maintenance"
        if name == "archolith_oauth" and heads[name] != prior_repos[name]:
            changed_oauth = True

    menhir_base = prior_repos["menhir"]
    menhir_head = heads["menhir"]
    changed_client_policy = isinstance(current_secrets, dict) \
        and isinstance(prior_secrets, dict) \
        and current_secrets.get("client-policy") != prior_secrets.get("client-policy")
    if menhir_base == menhir_head:
        if changed_oauth:
            return "maintenance"
        return "security-config" if changed_operations or changed_client_policy else "maintenance"
    changed = _git(
        Path(repository_paths["menhir"]),
        "diff", "--name-only", "--diff-filter=ACDMRTUXB",
        menhir_base, menhir_head,
    ).stdout.splitlines()
    if not changed or changed_oauth:
        return "maintenance"
    if all(any(pattern.search(path) for pattern in APP_ONLY_SOURCE_PATHS) for path in changed):
        return "app-only"
    if all(any(pattern.search(path) for pattern in SECURITY_CONFIG_PATHS) for path in changed):
        return "security-config"
    return "maintenance"


def _deployment_class(fragments: list[Any], spec: dict[str, Any]) -> str:
    classes = [_fragment_value(fragment, "deployment_class") for fragment in fragments]
    if not classes or any(value not in CLASS_ORDER for value in classes):
        raise ReleaseFlowError("release-note fragments do not declare valid deployment classes")
    return max(
        [*classes, _candidate_deployment_class(spec)],
        key=CLASS_ORDER.__getitem__,
    )
