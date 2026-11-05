"""E2E-20 - checkout config and upgrade (#168 final-gate item 60).

Four questions, all about configuration a user gets without touching a flag:

  - ``menhir setup --repo <checkout>`` copies ``.env.example`` to ``.env`` byte-for-byte and
    leaves every feature-flag line commented;
  - the defaults ``.env.example`` documents are the defaults the code has;
  - a checkout ``.env`` and a wheel state-dir ``.env`` resolve to the same effective settings
    for every flag neither file sets explicitly, and both equal the code defaults;
  - upgrading from the v0.2.3 release wheel (D3) leaves the ``.env`` byte-identical through a
    setup rerun except blank local URL/model keys, which setup fills with the code default
    (acab90a); flags it pins keep their values and every other flag takes the candidate's
    code default, read from the state dir in another cwd.

The checkout is a minimal COPY (pyproject.toml and .env.example) under ``tmp_path``; nothing is
ever written into the real repository. The upgrade downloads the release wheel (public, digest
pinned) and installs it into its own venv.

No graph, backend or provider is used; the fresh-install child environment still pins the
disposable graph as process env so nothing can reach a production database.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from menhir.config.feature_flags import FEATURES
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.fresh_install import (
    EFFECTIVE_SETTINGS_CODE,
    code_defaults,
    edit_env,
    effective_settings,
    flag_lines,
    fresh_install_environment,
    make_fresh_home,
    run_cli,
)
from tests.e2e._harness.phase3 import close_pass
from tests.e2e._harness.stack import REPO_ROOT

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400)]

CRITERIA = [
    "checkout_setup_copies_template",
    "template_documents_code_defaults",
    "checkout_and_wheel_effective_settings_agree",
    "upgrade_keeps_file_and_takes_new_defaults",
]

# Upgrade source (owner decision D3): the v0.2.3 GitHub release wheel. The digest is the one
# published in that release's SHA256SUMS; a different download fails the criterion.
UPGRADE_FROM_URL = (
    "https://github.com/Archolith/menhir/releases/download/v0.2.3/archolith_menhir-0.2.3-py3-none-any.whl"
)
UPGRADE_FROM_SHA256 = "9fec32619329a7f4dd2c65a8c1ea028319632d4e9953df27af3d9cc8a1a44461"
UPGRADE_PINNED = {
    "MENHIR_CANONICAL_SELF_BINDING_MODE": "off",
}

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

    # 4. upgrade from the previous release keeps the file and takes the new defaults.
    upgrade_home = make_fresh_home(tmp_path / "upgrade")
    passed, detail = _upgrade_check(e2e_config, e2e_installed, upgrade_home, tmp_path, lane_evidence)
    lane_evidence.record("upgrade_keeps_file_and_takes_new_defaults", passed=passed, detail=detail)

    close_pass(lane_evidence, CRITERIA)


_SECRET_KEY = re.compile(r"KEY|PASSWORD|SECRET|TOKEN")


def _classify_upgrade_changes(before: bytes, after: bytes) -> tuple[list[str], bool]:
    """Split a setup rerun's edits into allowed blank-default fills and anything else.

    `setup --provider local` fills a BLANK local URL/model with the code default and never
    replaces a value (acab90a). Every other byte must survive the upgrade unchanged.
    """

    from menhir.config import MemorySettings

    fillable = {
        "LOCAL_LLM_BASE_URL": MemorySettings.local_llm_base_url,
        "LOCAL_LLM_CHAT_MODEL": MemorySettings.local_llm_chat_model,
    }
    old = before.decode("utf-8").split("\n")
    new = after.decode("utf-8").split("\n")
    if len(old) != len(new):
        return [], True
    filled: list[str] = []
    for old_line, new_line in zip(old, new):
        if old_line == new_line:
            continue
        key = old_line.split("=", 1)[0]
        if key in fillable and old_line == f"{key}=" and new_line == f"{key}={fillable[key]}":
            filled.append(key)
            continue
        return filled, True
    return filled, False


def _masked_diff(before: bytes, after: bytes) -> list[str]:
    """Changed lines between two env files, values of secret-looking keys masked."""

    def mask(line: str) -> str:
        key = line.lstrip("#").strip().split("=", 1)[0]
        if "=" in line and _SECRET_KEY.search(key):
            return line.split("=", 1)[0] + "=<masked>"
        return line

    old = [mask(line) for line in before.decode("utf-8", "replace").split("\n")]
    new = [mask(line) for line in after.decode("utf-8", "replace").split("\n")]
    changes = [
        line
        for line in difflib.unified_diff(old, new, "before", "after", lineterm="", n=0)
        if line.startswith(("+", "-", "@@"))
    ]
    if b"\r" in after and b"\r" not in before:
        changes.append("line endings changed to CRLF")
    return changes[:200]


def _venv_bin(venv: Path, name: str) -> Path:
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    return scripts / (f"{name}.exe" if os.name == "nt" else name)


def _run(argv: list, *, env: dict, cwd: Path, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(a) for a in argv], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout
    )


def _upgrade_check(
    config: E2EConfig,
    installed,
    home,
    tmp_path: Path,
    lane_evidence: LaneEvidence,
) -> tuple[bool, dict]:
    detail: dict = {"from_url": UPGRADE_FROM_URL, "from_sha256_pinned": UPGRADE_FROM_SHA256}
    env = fresh_install_environment(config, home)

    # The previous release, verified against the digest published in its SHA256SUMS.
    old_wheel = tmp_path / UPGRADE_FROM_URL.rsplit("/", 1)[-1]
    try:
        with urllib.request.urlopen(UPGRADE_FROM_URL, timeout=300) as response:
            old_wheel.write_bytes(response.read())
    except OSError as exc:
        detail["download_error"] = repr(exc)
        return False, detail
    digest = hashlib.sha256(old_wheel.read_bytes()).hexdigest()
    detail["from_sha256"] = digest
    if digest != UPGRADE_FROM_SHA256:
        return False, detail

    venv = tmp_path / "upgrade-venv"
    python = _venv_bin(venv, "python")
    steps = [
        ("venv", [sys.executable, "-m", "venv", venv]),
        ("install_old", [python, "-m", "pip", "install", "--no-cache-dir", "-q", old_wheel]),
    ]
    for name, argv in steps:
        result = _run(argv, env=env, cwd=home.cwd)
        detail[f"{name}_returncode"] = result.returncode
        if result.returncode != 0:
            lane_evidence.attach(f"upgrade-{name}.stderr.txt", result.stderr)
            return False, detail
    old_version = _run(
        [python, "-c", "import importlib.metadata as m; print(m.version('archolith-menhir'))"],
        env=env,
        cwd=home.cwd,
    )
    detail["old_version"] = old_version.stdout.strip()

    old_setup = _run([_venv_bin(venv, "menhir"), "setup", "--provider", "local"], env=env, cwd=home.cwd)
    lane_evidence.attach("upgrade-old-setup.stdout.txt", old_setup.stdout)
    lane_evidence.attach("upgrade-old-setup.stderr.txt", old_setup.stderr)
    detail["old_setup_returncode"] = old_setup.returncode
    detail["old_setup_wrote_state_env"] = home.env_path.is_file()
    if old_setup.returncode != 0 or not home.env_path.is_file():
        return False, detail
    edit_env(home.env_path, set_values=UPGRADE_PINNED)
    before = home.env_path.read_bytes()

    # Install the candidate over it: same version string, so force the package itself, then
    # let pip add any dependency the candidate needs that the old release did not.
    for name, argv in (
        ("install_candidate", [python, "-m", "pip", "install", "--no-cache-dir", "-q",
                               "--force-reinstall", "--no-deps", installed.wheel_path]),
        ("install_candidate_deps", [python, "-m", "pip", "install", "--no-cache-dir", "-q",
                                    installed.wheel_path]),
    ):
        result = _run(argv, env=env, cwd=home.cwd)
        detail[f"{name}_returncode"] = result.returncode
        if result.returncode != 0:
            lane_evidence.attach(f"upgrade-{name}.stderr.txt", result.stderr)
            return False, detail
    installed_env_file = _run(
        [python, "-c", "import menhir.env_file as m; print(m.__file__)"], env=env, cwd=home.other_cwd
    ).stdout.strip()
    candidate_active = bool(installed_env_file) and (
        Path(installed_env_file).read_bytes() == (REPO_ROOT / "src/menhir/env_file.py").read_bytes()
    )
    detail["candidate_code_active"] = candidate_active

    new_setup = _run([_venv_bin(venv, "menhir"), "setup", "--provider", "local"], env=env, cwd=home.cwd)
    lane_evidence.attach("upgrade-new-setup.stdout.txt", new_setup.stdout)
    lane_evidence.attach("upgrade-new-setup.stderr.txt", new_setup.stderr)
    detail["new_setup_returncode"] = new_setup.returncode
    after = home.env_path.read_bytes() if home.env_path.is_file() else b""
    detail["env_byte_identical"] = after == before
    filled, unexpected = _classify_upgrade_changes(before, after)
    detail["blank_defaults_filled"] = filled
    if unexpected:
        detail["env_changes"] = _masked_diff(before, after)

    probe = _run([python, "-c", EFFECTIVE_SETTINGS_CODE], env=env, cwd=home.other_cwd, timeout=300)
    if probe.returncode != 0:
        lane_evidence.attach("upgrade-settings.stderr.txt", probe.stderr)
        detail["settings_probe_returncode"] = probe.returncode
        return False, detail
    resolved = json.loads([line for line in probe.stdout.splitlines() if line.strip()][-1])
    values = resolved["values"]
    detail["env_file_resolved"] = resolved["env_file"]
    defaults = code_defaults()
    wrong: list[dict] = []
    for key, expected in defaults.items():
        if key in UPGRADE_PINNED:
            raw = UPGRADE_PINNED[key]
            expected = raw.lower() in _TRUE if isinstance(expected, bool) else raw
        if values.get(key) != expected:
            wrong.append({"env_var": key, "effective": values.get(key), "expected": expected})
    detail["wrong"] = wrong
    detail["pinned"] = sorted(UPGRADE_PINNED)
    passed = (
        candidate_active
        and new_setup.returncode == 0
        and not unexpected
        and Path(resolved["env_file"]) == home.env_path
        and not wrong
    )
    return passed, detail
