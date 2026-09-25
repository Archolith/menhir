"""Command-line surface for the Menhir host scaffold verifier."""

from __future__ import annotations

import argparse
from pathlib import Path

from menhir_scaffold_constants import CONTRACT_PATH, RECEIPT_PATH
from menhir_scaffold_maintenance import maintenance_binding


def add_maintenance_binding_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--release-id", required=True)
    command.add_argument("--release-manifest-sha256", required=True)
    command.add_argument("--runner-sha256", required=True)
    command.add_argument("--approval-sha256", required=True)
    command.add_argument("--promotion-attempt-id", required=True)
    command.add_argument("--approved-utc", required=True)
    command.add_argument("--promotion-started-utc", required=True)


def binding_from_arguments(args: argparse.Namespace) -> dict[str, str]:
    return maintenance_binding(
        args.release_id,
        args.release_manifest_sha256,
        args.runner_sha256,
        args.approval_sha256,
        args.promotion_attempt_id,
        args.approved_utc,
        args.promotion_started_utc,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    result.add_argument("--receipt", type=Path, default=RECEIPT_PATH)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("capture")
    verify = commands.add_parser("verify")
    verify.add_argument("--app-only", action="store_true")
    commands.add_parser("status")
    commands.add_parser("seed-drill")
    abandon = commands.add_parser("abandon-maintenance")
    abandon.add_argument("--reason", required=True)
    for name in (
        "begin-maintenance", "hold-maintenance", "assert-maintenance",
        "complete-maintenance",
    ):
        add_maintenance_binding_arguments(commands.add_parser(name))
    commands.choices["complete-maintenance"].add_argument(
        "--artifact-only", action="store_true",
        help="close a maintenance that installed artifacts without an image cutover; "
             "proven against the live authority, verifier, runtime and endpoint",
    )
    return result
