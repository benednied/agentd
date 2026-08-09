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
require_command sha256sum
require_command python3
require_command systemctl
require_provisioned_layout

target_release=$AGENTD_RELEASE_ROOT/$target_sha
selected_backup=$AGENTD_BACKUP_ROOT/$backup_id
[ -d "$target_release" ] || die "target release is not installed"
[ -d "$selected_backup" ] || die "selected backup does not exist"
[ "$(cat "$selected_backup/release.sha")" = "$target_sha" ] \
    || die "backup does not belong to the requested release"

was_active=false
if stop_if_active; then
    was_active=true
fi
current_sha=$(current_release_sha)
safety_backup=$(backup_state "before-rollback-to-$target_sha" "$current_sha")

restore_state "$selected_backup"
atomic_release_link "$target_sha"
if ! systemctl --user start "$AGENTD_SERVICE"; then
    if [ -n "$current_sha" ]; then
        restore_state "$safety_backup"
        atomic_release_link "$current_sha"
        if [ "$was_active" = true ]; then
            systemctl --user start "$AGENTD_SERVICE" || true
        fi
    fi
    die "rollback target failed to start; prior release was restored"
fi

printf '%s\n' \
    "Rolled back to $target_sha using $selected_backup" \
    "Pre-rollback safety backup: $safety_backup"
