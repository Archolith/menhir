"""E2E-20 - checkout config and upgrade (#168 final-gate item 60).

Three questions, all about configuration a user gets without touching a flag:

  - ``menhir setup --repo <checkout>`` copies ``.env.example`` to ``.env`` byte-for-byte and
    leaves every feature-flag line commented;
  - the defaults ``.env.example`` documents are the defaults the code has;
  - a checkout ``.env`` and a wheel state-dir ``.env`` resolve to the same effective settings
    for every flag neither file sets explicitly, and both equal the code defaults.

The checkout is a minimal COPY (pyproject.toml and .env.example) under ``tmp_path``; nothing is
ever written into the real repository.

Pending, deliberately not a criterion: the upgrade check (install a previous published wheel,
then the candidate over it, and compare ``.env`` handling). It waits on an owner decision (D3).

No graph, backend or provider is used; the fresh-install child environment still pins the
disposable graph as process env so nothing can reach a production database.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from menhir.config.feature_flags import FEATURES
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.fresh_install import (
    code_defaults,
    effective_settings,
    flag_lines,
    make_fresh_home,
    run_cli,
)
from tests.e2e._harness.phase3 import close_pass
from tests.e2e._harness.stack import REPO_ROOT

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]

CRITERIA = [
    "checkout_setup_copies_template",
    "template_documents_code_defaults",
    "checkout_and_wheel_effective_settings_agree",
]

_DOC_LINE = re.compile(r"^\s*#\s*([A-Z][A-Z0-9_]*)=(.*)$")
_INLINE_COMMENT = re.compile(r"\s+#")
_ACTIVE_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _active_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.lstrip().startswith("#"):
            continue
        match = _ACTIVE_LINE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def _documented_value(raw: str) -> str:
    value = _INLINE_COMMENT.split(raw, maxsplit=1)[0].strip()
    return value.strip("\"'")


def test_e2e_20_checkout_config(
    e2e_config: E2EConfig,
    e2e_installed,
    lane_evidence: LaneEvidence,
    tmp_path: Path,
) -> None:
    lane_evidence.record_stack(**e2e_installed.as_evidence())

    template = REPO_ROOT / ".env.example"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shutil.copyfile(REPO_ROOT / "pyproject.toml", checkout / "pyproject.toml")
    shutil.copyfile(template, checkout / ".env.example")
    home = make_fresh_home(tmp_path / "fresh")

    # 1. setup --repo copies the template and leaves every flag commented.
    setup = run_cli(e2e_config, e2e_installed, home, "setup", "--repo", str(checkout), "--no-git-hooks")
    lane_evidence.attach("checkout-setup.stdout.txt", setup.stdout)
    lane_evidence.attach("checkout-setup.stderr.txt", setup.stderr)
    checkout_env = checkout / ".env"
    env_exists = checkout_env.is_file()
    same_bytes = env_exists and checkout_env.read_bytes() == template.read_bytes()
    active = (
        [line for line in flag_lines(checkout_env) if not line.lstrip().startswith("#")]
        if env_exists
        else []
    )
    lane_evidence.record(
        "checkout_setup_copies_template",
        passed=setup.returncode == 0 and env_exists and same_bytes and not active,
        detail={
            "returncode": setup.returncode,
            "env_exists": env_exists,
            "bytes_equal_template": same_bytes,
            "active_flag_lines": active,
        },
    )

    # 2. the template documents the defaults the code has.
    by_env = {flag.env_var: flag for flag in FEATURES.values()}
    mismatches: list[dict] = []
    examples: list[dict] = []
    checked: set[str] = set()
    mentioned: set[str] = set()
    for line in template.read_text(encoding="utf-8").split("\n"):
        match = _DOC_LINE.match(line)
        if not match:
            continue
        key = match.group(1)
        flag = by_env.get(key)
        if flag is None:
            continue
        mentioned.add(key)
        default = flag.default
        if not isinstance(default, (bool, str)):
            continue
        value = _documented_value(match.group(2))
        if default == "" and value:
            # Unset by default; a commented value here is a fill-in example, not a default claim.
            examples.append({"env_var": key, "example": value})
            continue
        checked.add(key)
        if isinstance(default, bool):
            allowed = _TRUE if default else _FALSE
            ok = value.lower() in allowed
        else:
            ok = value == default
        if not ok:
            mismatches.append({"env_var": key, "documented": value, "code_default": default})
    unmentioned = sorted(
        key
        for key, flag in by_env.items()
        if isinstance(flag.default, (bool, str)) and key not in mentioned
    )
    lane_evidence.record(
        "template_documents_code_defaults",
        passed=not mismatches,
        detail={
            "mismatches": mismatches,
            "flags_checked": len(checked),
            "examples_for_unset_defaults": examples,
            "bool_str_flags_not_in_template": unmentioned,
        },
    )

    # 3. checkout .env and wheel state-dir .env resolve to the same effective settings.
    wheel_setup = run_cli(e2e_config, e2e_installed, home, "setup", "--provider", "local")
    lane_evidence.attach("wheel-setup.stdout.txt", wheel_setup.stdout)
    lane_evidence.attach("wheel-setup.stderr.txt", wheel_setup.stderr)
    differing: list[dict] = []
    detail: dict = {"wheel_setup_returncode": wheel_setup.returncode}
    passed = wheel_setup.returncode == 0
    if passed:
        checkout_values = effective_settings(e2e_config, e2e_installed, home, cwd=checkout)["values"]
        wheel_values = effective_settings(e2e_config, e2e_installed, home, cwd=home.other_cwd)["values"]
        explicit = _active_keys(home.env_path) if home.env_path.is_file() else set()
        if env_exists:
            explicit |= _active_keys(checkout_env)
        defaults = code_defaults()
        keys = (set(checkout_values) | set(wheel_values)) - explicit
        lane_evidence.attach(
            "effective-settings.json",
            json.dumps({"checkout": checkout_values, "wheel": wheel_values}, indent=2, default=str),
        )
        for key in sorted(keys):
            left = checkout_values.get(key)
            right = wheel_values.get(key)
            expected = defaults.get(key)
            if left != right or left != expected:
                differing.append(
                    {"env_var": key, "checkout": left, "wheel": right, "code_default": expected}
                )
        detail.update(keys_compared=len(keys), explicit_keys_dropped=sorted(explicit))
    detail["differing"] = differing
    lane_evidence.record(
        "checkout_and_wheel_effective_settings_agree",
        passed=passed and not differing,
        detail=detail,
    )

    close_pass(lane_evidence, CRITERIA)
