"""Credential scrub for structured read output (#577).

``scrub_payload`` walks dicts, lists, tuples and sets and redacts every string leaf with the
same detector the write path uses. Dict keys and non-string leaves are untouched, so JSON
structure survives. ``off`` returns the value as given. Fails closed: a scan error raises
``PayloadScrubFailed`` instead of returning unchecked text.
"""
from __future__ import annotations

import logging
from typing import Any

from menhir.domain.secret_scrub import redact, resolve_policy

logger = logging.getLogger(__name__)

WITHHELD_MESSAGE = "Output withheld: the credential scan failed."


class PayloadScrubFailed(RuntimeError):
    """The detector raised while scrubbing a read payload."""


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[0] if value else value
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    if isinstance(value, tuple):
        walked = tuple(_walk(v) for v in value)
        # Keep namedtuple/subclass types; plain tuples stay plain.
        return walked if type(value) is tuple else type(value)(*walked)
    if isinstance(value, (set, frozenset)):
        return type(value)(_walk(v) for v in value)
    return value


def scrub_payload(value: Any) -> Any:
    """Return ``value`` with credentials redacted from every string leaf."""
    if resolve_policy() == "off":
        return value
    try:
        return _walk(value)
    except Exception as exc:
        logger.exception("Payload credential scan failed; withholding output")
        raise PayloadScrubFailed(WITHHELD_MESSAGE) from exc
