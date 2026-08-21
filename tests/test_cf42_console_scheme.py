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


# ---------------------------------------------------------------------------
# CF-42, second half: the backend URL itself (owner ruling 2026-08-21)
# ---------------------------------------------------------------------------
#
# The console fix above covered the dashboard. `MENHIR_BACKEND_URL` was left open as "a
# settings-level decision"; the ruling made it: require HTTPS whenever the backend is
# non-loopback, allow HTTP for loopback.
#
# WHERE THE GUARD GOES, and this took three attempts to get right:
#   * NOT `resolve_mcp_backend_url` -- its only production caller is
#     `build_mcp_backend_diagnostics`, and a reporting surface must DESCRIBE a bad configuration,
#     not crash on it. Guarding there broke `menhir diagnostics` on exactly the misconfiguration
#     it exists to surface.
#   * NOT `_normalized_backend_url` -- it also backs `backend_client_mode_enabled`, a predicate
#     asked all over the codebase. A raising predicate breaks everything except the thing it
#     was meant to stop.
#   * The construction of the `BackendClient` that will attach the bearer key. That is the one
#     point where the credential actually leaves the process.
#
# The CLI's own `http://{api_host}:{api_port}` self-connection is deliberately NOT guarded: it is
# a process reaching its own server via its bind address (`0.0.0.0` on this deployment), not an
# operator-configured remote backend.


def _settings(backend_url: str = ""):
    from types import SimpleNamespace

    return SimpleNamespace(backend_url=backend_url, api_host="127.0.0.1", api_port=8090)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8090", "http://localhost:8090"])
def test_a_loopback_backend_may_stay_plaintext(monkeypatch, url: str) -> None:
    """POSITIVE CONTROL and the reason the rule is scoped to the HOST, not the scheme: local
    development must be untouched. This deployment's own MENHIR_BACKEND_URL is loopback http."""
    from menhir.mcp.service_access import resolve_mcp_backend_url

    monkeypatch.delenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", raising=False)
    from menhir.mcp.service_access import _require_secure_backend_url

    assert _require_secure_backend_url(url) == url


def test_a_non_loopback_plaintext_backend_is_refused(monkeypatch) -> None:
    """THE RULING. Every request to this URL carries an Authorization bearer key -- per CF-41
    possibly the operator one."""
    from menhir.mcp.service_access import _require_secure_backend_url

    monkeypatch.delenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", raising=False)
    with pytest.raises(ValueError, match="Refusing a plaintext backend URL"):
        _require_secure_backend_url("http://192.168.86.56:8090")


def test_diagnostics_reports_a_bad_backend_url_instead_of_crashing(monkeypatch) -> None:
    """THE REGRESSION THAT MADE ME MOVE THE GUARD. `menhir diagnostics` must still render when the
    backend URL is exactly the misconfiguration this rule refuses -- reporting it is the surface's
    entire purpose."""
    from types import SimpleNamespace

    from menhir.mcp.service_access import build_mcp_backend_diagnostics

    monkeypatch.delenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", raising=False)
    block = build_mcp_backend_diagnostics(
        SimpleNamespace(
            backend_url="http://backend:8099", api_host="127.0.0.1", api_port=8090,
            agent_key="k", api_key="",
        )
    )

    assert block["backend_url"] == "http://backend:8099"


def test_the_mode_predicate_does_not_raise_either(monkeypatch) -> None:
    """`backend_client_mode_enabled` is asked all over the codebase. A raising predicate breaks
    everything except the thing it was meant to stop."""
    from types import SimpleNamespace

    from menhir.mcp.service_access import backend_client_mode_enabled

    monkeypatch.delenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", raising=False)
    assert backend_client_mode_enabled(
        SimpleNamespace(backend_url="http://backend:8099")
    ) is True


def test_https_to_anywhere_is_fine(monkeypatch) -> None:
    from menhir.mcp.service_access import resolve_mcp_backend_url

    monkeypatch.delenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", raising=False)
    from menhir.mcp.service_access import _require_secure_backend_url

    assert (
        _require_secure_backend_url("https://menhir.example:8090")
        == "https://menhir.example:8090"
    )


def test_the_override_is_honoured_and_warns(monkeypatch, caplog) -> None:
    """Mirrors MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH, the codebase's existing precedent for this
    shape: refuse by default, let an operator say otherwise out loud, and warn when they do."""
    from menhir.mcp.service_access import _require_secure_backend_url

    monkeypatch.setenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", "1")
    with caplog.at_level(logging.WARNING, logger="menhir.mcp.service_access"):
        url = _require_secure_backend_url("http://192.168.86.56:8090")

    assert url == "http://192.168.86.56:8090"
    assert any("plaintext" in r.message for r in caplog.records)


def test_the_default_when_nothing_is_configured_is_unchanged(monkeypatch) -> None:
    """POSITIVE CONTROL: with no backend_url the resolver builds one from api_host/api_port and
    that path must not start refusing itself."""
    from menhir.mcp.service_access import resolve_mcp_backend_url

    monkeypatch.delenv("MENHIR_ALLOW_INSECURE_BACKEND_URL", raising=False)
    assert resolve_mcp_backend_url(_settings()) == "http://127.0.0.1:8090"
