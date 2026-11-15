"""Filesystem containment checks shared by the ingest guard and the scanners.

Lives in infrastructure so the scanners can use it without importing ``menhir.core``.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

# Host system trees refused for every tier, operator included: process environments
# (/proc/self/environ), kernel and device files, mounted secrets (/run/secrets) and system config.
# Operator reach covers the user's files, not the server's own credentials.
DENIED_SYSTEM_TOP_DIRS = frozenset({"proc", "sys", "dev", "run", "etc"})


def is_host_system_path(parts: tuple[str, ...]) -> bool:
    """Return True for a POSIX path under a denied system tree (``/proc``, ``/var/run``...)."""
    if len(parts) < 2 or parts[0] != "/":
        return False
    if parts[1].lower() == "private":  # macOS: /etc and /var/run resolve under /private
        parts = ("/", *parts[2:])
        if len(parts) < 2:
            return False
    top = parts[1].lower()
    return top in DENIED_SYSTEM_TOP_DIRS or (
        top == "var" and len(parts) > 2 and parts[2].lower() == "run"
    )


def _within(real: Path, real_root: Path) -> bool:
    return real == real_root or real_root in real.parents


def is_contained_dir(path: str | Path, root: str | Path) -> bool:
    """True when directory ``path`` resolves inside ``root`` (catches symlinks and junctions)."""
    try:
        return _within(Path(os.path.realpath(path)), Path(os.path.realpath(root)))
    except (OSError, ValueError):
        return False


def check_contained_regular_file(path: str | Path, root: str | Path) -> bool:
    """True when ``path``, symlinks followed, is a regular file inside ``root`` (resolved).

    The ingest guard checks the root a caller names; the files read beneath it are repository
    content. A planted symlink (``README.md -> /proc/self/environ``, ``x.py -> /dev/zero``)
    must not reach outside the root or hang a read, for any tier.

    False is a definite answer: outside the root, not a regular file, a dangling or looping
    symlink, or a host system file under a root that is not itself under one. Raises OSError
    when the file cannot be examined (I/O error, vanished file), because that is not proof of
    absence and must not let a scan prune it.
    """
    real = Path(os.path.realpath(path))
    real_root = Path(os.path.realpath(root))
    try:
        mode = os.stat(real).st_mode
    except FileNotFoundError:
        if os.path.lexists(path):
            return False  # dangling symlink
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            return False
        raise
    if not stat.S_ISREG(mode):
        return False
    if not _within(real, real_root):
        return False
    # A project that itself lives under /run or /etc (a mounted drive, /etc/nixos) keeps its files.
    return not (is_host_system_path(real.parts) and not is_host_system_path(real_root.parts))


def is_contained_regular_file(path: str | Path, root: str | Path) -> bool:
    """:func:`check_contained_regular_file`, with an unexaminable file counted as not contained."""
    try:
        return check_contained_regular_file(path, root)
    except (OSError, ValueError):
        return False
