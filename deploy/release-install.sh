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
/etc/systemd/system/menhir-caddy-reconcile.path
/etc/systemd/system/menhir-caddy-reconcile.service
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
/srv/menhir/production/bin/caddy-release.sh
/srv/menhir/production/bin/caddy-route-apply
/srv/menhir/production/bin/caddy-route-rollback
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
/srv/menhir/production/bin/release-run
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
/srv/yawn/projects/yawn.deploy/Caddyfile
/srv/yawn/projects/yawn.deploy/check-drift.sh
/srv/yawn/projects/yawn.deploy/docker-compose.yml
/srv/yawn/projects/yawn.deploy/releases.json
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
install -d -o root -g root -m 0700 /var/backups/menhir-install
backup_dir="$(mktemp -d "/var/backups/menhir-install/${release_id}.XXXXXX")"
existing_list="${backup_dir}/existing.list"
created_list="${backup_dir}/created.list"
: > "$existing_list"
: > "$created_list"
mutated=0
operations_was_active=0
caddy_path_was_active=0
if systemctl is-active --quiet menhir-oauth-operations.service; then
    operations_was_active=1
fi
if systemctl is-active --quiet menhir-caddy-reconcile.path; then
    caddy_path_was_active=1
fi

rollback_install() {
    local status="$?" mode destination source temporary
    if [ "$status" -ne 0 ] && [ "$mutated" -eq 1 ]; then
        while IFS= read -r destination; do
            [ -n "$destination" ] && rm -f -- "$destination"
        done < "$created_list"
        while IFS=$'\t' read -r mode destination; do
            [ -n "$destination" ] || continue
            source="${backup_dir}/rootfs${destination}"
            temporary="$(dirname "$destination")/.${destination##*/}.restore.$$"
            cp -a -- "$source" "$temporary"
            mv -fT -- "$temporary" "$destination"
        done < "$existing_list"
        if ! systemctl daemon-reload; then
            echo "warning: systemd could not reload restored unit definitions" >&2
        fi
        if [ "$operations_was_active" -eq 1 ] \
                && ! systemctl restart menhir-oauth-operations.service; then
            echo "warning: restored operations gateway could not be restarted" >&2
        fi
        if [ "$caddy_path_was_active" -eq 1 ] \
                && ! systemctl try-restart menhir-caddy-reconcile.path; then
            echo "warning: restored Caddy reconcile path could not be restarted" >&2
        fi
        echo "installation failed; replaced files restored from ${backup_dir}" >&2
    fi
    rm -f -- "$install_plan"
    exit "$status"
}
trap rollback_install EXIT

while IFS=$'\t' read -r mode destination; do
    [ -n "$destination" ] || continue
    current="/"
    IFS='/' read -r -a components <<< "${destination#/}"
    for component in "${components[@]:0:${#components[@]}-1}"; do
        current="${current%/}/${component}"
        if [ -L "$current" ]; then
            echo "destination parent is a symlink: $current" >&2
            exit 1
        fi
    done
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        [ -f "$destination" ] && [ ! -L "$destination" ] || {
            echo "destination is not a regular non-symlink file: $destination" >&2
            exit 1
        }
        backup="${backup_dir}/rootfs${destination}"
        mkdir -p -- "$(dirname "$backup")"
        cp -a -- "$destination" "$backup"
        printf '%s\t%s\n' "$mode" "$destination" >> "$existing_list"
    else
        printf '%s\n' "$destination" >> "$created_list"
    fi
done < "$install_plan"

mutated=1
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
done < "$install_plan"

systemctl daemon-reload
python3 /srv/menhir/production/bin/menhir_schema.py \
    validate-release /srv/menhir/production/release/release.json
/srv/menhir/production/bin/verify-artifacts
if [ "$operations_was_active" -eq 1 ]; then
    systemctl restart menhir-oauth-operations.service
    systemctl is-active --quiet menhir-oauth-operations.service
fi
if [ "$caddy_path_was_active" -eq 1 ]; then
    systemctl try-restart menhir-caddy-reconcile.path
    systemctl is-active --quiet menhir-caddy-reconcile.path
fi
mutated=0
trap - EXIT
rm -f -- "$install_plan"
echo "installed ${release_id}; replaced files backed up at ${backup_dir}"
echo "production cutover was not started"
