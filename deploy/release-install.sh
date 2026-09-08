#!/usr/bin/env bash
set -euo pipefail
umask 077

[ "$#" -eq 0 ] || { echo "release installer accepts no arguments" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "release installer must run as root" >&2; exit 1; }

bundle="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
manifest="${bundle}/bundle-manifest.json"
rootfs="${bundle}/rootfs"
release_source="${rootfs}/srv/menhir/production/release/release.json"
schema_source="${rootfs}/srv/menhir/production/bin/menhir_schema.py"
install_plan="$(mktemp)"
trap 'rm -f -- "$install_plan"' EXIT

python3 - "$bundle" "$manifest" "$install_plan" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys

bundle, manifest_path, plan_path = sys.argv[1:]
release_destination = "/srv/menhir/production/release/release.json"
allowed = frozenset(line for line in """
/etc/sudoers.d/menhir-production
/etc/systemd/system/menhir-oauth-operations.service
/etc/systemd/system/menhir-op@.service
/etc/tmpfiles.d/menhir-production.conf
/etc/yawn-vps/menhir-oauth-policy.json
/etc/yawn-vps/menhir-oauth-public.pem
/etc/yawn-vps/menhir-python-runtime.sha256
/srv/menhir/production/bin/authority_digest.py
/srv/menhir/production/bin/backup
/srv/menhir/production/bin/backup-status
/srv/menhir/production/bin/backup-generation.sh
/srv/menhir/production/bin/backup_cleanup_txn.py
/srv/menhir/production/bin/candidate-accept
/srv/menhir/production/bin/candidate-accept.sh
/srv/menhir/production/bin/candidate-deploy
/srv/menhir/production/bin/candidate-deploy.sh
/srv/menhir/production/bin/generation-inspect
/srv/menhir/production/bin/lib.sh
/srv/menhir/production/bin/logs
/srv/menhir/production/bin/make_manifest.py
/srv/menhir/production/bin/mcp_acceptance_probe.py
/srv/menhir/production/bin/menhir_schema.py
/srv/menhir/production/bin/promote
/srv/menhir/production/bin/promote.sh
/srv/menhir/production/bin/recover
/srv/menhir/production/bin/release-inspect
/srv/menhir/production/bin/release-lib.sh
/srv/menhir/production/bin/release-validate.sh
/srv/menhir/production/bin/release-run.sh
/srv/menhir/production/bin/restore-production
/srv/menhir/production/bin/restore-rehearsal
/srv/menhir/production/bin/restore-generation.sh
/srv/menhir/production/bin/restore_authority_txn.py
/srv/menhir/production/bin/rollback
/srv/menhir/production/bin/rollback.sh
/srv/menhir/production/bin/secrets-map.sh
/srv/menhir/production/bin/same-host-fence.sh
/srv/menhir/production/bin/same_host_fence.py
/srv/menhir/production/bin/stage-generation.sh
/srv/menhir/production/bin/stage_generation.py
/srv/menhir/production/bin/status
/srv/menhir/production/bin/validate_durable_inventory.py
/srv/menhir/production/bin/verify-artifacts
/srv/menhir/production/bin/verify_python_runtime.py
/srv/menhir/production/bin/worker
/srv/menhir/production/deploy/Dockerfile
/srv/menhir/production/deploy/docker-compose.production.yml
/srv/menhir/production/deploy/durable-state-inventory.json
/srv/menhir/production/deploy/installed-artifacts.json
/srv/menhir/production/policy/client-policy.json
/srv/menhir/production/release/production.env
/srv/yawn/projects/yawn.vps/menhir_server.py
/srv/yawn/projects/yawn.vps/vps/core.py
/srv/yawn/projects/yawn.vps/vps/menhir_capabilities.py
/srv/yawn/projects/yawn.vps/vps/menhir_tools.py
/srv/yawn/projects/yawn.vps/vps/oauth_policy.py
/usr/local/sbin/menhir-backup-local
""".splitlines() if line)

def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SystemExit("duplicate bundle JSON key: " + key)
        value[key] = item
    return value

def load(path, label):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("invalid %s JSON: %s" % (label, exc))
    if not isinstance(value, dict):
        raise SystemExit(label + " must be an object")
    return value

def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()

def mode_for(destination):
    if destination in {release_destination,
                       "/srv/menhir/production/release/production.env"}:
        return "0400"
    if destination == "/etc/sudoers.d/menhir-production":
        return "0440"
    if (destination.startswith("/srv/menhir/production/bin/") \
            and not destination.endswith(".py") \
            and destination != "/srv/menhir/production/bin/lib.sh") \
            or destination == "/usr/local/sbin/menhir-backup-local" \
            or destination.endswith("/check-drift.sh"):
        return "0755"
    return "0644"

