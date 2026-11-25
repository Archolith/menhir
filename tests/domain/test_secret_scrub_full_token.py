"""GHSA-ppqh-gffg-6j74: a redacted token must lose its whole body, not just its prefix."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import detect_secrets.plugins as ds_plugins
from detect_secrets.plugins.base import RegexBasedDetector
from menhir.domain import secret_scrub
from menhir.domain.secret_scrub import SecretRefused, detect_secrets, prepare_memory_text, redact

BODY36 = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GITHUB = [f"{p}_{BODY36}" for p in ("ghp", "gho", "ghu", "ghs", "ghr")] + [
    "ghp_" + BODY36 + "XYZ9",  # longer than the plugin's fixed 36
    "github_pat_" + "c" * 22 + "_" + "d" * 59,
]
GITLAB = [f"{p}-Zq9xW8vU7tS6rR5qP4oN3m" for p in ("glpat", "gldt", "glft", "glsoat", "glrt")] + [
    "glpat-" + "k" * 60,  # longer than the plugin's 50
    "glcbt-Zq9xW8vU7tS6rR5qP4oN3m",  # no optional hex part: the plugin found nothing
    "glcbt-1f_Zq9xW8vU7tS6rR5qP4oN3m",
]


@pytest.mark.parametrize(
    "token,kind",
    [(t, "GitHub Token") for t in GITHUB] + [(t, "GitLab Token") for t in GITLAB],
)
def test_token_is_redacted_whole(token: str, kind: str) -> None:
    text, hits = redact(f"use {token} now")
    assert text == f"use [REDACTED:{kind}] now"
    assert [h.kind for h in hits] == [kind]
    assert hits[0].value == token


def test_body_never_survives_the_writer_path() -> None:
    token = GITHUB[0]
    stored = prepare_memory_text(f"deploy key {token}", policy="redact")
    assert BODY36 not in stored
    assert stored.kinds == ("GitHub Token",)


def test_refuse_policy_still_refuses() -> None:
    with pytest.raises(SecretRefused):
        prepare_memory_text(f"x {GITLAB[0]}", policy="refuse")


def test_plain_words_with_a_prefix_are_not_hits() -> None:
    assert detect_secrets("ghp and glpat are token prefixes; ghp_short is not a token") == []


#: Enabled plugins whose patterns have capture groups, reviewed to capture the secret itself (not
#: a prefix or label). redact() replaces the captured value, so a group that does not cover the
#: secret leaks the rest. A new entry needs the same review.
_GROUP_CAPTURES_SECRET = {
    "AWSKeyDetector",
    "BasicAuthDetector",
    "CloudantDetector",
    "IbmCloudIamDetector",
    "IbmCosHmacDetector",
    "NpmDetector",
    "SoftlayerDetector",
}


def test_every_enabled_capture_group_plugin_is_reviewed() -> None:
    enabled = set(secret_scrub._PLUGINS)
    with_groups: set[str] = set()
    for info in pkgutil.iter_modules(ds_plugins.__path__):
        module = importlib.import_module(f"detect_secrets.plugins.{info.name}")
        for name, cls in vars(module).items():
            if name not in enabled or not inspect.isclass(cls):
                continue
            if not issubclass(cls, RegexBasedDetector):
                continue
            if any(pattern.groups for pattern in cls().denylist):
                with_groups.add(name)
    assert "GitHubTokenDetector" not in enabled and "GitLabTokenDetector" not in enabled
    assert with_groups <= _GROUP_CAPTURES_SECRET, sorted(with_groups - _GROUP_CAPTURES_SECRET)
