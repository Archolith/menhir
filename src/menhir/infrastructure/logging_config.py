"""Central logging configuration for menhir application entrypoints."""

from __future__ import annotations

import logging
import logging.config
import os
from copy import deepcopy
from typing import Any

DEFAULT_LOG_LEVEL = "INFO"

_BASE_LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(asctime)s - %(name)s - %(levelname)s - %(client_addr)s - "%(request_line)s" %(status_code)s',
        },
    },
    "handlers": {
        "console_stderr": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "stream": "ext://sys.stderr",
        },
        "console_stdout": {
            "class": "logging.StreamHandler",
            "formatter": "access",
            "stream": "ext://sys.stdout",
        },
        "app_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": "",
            "encoding": "utf-8",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
        },
        "error_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": "",
            "encoding": "utf-8",
            "level": "WARNING",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
        },
        "access_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "access",
            "filename": "",
            "encoding": "utf-8",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
        },
    },
    "root": {
        "level": DEFAULT_LOG_LEVEL,
        "handlers": ["console_stderr", "app_file", "error_file"],
    },
    "loggers": {
        "uvicorn": {
            "level": DEFAULT_LOG_LEVEL,
            "handlers": ["console_stderr", "app_file", "error_file"],
            "propagate": False,
        },
        "uvicorn.error": {
            "level": DEFAULT_LOG_LEVEL,
            "handlers": ["console_stderr", "app_file", "error_file"],
            "propagate": False,
        },
        "uvicorn.access": {
            "level": DEFAULT_LOG_LEVEL,
            "handlers": ["console_stdout", "access_file"],
            "propagate": False,
        },
        "menhir": {
            "level": DEFAULT_LOG_LEVEL,
            "handlers": [],
            "propagate": True,
        },
        "httpx": {
            "level": "WARNING",
            "handlers": [],
            "propagate": True,
        },
        "httpcore": {
            "level": "WARNING",
            "handlers": [],
            "propagate": True,
        },
        "neo4j": {
            "level": "WARNING",
            "handlers": [],
            "propagate": True,
        },
        "neo4j.notifications": {
            "level": "WARNING",
            "handlers": [],
            "propagate": True,
        },
        "posthog": {
            "level": "WARNING",
            "handlers": [],
            "propagate": True,
        },
        "urllib3": {
            "level": "WARNING",
            "handlers": [],
            "propagate": True,
        },
    },
}


def _default_log_dir() -> str:
    return os.getenv("MENHIR_LOG_DIR") or os.path.join(os.getcwd(), "logs")


def _default_log_level() -> str:
    """Log level from the environment, falling back to INFO.

    `serve` calls `build_logging_config()` with no level, so before this there was no way to
    raise verbosity without editing code -- which meant diagnosing a library's DEBUG-level
    accounting (e.g. graphiti's per-edge dedupe candidate counts) required a source edit.
    An explicit `level=` argument still wins; this only changes the default.
    """
    return (os.getenv("MENHIR_LOG_LEVEL") or DEFAULT_LOG_LEVEL).strip().upper()


def build_logging_config(
    *,
    level: str | None = None,
    log_dir: str | None = None,
    include_console: bool = True,
) -> dict[str, Any]:
    """Return a dictConfig-compatible logging configuration."""

    normalized_level = str(level or _default_log_level()).upper()
    resolved_log_dir = log_dir or _default_log_dir()
    os.makedirs(resolved_log_dir, exist_ok=True)
    config = deepcopy(_BASE_LOGGING_CONFIG)
    config["handlers"]["app_file"]["filename"] = os.path.join(resolved_log_dir, "server.log")
    config["handlers"]["error_file"]["filename"] = os.path.join(resolved_log_dir, "server.err.log")
    config["handlers"]["access_file"]["filename"] = os.path.join(resolved_log_dir, "server.access.log")
    config["root"]["level"] = normalized_level
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "menhir"):
        config["loggers"][logger_name]["level"] = normalized_level
    if not include_console:
        config["root"]["handlers"] = ["app_file", "error_file"]
        config["loggers"]["uvicorn"]["handlers"] = ["app_file", "error_file"]
        config["loggers"]["uvicorn.error"]["handlers"] = ["app_file", "error_file"]
        config["loggers"]["uvicorn.access"]["handlers"] = ["access_file"]
    return config


def configure_logging(
    *,
    level: str = DEFAULT_LOG_LEVEL,
    log_dir: str | None = None,
    include_console: bool = True,
) -> None:
    """Apply central logging configuration for application entrypoints."""

    logging.config.dictConfig(
        build_logging_config(level=level, log_dir=log_dir, include_console=include_console)
    )
    install_neo4j_notification_filter()


class _Neo4jUnknownKeyNotificationFilter(logging.Filter):
    """Drop Neo4j ``01N52`` "property key does not exist" notifications.

    Menhir's own drivers already disable the UNRECOGNIZED classification at connection time,
    but Graphiti opens its own driver, and on a fresh graph its first ``add_episode`` queries
    properties that no node has yet (``entity_edges``, ``name_embedding``, ``fact_embedding``,
    ...). Each one is logged at WARNING by the ``neo4j.notifications`` logger -- dozens of lines
    on a first write, none actionable. Filtering on the logger covers every driver in the
    process; every other notification (deprecations, performance hints) still passes.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ("01N52" in message or "property key does not exist" in message)


def install_neo4j_notification_filter() -> None:
    target = logging.getLogger("neo4j.notifications")
    if not any(isinstance(f, _Neo4jUnknownKeyNotificationFilter) for f in target.filters):
        target.addFilter(_Neo4jUnknownKeyNotificationFilter())
