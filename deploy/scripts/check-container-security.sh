#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${1:-/home/bened/.local/share/agentd/current/release.env}
MODE=${2:-static}

case "$MODE" in static|runtime) ;; *) printf '%s\n' "mode must be static or runtime" >&2; exit 2 ;; esac
[ -f "$ENV_FILE" ] || { printf '%s\n' "missing env file: $ENV_FILE" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { printf '%s\n' "docker is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' "python3 is required" >&2; exit 1; }

rendered=$(mktemp)
trap 'rm -f -- "$rendered"' EXIT HUP INT TERM
docker compose \
    --project-directory "$DEPLOY_DIR" \
    --env-file "$ENV_FILE" \
    --file "$DEPLOY_DIR/compose.yaml" \
    config --format json >"$rendered"
python3 "$DEPLOY_DIR/security/validate_compose.py" "$rendered"

if [ "$MODE" = runtime ]; then
    docker compose \
        --project-directory "$DEPLOY_DIR" \
        --env-file "$ENV_FILE" \
        --file "$DEPLOY_DIR/compose.yaml" \
        run --rm --no-deps --no-TTY --entrypoint /bin/sh agentd -ec '
            test "$(id -u):$(id -g)" = "1000:1000"
            test "$(awk "/^NoNewPrivs:/ {print \$2}" /proc/self/status)" = 1
            test "$(awk "/^CapEff:/ {print \$2}" /proc/self/status)" = 0000000000000000
            if touch /agentd-security-write-test 2>/dev/null; then
                echo "root filesystem is writable" >&2
                exit 1
            fi
            test ! -S /var/run/docker.sock
            test ! -S /run/docker.sock
            awk '\''$5 == "/home/bened" { broad = 1 } END { exit broad }'\'' /proc/self/mountinfo
            unshare --user --map-root-user /bin/true
            /opt/agentd/venv/bin/python \
                /opt/agentd/security/runtime_sandbox_probe.py
        '
fi

printf '%s\n' "agentd container security checks passed ($MODE)"
