#!/usr/bin/env bash
# Explicit secret owner/mode map and its enforcement/verification (blocker 1).
#
# Owners: Menhir/OAuth secrets are readable only by GID 10001; neo4j-auth is
# readable only by GID 7474. Parents are traversable but not listable by the
# other service, so there is no cross-service leakage.
#
#   secrets/                    0711 root:root   (traversable, not listable)
#   secrets/neo4j/              0750 root:7474
#   secrets/neo4j/neo4j-auth    0440 root:7474
#   secrets/menhir/             0750 root:10001
#   secrets/menhir/*            0440 root:10001
#   secrets/oauth/              0750 root:10001
#   secrets/oauth/*             0440 root:10001
#
# Sourced (not executed) by the lifecycle scripts; also runnable with
# `enforce <root>` / `verify <root>` for standalone use.
set -euo pipefail

# shellcheck disable=SC2034
SECRETS_UID=0

_enforce_dir() { # path owner group mode
    local p="$1" o="$2" g="$3" m="$4"
    [ -d "$p" ] || install -d -o "$o" -g "$g" -m "$m" "$p"
    chown "$o:$g" "$p" && chmod "$m" "$p"
}

_verify_perm() { # path expected_mode expected_uid expected_gid label
    local p="$1" m="$2" u="$3" g="$4" label="$5"
    [ -e "$p" ] || { echo "$label missing: $p" >&2; return 1; }
    [ ! -L "$p" ] || { echo "$label must not be a symlink: $p" >&2; return 1; }
    local am au ag normalized_mode
    am="$(stat -c '%a' "$p")"; au="$(stat -c '%u' "$p")"; ag="$(stat -c '%g' "$p")"
    normalized_mode="${m#0}"
    [ "$am" = "$normalized_mode" ] \
        || { echo "$label mode $am != $normalized_mode: $p" >&2; return 1; }
    [ "$au" = "$u" ] || { echo "$label uid $au != $u: $p" >&2; return 1; }
    [ "$ag" = "$g" ] || { echo "$label gid $ag != $g: $p" >&2; return 1; }
}

secrets_enforce() { # root
    local root="$1"
    _enforce_dir "$root"                0 0     0711
    _enforce_dir "$root/neo4j"          0 7474  0750
    _enforce_dir "$root/menhir"         0 10001 0750
    _enforce_dir "$root/oauth"          0 10001 0750
    # Files (only those that exist; the required set is checked separately).
    if [ -f "$root/neo4j/neo4j-auth" ]; then
        chown 0:7474 "$root/neo4j/neo4j-auth" && chmod 0440 "$root/neo4j/neo4j-auth"
    fi
    local f
    for f in "$root"/menhir/*; do
        [ -f "$f" ] || continue
        chown 0:10001 "$f" && chmod 0440 "$f"
    done
    for f in "$root"/oauth/*; do
        [ -f "$f" ] || continue
        chown 0:10001 "$f" && chmod 0440 "$f"
    done
}

secrets_verify() { # root
    local root="$1"
    _verify_perm "$root"              0711 0 0     "secrets root"
    _verify_perm "$root/neo4j"        0750 0 7474  "secrets/neo4j"
    _verify_perm "$root/menhir"       0750 0 10001 "secrets/menhir"
    _verify_perm "$root/oauth"        0750 0 10001 "secrets/oauth"
    [ -f "$root/neo4j/neo4j-auth" ] || { echo "missing secrets/neo4j/neo4j-auth" >&2; return 1; }
    _verify_perm "$root/neo4j/neo4j-auth" 0440 0 7474 "neo4j-auth"
    local f
    for f in "$root"/menhir/* "$root"/oauth/*; do
        [ -f "$f" ] || continue
        _verify_perm "$f" 0440 0 10001 "menhir/oauth secret"
    done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    [ "$#" -eq 2 ] || { echo "usage: $0 <enforce|verify> <secrets-root>" >&2; exit 2; }
    case "$1" in
        enforce) secrets_enforce "$2" ;;
        verify) secrets_verify "$2" ;;
        *) echo "usage: $0 <enforce|verify> <secrets-root>" >&2; exit 2 ;;
    esac
fi
