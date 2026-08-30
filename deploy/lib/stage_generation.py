#!/usr/bin/env python3
"""Safely extract one decrypted Menhir generation archive into a fixed root."""

from __future__ import annotations

import argparse
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def stage(archive: Path, generation: str, destination_root: Path) -> Path:
    if not generation.startswith("generation.") or not generation[11:].isalnum():
        raise ValueError("generation id is invalid")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / generation
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("existing decrypted generation destination is unsafe")
        return destination
    temporary = Path(tempfile.mkdtemp(prefix=".stage-generation-", dir=destination_root))
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if not members:
                raise ValueError("generation archive is empty")
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts \
                        or path.parts[0] != generation:
                    raise ValueError(f"unsafe or cross-generation archive member: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise ValueError(f"archive contains a link or special entry: {member.name}")
            bundle.extractall(temporary, filter="data")
        extracted = temporary / generation
        if not extracted.is_dir() or extracted.is_symlink():
            raise ValueError("archive did not contain the expected generation root")
        os.replace(extracted, destination)
        if os.name != "nt":
            directory = os.open(destination_root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return destination
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("generation")
    parser.add_argument("destination_root", type=Path)
    args = parser.parse_args()
    print(stage(args.archive, args.generation, args.destination_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
