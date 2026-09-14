#!/usr/bin/env bash
# Host side of an artifact-only Menhir release install. Run as the operator
# login (sudo -n to root for each privileged step); driven by
# scripts/menhir-release-install.ps1, which uploads the bundle and this file.
#
# Steps, in the order releases 15-18 ran them:
#   1. stage the uploaded bundle root-owned under /srv/menhir/staging-transactions
#      and restore the manifest modes scp dropped (pipeline/apply_modes.py)
#   2. begin-maintenance: bind the journal to the release, runner and approval
#   3. release-install.sh as a oneshot unit (snapshot -> retire -> place ->
#      verify -> commit, or roll back); it takes no outage
#   4. verify-artifacts, live release digest, readyz
#   5. complete-maintenance --artifact-only (proven completion)
#   6. scaffold audit. It goes RED after every artifact-only release until a
#      rehearsal has run under the new release, because the rehearsal receipt
#      binds the release manifest; the nightly backup rehearses, or pass
#      MENHIR_CEREMONY_BACKUP=1 to run one now (~3 minutes of downtime).
#
# Required environment (the desktop script sets it from the release workspace):
#   MENHIR_RELEASE_ID          menhir-prod-0.2.0-N
#   MENHIR_RELEASE_SHA256      release-flow.json release_sha256
#   MENHIR_RUNNER_SHA256       release.json artifacts[.../bin/release-run.sh].sha256
#   MENHIR_APPROVAL_SHA256     release-flow.json security_review_sha256
#   MENHIR_APPROVED_UTC        security-review.json reviewed_utc
#   MENHIR_ATTEMPT_ID          32 lowercase hex, minted once per attempt
set -u

for name in MENHIR_RELEASE_ID MENHIR_RELEASE_SHA256 MENHIR_RUNNER_SHA256 \
            MENHIR_APPROVAL_SHA256 MENHIR_APPROVED_UTC MENHIR_ATTEMPT_ID; do
    [ -n "${!name:-}" ] || { echo "FATAL $name is not set" >&2; exit 2; }
done
[[ "$MENHIR_RELEASE_ID" =~ ^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$ ]] \
    || { echo "FATAL release id is malformed" >&2; exit 2; }
[[ "$MENHIR_ATTEMPT_ID" =~ ^[a-f0-9]{32}$ ]] || { echo "FATAL attempt id must be 32 hex" >&2; exit 2; }

UPLOAD="/home/thron/.menhir-release-upload"
STAGE="/srv/menhir/staging-transactions/release-${MENHIR_RELEASE_ID}"
SCAFFOLD=/srv/menhir/scaffold/bin/menhir_scaffold.py
JOURNAL=/var/lib/menhir-production/release-run.json
BIND=(--release-id "$MENHIR_RELEASE_ID" --release-manifest-sha256 "$MENHIR_RELEASE_SHA256"
      --runner-sha256 "$MENHIR_RUNNER_SHA256" --approval-sha256 "$MENHIR_APPROVAL_SHA256"
      --promotion-attempt-id "$MENHIR_ATTEMPT_ID" --approved-utc "$MENHIR_APPROVED_UTC")

