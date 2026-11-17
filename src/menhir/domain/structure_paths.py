"""Caller path spellings -> the stored `structure_path` form (#679, #683).

Stored paths are forward-slashed and repo-relative, with no leading `./` or `/`. Callers pass
Windows paths, `./` prefixes and absolute paths. A spelling that misses the index used to read
as "no importers, no tests", so every structure query normalizes its input with this module.

Pure string work: the server may not have the project's checkout on disk (#104), so nothing
here touches the filesystem.
"""

from __future__ import annotations

import re

_DRIVE = re.compile(r"^([A-Za-z]):/")
# Git Bash `/c/...` and WSL `/mnt/c/...` spellings of a Windows drive.
_POSIX_DRIVE = re.compile(r"^/(?:mnt/)?([A-Za-z])/")


def _drive_form(path: str) -> str | None:
    """`c:/rest` for a Windows-drive path in any spelling, else None."""
    m = _DRIVE.match(path)
    if m:
        return f"{m.group(1).lower()}:/{path[m.end():]}"
    m = _POSIX_DRIVE.match(path)
    if m:
        return f"{m.group(1).lower()}:/{path[m.end():]}"
    return None


def is_absolute_spelling(path: str) -> bool:
    p = str(path).strip().replace("\\", "/")
    return p.startswith("/") or bool(_DRIVE.match(p))


def _relative(path: str) -> str:
    p = path
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def normalize_query_path(path: str, root_path: str = "", *, prefix: bool = False) -> str:
    """Return the stored spelling of *path*, or the path unchanged when it cannot be mapped.

    ``prefix=True`` keeps a trailing slash: for `files` and `symbols` a trailing `/` means
    "directory", and dropping it would turn `src/` into a match for `src_old/`.

    An absolute path under *root_path* is made repo-relative (compared separator-insensitively,
    and case-insensitively for Windows drives). Outside the root, a Windows-drive spelling
    (`C:/...`, `/c/...`, `/mnt/c/...`) comes back forward-slashed but still absolute, so it
    reports as not indexed instead of matching a different file. A plain leading `/` is read as
    repo-relative (`/src/a.py` -> `src/a.py`), as it always was; callers echo the rewrite.
    """
    raw = str(path).strip().replace("\\", "/")
    trailing = prefix and raw.endswith("/")
    if is_absolute_spelling(raw):
        rel = path_relative_to_root(raw, root_path)
        if rel is None:
            if _drive_form(raw) is not None:
                return raw
            rel = _relative(raw)
    else:
        rel = _relative(raw)
    rel = rel.rstrip("/")
    return rel + "/" if trailing and rel else rel


def path_relative_to_root(path: str, root_path: str) -> str | None:
    """Return a path under this indexed root, or None for an unrelated path."""
    path = path.strip().replace("\\", "/")
    root = str(root_path or "").strip().replace("\\", "/").rstrip("/")
    if not root:
        return None
    path_drive, root_drive = _drive_form(path), _drive_form(root + "/")
    if path_drive is not None and root_drive is not None:
        candidate, base = path_drive, root_drive.rstrip("/")
        if candidate.lower().startswith(base.lower() + "/"):
            return candidate[len(base) + 1:]
        return None
    if path.startswith(root + "/"):
        return path[len(root) + 1:]
    return None


def normalize_query_paths(paths: list[str], root_path: str = "") -> tuple[list[str], dict[str, str]]:
    """Normalize, drop empties, de-duplicate (first spelling wins).

    Returns ``(normalized, rewrites)`` where *rewrites* maps each caller spelling that changed to
    its stored form, for echoing back to the caller.
    """
    out: list[str] = []
    rewrites: dict[str, str] = {}
    for original in paths:
        stripped = str(original).strip()
        if not stripped:
            continue
        normalized = normalize_query_path(stripped, root_path)
        if not normalized:
            continue
        if normalized != stripped:
            rewrites[stripped] = normalized
        if normalized not in out:
            out.append(normalized)
    return out, rewrites


__all__ = [
    "is_absolute_spelling",
    "normalize_query_path",
    "normalize_query_paths",
]
