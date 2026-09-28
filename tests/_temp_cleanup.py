"""Cleanup for test scratch directories that can contain read-only Git objects."""

from __future__ import annotations

import gc
import os
import shutil
import stat
from pathlib import Path


def remove_test_dir(path: Path) -> None:
    """Remove test scratch or fail if files remain, including open SQLite files."""

    gc.collect()

    def retry_readonly(func, name: str, error: OSError) -> None:
        if not isinstance(error, PermissionError):
            raise error
        os.chmod(name, stat.S_IREAD | stat.S_IWRITE)
        func(name)

    shutil.rmtree(path, onexc=retry_readonly)
    if path.exists():
        raise OSError(f"test scratch cleanup left residue: {path}")
