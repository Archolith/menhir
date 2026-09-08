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
expected={"contract.production.json","menhir_scaffold.py","menhir_app_only.py","menhir_security_config.py","personal_stage_vps.py","menhir-scaffold-audit.service","menhir-scaffold-audit.timer","menhir-scaffold.sudoers","install.sh"}
if set(value)!={"schema","kind","files"} or value["schema"]!=1 or value["kind"]!="menhir-scaffold-bundle" or set(value["files"])!=expected:
    raise SystemExit("scaffold bundle manifest mismatch")
for name,want in value["files"].items():
    path=os.path.join(root,name); info=os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit("unsafe scaffold bundle file: "+name)
    got=hashlib.sha256(open(path,"rb").read()).hexdigest()
    if got!=want: raise SystemExit("scaffold bundle digest mismatch: "+name)
PY

command -v visudo >/dev/null || { echo "visudo is required" >&2; exit 1; }
transaction_root="$(mktemp -d /var/tmp/menhir-scaffold-install.XXXXXX)"
case "$transaction_root" in
    /var/tmp/menhir-scaffold-install.*) ;;
    *) echo "unsafe scaffold transaction path" >&2; exit 1 ;;
esac

cleanup_preflight() {
    local status="$?"
    trap - EXIT HUP INT TERM
    rm -rf -- "$transaction_root"
    exit "$status"
}
trap cleanup_preflight EXIT
trap 'exit 1' HUP INT TERM

staged_bundle="${transaction_root}/bundle"
mkdir -m 0700 "$staged_bundle"
bundle_files=(
    contract.production.json menhir_scaffold.py menhir_app_only.py
    menhir_security_config.py personal_stage_vps.py
    menhir-scaffold-audit.service menhir-scaffold-audit.timer
    menhir-scaffold.sudoers install.sh
)
for name in "${bundle_files[@]}"; do
    cp -a -- "${bundle}/${name}" "${staged_bundle}/${name}"
done
cp -a -- "$manifest" "${staged_bundle}/bundle-manifest.json"

python3 - "$staged_bundle" "${staged_bundle}/bundle-manifest.json" <<'PY'
import hashlib,json,os,stat,sys
root,manifest=sys.argv[1:]
def unique(pairs):
    value={}
    for key,item in pairs:
        if key in value: raise SystemExit("duplicate staged scaffold bundle manifest key: "+key)
        value[key]=item
    return value
value=json.load(open(manifest,encoding="utf-8"),object_pairs_hook=unique)
expected={"contract.production.json","menhir_scaffold.py","menhir_app_only.py","menhir_security_config.py","personal_stage_vps.py","menhir-scaffold-audit.service","menhir-scaffold-audit.timer","menhir-scaffold.sudoers","install.sh"}
if set(value)!={"schema","kind","files"} or value["schema"]!=1 or value["kind"]!="menhir-scaffold-bundle" or set(value["files"])!=expected:
    raise SystemExit("staged scaffold bundle manifest mismatch")
for name,want in value["files"].items():
    path=os.path.join(root,name); info=os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit("unsafe staged scaffold bundle file: "+name)
    got=hashlib.sha256(open(path,"rb").read()).hexdigest()
    if got!=want: raise SystemExit("staged scaffold bundle digest mismatch: "+name)
PY

install -o root -g root -m 0440 \
    "${staged_bundle}/menhir-scaffold.sudoers" "${transaction_root}/candidate.sudoers"
if ! visudo -c -f "${transaction_root}/candidate.sudoers" >/dev/null; then
    echo "candidate scaffold sudoers validation failed" >&2
    exit 1
fi

file_keys=(
    contract scaffold app_only security_config stage_vps scheduled_backup
    backup_service backup_timer audit_service audit_timer sudoers
    scaffold_receipt drill_receipt
)
file_paths=(
    /etc/menhir/scaffold-contract.json
    /srv/menhir/scaffold/bin/menhir_scaffold.py
    /srv/menhir/scaffold/bin/menhir_app_only.py
    /srv/menhir/scaffold/bin/menhir_security_config.py
    /srv/menhir/scaffold/bin/menhir_stage_vps.py
    /usr/local/sbin/menhir-scheduled-backup
    /etc/systemd/system/menhir-backup.service
    /etc/systemd/system/menhir-backup.timer
    /etc/systemd/system/menhir-scaffold-audit.service
    /etc/systemd/system/menhir-scaffold-audit.timer
    /etc/sudoers.d/menhir-scaffold
    /var/lib/menhir-production/scaffold-receipt.json
    /var/lib/menhir-production/scaffold-restore-drill-receipt.json
)
directory_keys=(
    scaffold_bin scaffold_root staging_transactions install_transactions
    scaffold_transactions status_root
)
directory_paths=(
    /srv/menhir/scaffold/bin
    /srv/menhir/scaffold
    /srv/menhir/staging-transactions
    /srv/menhir/install-transactions
    /srv/menhir/scaffold-transactions
    /var/lib/menhir-production
)
units=(
    menhir-backup.service
    menhir-backup.timer
    menhir-scaffold-audit.service
    menhir-scaffold-audit.timer
)