manifest_info = os.lstat(manifest_path)
if not stat.S_ISREG(manifest_info.st_mode) or stat.S_ISLNK(manifest_info.st_mode):
    raise SystemExit("bundle manifest must be a regular non-symlink file")
installer_path = os.path.join(bundle, "install.sh")
installer_info = os.lstat(installer_path)
if not stat.S_ISREG(installer_info.st_mode) or stat.S_ISLNK(installer_info.st_mode) \
        or stat.S_IMODE(installer_info.st_mode) != 0o755:
    raise SystemExit("bundle installer must be a mode 0755 regular non-symlink file")
manifest = load(manifest_path, "bundle manifest")
manifest_keys = {"schema", "kind", "release_id", "release_sha256", "files"}
if set(manifest) != manifest_keys or manifest.get("schema") != 1 \
        or manifest.get("kind") != "menhir-release-install-bundle":
    raise SystemExit("bundle manifest schema mismatch")
if not re.fullmatch(r"menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+",
                    str(manifest.get("release_id", ""))):
    raise SystemExit("bundle manifest release id is invalid")
if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("release_sha256", ""))):
    raise SystemExit("bundle manifest release digest is invalid")
files = manifest.get("files")
expected_destinations = allowed | {release_destination}
if not isinstance(files, dict) or set(files) != expected_destinations:
    raise SystemExit("bundle manifest destination allowlist mismatch")

expected_files = {"bundle-manifest.json", "install.sh"}
plan = []
for destination in sorted(files):
    if "\\" in destination or not destination.startswith("/") \
            or any(part in {"", ".", ".."} for part in destination.split("/")[1:]):
        raise SystemExit("unsafe bundle destination: " + destination)
    row = files[destination]
    if not isinstance(row, dict) or set(row) != {"mode", "sha256"}:
        raise SystemExit("invalid bundle manifest row: " + destination)
    if row.get("mode") != mode_for(destination) \
            or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", ""))):
        raise SystemExit("invalid bundle mode or digest: " + destination)
    relative = "rootfs" + destination
    expected_files.add(relative)
    source = os.path.join(bundle, relative)
    info = os.lstat(source)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit("unsafe bundle payload: " + destination)
    if stat.S_IMODE(info.st_mode) != int(row["mode"], 8):
        raise SystemExit("bundle payload mode mismatch: " + destination)
    if digest(source) != row["sha256"]:
        raise SystemExit("bundle payload digest mismatch: " + destination)
    plan.append((destination, row["mode"]))

observed_files = set()
for current, directories, names in os.walk(bundle, topdown=True, followlinks=False):
    for name in list(directories):
        path = os.path.join(current, name)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("unsafe bundle directory: " + os.path.relpath(path, bundle))
    for name in names:
        path = os.path.join(current, name)
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise SystemExit("unsafe bundle file: " + os.path.relpath(path, bundle))
        observed_files.add(os.path.relpath(path, bundle).replace(os.sep, "/"))
if observed_files != expected_files:
    raise SystemExit("bundle file census mismatch")

release_path = os.path.join(bundle, "rootfs" + release_destination)
if digest(release_path) != manifest["release_sha256"]:
    raise SystemExit("bundle release digest mismatch")
release = load(release_path, "release authority")
if release.get("release_id") != manifest["release_id"]:
    raise SystemExit("bundle release id mismatch")
artifacts = release.get("artifacts")
if not isinstance(artifacts, dict) or set(artifacts) != allowed:
    raise SystemExit("release artifact allowlist mismatch")
for destination, entry in artifacts.items():
    if not isinstance(entry, dict) or entry.get("sha256") != files[destination]["sha256"]:
        raise SystemExit("release artifact binding mismatch: " + destination)

with open(plan_path, "w", encoding="ascii", newline="\n") as handle:
    for destination, mode in plan:
        handle.write(mode + "\t" + destination + "\n")
PY

python3 "$schema_source" validate-release "$release_source"
visudo -c -f "${rootfs}/etc/sudoers.d/menhir-production" >/dev/null

release_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8"))["release_id"])' "$manifest")"
release_sha="$(sha256sum "$release_source" | cut -d' ' -f1)"
runner_source="${rootfs}/srv/menhir/production/bin/release-run.sh"
runner_sha="$(sha256sum "$runner_source" | cut -d' ' -f1)"
admission_helper="/srv/menhir/scaffold/bin/menhir_scaffold.py"
maintenance_state="/var/lib/menhir-production/release-run.json"
install_root="/var/backups/menhir-install"
transaction_root="${install_root}/active"
journal="${transaction_root}/journal.json"
snapshot_root="${transaction_root}/snapshot"
snapshot_entries="${snapshot_root}/entries.list"
mutation_lock="/run/lock/menhir-production.lock"
transaction_active=0
transaction_step="validating maintenance admission"

