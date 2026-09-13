#!/usr/bin/env bash
set -euo pipefail
umask 077

[ "$(id -u)" -eq 0 ] || { echo "scaffold installer must run as root" >&2; exit 1; }

recover_only=0
transaction_id=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --recover)
            recover_only=1
            shift
            ;;
        --transaction-id)
            [ "$#" -ge 2 ] || { echo "--transaction-id requires a value" >&2; exit 2; }
            transaction_id="$2"
            shift 2
            ;;
        *)
            echo "usage: install.sh [--recover] [--transaction-id <32 lowercase hex>]" >&2
            exit 2
            ;;
    esac
done

if [ "$recover_only" -eq 0 ]; then
    [[ "$transaction_id" =~ ^[a-f0-9]{32}$ ]] || {
        echo "scaffold install requires an explicit 32-character transaction id" >&2
        exit 2
    }
elif [ -n "$transaction_id" ]; then
    echo "--recover selects the one durable active transaction; do not supply an id" >&2
    exit 2
fi

bundle="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
manifest="${bundle}/bundle-manifest.json"
transactions_root="/srv/menhir/scaffold-transactions"
active_root="${transactions_root}/active-install"
preparing_root="${transactions_root}/preparing-install"
history_root="${transactions_root}/history"
admission_lock="/run/lock/menhir-production-admission.lock"
production_lock="/run/lock/menhir-production.lock"
manifest_sha=""
preflight_root=""
staged_bundle=""
transaction_root=""
transaction_active=0
transaction_step="preflight"
requested_transaction_id="$transaction_id"

bundle_files=(
    contract.production.json menhir_scaffold.py menhir_app_only.py
    menhir_security_config.py personal_stage_vps.py
    scheduled-backup.sh menhir-backup.service menhir-backup.timer
    menhir-scaffold-audit.service menhir-scaffold-audit.timer
    menhir-scaffold.sudoers install.sh
)
# systemd's Persistent= catch-up uses this stamp's mtime as the last trigger.
# A never-run timer has no stamp, so starting it fires the backup immediately.
backup_timer_stamp="/var/lib/systemd/timers/stamp-menhir-backup.timer"
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
    status_root
)
directory_paths=(
    /srv/menhir/scaffold/bin
    /srv/menhir/scaffold
    /srv/menhir/staging-transactions
    /srv/menhir/install-transactions
    /var/lib/menhir-production
)
units=(
    menhir-backup.service
    menhir-backup.timer
    menhir-scaffold-audit.service
    menhir-scaffold-audit.timer
)

validate_bundle() {
    local root="$1" manifest_path="$2" label="$3"
    python3 - "$root" "$manifest_path" "$label" <<'PY'
import hashlib,json,os,stat,sys
root,manifest,label=sys.argv[1:]
def unique(pairs):
    value={}
    for key,item in pairs:
        if key in value: raise SystemExit("duplicate "+label+" manifest key: "+key)
        value[key]=item
    return value
root_info=os.stat(root, follow_symlinks=False)
if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != 0 or root_info.st_gid != 0 \
        or root_info.st_mode & 0o022:
    raise SystemExit(label+" root must be a root-owned directory not writable by group or other")
value=json.load(open(manifest,encoding="utf-8"),object_pairs_hook=unique)
expected={"contract.production.json","menhir_scaffold.py","menhir_app_only.py","menhir_security_config.py","personal_stage_vps.py","scheduled-backup.sh","menhir-backup.service","menhir-backup.timer","menhir-scaffold-audit.service","menhir-scaffold-audit.timer","menhir-scaffold.sudoers","install.sh"}
if set(value)!={"schema","kind","files"} or value["schema"]!=1 or value["kind"]!="menhir-scaffold-bundle" or set(value["files"])!=expected:
    raise SystemExit(label+" manifest mismatch")
for name,want in value["files"].items():
    path=os.path.join(root,name); info=os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
        raise SystemExit("unsafe "+label+" file: "+name)
    got=hashlib.sha256(open(path,"rb").read()).hexdigest()
    if got!=want: raise SystemExit(label+" digest mismatch: "+name)
print(hashlib.sha256(open(manifest,"rb").read()).hexdigest())
PY
}