mkdir -m 0700 "${transaction_root}/files" "${transaction_root}/directories" \
    "${transaction_root}/units"

snapshot_file() {
    local key="$1" path="$2"
    if [ -e "$path" ] || [ -L "$path" ]; then
        [ ! -d "$path" ] || {
            echo "refusing to replace directory at scaffold file target: $path" >&2
            return 1
        }
        cp -a -- "$path" "${transaction_root}/files/${key}"
        printf '%s\n' present >"${transaction_root}/files/${key}.state"
    else
        printf '%s\n' absent >"${transaction_root}/files/${key}.state"
    fi
}

snapshot_directory() {
    local key="$1" path="$2"
    if [ -e "$path" ] || [ -L "$path" ]; then
        [ -d "$path" ] && [ ! -L "$path" ] || {
            echo "unsafe scaffold directory target: $path" >&2
            return 1
        }
        printf 'present %s\n' "$(stat -c '%u %g %a' -- "$path")" \
            >"${transaction_root}/directories/${key}.state"
    else
        printf '%s\n' absent >"${transaction_root}/directories/${key}.state"
    fi
}

snapshot_unit() {
    local unit="$1"
    systemctl show "$unit" --property=LoadState --property=UnitFileState \
        --property=ActiveState >"${transaction_root}/units/${unit}.state"
    grep -Eq '^LoadState=(loaded|not-found|masked)$' \
        "${transaction_root}/units/${unit}.state" || {
        echo "cannot capture load state for $unit" >&2
        return 1
    }
    grep -Eq '^UnitFileState=(|alias|disabled|enabled|enabled-runtime|indirect|linked|linked-runtime|masked|masked-runtime|static)$' \
        "${transaction_root}/units/${unit}.state" || {
        echo "cannot capture unit-file state for $unit" >&2
        return 1
    }
    grep -Eq '^ActiveState=(active|inactive)$' \
        "${transaction_root}/units/${unit}.state" || {
        echo "refusing transaction while $unit is not in a stable state" >&2
        return 1
    }
}

for index in "${!file_paths[@]}"; do
    snapshot_file "${file_keys[$index]}" "${file_paths[$index]}"
done
for index in "${!directory_paths[@]}"; do
    snapshot_directory "${directory_keys[$index]}" "${directory_paths[$index]}"
done
for unit in "${units[@]}"; do
    snapshot_unit "$unit"
done

restore_file() {
    local key="$1" path="$2" state
    state="$(<"${transaction_root}/files/${key}.state")"
    rm -f -- "$path" || return 1
    if [ "$state" = present ]; then
        cp -a -- "${transaction_root}/files/${key}" "$path"
    elif [ "$state" != absent ]; then
        echo "invalid rollback state for $path" >&2
        return 1
    fi
}

restore_directory() {
    local key="$1" path="$2" state owner group mode
    read -r state owner group mode <"${transaction_root}/directories/${key}.state"
    if [ "$state" = present ]; then
        chown "$owner:$group" -- "$path" && chmod "$mode" -- "$path"
    elif [ "$state" = absent ]; then
        rmdir -- "$path" 2>/dev/null || [ ! -e "$path" ]
    else
        echo "invalid rollback directory state for $path" >&2
        return 1
    fi
}

unit_property() {
    local unit="$1" property="$2"
    sed -n "s/^${property}=//p" "${transaction_root}/units/${unit}.state"
}

restore_unit_enablement() {
    local unit="$1" state
    state="$(unit_property "$unit" UnitFileState)"
    case "$state" in
        enabled) systemctl enable "$unit" ;;
        enabled-runtime) systemctl enable --runtime "$unit" ;;
        masked-runtime) systemctl mask --runtime "$unit" ;;
        ""|alias|disabled|indirect|linked|linked-runtime|masked|static) ;;
        *) echo "cannot restore unit-file state $state for $unit" >&2; return 1 ;;
    esac
}

restore_unit_activity() {
    local unit="$1" state
    state="$(unit_property "$unit" ActiveState)"
    case "$state" in
        active) systemctl start "$unit" ;;
        inactive) ;;
        *) echo "cannot restore active state $state for $unit" >&2; return 1 ;;
    esac
}