require_safe_root_file() {
    local path="$1" label="$2" owner mode
    [ -f "$path" ] && [ ! -L "$path" ] \
        || { echo "$label must be a regular non-symlink file: $path" >&2; return 1; }
    owner="$(stat -c '%u' -- "$path")"
    mode="$(stat -c '%a' -- "$path")"
    [ "$owner" = 0 ] \
        || { echo "$label must be root-owned: $path" >&2; return 1; }
    (( ((8#${mode}) & 8#022) == 0 )) \
        || { echo "$label must not be group/other writable: $path" >&2; return 1; }
}

require_safe_root_directory() {
    local path="$1" label="$2" owner mode
    [ -d "$path" ] && [ ! -L "$path" ] \
        || { echo "$label must be a non-symlink directory: $path" >&2; return 1; }
    owner="$(stat -c '%u' -- "$path")"
    mode="$(stat -c '%a' -- "$path")"
    [ "$owner" = 0 ] \
        || { echo "$label must be root-owned: $path" >&2; return 1; }
    (( ((8#${mode}) & 8#022) == 0 )) \
        || { echo "$label must not be group/other writable: $path" >&2; return 1; }
}

# The installer receives no caller-supplied authority.  It independently binds
# the bundle's exact release and root runner to the already-active, root-owned
# maintenance journal, then asks the installed scaffold authority to prove that
# the matching admission holder still owns its separate lock.
require_safe_root_file "$admission_helper" "maintenance admission authority"
require_safe_root_file "$maintenance_state" "maintenance journal"
IFS=$'\t' read -r approval_sha promotion_attempt_id approved_utc promotion_started_utc \
    < <(python3 - "$maintenance_state" "$release_id" "$release_sha" "$runner_sha" <<'PY'
import json
import os
import re
import stat
import sys

path, release_id, release_sha, runner_sha = sys.argv[1:]

def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SystemExit("duplicate maintenance journal JSON key: " + key)
        value[key] = item
    return value

info = os.lstat(path)
if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) \
        or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
    raise SystemExit("maintenance journal is not a safe root authority")
with open(path, encoding="ascii") as handle:
    value = json.load(handle, object_pairs_hook=unique)
keys = {
    "schema", "kind", "release_id", "release_manifest_sha256", "stage",
    "generation", "started_utc", "updated_utc", "completed_utc",
    "runner_sha256", "approval_sha256", "promotion_attempt_id",
    "approved_utc", "promotion_started_utc",
}
if set(value) != keys or value.get("schema") != 1 \
        or value.get("kind") != "menhir-release-run" \
        or value.get("completed_utc") is not None:
    raise SystemExit("maintenance journal is not an active exact-schema transaction")
expected = {
    "release_id": release_id,
    "release_manifest_sha256": release_sha,
    "runner_sha256": runner_sha,
}
for key, item in expected.items():
    if value.get(key) != item:
        raise SystemExit("maintenance journal bundle binding mismatch: " + key)
if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("approval_sha256", ""))) \
        or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("promotion_attempt_id", ""))):
    raise SystemExit("maintenance journal approval binding is malformed")
fields = ("approval_sha256", "promotion_attempt_id", "approved_utc", "promotion_started_utc")
if any("\t" in str(value[key]) or "\n" in str(value[key]) for key in fields):
    raise SystemExit("maintenance journal binding contains unsafe text")
print("\t".join(str(value[key]) for key in fields))
PY
)
[ -n "$approval_sha" ] && [ -n "$promotion_attempt_id" ] \
    && [ -n "$approved_utc" ] && [ -n "$promotion_started_utc" ] \
    || { echo "maintenance journal binding is incomplete" >&2; exit 1; }
admission_args=(
    --release-id "$release_id"
    --release-manifest-sha256 "$release_sha"
    --runner-sha256 "$runner_sha"
    --approval-sha256 "$approval_sha"
    --promotion-attempt-id "$promotion_attempt_id"
    --approved-utc "$approved_utc"
    --promotion-started-utc "$promotion_started_utc"
)
assert_maintenance() {
    python3 "$admission_helper" assert-maintenance "${admission_args[@]}" >/dev/null
}
assert_maintenance

# Admission is owned by the existing holder.  Taking that lock here would
# deadlock the exact transaction we just authenticated.  Acquire only the
# shared mutation lock, in admission-then-mutation order, and fail promptly if
# another admitted lane is still mutating.
install -d -o root -g root -m 0755 "$(dirname "$mutation_lock")"
exec 9>"$mutation_lock"
flock -n 9 || { echo "maintenance mutation lock is held: $mutation_lock" >&2; exit 75; }
assert_maintenance

operations_was_active=0
if systemctl is-active --quiet menhir-oauth-operations.service; then
    operations_was_active=1
fi
retired_caddy_units=(
    menhir-caddy-reconcile.path
    menhir-caddy-reconcile.service
)
retired_caddy_scripts=(
    /srv/menhir/production/bin/caddy-release.sh
    /srv/menhir/production/bin/caddy-route-apply
    /srv/menhir/production/bin/caddy-route-rollback
)
retired_caddy_routes=(
    /srv/yawn/releases/menhir-route-candidate
)

fsync_directory() {
    python3 - "$1" <<'PY'
import os, sys
descriptor = os.open(sys.argv[1], os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

fsync_tree() {
    python3 - "$1" <<'PY'
import os, stat, sys
root = sys.argv[1]
directories = []
for current, names, files in os.walk(root, topdown=True, followlinks=False):
    directories.append(current)
    for name in files:
        path = os.path.join(current, name)
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
for path in reversed(directories):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

journal_action() { # init|inspect|phase [phase] [journal-path]
    local action="$1" phase="${2:-}" target="${3:-$journal}"
    python3 - "$action" "$target" "$phase" "$release_id" "$release_sha" \
        "$runner_sha" "$approval_sha" "$promotion_attempt_id" \
        "$approved_utc" "$promotion_started_utc" "$operations_was_active" <<'PY'
import datetime
import json
import os
import stat
import sys
import tempfile

action, path, requested, release_id, release_sha, runner_sha, approval_sha, attempt_id, approved_utc, promotion_started_utc, operations = sys.argv[1:]
phases = {
    "snapshotting", "armed", "retiring-caddy", "installing", "verifying",
    "rolling-back", "rolled-back", "committed",
}
keys = {
    "schema", "kind", "release_id", "release_sha256", "runner_sha256",
    "approval_sha256", "promotion_attempt_id", "approved_utc",
    "promotion_started_utc", "operations_was_active", "phase", "started_utc",
    "updated_utc",
}
binding = {
    "release_id": release_id,
    "release_sha256": release_sha,
    "runner_sha256": runner_sha,
    "approval_sha256": approval_sha,
    "promotion_attempt_id": attempt_id,
    "approved_utc": approved_utc,
    "promotion_started_utc": promotion_started_utc,
}

def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SystemExit("duplicate install journal JSON key: " + key)
        value[key] = item
    return value

def load():
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) \
            or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit("install journal is not a safe root-owned file")
    with open(path, encoding="ascii") as handle:
        value = json.load(handle, object_pairs_hook=unique)
    if set(value) != keys or value.get("schema") != 1 \
            or value.get("kind") != "menhir-release-install" \
            or value.get("phase") not in phases \
            or not isinstance(value.get("operations_was_active"), bool):
        raise SystemExit("install journal schema mismatch")
    return value

def atomic(value):
    parent = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".journal.", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

now = datetime.datetime.now(datetime.timezone.utc).isoformat()
if action == "init":
    if os.path.lexists(path):
        raise SystemExit("install journal already exists")
    value = {
        "schema": 1, "kind": "menhir-release-install", **binding,
        "operations_was_active": operations == "1", "phase": "snapshotting",
        "started_utc": now, "updated_utc": now,
    }
    atomic(value)
elif action == "inspect":
    value = load()
    print("\t".join(str(value[key]) for key in (
        "phase", "release_id", "release_sha256", "runner_sha256",
        "approval_sha256", "promotion_attempt_id", "approved_utc",
        "promotion_started_utc",
    )))
    print("1" if value["operations_was_active"] else "0")
elif action == "phase":
    value = load()
    if any(value.get(key) != item for key, item in binding.items()):
        raise SystemExit("install journal belongs to another maintenance binding")
    if bool(value["operations_was_active"]) != (operations == "1"):
        raise SystemExit("install journal service-state binding changed")
    current = value["phase"]
    allowed = {
        "snapshotting": {"snapshotting", "armed"},
        "armed": {"armed", "retiring-caddy", "rolling-back"},
        "retiring-caddy": {"retiring-caddy", "installing", "rolling-back"},
        "installing": {"installing", "verifying", "rolling-back"},
        "verifying": {"verifying", "committed", "rolling-back"},
        "rolling-back": {"rolling-back", "rolled-back"},
        "rolled-back": {"rolled-back", "rolling-back", "armed"},
        "committed": {"committed"},
    }
    if requested not in allowed[current]:
        raise SystemExit("invalid install journal transition: %s -> %s" % (current, requested))
    value["phase"] = requested
    value["updated_utc"] = now
    atomic(value)
else:
    raise SystemExit("unknown install journal action")
PY
}

validate_destination_parents() {
    local destination current component
    while IFS=$'\t' read -r _ destination; do
        [ -n "$destination" ] || continue
        current="/"
        IFS='/' read -r -a components <<< "${destination#/}"
        for component in "${components[@]:0:${#components[@]}-1}"; do
            current="${current%/}/${component}"
            if [ -L "$current" ]; then
                echo "destination parent is a symlink: $current" >&2
                return 1
            fi
        done
        if [ -e "$destination" ] || [ -L "$destination" ]; then
            [ -f "$destination" ] && [ ! -L "$destination" ] || {
                echo "destination is not a regular non-symlink file: $destination" >&2
                return 1
            }
        fi
    done < "$install_plan"
    for destination in "${retired_caddy_routes[@]}"; do
        for current in /srv /srv/yawn /srv/yawn/releases; do
            [ ! -L "$current" ] || {
                echo "retired Menhir route parent is a symlink: $current" >&2
                return 1
            }
        done
    done
    for destination in "${retired_caddy_units[@]}"; do
        current="/etc/systemd/system/${destination}"
        [ ! -d "$current" ] || {
            echo "retired Caddy unit path is a directory: $current" >&2
            return 1
        }
    done
    for destination in "${retired_caddy_scripts[@]}"; do
        [ ! -d "$destination" ] || {
            echo "retired Caddy script path is a directory: $destination" >&2
            return 1
        }
    done
}

snapshot_path() { # key path removal-policy
    local key="$1" path="$2" policy="$3"
    printf '%s\t%s\t%s\n' "$key" "$path" "$policy" >> "$snapshot_entries"
    if [ -e "$path" ] || [ -L "$path" ]; then
        cp -a -- "$path" "${snapshot_root}/files/${key}"
        printf '%s\n' present > "${snapshot_root}/files/${key}.state"
    else
        printf '%s\n' absent > "${snapshot_root}/files/${key}.state"
    fi
}

snapshot_unit() {
    local unit="$1" target="${snapshot_root}/units/${unit}.state"
    systemctl show "$unit" --property=LoadState --property=UnitFileState \
        --property=ActiveState --property=SubState > "$target"
    grep -Eq '^LoadState=(loaded|not-found|masked)$' "$target" \
        || { echo "cannot capture Caddy unit load state: $unit" >&2; return 1; }
    grep -Eq '^UnitFileState=(|alias|disabled|enabled|enabled-runtime|indirect|linked|linked-runtime|masked|masked-runtime|static)$' "$target" \
        || { echo "cannot capture Caddy unit enablement: $unit" >&2; return 1; }
    grep -Eq '^ActiveState=(active|inactive)$' "$target" \
        || { echo "refusing unstable Caddy unit state: $unit" >&2; return 1; }
    grep -Eq '^SubState=[A-Za-z0-9_-]+$' "$target" \
        || { echo "cannot capture Caddy unit substate: $unit" >&2; return 1; }
}

create_snapshot() {
    local index=0 mode destination unit path
    rm -rf -- "$snapshot_root"
    mkdir -m 0700 "$snapshot_root" "${snapshot_root}/files" "${snapshot_root}/units"
    : > "$snapshot_entries"
    while IFS=$'\t' read -r mode destination; do
        [ -n "$destination" ] || continue
        snapshot_path "install-${index}" "$destination" file
        index=$((index + 1))
    done < "$install_plan"
    for unit in "${retired_caddy_units[@]}"; do
        snapshot_path "caddy-${index}" "/etc/systemd/system/${unit}" file
        index=$((index + 1))
        snapshot_unit "$unit"
    done
    for path in "${retired_caddy_scripts[@]}"; do
        snapshot_path "caddy-${index}" "$path" file
        index=$((index + 1))
    done
    for path in "${retired_caddy_routes[@]}"; do
        snapshot_path "caddy-${index}" "$path" tree
        index=$((index + 1))
    done
    fsync_tree "$snapshot_root"
    fsync_directory "$transaction_root"
    journal_action phase armed
}

unit_property() {
    local unit="$1" property="$2"
    sed -n "s/^${property}=//p" "${snapshot_root}/units/${unit}.state"
}

restore_unit_enablement() {
    local unit="$1" state
    state="$(unit_property "$unit" UnitFileState)"
    case "$state" in
        enabled) systemctl enable "$unit" ;;
        enabled-runtime) systemctl enable --runtime "$unit" ;;
        masked) systemctl mask "$unit" ;;
        masked-runtime) systemctl mask --runtime "$unit" ;;
        ""|alias|disabled|indirect|linked|linked-runtime|static) ;;
        *) echo "cannot restore Caddy unit-file state $state for $unit" >&2; return 1 ;;
    esac
}

