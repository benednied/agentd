#!/bin/sh

# Shared deployment primitives. Callers must use `set -eu` before sourcing.

AGENTD_HOME=/home/bened
AGENTD_REPOSITORY_ROOT=$AGENTD_HOME/goldenage
AGENTD_STATE_ROOT=$AGENTD_HOME/.local/state/agentd
AGENTD_DB=$AGENTD_STATE_ROOT/state.sqlite
AGENTD_SHARE_ROOT=$AGENTD_HOME/.local/share/agentd
AGENTD_SOURCE_ROOT=$AGENTD_SHARE_ROOT/source
AGENTD_WORKSPACE_ROOT=$AGENTD_SHARE_ROOT/workspaces
AGENTD_CODEX_HOME=$AGENTD_SHARE_ROOT/codex-home
AGENTD_CODEX_CONFIG=$AGENTD_CODEX_HOME/config.toml
AGENTD_UV_CACHE=$AGENTD_HOME/.cache/uv
AGENTD_RELEASE_ROOT=$AGENTD_SHARE_ROOT/releases
AGENTD_CURRENT_LINK=$AGENTD_SHARE_ROOT/current
AGENTD_BACKUP_ROOT=$AGENTD_STATE_ROOT/backups
AGENTD_SERVICE=agentd.service
AGENTD_SQLITE_TOOL=$SCRIPT_DIR/../security/sqlite_snapshot.py

die() {
    printf '%s\n' "agentd deployment: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

validate_sha() {
    case "$1" in
        *[!0-9a-f]*|'') die "release SHA must be 40 lowercase hexadecimal characters" ;;
    esac
    [ "${#1}" -eq 40 ] || die "release SHA must contain exactly 40 characters"
}

validate_backup_id() {
    case "$1" in
        *[!A-Za-z0-9._-]*|'') die "invalid backup identifier" ;;
    esac
}

require_provisioned_layout() {
    for directory in \
        "$AGENTD_STATE_ROOT" \
        "$AGENTD_WORKSPACE_ROOT" \
        "$AGENTD_CODEX_HOME" \
        "$AGENTD_UV_CACHE" \
        "$AGENTD_RELEASE_ROOT" \
        "$AGENTD_BACKUP_ROOT" \
        "$AGENTD_REPOSITORY_ROOT"
    do
        [ -d "$directory" ] || die "run provision-host.sh first; missing $directory"
        [ ! -L "$directory" ] || die "managed directory must not be a symlink: $directory"
    done
}

current_release_sha() {
    if [ ! -L "$AGENTD_CURRENT_LINK" ]; then
        return 0
    fi
    current_target=$(readlink "$AGENTD_CURRENT_LINK")
    case "$current_target" in
        releases/*) printf '%s\n' "${current_target#releases/}" ;;
        *) die "current release link has an unexpected target" ;;
    esac
}

atomic_release_link() {
    release_sha=$1
    validate_sha "$release_sha"
    [ -d "$AGENTD_RELEASE_ROOT/$release_sha" ] \
        || die "release does not exist: $release_sha"
    temporary_link=$AGENTD_SHARE_ROOT/.current-$release_sha-$$
    [ ! -e "$temporary_link" ] && [ ! -L "$temporary_link" ] \
        || die "temporary release link already exists"
    ln -s "releases/$release_sha" "$temporary_link"
    mv -Tf "$temporary_link" "$AGENTD_CURRENT_LINK"
}

backup_state() {
    reason=$1
    release_sha=$2
    timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    backup_id=$timestamp-$reason-$$
    validate_backup_id "$backup_id"
    backup_dir=$AGENTD_BACKUP_ROOT/$backup_id
    install -d -m 0700 "$backup_dir"
    printf '%s\n' "${release_sha:-none}" >"$backup_dir/release.sha"
    chmod 0600 "$backup_dir/release.sha"

    if [ -f "$AGENTD_DB" ]; then
        python3 "$AGENTD_SQLITE_TOOL" backup \
            "$AGENTD_DB" "$backup_dir/state.sqlite" \
            || die "SQLite backup failed integrity validation"
        chmod 0600 "$backup_dir/state.sqlite"
        sha256sum "$backup_dir/state.sqlite" >"$backup_dir/SHA256SUMS"
        chmod 0600 "$backup_dir/SHA256SUMS"
    else
        : >"$backup_dir/NO_DATABASE"
        chmod 0600 "$backup_dir/NO_DATABASE"
    fi
    printf '%s\n' "$backup_dir"
}

restore_state() {
    backup_dir=$1
    [ -d "$backup_dir" ] || die "backup directory does not exist"
    [ -f "$backup_dir/state.sqlite" ] \
        || die "selected backup has no database snapshot"
    (
        cd "$backup_dir"
        sha256sum --check SHA256SUMS >/dev/null
    ) || die "backup checksum validation failed"
    python3 "$AGENTD_SQLITE_TOOL" verify "$backup_dir/state.sqlite" \
        || die "backup database failed integrity validation"
    restore_tmp=$AGENTD_STATE_ROOT/.state.sqlite.restore-$$
    [ ! -e "$restore_tmp" ] || die "temporary restore target already exists"
    python3 "$AGENTD_SQLITE_TOOL" backup \
        "$backup_dir/state.sqlite" "$restore_tmp" \
        || die "failed to create restored SQLite snapshot"
    chmod 0600 "$restore_tmp"
    # The service is stopped before every restore. Remove sidecars from the
    # displaced database so SQLite cannot replay an obsolete WAL into the
    # newly restored snapshot when the service starts again.
    rm -f -- "$AGENTD_DB-wal" "$AGENTD_DB-shm"
    mv -f "$restore_tmp" "$AGENTD_DB"
}

stop_if_active() {
    if systemctl --user is-active --quiet "$AGENTD_SERVICE"; then
        systemctl --user stop "$AGENTD_SERVICE"
        return 0
    fi
    return 1
}
