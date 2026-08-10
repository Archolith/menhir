"""Architecture checks for the one-way config/API dependency boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from menhir.api.auth_mode import (
    AuthMode as ApiAuthMode,
    auth_mode_from as api_auth_mode_from,
    resolve_auth_mode as api_resolve_auth_mode,
)
from menhir.api.oauth import (
    OAuthConfig as ApiOAuthConfig,
    _as_bool as api_as_bool,
    _as_tuple as api_as_tuple,
    _get_setting as api_get_setting,
    build_oauth_config as api_build_oauth_config,
)
from menhir.config.auth_mode import AuthMode, auth_mode_from, resolve_auth_mode
from menhir.config.oauth import (
    OAuthConfig,
    _as_bool,
    _as_tuple,
    _get_setting,
    build_oauth_config,
)


def test_config_does_not_import_api_modules() -> None:
    config_root = Path(__file__).parents[1] / "src" / "menhir" / "config"
    violations: dict[str, list[str]] = {}

    for path in config_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(
                    alias.name
                    for alias in node.names
                    if alias.name == "menhir.api"
                    or alias.name.startswith("menhir.api.")
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "menhir.api" or module.startswith("menhir.api."):
                    imported.append(module)
                elif node.level >= 2 and (
                    module == "api" or module.startswith("api.")
                ):
                    imported.append("." * node.level + module)
        if imported:
            violations[str(path.relative_to(config_root))] = sorted(imported)

    assert not violations


def test_api_auth_mode_exports_are_compatibility_aliases() -> None:
    assert ApiAuthMode is AuthMode
    assert api_auth_mode_from is auth_mode_from
    assert api_resolve_auth_mode is resolve_auth_mode


def test_api_oauth_config_exports_are_compatibility_aliases() -> None:
    assert ApiOAuthConfig is OAuthConfig
    assert api_build_oauth_config is build_oauth_config
    assert api_get_setting is _get_setting
    assert api_as_bool is _as_bool
    assert api_as_tuple is _as_tuple
