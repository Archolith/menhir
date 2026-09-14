#!/usr/bin/env python3
"""Apply bundle-manifest modes to a staged install bundle on the host.

scp from Windows drops POSIX modes, so a bundle copied into
/srv/menhir/staging-transactions/<release> has to be given the modes its
manifest declares before release-install.sh will accept it. Run as root:

    python3 apply_modes.py /srv/menhir/staging-transactions/release-<id>
"""
import json
import os
import sys

bundle = os.path.realpath(sys.argv[1])
manifest = json.load(open(os.path.join(bundle, "bundle-manifest.json"), encoding="utf-8"))
rootfs = os.path.join(bundle, "rootfs")
for destination, info in manifest["files"].items():
    # The manifest is operator-built, but never let it point outside the
    # bundle or through a symlink: chmod follows links.
    parts = destination.split("/")
    if not destination.startswith("/") or any(part in ("", ".", "..") for part in parts[1:]):
        raise SystemExit(f"unsafe destination in manifest: {destination}")
    target = os.path.join(rootfs, destination.lstrip("/"))
    if os.path.commonpath([rootfs, os.path.realpath(target)]) != rootfs:
        raise SystemExit(f"destination escapes the bundle: {destination}")
    if os.path.islink(target) or not os.path.isfile(target):
        raise SystemExit(f"destination is not a regular file: {destination}")
    mode = int(str(info["mode"]), 8)
    if mode not in (0o400, 0o444, 0o600, 0o644, 0o755):
        raise SystemExit(f"unexpected mode {oct(mode)} for {destination}")
    os.chmod(target, mode)
for current, _dirs, _files in os.walk(os.path.join(bundle, "rootfs")):
    os.chmod(current, 0o755)
os.chmod(bundle, 0o755)
os.chmod(os.path.join(bundle, "install.sh"), 0o755)
os.chmod(os.path.join(bundle, "bundle-manifest.json"), 0o644)
print("modes applied:", len(manifest["files"]))