restore_unit_activity() {
    local unit="$1" state
    state="$(unit_property "$unit" ActiveState)"
    case "$state" in
        active) systemctl start "$unit" ;;
        inactive) systemctl stop "$unit" ;;
        *) echo "cannot restore Caddy active state $state for $unit" >&2; return 1 ;;
    esac
}

verify_restored_units() {
    local unit property expected actual
    for unit in "${retired_caddy_units[@]}"; do
        for property in LoadState UnitFileState ActiveState SubState; do
            expected="$(unit_property "$unit" "$property")"
            actual="$(systemctl show "$unit" --property="$property" --value)"
            [ "$actual" = "$expected" ] || {
                echo "restored Caddy unit $unit $property differs: expected $expected, got $actual" >&2
                return 1
            }
        done
    done
}

rollback_install() {
    local failed=0 key destination policy state source parent unit
    transaction_step="rolling back installation"
    journal_action phase rolling-back || return 1
    set +e
    for unit in "${retired_caddy_units[@]}"; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        systemctl stop "$unit" >/dev/null 2>&1 || true
        systemctl unmask "$unit" >/dev/null 2>&1 || true
    done
    while IFS=$'\t' read -r key destination policy; do
        [ -n "$destination" ] || continue
        if [ "$policy" = tree ]; then
            rm -rf -- "$destination" || failed=1
        elif [ "$policy" = file ]; then
            rm -f -- "$destination" || failed=1
        else
            echo "invalid snapshot removal policy for $destination" >&2
            failed=1
            continue
        fi
        state="$(<"${snapshot_root}/files/${key}.state")"
        if [ "$state" = present ]; then
            source="${snapshot_root}/files/${key}"
            parent="$(dirname "$destination")"
            mkdir -p -- "$parent" || failed=1
            cp -a -- "$source" "$destination" || failed=1
            fsync_directory "$parent" || failed=1
        elif [ "$state" != absent ]; then
            echo "invalid snapshot state for $destination" >&2
            failed=1
        else
            parent="$(dirname "$destination")"
            if [ -d "$parent" ]; then
                fsync_directory "$parent" || failed=1
            fi
        fi
    done < "$snapshot_entries"
    systemctl daemon-reload || failed=1
    for unit in "${retired_caddy_units[@]}"; do
        restore_unit_enablement "$unit" || failed=1
    done
    for unit in "${retired_caddy_units[@]}"; do
        restore_unit_activity "$unit" || failed=1
    done
    verify_restored_units || failed=1
    if [ "$operations_was_active" -eq 1 ]; then
        systemctl restart menhir-oauth-operations.service || failed=1
        systemctl is-active --quiet menhir-oauth-operations.service || failed=1
    fi
    set -e
    if [ "$failed" -ne 0 ]; then
        echo "FATAL: install rollback incomplete; evidence retained at $transaction_root" >&2
        return 1
    fi
    journal_action phase rolled-back
    transaction_active=0
    echo "installation rolled back from durable snapshot at $transaction_root" >&2
}

