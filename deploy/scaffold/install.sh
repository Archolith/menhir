#!/usr/bin/env bash
set -euo pipefail
umask 077

[ "$(id -u)" -eq 0 ] || { echo "scaffold installer must run as root" >&2; exit 1; }
bundle="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
manifest="${bundle}/bundle-manifest.json"
[ -f "$manifest" ] && [ ! -L "$manifest" ] || { echo "bundle manifest missing" >&2; exit 1; }

python3 - "$bundle" "$manifest" <<'PY'
import hashlib,json,os,stat,sys
root,manifest=sys.argv[1:]
def unique(pairs):
    value={}
    for key,item in pairs:
        if key in value: raise SystemExit("duplicate scaffold bundle manifest key: "+key)
        value[key]=item
    return value
value=json.load(open(manifest,encoding="utf-8"),object_pairs_hook=unique)
expected={"contract.production.json","menhir_scaffold.py","menhir-scaffold-audit.service","menhir-scaffold-audit.timer","menhir-scaffold.sudoers","install.sh"}
if set(value)!={"schema","kind","files"} or value["schema"]!=1 or value["kind"]!="menhir-scaffold-bundle" or set(value["files"])!=expected:
    raise SystemExit("scaffold bundle manifest mismatch")
for name,want in value["files"].items():
    path=os.path.join(root,name); info=os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit("unsafe scaffold bundle file: "+name)
    got=hashlib.sha256(open(path,"rb").read()).hexdigest()
    if got!=want: raise SystemExit("scaffold bundle digest mismatch: "+name)
PY

install -d -o root -g root -m 0755 /srv/menhir/scaffold /srv/menhir/scaffold/bin
install -o root -g root -m 0400 "${bundle}/contract.production.json" /etc/menhir/scaffold-contract.json
install -o root -g root -m 0755 "${bundle}/menhir_scaffold.py" /srv/menhir/scaffold/bin/menhir_scaffold.py
systemctl disable --now menhir-backup.timer 2>/dev/null || true
rm -f /usr/local/sbin/menhir-scheduled-backup \
    /etc/systemd/system/menhir-backup.service \
    /etc/systemd/system/menhir-backup.timer
install -o root -g root -m 0644 "${bundle}/menhir-scaffold-audit.service" /etc/systemd/system/menhir-scaffold-audit.service
install -o root -g root -m 0644 "${bundle}/menhir-scaffold-audit.timer" /etc/systemd/system/menhir-scaffold-audit.timer
install -o root -g root -m 0440 "${bundle}/menhir-scaffold.sudoers" /etc/sudoers.d/menhir-scaffold
visudo -c -f /etc/sudoers.d/menhir-scaffold
systemctl daemon-reload
systemctl enable --now menhir-scaffold-audit.timer
/srv/menhir/scaffold/bin/menhir_scaffold.py capture
/srv/menhir/scaffold/bin/menhir_scaffold.py seed-drill
/srv/menhir/scaffold/bin/menhir_scaffold.py verify
