"""Filesystem containment checks shared by the ingest guard and the scanners.

Lives in infrastructure so the scanners can use it without importing ``menhir.core``.
"""

from __future__ import annotations

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


def is_contained_regular_file(path: str | Path, root: str | Path) -> bool:
    """True when ``path``, symlinks followed, is a regular file inside ``root`` (resolved).

    The ingest guard checks the root a caller names; the files read beneath it are repository
    content. A planted symlink (``README.md -> /proc/self/environ``, ``x.py -> /dev/zero``)
    must not reach outside the root or hang a read, for any tier.
    """
    try:
        real = Path(os.path.realpath(path))
        real_root = Path(os.path.realpath(root))
        mode = os.stat(real).st_mode
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(mode):
        return False
    if real != real_root and real_root not in real.parents:
        return False
    return not is_host_system_path(real.parts)