finish_install() {
    local status="$?" rollback_status=0
    trap - EXIT HUP INT TERM
    if [ "$status" -ne 0 ] && [ "$transaction_active" -eq 1 ]; then
        echo "installation failed during: $transaction_step" >&2
        rollback_install || rollback_status="$?"
        [ "$rollback_status" -eq 0 ] || status=1
    fi
    rm -f -- "$install_plan"
    exit "$status"
}
trap finish_install EXIT
trap 'exit 1' HUP INT TERM

archive_terminal_transaction() {
    local digest target
    digest="$(sha256sum "$journal" | cut -d' ' -f1)"
    install -d -o root -g root -m 0700 "${install_root}/history"
    target="${install_root}/history/${digest}"
    [ ! -e "$target" ] && [ ! -L "$target" ] \
        || { echo "install history target already exists: $target" >&2; return 1; }
    mv -T -- "$transaction_root" "$target"
    fsync_directory "${install_root}/history"
    fsync_directory "$install_root"
}

validate_snapshot_census() {
    local expected_entries index=0 mode destination unit path key policy state
    require_safe_root_directory "$transaction_root" "active install transaction"
    require_safe_root_directory "$snapshot_root" "install snapshot"
    require_safe_root_directory "${snapshot_root}/files" "install file snapshot"
    require_safe_root_directory "${snapshot_root}/units" "install unit snapshot"
    require_safe_root_file "$snapshot_entries" "install snapshot census"
    expected_entries="$(mktemp)"
    while IFS=$'\t' read -r mode destination; do
        [ -n "$destination" ] || continue
        printf 'install-%s\t%s\tfile\n' "$index" "$destination" >> "$expected_entries"
        index=$((index + 1))
    done < "$install_plan"
    for unit in "${retired_caddy_units[@]}"; do
        printf 'caddy-%s\t/etc/systemd/system/%s\tfile\n' "$index" "$unit" >> "$expected_entries"
        index=$((index + 1))
    done
    for path in "${retired_caddy_scripts[@]}"; do
        printf 'caddy-%s\t%s\tfile\n' "$index" "$path" >> "$expected_entries"
        index=$((index + 1))
    done
    for path in "${retired_caddy_routes[@]}"; do
        printf 'caddy-%s\t%s\ttree\n' "$index" "$path" >> "$expected_entries"
        index=$((index + 1))
    done
    if ! cmp -s -- "$expected_entries" "$snapshot_entries"; then
        rm -f -- "$expected_entries"
        echo "durable install snapshot exact-path census mismatch" >&2
        return 1
    fi
    rm -f -- "$expected_entries"
    while IFS=$'\t' read -r key destination policy; do
        require_safe_root_file "${snapshot_root}/files/${key}.state" \
            "install snapshot state" || return 1
        state="$(<"${snapshot_root}/files/${key}.state")"
        case "$state" in
            present)
                [ -e "${snapshot_root}/files/${key}" ] \
                    || [ -L "${snapshot_root}/files/${key}" ] \
                    || { echo "install snapshot payload is missing: $destination" >&2; return 1; }
                ;;
            absent)
                [ ! -e "${snapshot_root}/files/${key}" ] \
                    && [ ! -L "${snapshot_root}/files/${key}" ] \
                    || { echo "absent install snapshot has a payload: $destination" >&2; return 1; }
                ;;
            *) echo "install snapshot state is invalid: $destination" >&2; return 1 ;;
        esac
    done < "$snapshot_entries"
    for unit in "${retired_caddy_units[@]}"; do
        require_safe_root_file "${snapshot_root}/units/${unit}.state" \
            "Caddy unit snapshot" || return 1
    done
}

