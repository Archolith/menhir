"""Command-line surface for the app-only deployment runner."""

from __future__ import annotations

import argparse


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("bundle_id")
    deploy_command = commands.add_parser("deploy")
    deploy_command.add_argument("bundle_id")
    deploy_command.add_argument("expected_runner_sha256")
    deploy_command.add_argument("expected_release_sha256")
    deploy_command.add_argument("expected_ingress_container_id")
    for name in (
        "expected_bundle_sha256", "expected_staging_receipt_sha256",
        "expected_approval_sha256", "promotion_attempt_id", "approved_utc",
        "promotion_started_utc",
    ):
        deploy_command.add_argument(name)
    adopt_command = commands.add_parser("adopt")
    for name in (
        "expected_runner_sha256", "expected_release_sha256",
        "expected_ingress_container_id", "expected_bundle_sha256",
        "expected_staging_receipt_sha256", "expected_approval_sha256",
        "promotion_attempt_id", "approved_utc", "promotion_started_utc",
    ):
        adopt_command.add_argument(name)
    commands.add_parser("recover")
    commands.add_parser("live")
    commands.add_parser("check")
    commands.add_parser("accept-current")
    commands.add_parser("receipt")
    return result