say() { printf '== %s\n' "$*"; }
journal_field() { sudo -n python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$JOURNAL" "$1" 2>/dev/null; }

say "preflight"
[ -d "$UPLOAD/install-bundle" ] || { echo "FATAL no uploaded bundle at $UPLOAD/install-bundle" >&2; exit 1; }
[ -f "$UPLOAD/apply_modes.py" ] || { echo "FATAL apply_modes.py not uploaded" >&2; exit 1; }
if [ -f "$JOURNAL" ]; then
    stage="$(journal_field stage)"; current="$(journal_field release_id)"
    if [ "$stage" != "complete" ] && [ "$current" != "$MENHIR_RELEASE_ID" ]; then
        echo "FATAL maintenance for $current is open at stage $stage; complete or abandon it first" >&2
        exit 1
    fi
fi
sudo -n flock -n /run/lock/menhir-production-admission.lock true \
    || echo "note: admission is held (a previous attempt of this release?); begin-maintenance will decide"

say "1. stage bundle"
sudo -n rm -rf -- "$STAGE"
sudo -n cp -a --no-preserve=ownership "$UPLOAD/install-bundle" "$STAGE"
sudo -n chown -R root:root "$STAGE"
sudo -n python3 "$UPLOAD/apply_modes.py" "$STAGE" || { echo "FATAL apply_modes failed" >&2; exit 1; }
manifest="$(sudo -n sha256sum "$STAGE/bundle-manifest.json" | cut -d' ' -f1)"
echo "bundle-manifest sha256 $manifest"

say "2. begin-maintenance"
if [ "$(journal_field release_id)" = "$MENHIR_RELEASE_ID" ] && [ "$(journal_field stage)" = "start" ]; then
    echo "journal already open for this release at stage start; reusing it"
else
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    sudo -n "$SCAFFOLD" begin-maintenance "${BIND[@]}" --promotion-started-utc "$started" >/tmp/menhir-begin.out 2>&1 \
        || { cat /tmp/menhir-begin.out; echo "FATAL begin-maintenance refused" >&2; exit 1; }
fi
echo "journal stage $(journal_field stage), admission $(systemctl is-active menhir-maintenance-admission.service)"

say "3. release-install.sh"
unit="menhir-release-install-${MENHIR_RELEASE_ID##menhir-prod-}-${MENHIR_ATTEMPT_ID:0:6}"
sudo -n systemd-run --unit="$unit" --collect --wait --property=Type=oneshot \
    --property=StandardOutput=journal --property=StandardError=journal \
    bash "$STAGE/install.sh" >/tmp/menhir-install.out 2>&1
install_rc=$?
sudo -n journalctl -u "$unit" --no-pager -o cat | grep -vE '^OK /' | grep -E 'installed menhir|failed during|FATAL|rollback|refus|REFUSED' | tail -5
if [ "$install_rc" -ne 0 ]; then
    echo "FATAL installer exited $install_rc; the transaction rolled back or refused (journal above). Maintenance stays open." >&2
    exit 1
fi

say "4. verify"
sudo -n /srv/menhir/production/bin/verify-artifacts >/tmp/menhir-verify.out 2>&1
verify_rc=$?
echo "verify-artifacts exit $verify_rc, $(grep -c '^OK' /tmp/menhir-verify.out) OK"
live="$(sudo -n sha256sum /srv/menhir/production/release/release.json | cut -d' ' -f1)"
[ "$live" = "$MENHIR_RELEASE_SHA256" ] && echo "live release is $MENHIR_RELEASE_ID" \
    || { echo "FATAL live release digest $live is not this release" >&2; exit 1; }
curl -s -o /tmp/readyz.out -w 'readyz=%{http_code}\n' https://memory.ctharvey.me/readyz
docker ps --format '{{.Names}} {{.Status}}' | grep menhir-prod
[ "$verify_rc" -eq 0 ] || { echo "FATAL verify-artifacts failed after a committed install; inspect /tmp/menhir-verify.out" >&2; exit 1; }

say "5. complete-maintenance --artifact-only"
promotion_started="$(journal_field promotion_started_utc | sed 's/+00:00$/Z/')"
sudo -n "$SCAFFOLD" complete-maintenance --artifact-only "${BIND[@]}" --promotion-started-utc "$promotion_started" \
    >/tmp/menhir-complete.out 2>&1 || { cat /tmp/menhir-complete.out; echo "FATAL complete-maintenance refused" >&2; exit 1; }
echo "journal stage $(journal_field stage), admission $(systemctl is-active menhir-maintenance-admission.service)"

if [ "${MENHIR_CEREMONY_BACKUP:-0}" = "1" ]; then
    say "6a. backup + rehearsal under the new release (about 3 minutes of downtime)"
    sudo -n systemctl start menhir-backup.service
    echo "backup unit $(systemctl show menhir-backup.service -p Result --value)"
    sudo -n cat /var/lib/menhir-production/scheduled-backup-last-run.json; echo
fi

say "6. scaffold audit"
sudo -n systemctl reset-failed menhir-scaffold-audit.service 2>/dev/null
sudo -n systemctl start menhir-scaffold-audit.service
audit="$(systemctl show menhir-scaffold-audit.service -p Result --value)"
echo "audit $audit"
if [ "$audit" != "success" ]; then
    sudo -n journalctl -u menhir-scaffold-audit.service -n 20 --no-pager -o cat | grep -E 'REFUSED' | tail -1
    echo "note: red until a rehearsal runs under $MENHIR_RELEASE_ID (nightly 09:00 UTC, or rerun with MENHIR_CEREMONY_BACKUP=1)"
fi

say "cleanup"
rm -rf -- "$UPLOAD"
sudo -n ls /srv/menhir/staging-transactions/
echo "done: $MENHIR_RELEASE_ID installed; staging bundle kept at $STAGE until the next release"
