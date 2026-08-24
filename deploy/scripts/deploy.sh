#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"

[ "$#" -ge 1 ] && [ "$#" -le 3 ] \
    || die "usage: deploy.sh GIT_SHA [SOURCE_REPOSITORY] [IMAGE_REPOSITORY]"
release_sha=$1
source_repository=${2:-$AGENTD_SOURCE_ROOT}
image_repository=${3:-agentd}
validate_sha "$release_sha"
case "$image_repository" in
    *[!A-Za-z0-9._/-]*|'') die "invalid local image repository" ;;
esac

require_command cmp
require_command docker
require_command git
require_command install
require_command mv
require_command python3
require_command sha256sum
require_command stat
require_command systemctl
require_command tar
require_provisioned_layout

resolved_sha=$(git -C "$source_repository" rev-parse --verify "$release_sha^{commit}")
[ "$resolved_sha" = "$release_sha" ] || die "requested SHA is not a commit"
release_dir=$AGENTD_RELEASE_ROOT/$release_sha
[ ! -e "$release_dir" ] || die "release already exists; refusing to overwrite it"

staging=$(mktemp -d "$AGENTD_RELEASE_ROOT/.staging-$release_sha.XXXXXX")
cleanup_staging() {
    case "$staging" in
        "$AGENTD_RELEASE_ROOT"/.staging-*) rm -rf -- "$staging" ;;
        *) die "refusing to clean an unexpected staging path" ;;
    esac
}
trap cleanup_staging EXIT HUP INT TERM

archive=$staging/release.tar
git -C "$source_repository" archive --format=tar --output="$archive" "$release_sha"
tar -xf "$archive" -C "$staging"
rm -f -- "$archive"
[ -f "$staging/Dockerfile" ] && [ -f "$staging/deploy/compose.yaml" ] \
    || die "release commit does not contain deployment artifacts"

model=${AGENTD_CODEX_MODEL:-gpt-5.6-terra}
reasoning=${AGENTD_CODEX_REASONING_EFFORT:-medium}
[ "$model" = gpt-5.6-terra ] || die "production Codex model is fixed to gpt-5.6-terra"
[ "$reasoning" = medium ] || die "production Codex reasoning effort is fixed to medium"
cat >"$staging/release.env" <<EOF
AGENTD_IMAGE=$image_repository:$release_sha
AGENTD_STATE_ROOT=$AGENTD_STATE_ROOT
AGENTD_DB=$AGENTD_DB
AGENTD_WORKSPACE_ROOT=$AGENTD_WORKSPACE_ROOT
AGENTD_CODEX_HOME=$AGENTD_CODEX_HOME
AGENTD_CODEX_CONFIG=$AGENTD_CODEX_CONFIG
AGENTD_UV_CACHE=$AGENTD_UV_CACHE
UV_CACHE_DIR=$AGENTD_UV_CACHE
AGENTD_REPOSITORY_ROOT=$AGENTD_REPOSITORY_ROOT
AGENTD_CODEX_MODEL=$model
AGENTD_CODEX_REASONING_EFFORT=$reasoning
AGENTD_POLL_SECONDS=${AGENTD_POLL_SECONDS:-1}
AGENTD_ACCOUNT_POLL_SECONDS=${AGENTD_ACCOUNT_POLL_SECONDS:-60}
AGENTD_ACCOUNT_STALE_SECONDS=${AGENTD_ACCOUNT_STALE_SECONDS:-300}
AGENTD_QUOTA_TOP_UP_TOKENS=${AGENTD_QUOTA_TOP_UP_TOKENS:-25000}
AGENTD_HARD_CAP_GRACE_SECONDS=${AGENTD_HARD_CAP_GRACE_SECONDS:-120}
EOF
chmod 0600 "$staging/release.env"

docker build --pull=false \
    --build-arg "SOURCE_SHA=$release_sha" \
    --tag "$image_repository:$release_sha" \
    "$staging"
docker compose \
    --project-directory "$staging/deploy" \
    --env-file "$staging/release.env" \
    --file "$staging/deploy/compose.yaml" \
    config --quiet
"$staging/deploy/scripts/check-container-security.sh" \
    "$staging/release.env" static

mv "$staging" "$release_dir"
trap - EXIT HUP INT TERM
chmod -R go-w "$release_dir"

was_active=false
if stop_if_active; then
    was_active=true
fi
previous_sha=$(current_release_sha)
backup_dir=$(backup_state "before-$release_sha" "$previous_sha")

restore_previous_activation() {
    stop_current_release_containers \
        || die "failed to stop containers before restoring the previous release"
    restore_state "$backup_dir"
    active_sha=$(current_release_sha)
    if [ -n "$previous_sha" ]; then
        if [ "$active_sha" != "$previous_sha" ]; then
            atomic_release_link "$previous_sha" \
                || die "failed to restore the previous release link"
        fi
    elif [ -n "$active_sha" ]; then
        [ "$active_sha" = "$release_sha" ] \
            || die "refusing to remove an unexpected current release link"
        unlink "$AGENTD_CURRENT_LINK"
    fi
    if [ "$was_active" = true ]; then
        systemctl --user start "$AGENTD_SERVICE" || true
    fi
}

if ! install_release_config "$release_sha"; then
    restore_previous_activation
    die "failed to install the release-coupled Codex config"
fi
if ! atomic_release_link "$release_sha"; then
    restore_previous_activation
    die "failed to activate the new release link"
fi

if ! systemctl --user start "$AGENTD_SERVICE"; then
    restore_previous_activation
    die "new release failed to start; previous release was restored when available"
fi

printf '%s\n' \
    "Deployed $release_sha" \
    "Pre-deployment state backup: $backup_dir"
