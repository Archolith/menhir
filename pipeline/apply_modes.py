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

bundle = sys.argv[1]
manifest = json.load(open(os.path.join(bundle, "bundle-manifest.json"), encoding="utf-8"))
for destination, info in manifest["files"].items():
    os.chmod(os.path.join(bundle, "rootfs" + destination), int(str(info["mode"]), 8))
for current, _dirs, _files in os.walk(os.path.join(bundle, "rootfs")):
    os.chmod(current, 0o755)
os.chmod(bundle, 0o755)
os.chmod(os.path.join(bundle, "install.sh"), 0o755)
os.chmod(os.path.join(bundle, "bundle-manifest.json"), 0o644)
print("modes applied:", len(manifest["files"]))
