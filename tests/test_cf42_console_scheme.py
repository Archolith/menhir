"""CF-42: the operator dashboard sent a bearer key over a hard-coded plaintext scheme.

`run_console` built its base URL as `f"http://{host}:{port}"` -- a literal, with no TLS option and
no warning -- while `_poll_json` attaches `Authorization: Bearer <key>` to every request.

On loopback that is harmless, and that is the default. But the service is deliberately
LAN-exposable (`MENHIR_API_HOST=0.0.0.0`) and `--host` is an ordinary user-facing flag, so pointing
the dashboard at a non-loopback address put a bearer key on the wire in the clear. Per CF-41 that
key may be the operator one.

THE SHAPE OF THE FIX. Not "always https", which would break every existing loopback dashboard, and
not "refuse", which would break an unauthenticated remote dashboard that works today. The scheme is
decided by what is actually at risk: a credential leaving the machine. Key + remote -> https.
Anything else -> unchanged. An operator whose remote backend really is plaintext can say so with
`MENHIR_CONSOLE_SCHEME=http` and gets a warning rather than silence.
"""

from __future__ import annotations

import logging

import pytest

from menhir.cli.console import _SCHEME_ENV, _dashboard_base_url

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# the finding
# ---------------------------------------------------------------------------


def test_a_key_to_a_remote_host_is_not_sent_over_plaintext(monkeypatch) -> None:
    """THE FINDING. This is the combination that leaked."""
    monkeypatch.delenv(_SCHEME_ENV, raising=False)

    assert _dashboard_base_url("10.0.0.5", 8099, api_key="secret") == "https://10.0.0.5:8099"


def test_the_remote_key_case_warns(monkeypatch, caplog) -> None:
    """A silent scheme switch would surprise an operator whose backend is plaintext; the warning
    is what points them at the override."""
    monkeypatch.delenv(_SCHEME_ENV, raising=False)

    with caplog.at_level(logging.WARNING, logger="menhir.cli.console"):
        _dashboard_base_url("10.0.0.5", 8099, api_key="secret")

    assert any("https" in r.message and _SCHEME_ENV in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# positive controls -- everything that worked before must still work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_with_a_key_is_unchanged(monkeypatch, host: str) -> None:
    """POSITIVE CONTROL, the one that matters most: the default workflow is loopback + key, and a
    fix that forced https there would break every existing dashboard."""
    monkeypatch.delenv(_SCHEME_ENV, raising=False)

    assert _dashboard_base_url(host, 8099, api_key="secret") == f"http://{host}:8099"


def test_a_remote_host_with_no_key_is_unchanged(monkeypatch) -> None:
    """POSITIVE CONTROL: there is no credential to protect, so refusing or upgrading here would
    break an unauthenticated remote dashboard that works today."""
    monkeypatch.delenv(_SCHEME_ENV, raising=False)

    assert _dashboard_base_url("10.0.0.5", 8099, api_key=None) == "http://10.0.0.5:8099"


def test_no_warning_on_the_unchanged_paths(monkeypatch, caplog) -> None:
    """POSITIVE CONTROL: a guard that always warned would satisfy the warning test above and
    become noise the operator learns to ignore."""
    monkeypatch.delenv(_SCHEME_ENV, raising=False)

    with caplog.at_level(logging.WARNING, logger="menhir.cli.console"):
        _dashboard_base_url("127.0.0.1", 8099, api_key="secret")
        _dashboard_base_url("10.0.0.5", 8099, api_key=None)

    assert caplog.records == []


# ---------------------------------------------------------------------------
# the override
# ---------------------------------------------------------------------------


def test_the_override_is_honoured_and_still_warns(monkeypatch, caplog) -> None:
    """An operator who really does have a plaintext remote backend must be able to say so -- and
    must be told what it costs, rather than the guard failing silently."""
    monkeypatch.setenv(_SCHEME_ENV, "http")

    with caplog.at_level(logging.WARNING, logger="menhir.cli.console"):
        url = _dashboard_base_url("10.0.0.5", 8099, api_key="secret")

    assert url == "http://10.0.0.5:8099"
    assert any("in the clear" in r.message for r in caplog.records)


def test_the_override_can_force_https_on_loopback(monkeypatch) -> None:
    """The override works both ways -- a loopback backend behind local TLS is a real setup."""
    monkeypatch.setenv(_SCHEME_ENV, "https")

    assert _dashboard_base_url("127.0.0.1", 8099, api_key=None) == "https://127.0.0.1:8099"


def test_a_junk_override_falls_back_to_the_safe_default(monkeypatch) -> None:
    """A typo in the env var must not disable the protection. `MENHIR_CONSOLE_SCHEME=htp` is
    ignored, and the remote+key case still upgrades."""
    monkeypatch.setenv(_SCHEME_ENV, "htp")

    assert _dashboard_base_url("10.0.0.5", 8099, api_key="secret") == "https://10.0.0.5:8099"
