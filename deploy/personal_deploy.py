#!/usr/bin/env python3
"""Coordinate Menhir's personal stage, approval, and promotion workflow.

This coordinator consumes a finalized product-release workspace but never builds
or republishes it.  A separate staging runner must prove the exact immutable
bundle in disposable infrastructure and emit the strict receipt validated here.
Only one explicit, receipt-bound approval can unlock the production runner.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    # The sibling parts package lives beside this script; make it importable even
    # when this module is loaded by path (importlib spec) rather than as a script.
    sys.path.append(str(_SCRIPT_DIR))

from personal_deploy_parts import (  # noqa: E402
    APPROVAL_KEYS as APPROVAL_KEYS,
    APPROVAL_NAME as APPROVAL_NAME,
    ATTEMPT_RE as ATTEMPT_RE,
    BUNDLE_NAME as BUNDLE_NAME,
    DEFAULT_PROMOTION_WRAPPER as DEFAULT_PROMOTION_WRAPPER,
    DEPLOYMENT_CLASSES as DEPLOYMENT_CLASSES,
    IMAGE_RE as IMAGE_RE,
    KIND as KIND,
    MAX_STAGING_AGE as MAX_STAGING_AGE,
    OPERATOR_ROOT as OPERATOR_ROOT,
    PHASES as PHASES,
    POWERSHELL as POWERSHELL,
    PREFLIGHT_CHECK_KEYS as PREFLIGHT_CHECK_KEYS,
    PREFLIGHT_KEYS as PREFLIGHT_KEYS,
    PROMOTION_KEYS as PROMOTION_KEYS,
    PROMOTION_RECEIPT_NAME as PROMOTION_RECEIPT_NAME,
    RELEASE_ID_RE as RELEASE_ID_RE,
    RELEASE_NAME as RELEASE_NAME,
    RELEASE_STATE_NAME as RELEASE_STATE_NAME,
    ROOT_TRANSACTION_RECEIPT_NAME as ROOT_TRANSACTION_RECEIPT_NAME,
    SCHEMA as SCHEMA,
    SCRIPT_DIR as SCRIPT_DIR,
    SHA256_RE as SHA256_RE,
    STAGING_CHECKS as STAGING_CHECKS,
    STAGING_KEYS as STAGING_KEYS,
    STAGING_RECEIPT_NAME as STAGING_RECEIPT_NAME,
    STATE_KEYS as STATE_KEYS,
    STATE_NAME as STATE_NAME,
    PersonalDeployError as PersonalDeployError,
    _atomic_json as _atomic_json,
    _composite_sha256 as _composite_sha256,
    _directory as _directory,
    _exact_keys as _exact_keys,
    _load_json as _load_json,
    _load_state as _load_state,
    _now as _now,
    _operator_wrapper as _operator_wrapper,
    _promotion_command as _promotion_command,
    _regular_file as _regular_file,
    _release_binding as _release_binding,
    _root_runner_sha256 as _root_runner_sha256,
    _runner as _runner,
    _runner_sha256 as _runner_sha256,
    _sha256 as _sha256,
    _stage_command as _stage_command,
    _state_path as _state_path,
    _validate_promotion_receipt as _validate_promotion_receipt,
    _validate_staging_receipt as _validate_staging_receipt,
    _verify_approval as _verify_approval,
    _verify_publication as _verify_publication,
    _verify_staging as _verify_staging,
    _tree_sha256 as _tree_sha256,
    _unique_pairs as _unique_pairs,
    _utc as _utc,
    approve_flow as approve_flow,
    promote_flow as promote_flow,
    rehearse_flow as rehearse_flow,
    select_flow as select_flow,
    stage_flow as stage_flow,
    status_flow as status_flow,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("--release-workspace", type=Path, required=True)
    select.add_argument("--workspace", type=Path, required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--workspace", type=Path, required=True)
    stage.add_argument("--runner", type=Path, required=True)
    stage.add_argument("--execute", action="store_true")
    rehearse = commands.add_parser("rehearse")
    rehearse.add_argument("--release-workspace", type=Path, required=True)
    rehearse.add_argument("--workspace", type=Path, required=True)
    rehearse.add_argument("--runner", type=Path, required=True)
    rehearse.add_argument("--execute", action="store_true")
    approve = commands.add_parser("approve")
    approve.add_argument("--workspace", type=Path, required=True)
    approve.add_argument("--confirm-release-id")
    approve.add_argument("--confirm-staging-sha256")
    approve.add_argument("--approved-by")
    promote = commands.add_parser("promote")
    promote.add_argument("--workspace", type=Path, required=True)
    promote.add_argument("--confirm-release-id")
    promote.add_argument("--confirm-staging-sha256")
    promote.add_argument("--execute", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--workspace", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "select":
            result: Any = select_flow(args.release_workspace, args.workspace)
        elif args.command == "stage":
            result = stage_flow(args.workspace, args.runner, execute=args.execute)
        elif args.command == "rehearse":
            result = rehearse_flow(
                args.release_workspace,
                args.workspace,
                args.runner,
                execute=args.execute,
            )
        elif args.command == "approve":
            approved_by = args.approved_by
            if approved_by is None and args.confirm_release_id is not None \
                    and args.confirm_staging_sha256 is not None:
                # Preserve the legacy CLI's environment-derived identity only
                # when its two formerly-required confirmations are supplied.
                approved_by = os.environ.get("USERNAME") or os.environ.get("USER")
            result = approve_flow(
                args.workspace,
                args.confirm_release_id,
                args.confirm_staging_sha256,
                approved_by,
            )
        elif args.command == "promote":
            result = promote_flow(
                args.workspace,
                args.confirm_release_id,
                args.confirm_staging_sha256,
                execute=args.execute,
            )
        else:
            result = status_flow(args.workspace)
    except (OSError, subprocess.CalledProcessError, PersonalDeployError, ValueError) as exc:
        print(f"personal deployment failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
