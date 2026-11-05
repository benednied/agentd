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
require_command stat

owner_record=$(getent passwd 1000 || true)
[ -n "$owner_record" ] || die "UID 1000 must exist before provisioning"
owner_home=$(printf '%s\n' "$owner_record" | awk -F: '{print $6}')
[ "$owner_home" = "$AGENTD_HOME" ] \
    || die "UID 1000 must own the expected home $AGENTD_HOME"

[ -d "$AGENTD_REPOSITORY_ROOT/.git" ] \
    || die "expected Git repository is missing: $AGENTD_REPOSITORY_ROOT"
[ "$(stat -c '%u:%g' "$AGENTD_REPOSITORY_ROOT")" = "1000:1000" ] \
    || die "repository must be owned by UID/GID 1000"

install -d -o 1000 -g 1000 -m 0700 \
    "$AGENTD_STATE_ROOT" \
    "$AGENTD_BACKUP_ROOT" \
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
if [ -e "$AGENTD_CODEX_CONFIG" ] || [ -L "$AGENTD_CODEX_CONFIG" ]; then
    # Release activation owns updates after the initial install so its backup
    # captures the policy paired with the release being replaced.
    require_safe_codex_config
    config_status=preserved
else
    install_config_atomically "$config_source" \
        || die "failed to install reviewed Codex config"
    config_status=installed
fi

if [ -r /proc/sys/kernel/unprivileged_userns_clone ]; then
    [ "$(cat /proc/sys/kernel/unprivileged_userns_clone)" = 1 ] \
        || die "kernel.unprivileged_userns_clone must be 1; not changing sysctls automatically"
fi
if [ -r /proc/sys/user/max_user_namespaces ]; then
    [ "$(cat /proc/sys/user/max_user_namespaces)" -gt 0 ] \
        || die "user.max_user_namespaces must be positive"
fi

unit_dir=$AGENTD_HOME/.config/systemd/user
install -d -o 1000 -g 1000 -m 0700 "$unit_dir"
install -o 1000 -g 1000 -m 0644 \
    "$SCRIPT_DIR/../systemd/$AGENTD_SERVICE" \
    "$unit_dir/$AGENTD_SERVICE"
if [ "${AGENTD_PROFILE:-goldenage}" = goldenage ]; then
    for coding_unit in agentd-worker.service agentd-publisher.service agentd-coding-controller.service; do
        install -o 1000 -g 1000 -m 0644 \
            "$SCRIPT_DIR/../systemd/$coding_unit" "$unit_dir/$coding_unit"
    done
fi

printf '%s\n' \
    "Provisioned exact agentd paths and $config_status reviewed config.toml." \
    "For a new service home, as UID 1000 run: agentd --codex-home $AGENTD_CODEX_HOME codex-reauthorize --device-auth" \
    "As UID 1000, run: systemctl --user daemon-reload"
