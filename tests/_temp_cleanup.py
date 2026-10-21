"""Cleanup for test scratch directories that can contain read-only Git objects."""

from __future__ import annotations

import gc
import logging
import os
import shutil
import stat
from pathlib import Path


def close_test_log_handlers(root: Path) -> None:
    """Release Menhir CLI file handlers created inside this pytest run."""

    root = root.resolve()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "menhir"):
        logger = logging.getLogger(name)
        for handler in tuple(logger.handlers):
            if isinstance(handler, logging.FileHandler) and Path(
                handler.baseFilename
            ).resolve().is_relative_to(root):
                logger.removeHandler(handler)
                handler.close()


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