write_state() {
    local phase="$1"
    python3 - "$transaction_root/state.json" "$transaction_id" "$manifest_sha" "$phase" <<'PY'
import json,os,sys,tempfile
path,transaction_id,manifest_sha,phase=sys.argv[1:]
allowed={"snapshotted","mutating","rollback-started","rollback-failed","rolled-back","committed"}
if phase not in allowed: raise SystemExit("invalid scaffold transaction phase")
value={"schema":1,"kind":"menhir-scaffold-install","transaction_id":transaction_id,
       "bundle_manifest_sha256":manifest_sha,"phase":phase}
parent=os.path.dirname(path); fd,tmp=tempfile.mkstemp(prefix=".state-",dir=parent,text=True)
try:
    with os.fdopen(fd,"w",encoding="ascii") as handle:
        json.dump(value,handle,sort_keys=True,separators=(",",":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.chmod(tmp,0o600); os.replace(tmp,path)
    directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PY
}

load_active_state() {
    python3 - "$active_root/state.json" <<'PY'
import json,os,re,stat,sys
path=sys.argv[1]; root=os.path.dirname(path)
info=os.lstat(root)
if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_gid!=0 or info.st_mode & 0o077:
    raise SystemExit("unsafe active scaffold transaction root")
info=os.lstat(path)
if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_gid!=0 or info.st_mode & 0o077:
    raise SystemExit("unsafe active scaffold transaction state")
with open(path,encoding="ascii") as handle: value=json.load(handle)
if set(value)!={"schema","kind","transaction_id","bundle_manifest_sha256","phase"} \
        or value.get("schema")!=1 or value.get("kind")!="menhir-scaffold-install" \
        or re.fullmatch(r"[a-f0-9]{32}",str(value.get("transaction_id",""))) is None \
        or re.fullmatch(r"[a-f0-9]{64}",str(value.get("bundle_manifest_sha256",""))) is None \
        or value.get("phase") not in {"snapshotted","mutating","rollback-started","rollback-failed","rolled-back","committed"}:
    raise SystemExit("active scaffold transaction state is invalid")
print(value["transaction_id"],value["bundle_manifest_sha256"],value["phase"])
PY
}

prepare_bundle() {
    [ -f "$manifest" ] && [ ! -L "$manifest" ] || { echo "bundle manifest missing" >&2; exit 1; }
    command -v visudo >/dev/null || { echo "visudo is required" >&2; exit 1; }
    preflight_root="$(mktemp -d /var/tmp/menhir-scaffold-preflight.XXXXXX)"
    case "$preflight_root" in
        /var/tmp/menhir-scaffold-preflight.*) ;;
        *) echo "unsafe scaffold preflight path" >&2; exit 1 ;;
    esac
    staged_bundle="${preflight_root}/bundle"
    install -d -o root -g root -m 0700 "$staged_bundle"
    for name in "${bundle_files[@]}"; do
        install -o root -g root -m 0600 "${bundle}/${name}" "${staged_bundle}/${name}"
    done
    install -o root -g root -m 0600 "$manifest" "${staged_bundle}/bundle-manifest.json"
    manifest_sha="$(validate_bundle "$staged_bundle" "${staged_bundle}/bundle-manifest.json" "staged scaffold bundle")"
    install -o root -g root -m 0440 \
        "${staged_bundle}/menhir-scaffold.sudoers" "${preflight_root}/candidate.sudoers"
    if ! visudo -c -f "${preflight_root}/candidate.sudoers" >/dev/null; then
        echo "candidate scaffold sudoers validation failed" >&2
        exit 1
    fi
}

acquire_shared_locks() {
    for path in "$admission_lock" "$production_lock"; do
        [ ! -L "$path" ] || { echo "refusing symlink lock path: $path" >&2; return 1; }
    done
    exec 9>>"$admission_lock"
    flock -n 9 || { echo "another Menhir production lane owns admission" >&2; exit 75; }
    exec 8>>"$production_lock"
    flock -n 8 || { echo "another Menhir production mutation is active" >&2; exit 75; }
    chown root:root "$admission_lock" "$production_lock"
    chmod 0600 "$admission_lock" "$production_lock"
}

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

unit_property() {
    local unit="$1" property="$2"
    sed -n "s/^${property}=//p" "${transaction_root}/units/${unit}.state"
}

snapshot_unit() {
    local unit="$1" load_state unit_state active_state
    systemctl show "$unit" --property=LoadState --property=UnitFileState \
        --property=ActiveState >"${transaction_root}/units/${unit}.state"
    load_state="$(unit_property "$unit" LoadState)"
    unit_state="$(unit_property "$unit" UnitFileState)"
    active_state="$(unit_property "$unit" ActiveState)"
    case "${load_state}:${unit_state}" in
        not-found:|loaded:disabled|loaded:enabled|loaded:enabled-runtime|loaded:static|masked:masked|masked:masked-runtime) ;;
        *) echo "refusing unit state that rollback cannot reproduce exactly: $unit (${load_state}:${unit_state})" >&2; return 1 ;;
    esac
    case "$active_state" in
        active|inactive) ;;
        *) echo "refusing transaction while $unit is not in a stable state" >&2; return 1 ;;
    esac
}

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