verify_caddy_retired() {
    local unit path load_state active_state sub_state
    for unit in "${retired_caddy_units[@]}"; do
        path="/etc/systemd/system/${unit}"
        [ ! -e "$path" ] && [ ! -L "$path" ] \
            || { echo "retired Caddy writer definition remains present: $path" >&2; return 1; }
        load_state="$(systemctl show "$unit" --property=LoadState --value)"
        active_state="$(systemctl show "$unit" --property=ActiveState --value)"
        sub_state="$(systemctl show "$unit" --property=SubState --value)"
        [ "$load_state" = not-found ] && [ "$active_state" = inactive ] && [ "$sub_state" = dead ] \
            || { echo "retired Caddy writer remains loaded or active: $unit" >&2; return 1; }
    done
    for path in "${retired_caddy_scripts[@]}" "${retired_caddy_routes[@]}"; do
        [ ! -e "$path" ] && [ ! -L "$path" ] \
            || { echo "retired Caddy state remains present: $path" >&2; return 1; }
    done
}

if [ -e "$install_root" ] || [ -L "$install_root" ]; then
    require_safe_root_directory "$install_root" "install transaction root"
else
    install -d -o root -g root -m 0700 "$install_root"
fi
phase=""
if [ -e "$transaction_root" ] || [ -L "$transaction_root" ]; then
    [ -d "$transaction_root" ] && [ ! -L "$transaction_root" ] \
        || { echo "unsafe active install transaction path" >&2; exit 1; }
    [ -f "$journal" ] && [ ! -L "$journal" ] \
        || { echo "active install transaction has no safe journal; refusing to re-baseline" >&2; exit 1; }
    journal_output="$(journal_action inspect)"
    mapfile -t journal_info <<< "$journal_output"
    IFS=$'\t' read -r phase journal_release_id journal_release_sha journal_runner_sha \
        journal_approval_sha journal_attempt_id journal_approved_utc \
        journal_promotion_started_utc <<< "${journal_info[0]}"
    journal_operations="${journal_info[1]}"
    same_binding=0
    if [ "$journal_release_id" = "$release_id" ] \
            && [ "$journal_release_sha" = "$release_sha" ] \
            && [ "$journal_runner_sha" = "$runner_sha" ] \
            && [ "$journal_approval_sha" = "$approval_sha" ] \
            && [ "$journal_attempt_id" = "$promotion_attempt_id" ] \
            && [ "$journal_approved_utc" = "$approved_utc" ] \
            && [ "$journal_promotion_started_utc" = "$promotion_started_utc" ]; then
        same_binding=1
        operations_was_active="$journal_operations"
    fi
    if [ "$same_binding" -ne 1 ]; then
        case "$phase" in
            committed|rolled-back) archive_terminal_transaction; phase="" ;;
            *) echo "unfinished install transaction belongs to another maintenance binding; refusing to re-baseline" >&2; exit 1 ;;
        esac
    fi
