#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"

[ "$#" -eq 2 ] || die "usage: rollback.sh TARGET_GIT_SHA BACKUP_ID"
target_sha=$1
backup_id=$2
validate_sha "$target_sha"
validate_backup_id "$backup_id"
require_command cmp
require_command docker
require_command install
require_command sha256sum
require_command python3
require_command stat
require_command systemctl
require_provisioned_layout

target_release=$AGENTD_RELEASE_ROOT/$target_sha
selected_backup=$AGENTD_BACKUP_ROOT/$backup_id
[ -d "$target_release" ] || die "target release is not installed"
[ -d "$selected_backup" ] || die "selected backup does not exist"
[ "$(cat "$selected_backup/release.sha")" = "$target_sha" ] \
    || die "backup does not belong to the requested release"
target_config=$target_release/deploy/container/config.toml
[ -f "$target_config" ] && [ ! -L "$target_config" ] \
    || die "target release has no regular bundled Codex config"
[ -f "$selected_backup/config.toml" ] && [ ! -L "$selected_backup/config.toml" ] \
    || die "selected backup has no regular Codex config snapshot"
cmp -s "$selected_backup/config.toml" "$target_config" \
    || die "backup Codex config does not match the target release"

was_active=false
if stop_if_active; then
    was_active=true
fi
current_sha=$(current_release_sha)
safety_backup=$(backup_state "before-rollback-to-$target_sha" "$current_sha")

restore_pre_rollback_activation() {
    stop_current_release_containers \
        || die "failed to stop containers before restoring the pre-rollback release"
    restore_state "$safety_backup"
    active_sha=$(current_release_sha)
    if [ -n "$current_sha" ]; then
        if [ "$active_sha" != "$current_sha" ]; then
            atomic_release_link "$current_sha" \
                || die "failed to restore the pre-rollback release link"
        fi
    elif [ -n "$active_sha" ]; then
        [ "$active_sha" = "$target_sha" ] \
            || die "refusing to remove an unexpected current release link"
        unlink "$AGENTD_CURRENT_LINK"
    fi
    if [ "$was_active" = true ]; then
        systemctl --user start "$AGENTD_SERVICE" || true
    fi
}

restore_state "$selected_backup"
if ! atomic_release_link "$target_sha"; then
    restore_pre_rollback_activation
    die "failed to activate the rollback release link"
fi
if ! systemctl --user start "$AGENTD_SERVICE"; then
    restore_pre_rollback_activation
    die "rollback target failed to start; prior release was restored"
fi

printf '%s\n' \
    "Rolled back to $target_sha using $selected_backup" \
    "Pre-rollback safety backup: $safety_backup"