restore_unit_enablement() {
    local unit="$1" state
    state="$(unit_property "$unit" UnitFileState)"
    case "$state" in
        enabled) systemctl enable "$unit" ;;
        enabled-runtime) systemctl enable --runtime "$unit" ;;
        ""|disabled|masked|masked-runtime|static) ;;
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

verify_restored_unit() {
    local unit="$1" property expected actual
    for property in LoadState UnitFileState ActiveState; do
        expected="$(unit_property "$unit" "$property")"
        actual="$(systemctl show "$unit" --property="$property" --value)"
        [ "$actual" = "$expected" ] || {
            echo "rollback did not restore $unit $property (expected '$expected', got '$actual')" >&2
            return 1
        }
    done
}

archive_transaction() {
    local disposition="$1" target
    install -d -o root -g root -m 0700 "$history_root"
    target="${history_root}/${transaction_id}-${disposition}"
    [ ! -e "$target" ] && [ ! -L "$target" ] || {
        echo "scaffold transaction history already exists: $target" >&2
        return 1
    }
    rm -rf -- "${transaction_root}/bundle"
    mv -- "$transaction_root" "$target"
    transaction_root="$target"
}

rollback() {
    local rollback_failed=0 index unit
    set +e
    echo "scaffold installation failed during: ${transaction_step}" >&2
    echo "restoring prior scaffold from ${transaction_root}" >&2
    write_state rollback-started || rollback_failed=1

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
    for unit in "${units[@]}"; do
        verify_restored_unit "$unit" || rollback_failed=1
    done
    for index in "${!directory_paths[@]}"; do
        restore_directory "${directory_keys[$index]}" \
            "${directory_paths[$index]}" || rollback_failed=1
    done

    if [ "$rollback_failed" -eq 0 ]; then
        write_state rolled-back || rollback_failed=1
    else
        write_state rollback-failed || true
    fi
    if [ "$rollback_failed" -eq 0 ]; then
        archive_transaction rolled-back || rollback_failed=1
        echo "prior scaffold and exact unit state restored; rollback evidence archived at ${transaction_root}" >&2
    else
        echo "scaffold rollback incomplete; recovery evidence retained at ${transaction_root}" >&2
    fi
    return "$rollback_failed"
}

recover_active() {
    local recovered_state
    [ -d "$active_root" ] && [ ! -L "$active_root" ] || {
        [ ! -e "$active_root" ] && [ ! -L "$active_root" ] && return 0
        echo "unsafe active scaffold transaction path" >&2
        return 1
    }
    read -r transaction_id manifest_sha recovered_state < <(load_active_state)
    transaction_root="$active_root"
    case "$recovered_state" in
        committed)
            /srv/menhir/scaffold/bin/menhir_scaffold.py verify
            rm -rf -- "$transaction_root"
            transaction_root=""
            ;;
        rolled-back)
            archive_transaction rolled-back
            ;;
        snapshotted|mutating|rollback-started|rollback-failed)
            transaction_step="recovering interrupted ${recovered_state} transaction"
            rollback
            ;;
        *)
            echo "active scaffold transaction phase is not recoverable" >&2
            return 1
            ;;
    esac
}

