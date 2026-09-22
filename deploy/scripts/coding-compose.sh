#!/bin/sh
# Run the coding Compose project with the fixed host policy and optional
# host-specific mounts. The override is never sourced as shell code.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
release_root=$(CDPATH= cd -- "$script_dir/../.." && pwd -P)
coding_env=/home/bened/.local/share/agentd/coding.env
override=/home/bened/.local/share/agentd/coding.override.yaml

[ -f "$release_root/release.env" ] || {
    echo "missing release.env in $release_root" >&2
    exit 1
}
[ -f "$coding_env" ] || {
    echo "missing coding.env at $coding_env" >&2
    exit 1
}

if [ -e "$override" ]; then
    [ -f "$override" ] && [ ! -L "$override" ] || {
        echo "coding override must be a regular non-symlink file" >&2
        exit 1
    }
    set -- --file "$override" "$@"
fi
set -- --project-directory "$release_root" \
    --env-file "$release_root/release.env" \
    --env-file "$coding_env" \
    --file "$release_root/deploy/compose.coding.yaml" \
    "$@"
exec /usr/bin/docker compose "$@"
