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

require_safe_codex_config() {
    [ -f "$AGENTD_CODEX_CONFIG" ] && [ ! -L "$AGENTD_CODEX_CONFIG" ] \
        || die "reviewed Codex config must be a regular non-symlink file"
    [ "$(stat -c '%u:%g' "$AGENTD_CODEX_CONFIG")" = "1000:1000" ] \
        || die "reviewed Codex config must be owned by UID/GID 1000"
    [ "$(stat -c '%a' "$AGENTD_CODEX_CONFIG")" = "600" ] \
        || die "reviewed Codex config must have mode 0600"
}

install_config_atomically() {
    config_source=$1
    [ -f "$config_source" ] && [ ! -L "$config_source" ] || return 1
    config_tmp=$AGENTD_CODEX_HOME/.config.toml.install-$$
    [ ! -e "$config_tmp" ] && [ ! -L "$config_tmp" ] || return 1
    if ! install -o 1000 -g 1000 -m 0600 "$config_source" "$config_tmp"; then
        rm -f -- "$config_tmp"
        return 1
    fi
    if ! mv -f "$config_tmp" "$AGENTD_CODEX_CONFIG"; then
        rm -f -- "$config_tmp"
        return 1
    fi
    require_safe_codex_config
}

install_release_config() {
    release_sha=$1
    validate_sha "$release_sha"
    release_config=$AGENTD_RELEASE_ROOT/$release_sha/deploy/container/config.toml
    install_config_atomically "$release_config"
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
    require_safe_codex_config
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
    if [ ! -d "$AGENTD_RELEASE_ROOT/$release_sha" ]; then
        printf '%s\n' "release does not exist: $release_sha" >&2
        return 1
    fi
    temporary_link=$AGENTD_SHARE_ROOT/.current-$release_sha-$$
    if [ -e "$temporary_link" ] || [ -L "$temporary_link" ]; then
        printf '%s\n' "temporary release link already exists" >&2
        return 1
    fi
    if ! ln -s "releases/$release_sha" "$temporary_link"; then
        return 1
    fi
    if ! mv -Tf "$temporary_link" "$AGENTD_CURRENT_LINK"; then
        rm -f -- "$temporary_link"
        return 1
    fi
}

stop_current_release_containers() {
    if [ ! -L "$AGENTD_CURRENT_LINK" ]; then
        return 0
    fi
    current_release_sha >/dev/null
    docker compose \
        --project-directory "$AGENTD_CURRENT_LINK/deploy" \
        --env-file "$AGENTD_CURRENT_LINK/release.env" \
        --file "$AGENTD_CURRENT_LINK/deploy/compose.yaml" \
        down --remove-orphans --timeout 45
}

backup_state() {
    reason=$1
    release_sha=$2
    require_safe_codex_config
    if [ -n "$release_sha" ]; then
        validate_sha "$release_sha"
        release_config=$AGENTD_RELEASE_ROOT/$release_sha/deploy/container/config.toml
        [ -f "$release_config" ] && [ ! -L "$release_config" ] \
            || die "current release has no regular bundled Codex config"
        cmp -s "$AGENTD_CODEX_CONFIG" "$release_config" \
            || die "live Codex config does not match the current release"
    fi
    timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    backup_id=$timestamp-$reason-$$
    validate_backup_id "$backup_id"
    backup_dir=$AGENTD_BACKUP_ROOT/$backup_id
    install -d -m 0700 "$backup_dir"
    printf '%s\n' "${release_sha:-none}" >"$backup_dir/release.sha"
    chmod 0600 "$backup_dir/release.sha"
    install -o 1000 -g 1000 -m 0600 \
        "$AGENTD_CODEX_CONFIG" "$backup_dir/config.toml"

    if [ -f "$AGENTD_DB" ]; then
        python3 "$AGENTD_SQLITE_TOOL" backup \
            "$AGENTD_DB" "$backup_dir/state.sqlite" \
            || die "SQLite backup failed integrity validation"
        chmod 0600 "$backup_dir/state.sqlite"
        sha256sum \
            "$backup_dir/state.sqlite" \
            "$backup_dir/config.toml" \
            >"$backup_dir/SHA256SUMS"
        chmod 0600 "$backup_dir/SHA256SUMS"
    else
        : >"$backup_dir/NO_DATABASE"
        chmod 0600 "$backup_dir/NO_DATABASE"
        sha256sum "$backup_dir/config.toml" >"$backup_dir/SHA256SUMS"
        chmod 0600 "$backup_dir/SHA256SUMS"
    fi
    printf '%s\n' "$backup_dir"
}

restore_state() {
    backup_dir=$1
    [ -d "$backup_dir" ] || die "backup directory does not exist"
    [ -f "$backup_dir/state.sqlite" ] \
        || die "selected backup has no database snapshot"
    [ -f "$backup_dir/config.toml" ] && [ ! -L "$backup_dir/config.toml" ] \
        || die "selected backup has no regular Codex config snapshot"
    (
        cd "$backup_dir"
        sha256sum --check SHA256SUMS >/dev/null
    ) || die "backup checksum validation failed"
    python3 "$AGENTD_SQLITE_TOOL" verify "$backup_dir/state.sqlite" \
        || die "backup database failed integrity validation"
    restore_tmp=$AGENTD_STATE_ROOT/.state.sqlite.restore-$$
    [ ! -e "$restore_tmp" ] || die "temporary restore target already exists"
    config_restore_tmp=$AGENTD_CODEX_HOME/.config.toml.restore-$$
    [ ! -e "$config_restore_tmp" ] && [ ! -L "$config_restore_tmp" ] \
        || die "temporary config restore target already exists"
    if ! python3 "$AGENTD_SQLITE_TOOL" backup \
        "$backup_dir/state.sqlite" "$restore_tmp"
    then
        rm -f -- "$restore_tmp"
        die "failed to create restored SQLite snapshot"
    fi
    chmod 0600 "$restore_tmp"
    if ! install -o 1000 -g 1000 -m 0600 \
        "$backup_dir/config.toml" "$config_restore_tmp"
    then
        rm -f -- "$restore_tmp" "$config_restore_tmp"
        die "failed to prepare restored Codex config"
    fi
    # The service is stopped before every restore. Remove sidecars from the
    # displaced database so SQLite cannot replay an obsolete WAL into the
    # newly restored snapshot when the service starts again.
    rm -f -- "$AGENTD_DB-wal" "$AGENTD_DB-shm"
    if ! mv -f "$config_restore_tmp" "$AGENTD_CODEX_CONFIG"; then
        rm -f -- "$restore_tmp" "$config_restore_tmp"
        die "failed to atomically restore Codex config"
    fi
    if ! mv -f "$restore_tmp" "$AGENTD_DB"; then
        rm -f -- "$restore_tmp"
        die "failed to atomically restore SQLite state"
    fi
    require_safe_codex_config
}

stop_if_active() {
    if systemctl --user is-active --quiet "$AGENTD_SERVICE"; then
        systemctl --user stop "$AGENTD_SERVICE" \
            || die "failed to stop the active agentd service"
        return 0
    fi
    systemctl --user stop "$AGENTD_SERVICE" \
        || die "failed to confirm the agentd service is stopped"
    return 1
}