fi

if [ -z "$phase" ]; then
    initial_root="$(mktemp -d "${install_root}/.active.init.XXXXXX")"
    journal_action init snapshotting "${initial_root}/journal.json"
    fsync_tree "$initial_root"
    [ ! -e "$transaction_root" ] && [ ! -L "$transaction_root" ] \
        || { echo "active install transaction appeared concurrently" >&2; exit 75; }
    mv -T -- "$initial_root" "$transaction_root"
    fsync_directory "$install_root"
    phase=snapshotting
fi

validate_destination_parents
case "$phase" in
    snapshotting)
        transaction_step="capturing durable rollback state"
        create_snapshot
        phase=armed
        ;;
    armed|retiring-caddy|installing|verifying|rolling-back)
        validate_snapshot_census
        transaction_active=1
        rollback_install
        echo "interrupted installation recovered; retry the exact bundle to install" >&2
        exit 1
        ;;
    rolled-back)
        validate_snapshot_census
        transaction_active=1
        rollback_install
        journal_action phase armed
        phase=armed
        ;;
    committed)
        validate_snapshot_census
        verify_caddy_retired
        python3 /srv/menhir/production/bin/menhir_schema.py \
            validate-release /srv/menhir/production/release/release.json
        /srv/menhir/production/bin/verify-artifacts
        assert_maintenance
        rm -f -- "$install_plan"
        trap - EXIT HUP INT TERM
        echo "installed ${release_id}; committed transaction at ${transaction_root}"
        echo "production cutover was not started"
        exit 0
        ;;
    *) echo "install journal phase is invalid" >&2; exit 1 ;;
