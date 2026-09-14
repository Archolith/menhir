"""One rule for which ``.env`` Menhir reads, and a guard against a dependency reading another.

``python-dotenv``'s ``load_dotenv()`` with no path calls ``find_dotenv()``, which -- whenever
``__main__`` has a ``__file__`` (``python -m menhir.cli``, the installed ``menhir`` script) --
walks up from the *calling file's directory*, not from the current directory. Called from
``site-packages``, that walk reaches the venv's parent and loads whatever ``.env`` lives there.
Called from ``menhir/cli/__init__.py``, it reaches the source checkout. Neither is "the
directory the operator ran the command in", and the two disagree with each other.

``graphiti_core.helpers`` performs exactly such a bare ``load_dotenv()`` at import time. Any key
the operator's ``.env`` leaves unset is then silently filled from a bystander file -- observed
as ``GRAPHITI_EMBED_PROVIDER=openai`` appearing in a deployment whose ``.env`` said ``local``.

Menhir therefore (1) always passes an explicit path: ``ENV_FILE`` if set, else ``./.env`` if
present, else nothing; and (2) turns a dependency's bare ``load_dotenv()`` into a no-op.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import dotenv
import dotenv.main


def resolve_env_file() -> Path | None:
    """Return the ``.env`` Menhir should read: ``ENV_FILE``, else ``./.env`` when it exists."""

    configured = os.getenv("ENV_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    candidate = Path.cwd() / ".env"
    return candidate if candidate.is_file() else None


def load_menhir_env(*, override: bool = False) -> Path | None:
    """Load :func:`resolve_env_file` into the process environment. Returns the path used."""

    path = resolve_env_file()
    if path is not None and path.is_file():
        _original_load_dotenv(dotenv_path=str(path), override=override)
        return path
    return None


_original_load_dotenv = dotenv.main.load_dotenv
_GUARDED_PREFIXES = ("graphiti_core",)


def _caller_module_name() -> str:
    frame = inspect.currentframe()
    try:
        # this function -> guarded load_dotenv -> the caller we want
        caller = frame.f_back.f_back if frame and frame.f_back else None
        if caller is None:
            return ""
        return str(caller.f_globals.get("__name__", ""))
    finally:
        del frame


def _guarded_load_dotenv(dotenv_path=None, *args, **kwargs):  # type: ignore[no-untyped-def]
    if dotenv_path is None and not kwargs.get("stream"):
        module = _caller_module_name()
        if module.split(".")[0] in _GUARDED_PREFIXES:
            return False
    return _original_load_dotenv(dotenv_path, *args, **kwargs)


def install_dotenv_guard() -> None:
    """Make a dependency's path-less ``load_dotenv()`` a no-op. Idempotent."""

    if dotenv.main.load_dotenv is _guarded_load_dotenv:
        return
    dotenv.main.load_dotenv = _guarded_load_dotenv
    dotenv.load_dotenv = _guarded_load_dotenv


__all__ = ["install_dotenv_guard", "load_menhir_env", "resolve_env_file"]