finish() {
    local status="$?" rollback_status=0
    trap - EXIT HUP INT TERM
    if [ "$transaction_active" -eq 1 ]; then
        rollback || rollback_status="$?"
        if [ "$rollback_status" -ne 0 ]; then
            status=1
        fi
    fi
    if [ -n "$preflight_root" ] && [ -d "$preflight_root" ]; then
        rm -rf -- "$preflight_root"
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 1' HUP INT TERM

if [ "$recover_only" -eq 0 ]; then
    prepare_bundle
fi

acquire_shared_locks
install -d -o root -g root -m 0700 "$transactions_root"
recover_active

if [ "$recover_only" -eq 1 ]; then
    echo "durable scaffold recovery complete"
    exit 0
fi

[ ! -L "$preparing_root" ] || {
    echo "unsafe preparing scaffold transaction path" >&2
    exit 1
}
transaction_id="$requested_transaction_id"
manifest_sha="$(validate_bundle "$staged_bundle" "${staged_bundle}/bundle-manifest.json" "staged scaffold bundle")"
rm -rf -- "$preparing_root"
install -d -o root -g root -m 0700 "$preparing_root"
transaction_root="$preparing_root"
install -d -o root -g root -m 0700 \
    "${transaction_root}/files" "${transaction_root}/directories" \
    "${transaction_root}/units" "${transaction_root}/bundle"
for name in "${bundle_files[@]}"; do
    install -o root -g root -m 0600 "${staged_bundle}/${name}" \
        "${transaction_root}/bundle/${name}"
done
install -o root -g root -m 0600 "${staged_bundle}/bundle-manifest.json" \
    "${transaction_root}/bundle/bundle-manifest.json"
staged_bundle="${transaction_root}/bundle"
validate_bundle "$staged_bundle" "${staged_bundle}/bundle-manifest.json" \
    "durable scaffold bundle" >/dev/null

for index in "${!file_paths[@]}"; do
    snapshot_file "${file_keys[$index]}" "${file_paths[$index]}"
done
for index in "${!directory_paths[@]}"; do
    snapshot_directory "${directory_keys[$index]}" "${directory_paths[$index]}"
done
for unit in "${units[@]}"; do
    snapshot_unit "$unit"
done

write_state snapshotted
mv -- "$preparing_root" "$active_root"
transaction_root="$active_root"
staged_bundle="${transaction_root}/bundle"
transaction_active=1
transaction_step="beginning scaffold installation"
write_state mutating

transaction_step="creating scaffold directories"
install -d -o root -g root -m 0755 /srv/menhir/scaffold /srv/menhir/scaffold/bin
install -d -o root -g root -m 0700 \
    /srv/menhir/staging-transactions \
    /srv/menhir/install-transactions

transaction_step="installing scaffold contract and executables"
install -o root -g root -m 0400 "${staged_bundle}/contract.production.json" /etc/menhir/scaffold-contract.json
install -o root -g root -m 0755 "${staged_bundle}/menhir_scaffold.py" /srv/menhir/scaffold/bin/menhir_scaffold.py
install -o root -g root -m 0755 "${staged_bundle}/menhir_app_only.py" /srv/menhir/scaffold/bin/menhir_app_only.py
install -o root -g root -m 0755 "${staged_bundle}/menhir_security_config.py" /srv/menhir/scaffold/bin/menhir_security_config.py
install -o root -g root -m 0755 "${staged_bundle}/personal_stage_vps.py" /srv/menhir/scaffold/bin/menhir_stage_vps.py

transaction_step="installing nightly backup wrapper and units"
install -o root -g root -m 0755 "${staged_bundle}/scheduled-backup.sh" /usr/local/sbin/menhir-scheduled-backup
install -o root -g root -m 0644 "${staged_bundle}/menhir-backup.service" /etc/systemd/system/menhir-backup.service
install -o root -g root -m 0644 "${staged_bundle}/menhir-backup.timer" /etc/systemd/system/menhir-backup.timer

transaction_step="installing scaffold audit units and sudoers"
install -o root -g root -m 0644 "${staged_bundle}/menhir-scaffold-audit.service" /etc/systemd/system/menhir-scaffold-audit.service
install -o root -g root -m 0644 "${staged_bundle}/menhir-scaffold-audit.timer" /etc/systemd/system/menhir-scaffold-audit.timer
install -o root -g root -m 0440 "${preflight_root}/candidate.sudoers" /etc/sudoers.d/menhir-scaffold
transaction_step="validating installed scaffold sudoers"
visudo -c -f /etc/sudoers.d/menhir-scaffold

transaction_step="reloading systemd"
systemctl daemon-reload
transaction_step="enabling scaffold audit timer"
systemctl enable --now menhir-scaffold-audit.timer
transaction_step="enabling nightly backup timer"
if [ ! -e "$backup_timer_stamp" ]; then
    # First installation: record now as the last trigger so the timer waits
    # for its next 04:00 window instead of taking a backup mid-install.
    install -d -o root -g root -m 0755 "$(dirname -- "$backup_timer_stamp")"
    touch -- "$backup_timer_stamp"
fi
systemctl enable --now menhir-backup.timer
transaction_step="capturing scaffold receipt"
/srv/menhir/scaffold/bin/menhir_scaffold.py capture
transaction_step="seeding scaffold restore drill"
/srv/menhir/scaffold/bin/menhir_scaffold.py seed-drill
transaction_step="verifying installed scaffold"
/srv/menhir/scaffold/bin/menhir_scaffold.py verify

write_state committed
transaction_active=0
rm -rf -- "$transaction_root"
transaction_root=""
echo "scaffold installation committed"
