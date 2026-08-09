#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"

provisioning_uid=$(id -u)
case "$provisioning_uid" in
    0|1000) ;;
    *) die "trusted host provisioning must run as root or the exact UID 1000 owner" ;;
esac
require_command getent
require_command install
require_command python3
require_command stat

owner_record=$(getent passwd 1000 || true)
[ -n "$owner_record" ] || die "UID 1000 must exist before provisioning"
owner_home=$(printf '%s\n' "$owner_record" | awk -F: '{print $6}')
[ "$owner_home" = "$AGENTD_HOME" ] \
    || die "UID 1000 must own the expected home $AGENTD_HOME"

[ -d "$AGENTD_REPOSITORY_ROOT/.git" ] \
    || die "expected Git repository is missing: $AGENTD_REPOSITORY_ROOT"
[ "$(stat -c '%u:%g' "$AGENTD_REPOSITORY_ROOT")" = "1000:1000" ] \
    || die "goldenage must be owned by UID/GID 1000"

install -d -o 1000 -g 1000 -m 0700 \
    "$AGENTD_STATE_ROOT" \
    "$AGENTD_BACKUP_ROOT" \
    "$AGENTD_STATE_ROOT/auth-backups" \
    "$AGENTD_UV_CACHE" \
    "$AGENTD_WORKSPACE_ROOT" \
    "$AGENTD_CODEX_HOME"
install -d -o 1000 -g 1000 -m 0750 \
    "$AGENTD_SHARE_ROOT" \
    "$AGENTD_RELEASE_ROOT"
if [ ! -e "$AGENTD_DB" ]; then
    install -o 1000 -g 1000 -m 0600 /dev/null "$AGENTD_DB"
fi

config_source=$SCRIPT_DIR/../container/config.toml
[ -f "$config_source" ] && [ ! -L "$config_source" ] \
    || die "reviewed nonsecret Codex config.toml is missing"
config_tmp=$AGENTD_CODEX_HOME/.config.toml.install-$$
trap 'case "$config_tmp" in /home/bened/.local/share/agentd/codex-home/.config.toml.install-*) rm -f -- "$config_tmp" ;; esac' EXIT HUP INT TERM
install -o 1000 -g 1000 -m 0600 "$config_source" "$config_tmp"
mv -f "$config_tmp" "$AGENTD_CODEX_HOME/config.toml"
chown 1000:1000 "$AGENTD_CODEX_HOME/config.toml"
chmod 0600 "$AGENTD_CODEX_HOME/config.toml"
trap - EXIT HUP INT TERM

if [ -r /proc/sys/kernel/unprivileged_userns_clone ]; then
    [ "$(cat /proc/sys/kernel/unprivileged_userns_clone)" = 1 ] \
        || die "kernel.unprivileged_userns_clone must be 1; not changing sysctls automatically"
fi
if [ -r /proc/sys/user/max_user_namespaces ]; then
    [ "$(cat /proc/sys/user/max_user_namespaces)" -gt 0 ] \
        || die "user.max_user_namespaces must be positive"
fi

: "${AGENTD_AUTH_SOURCE:?set AGENTD_AUTH_SOURCE to the reviewed auth.json source}"
[ -f "$AGENTD_AUTH_SOURCE" ] || die "AGENTD_AUTH_SOURCE is not a regular file"
[ ! -L "$AGENTD_AUTH_SOURCE" ] || die "AGENTD_AUTH_SOURCE must not be a symlink"
[ "$(basename -- "$AGENTD_AUTH_SOURCE")" = auth.json ] \
    || die "AGENTD_AUTH_SOURCE must name auth.json"
[ "$(stat -c %s "$AGENTD_AUTH_SOURCE")" -le 1048576 ] \
    || die "auth.json is unexpectedly large"
python3 - "$AGENTD_AUTH_SOURCE" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict) or not payload:
    raise SystemExit("auth.json must contain a nonempty JSON object")
PY

target_auth=$AGENTD_CODEX_HOME/auth.json
if [ -f "$target_auth" ]; then
    auth_backup=$AGENTD_STATE_ROOT/auth-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$-auth.json
    install -o 1000 -g 1000 -m 0600 "$target_auth" "$auth_backup"
fi
auth_tmp=$AGENTD_CODEX_HOME/.auth.json.install-$$
trap 'case "$auth_tmp" in /home/bened/.local/share/agentd/codex-home/.auth.json.install-*) rm -f -- "$auth_tmp" ;; esac' EXIT HUP INT TERM
install -o 1000 -g 1000 -m 0600 "$AGENTD_AUTH_SOURCE" "$auth_tmp"
mv -f "$auth_tmp" "$target_auth"
chown 1000:1000 "$target_auth"
chmod 0600 "$target_auth"
trap - EXIT HUP INT TERM

unit_dir=$AGENTD_HOME/.config/systemd/user
install -d -o 1000 -g 1000 -m 0700 "$unit_dir"
install -o 1000 -g 1000 -m 0644 \
    "$SCRIPT_DIR/../systemd/agentd.service" \
    "$unit_dir/agentd.service"

printf '%s\n' \
    "Provisioned exact agentd paths, reviewed config.toml, and auth.json." \
    "As UID 1000, run: systemctl --user daemon-reload"