rollback() {
    local rollback_failed=0 index unit
    set +e
    echo "scaffold installation failed during: ${transaction_step}" >&2
    echo "restoring prior scaffold from ${transaction_root}" >&2

    for unit in "${units[@]}"; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        systemctl stop "$unit" >/dev/null 2>&1 || true
    done
    for index in "${!file_paths[@]}"; do
        restore_file "${file_keys[$index]}" "${file_paths[$index]}" || rollback_failed=1
    done
    systemctl daemon-reload || rollback_failed=1
    for unit in "${units[@]}"; do
        restore_unit_enablement "$unit" || rollback_failed=1
    done
    for unit in "${units[@]}"; do
        restore_unit_activity "$unit" || rollback_failed=1
    done
    for index in "${!directory_paths[@]}"; do
        restore_directory "${directory_keys[$index]}" \
            "${directory_paths[$index]}" || rollback_failed=1
    done

    if [ "$rollback_failed" -eq 0 ]; then
        echo "prior scaffold files and unit state restored; rollback evidence retained at ${transaction_root}" >&2
    else
        echo "scaffold rollback incomplete; recovery evidence retained at ${transaction_root}" >&2
    fi
    return "$rollback_failed"
}

finish() {
    local status="$?" rollback_status=0
    trap - EXIT HUP INT TERM
    if [ "$transaction_active" -eq 1 ]; then
        rollback || rollback_status="$?"
        if [ "$rollback_status" -ne 0 ]; then
            status=1
        fi
    elif [ "$status" -eq 0 ]; then
        rm -rf -- "$transaction_root" || \
            echo "warning: committed scaffold backup remains at ${transaction_root}" >&2
    fi
    exit "$status"
}
transaction_active=1
transaction_step="beginning scaffold installation"
trap finish EXIT
trap 'exit 1' HUP INT TERM

transaction_step="creating scaffold directories"
install -d -o root -g root -m 0755 /srv/menhir/scaffold /srv/menhir/scaffold/bin
install -d -o root -g root -m 0700 \
    /srv/menhir/staging-transactions \
    /srv/menhir/install-transactions \
    /srv/menhir/scaffold-transactions

transaction_step="installing scaffold contract and executables"
install -o root -g root -m 0400 "${staged_bundle}/contract.production.json" /etc/menhir/scaffold-contract.json
install -o root -g root -m 0755 "${staged_bundle}/menhir_scaffold.py" /srv/menhir/scaffold/bin/menhir_scaffold.py
install -o root -g root -m 0755 "${staged_bundle}/menhir_app_only.py" /srv/menhir/scaffold/bin/menhir_app_only.py
install -o root -g root -m 0755 "${staged_bundle}/menhir_security_config.py" /srv/menhir/scaffold/bin/menhir_security_config.py
install -o root -g root -m 0755 "${staged_bundle}/personal_stage_vps.py" /srv/menhir/scaffold/bin/menhir_stage_vps.py

transaction_step="retiring legacy scaffold backup files"
backup_load_state="$(unit_property menhir-backup.timer LoadState)"
if [ "$backup_load_state" != not-found ]; then
    systemctl disable --now menhir-backup.timer
fi
rm -f /usr/local/sbin/menhir-scheduled-backup \
    /etc/systemd/system/menhir-backup.service \
    /etc/systemd/system/menhir-backup.timer

transaction_step="installing scaffold audit units and sudoers"
install -o root -g root -m 0644 "${staged_bundle}/menhir-scaffold-audit.service" /etc/systemd/system/menhir-scaffold-audit.service
install -o root -g root -m 0644 "${staged_bundle}/menhir-scaffold-audit.timer" /etc/systemd/system/menhir-scaffold-audit.timer
install -o root -g root -m 0440 "${transaction_root}/candidate.sudoers" /etc/sudoers.d/menhir-scaffold
transaction_step="validating installed scaffold sudoers"
visudo -c -f /etc/sudoers.d/menhir-scaffold

transaction_step="reloading systemd"
systemctl daemon-reload
transaction_step="enabling scaffold audit timer"
systemctl enable --now menhir-scaffold-audit.timer
transaction_step="capturing scaffold receipt"
/srv/menhir/scaffold/bin/menhir_scaffold.py capture
transaction_step="seeding scaffold restore drill"
/srv/menhir/scaffold/bin/menhir_scaffold.py seed-drill
transaction_step="verifying installed scaffold"
/srv/menhir/scaffold/bin/menhir_scaffold.py verify

transaction_active=0
echo "scaffold installation committed"
