#!/usr/bin/env python3
"""Coordinate a Menhir release without bypassing review or deployment approval."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    # The sibling parts package lives beside this script; make it importable even
    # when this module is loaded by path (importlib spec) rather than as a script.
    sys.path.append(str(_SCRIPT_DIR))

from release_flow_parts import (  # noqa: E402
    APP_ONLY_SOURCE_PATHS as APP_ONLY_SOURCE_PATHS,
    BUNDLE_NAME as BUNDLE_NAME,
    CLASS_ORDER as CLASS_ORDER,
    COMMIT_RE as COMMIT_RE,
    FROZEN_ARTIFACT_KEYS as FROZEN_ARTIFACT_KEYS,
    KIND as KIND,
    LEGACY_STATE_KEYS as LEGACY_STATE_KEYS,
    MAINTENANCE_PATHS as MAINTENANCE_PATHS,
    NOTES_JSON_NAME as NOTES_JSON_NAME,
    NOTES_MARKDOWN_NAME as NOTES_MARKDOWN_NAME,
    PHASES as PHASES,
    PRE_INGRESS_STATE_KEYS as PRE_INGRESS_STATE_KEYS,
    PUBLICATION_KIND as PUBLICATION_KIND,
    PUBLICATION_NONCE_RE as PUBLICATION_NONCE_RE,
    PUBLICATION_RECEIPT_NAME as PUBLICATION_RECEIPT_NAME,
    RELEASE_AUTHORITY_BINDINGS as RELEASE_AUTHORITY_BINDINGS,
    RELEASE_ID_PARTS_RE as RELEASE_ID_PARTS_RE,
    RELEASE_ID_RE as RELEASE_ID_RE,
    RELEASE_NAME as RELEASE_NAME,
    REPOSITORIES as REPOSITORIES,
    REVIEW_REQUEST_NAME as REVIEW_REQUEST_NAME,
    SCHEMA as SCHEMA,
    SECURITY_CONFIG_PATHS as SECURITY_CONFIG_PATHS,
    SHA256_RE as SHA256_RE,
    SPEC_NAME as SPEC_NAME,
    STATE_KEYS as STATE_KEYS,
    STATE_NAME as STATE_NAME,
    VERSION_RE as VERSION_RE,
    ReleaseFlowError as ReleaseFlowError,
    _atomic_json as _atomic_json,
    _atomic_text as _atomic_text,
    _candidate_deployment_class as _candidate_deployment_class,
    _deployment_class as _deployment_class,
    _fragment_value as _fragment_value,
    _git as _git,
    _json_sha256 as _json_sha256,
    _json_text as _json_text,
    _load_json as _load_json,
    _load_state as _load_state,
    _publication_paths as _publication_paths,
    _publication_receipt as _publication_receipt,
    _regular_directory as _regular_directory,
    _regular_file as _regular_file,
    _remove_managed_path as _remove_managed_path,
    _remove_private_tree as _remove_private_tree,
    _sha256 as _sha256,
    _snapshot_fragments as _snapshot_fragments,
    _state_path as _state_path,
    _tree_sha256 as _tree_sha256,
    _unique_pairs as _unique_pairs,
    _validate_fragment_bindings as _validate_fragment_bindings,
    _verify_archive as _verify_archive,
    _verify_fragment_coverage as _verify_fragment_coverage,
    _verify_next_release_id as _verify_next_release_id,
    _verify_publication as _verify_publication,
    _verify_release_authority_bindings as _verify_release_authority_bindings,
    _verify_staged_files as _verify_staged_files,
    _workspace as _workspace,
    next_release_id as next_release_id,
    publish_flow as publish_flow,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_WRAPPER = SCRIPT_DIR.parents[3] / "scripts" / "deploy-menhir.ps1"


def _load_local_module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise ReleaseFlowError(f"cannot load release helper: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
        _verify_next_release_id(spec)
        fragment_bindings = _snapshot_fragments(fragments_dir)
        fragments = list(release_notes.collect_fragments(fragments_dir))
        if len(fragment_bindings) != len(fragments):
            raise ReleaseFlowError("prepared fragment set changed while it was collected")
        _verify_fragment_coverage(fragments, spec)
        deployment_class = _deployment_class(fragments, spec)

        notes_markdown = release_notes.render_markdown(fragments, spec["release_id"])
        notes_json = release_notes.render_json(fragments, spec["release_id"])
        if not isinstance(notes_markdown, str) or not isinstance(notes_json, str):
            raise ReleaseFlowError("release-note renderers must return text")
        _atomic_text(workspace / NOTES_MARKDOWN_NAME, notes_markdown)
        _atomic_text(workspace / NOTES_JSON_NAME, notes_json)
        notes_json_sha256 = _sha256(workspace / NOTES_JSON_NAME)
        notes_markdown_sha256 = _sha256(workspace / NOTES_MARKDOWN_NAME)
        if _snapshot_fragments(fragments_dir) != fragment_bindings:
            raise ReleaseFlowError("prepared fragment set changed while release notes were rendered")

        if spec.get("ingress_mode") != "cloudflared":
            raise ReleaseFlowError("generated release spec must declare Cloudflared ingress")
        for key in RELEASE_AUTHORITY_BINDINGS:
            if key == "ingress_mode":
                continue
            if key in spec:
                raise ReleaseFlowError(f"generated release spec unexpectedly supplies {key}")
        spec.update({
            "deployment_class": deployment_class,
            "ingress_mode": spec.get("ingress_mode"),
            "notes_json_sha256": notes_json_sha256,
            "notes_markdown_sha256": notes_markdown_sha256,
        })
        _atomic_json(spec_path, spec)

        review_request = workspace / REVIEW_REQUEST_NAME
        _run_release_author(spec_path, review_request)
        request = _load_json(review_request, "security review request")
        release = request.get("release")
        if not isinstance(release, dict):
            raise ReleaseFlowError("security review request has no release authority")
        for key in RELEASE_AUTHORITY_BINDINGS:
            if release.get(key) != spec[key]:
                raise ReleaseFlowError(
                    f"authored review request does not bind {key}"
                )
        if _deployment_class(fragments, spec) != deployment_class:
            raise ReleaseFlowError("deployment class changed while release authority was authored")

        state = {
            "schema": SCHEMA,
            "kind": KIND,
            "phase": "review_requested",
            "release_id": release.get("release_id"),
            "release_author": release.get("release_author"),
            "workspace": str(workspace),
            "deployment_class": deployment_class,
            "ingress_mode": spec["ingress_mode"],
            "inputs_sha256": _sha256(inputs_path),
            "spec_sha256": _sha256(spec_path),
            "notes_json_sha256": notes_json_sha256,
            "notes_markdown_sha256": notes_markdown_sha256,
            "review_request_sha256": _sha256(review_request),
            "security_review_sha256": None,
            "release_sha256": None,
            "bundle_manifest_sha256": None,
            "bundle_sha256": None,
            "fragments_dir": str(fragments_dir.resolve()),
            "fragments": fragment_bindings,
            "publication_nonce": None,
            "publication_receipt_sha256": None,
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
    if state["phase"] in {"bundled", "publishing", "published", "deployed"}:
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
        if set(state) == STATE_KEYS:
            authored_release = _load_json(staged_release, "release authority")
            for key in RELEASE_AUTHORITY_BINDINGS:
                if authored_release.get(key) != state[key]:
                    raise ReleaseFlowError(
                        f"final release authority does not bind {key}"
                    )
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
    mode = {
        "app-only": "AppOnly",
        "security-config": "SecurityConfig",
        "maintenance": "Maintenance",
    }[state["deployment_class"]]
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
    # A published release is a bundled release whose notes have also been
    # archived; the bundle is unchanged by publication. Before this, publish
    # advanced the phase past the only value deploy accepted, so a release
    # could be prepared, finalized and published but never deployed.
    if state["phase"] not in {"bundled", "published"}:
        raise ReleaseFlowError("only a bundled or published release can be deployed")
    _verify_staged_files(workspace, state)
    if confirmation != state["release_id"]:
        raise ReleaseFlowError("deployment confirmation must exactly match the release_id")
    command = deployment_command(workspace, state)
    if not execute:
        return command
    raise ReleaseFlowError(
        "direct product-release deployment is disabled; select this release with "
        "deploy/personal_deploy.py, complete staging, record one receipt-bound "
        "approval, and promote it from that separate workflow"
    )


def status_flow(workspace: Path) -> dict[str, Any]:
    workspace = _workspace(workspace)
    state = _load_state(workspace)
    _verify_staged_files(workspace, state)
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    next_id = commands.add_parser("next-id")
    next_id.add_argument("--prior-release", type=Path, required=True)
    next_id.add_argument("--version")
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
    publish = commands.add_parser("publish")
    publish.add_argument("--workspace", type=Path, required=True)
    publish.add_argument("--confirm-release-id", required=True)
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
        if args.command == "next-id":
            result = next_release_id(args.prior_release, args.version)
        elif args.command == "prepare":
            result: Any = prepare_flow(args.inputs, args.workspace, args.fragments)
        elif args.command == "finalize":
            result = finalize_flow(args.workspace, args.security_review)
        elif args.command == "publish":
            result = publish_flow(args.workspace, args.confirm_release_id)
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