esac

validate_snapshot_census
transaction_active=1

retire_caddy_writers() {
    local unit path parent load_state active_state sub_state
    for unit in "${retired_caddy_units[@]}"; do
        load_state="$(systemctl show "$unit" --property=LoadState --value)"
        active_state="$(systemctl show "$unit" --property=ActiveState --value)"
        sub_state="$(systemctl show "$unit" --property=SubState --value)"
        if [ "$load_state" = "not-found" ]; then
            if [ "$active_state" != "inactive" ] || [ "$sub_state" != "dead" ]; then
                echo "definition-free Caddy writer remains active: $unit" >&2
                return 1
            fi
            continue
        fi
        systemctl disable --now "$unit"
        active_state="$(systemctl show "$unit" --property=ActiveState --value)"
        sub_state="$(systemctl show "$unit" --property=SubState --value)"
        if [ "$active_state" != "inactive" ] || [ "$sub_state" != "dead" ]; then
            echo "retired Caddy writer did not stop cleanly: $unit" >&2
            return 1
        fi
    done

    for unit in "${retired_caddy_units[@]}"; do
        rm -f -- "/etc/systemd/system/${unit}"
    done
    for path in "${retired_caddy_scripts[@]}"; do
        rm -f -- "$path"
    done
    for path in "${retired_caddy_routes[@]}"; do rm -rf -- "$path"; done
    systemctl daemon-reload
    verify_caddy_retired
}

transaction_step="retiring legacy Caddy writers"
journal_action phase retiring-caddy
assert_maintenance
retire_caddy_writers

transaction_step="installing release artifacts"
assert_maintenance
journal_action phase installing
while IFS=$'\t' read -r mode destination; do
    [ -n "$destination" ] || continue
    source="${rootfs}${destination}"
    parent="$(dirname "$destination")"
    mkdir -p -- "$parent"
    temporary="${parent}/.${destination##*/}.install.$$"
    [ ! -e "$temporary" ] && [ ! -L "$temporary" ] || {
        echo "temporary install path already exists: $temporary" >&2
        exit 1
    }
    install -o root -g root -m "$mode" "$source" "$temporary"
    mv -fT -- "$temporary" "$destination"
    fsync_directory "$parent"
done < "$install_plan"

transaction_step="verifying installed release"
assert_maintenance
journal_action phase verifying
systemctl daemon-reload
python3 /srv/menhir/production/bin/menhir_schema.py \
    validate-release /srv/menhir/production/release/release.json
/srv/menhir/production/bin/verify-artifacts
verify_caddy_retired
if [ "$operations_was_active" -eq 1 ]; then
    systemctl restart menhir-oauth-operations.service
    systemctl is-active --quiet menhir-oauth-operations.service
fi
assert_maintenance
journal_action phase committed
transaction_active=0
trap - EXIT HUP INT TERM
rm -f -- "$install_plan"
echo "installed ${release_id}; durable rollback evidence retained at ${transaction_root}"
echo "production cutover was not started"
