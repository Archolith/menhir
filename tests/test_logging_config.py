"""Regression coverage for Menhir's centralized logging configuration."""

from __future__ import annotations

import logging

from uvicorn.logging import AccessFormatter

from menhir.infrastructure import logging_config
from menhir.infrastructure.logging_config import build_logging_config


def test_uvicorn_access_formatter_expands_positional_request_fields(tmp_path) -> None:
    """Access records must not fail looking up client_addr/request_line fields."""
    config = build_logging_config(log_dir=str(tmp_path), include_console=False)
    access_config = config["formatters"]["access"]

    assert access_config["()"] == "uvicorn.logging.AccessFormatter"
    formatter = AccessFormatter(fmt=access_config["fmt"])
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", "/api/internal/backend/example", "1.1", 200),
        None,
    )

    rendered = formatter.format(record)

    assert "127.0.0.1:5000" in rendered
    assert '"POST /api/internal/backend/example HTTP/1.1" 200' in rendered


def test_log_level_defaults_to_info_without_the_env_var(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MENHIR_LOG_LEVEL", raising=False)
    config = build_logging_config(log_dir=str(tmp_path))
    assert config["root"]["level"] == "INFO"


def test_env_var_raises_the_default_log_level(monkeypatch, tmp_path) -> None:
    """`serve` calls build_logging_config() with no level, so without this there is no way to
    raise verbosity for troubleshooting without editing source."""
    monkeypatch.setenv("MENHIR_LOG_LEVEL", "debug")
    config = build_logging_config(log_dir=str(tmp_path))
    assert config["root"]["level"] == "DEBUG"
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "menhir"):
        assert config["loggers"][name]["level"] == "DEBUG"


def test_explicit_level_argument_still_wins_over_the_env_var(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MENHIR_LOG_LEVEL", "DEBUG")
    config = build_logging_config(level="WARNING", log_dir=str(tmp_path))
    assert config["root"]["level"] == "WARNING"


def _capture_configure_logging(monkeypatch, tmp_path, **kwargs) -> tuple[dict, list[str]]:
    captured: dict = {}
    installed: list[str] = []

    monkeypatch.setattr(logging_config.logging.config, "dictConfig", captured.update)
    monkeypatch.setattr(
        logging_config,
        "install_neo4j_notification_filter",
        lambda: installed.append("neo4j-notifications"),
    )

    logging_config.configure_logging(log_dir=str(tmp_path), **kwargs)
    return captured, installed


def test_configure_logging_defaults_to_info_without_the_env_var(monkeypatch, tmp_path) -> None:
    """configure_logging must not hard-code INFO past MENHIR_LOG_LEVEL."""
    monkeypatch.delenv("MENHIR_LOG_LEVEL", raising=False)
    captured, installed = _capture_configure_logging(monkeypatch, tmp_path)
    assert captured["root"]["level"] == "INFO"
    assert installed == ["neo4j-notifications"]


def test_configure_logging_honors_lowercase_debug_env_var(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MENHIR_LOG_LEVEL", "debug")
    captured, installed = _capture_configure_logging(monkeypatch, tmp_path)
    assert captured["root"]["level"] == "DEBUG"
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "menhir"):
        assert captured["loggers"][name]["level"] == "DEBUG"
    assert installed == ["neo4j-notifications"]


def test_configure_logging_explicit_level_overrides_the_env_var(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MENHIR_LOG_LEVEL", "DEBUG")
    captured, installed = _capture_configure_logging(monkeypatch, tmp_path, level="WARNING")
    assert captured["root"]["level"] == "WARNING"
    assert installed == ["neo4j-notifications"]
