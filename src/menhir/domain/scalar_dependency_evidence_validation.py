"""Scalar and structural validation helpers shared by scalar dependency evidence types."""

from __future__ import annotations

from typing import Any

from menhir.domain.scalar_dependency_evidence_limits import _SHA256_RE, _VERSION_TOKEN_RE


def _int(name: str, value: object) -> int:
    if type(value) is not int:  # bool is an int subclass, but never a valid bound/index.
        raise ValueError(f"{name} must be an int, not bool")
    return value


def _nonnegative(name: str, value: object) -> int:
    value = _int(name, value)
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


def _text(name: str, value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    if value != value.strip():
        raise ValueError(f"{name} must not have leading or trailing whitespace")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds maximum length {maximum}")
    return value


def _hash(name: str, value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex string")
    return value


def _version_token(name: str, value: object) -> str:
    if not isinstance(value, str) or _VERSION_TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase version token")
    return value


def _tuple(name: str, value: object) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    return value


def _bounded_tuple(name: str, value: object, maximum: int) -> tuple[Any, ...]:
    value = _tuple(name, value)
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds maximum size {maximum}")
    return value


def _validate_bounds(name: str, start: object, end: object, limit: int | None = None) -> None:
    start = _nonnegative(f"{name}.start", start)
    end = _nonnegative(f"{name}.end", end)
    if start >= end:
        raise ValueError(f"{name} must be a non-empty half-open span")
    if limit is not None and end > limit:
        raise ValueError(f"{name} exceeds containing bounds")
