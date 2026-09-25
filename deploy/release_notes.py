#!/usr/bin/env python3
"""Validate and render strict Menhir release-note fragments."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    # The sibling release-notes modules live beside this script; make them
    # importable even when this module is loaded by path (importlib spec)
    # rather than as a script or as a package member.
    sys.path.append(str(_SCRIPT_DIR))

from release_notes_fragments import (  # noqa: E402
    CATEGORIES as CATEGORIES,
    DEPLOYMENT_CLASSES as DEPLOYMENT_CLASSES,
    FRAGMENT_KEYS as FRAGMENT_KEYS,
    MAX_COMMITS_PER_REPOSITORY as MAX_COMMITS_PER_REPOSITORY,
    MAX_DETAILS_LENGTH as MAX_DETAILS_LENGTH,
    MAX_FRAGMENT_BYTES as MAX_FRAGMENT_BYTES,
    MAX_ID_LENGTH as MAX_ID_LENGTH,
    MAX_OPERATOR_IMPACT_LENGTH as MAX_OPERATOR_IMPACT_LENGTH,
    MAX_SECURITY_SCOPES as MAX_SECURITY_SCOPES,
    MAX_SUMMARY_LENGTH as MAX_SUMMARY_LENGTH,
    REPOSITORIES as REPOSITORIES,
    SCHEMA as SCHEMA,
    SECURITY_SCOPES as SECURITY_SCOPES,
    ReleaseNoteError as ReleaseNoteError,
    ReleaseNoteFragment as ReleaseNoteFragment,
    _CATEGORY_ORDER as _CATEGORY_ORDER,
    _COMMIT_RE as _COMMIT_RE,
    _RELEASE_ID_RE as _RELEASE_ID_RE,
    _SLUG_RE as _SLUG_RE,
    _fail as _fail,
    _fragment_sort_key as _fragment_sort_key,
    _read_regular_file as _read_regular_file,
    _reject_constant as _reject_constant,
    _repositories as _repositories,
    _security_scopes as _security_scopes,
    _text as _text,
    _unique_object as _unique_object,
    collect_fragments as collect_fragments,
    load_fragment as load_fragment,
)
from release_notes_rendering import (  # noqa: E402
    _fragment_dict as _fragment_dict,
    _ordered_fragments as _ordered_fragments,
    _release_id as _release_id,
    render_json as render_json,
    render_markdown as render_markdown,
)


def _safe_output_path(value: str) -> Path:
    if not value:
        _fail("output path must not be empty")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if windows_path.is_absolute() or windows_path.drive or posix_path.is_absolute():
        _fail("output path must be relative")
    if ".." in windows_path.parts or ".." in posix_path.parts:
        _fail("output path must not contain traversal")
    output = Path(value)
    root = Path.cwd().resolve()
    resolved = output.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        _fail("output path must remain below the working directory")
    return output


def _write_atomic(path: Path, content: str, *, overwrite: bool) -> None:
    parent = path.parent if path.parent != Path("") else Path(".")
    if not parent.is_dir():
        _fail(f"output parent is not a directory: {parent}")
    if path.is_symlink():
        _fail(f"output must not be a symlink: {path}")
    if path.exists():
        if not path.is_file():
            _fail(f"output is not a regular file: {path}")
        if not overwrite:
            _fail(f"output already exists (use --overwrite): {path}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                _fail(f"output already exists (use --overwrite): {path}")
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a fragment directory")
    validate.add_argument("directory")

    render = subparsers.add_parser("render", help="render a fragment directory")
    render.add_argument("directory")
    render.add_argument("output", nargs="?")
    render.add_argument("--output", dest="output_option")
    render.add_argument("--format", choices=("markdown", "json"))
    render.add_argument("--release-id")
    render.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the release-note command-line interface."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        fragments = collect_fragments(arguments.directory)
        if arguments.command == "validate":
            print(f"validated {len(fragments)} release-note fragment(s)")
            return 0

        outputs = [
            value for value in (arguments.output, arguments.output_option) if value
        ]
        if len(outputs) != 1:
            _fail("render requires exactly one output path")
        output = _safe_output_path(outputs[0])
        fragment_directory = Path(arguments.directory).resolve()
        try:
            output.resolve(strict=False).relative_to(fragment_directory)
        except ValueError:
            pass
        else:
            _fail("output path must not be inside the fragment directory")
        output_format = arguments.format
        if output_format is None:
            output_format = "json" if output.suffix == ".json" else "markdown"
        content = (
            render_json(fragments, arguments.release_id)
            if output_format == "json"
            else render_markdown(fragments, arguments.release_id)
        )
        _write_atomic(output, content, overwrite=arguments.overwrite)
        print(f"rendered {len(fragments)} release-note fragment(s) to {output}")
        return 0
    except (OSError, ReleaseNoteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
