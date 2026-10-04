#!/bin/sh
# Prepare protected local configuration. No credential contents leave HP.
set -eu
release=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
share=/home/bened/.local/share/agentd-selfhost
state=/home/bened/.local/state/agentd-selfhost/coding
base=${AGENTD_SELFHOST_BASE_COMMIT:-}
if [ -z "$base" ]; then
  base=$(/usr/bin/git -C "$release" rev-parse origin/master)
fi
python3 "$release/tools/configure_selfhost.py" --base-commit "$base"
install -m 0600 "$release/deploy/container/config.toml" "$share/coding-config.toml"
for role in read publish; do
  source=/home/bened/.local/state/agentd/coding-github-$role
  target="$state/github-$role"
  [ -f "$source/hosts.yml" ] && [ ! -L "$source/hosts.yml" ]
  install -m 0600 "$source/hosts.yml" "$target/hosts.yml"
done
auth="$share/codex-home/auth.json"
[ -f "$auth" ] && [ ! -L "$auth" ]
for role in controller worker; do
  target="$state/$role-codex/auth.json"
  [ -e "$target" ] || install -m 0600 "$auth" "$target"
done
transport="$state/transport"
if [ ! -f "$transport/worker.psk" ]; then
  umask 077
  openssl rand 64 > "$transport/worker.psk"
fi
if [ ! -f "$transport/worker.crt" ]; then
  umask 077
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
    -subj /CN=coding-worker -addext subjectAltName=DNS:coding-worker \
    -keyout "$transport/worker.key" -out "$transport/worker.crt" \
    >/dev/null 2>&1
fi
chmod 0600 "$transport/worker.psk" "$transport/worker.key" "$transport/worker.crt"
printf '%s\n' 'Protected self-host configuration prepared.'
