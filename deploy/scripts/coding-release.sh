#!/bin/sh
# Activate or roll back a prepared coding release. Never restore old databases.
set -eu
if [ "$#" -ne 2 ] || { [ "$1" != activate ] && [ "$1" != rollback ]; }; then
  echo 'usage: coding-release.sh activate|rollback /absolute/prepared-release' >&2
  exit 2
fi
release=$(realpath "$2")
base=/home/bened/.local/share/agentd
current="$base/coding-current"
units='agentd-coding-controller.service agentd-worker.service agentd-publisher.service'
[ -f "$release/deploy/compose.coding.yaml" ] && [ -f "$release/release.env" ] && [ -f "$base/coding.env" ]
compose() {
  location=$1
  shift
  "$location/deploy/scripts/coding-compose.sh" "$@"
}
compose "$release" config --quiet
previous=''
if [ -L "$current" ]; then
  previous=$(readlink -f "$current")
  if systemctl --user is-active --quiet agentd-coding-controller.service; then
    compose "$previous" exec -T coding-controller agentd github --config /etc/agentd/controller.json drain
    report=$(compose "$previous" exec -T coding-controller agentd github --config /etc/agentd/controller.json health || true)
    python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["controller_live"] and d["draining"] and not d["unresolved_runs"] and d["workers"] and all(w["fresh"] and w["active_runs"] == 0 for w in d["workers"]), "still draining or ownership unresolved; let reconciliation finish before retry"' <<EOF
$report
EOF
  fi
fi
# New and upgraded releases start drained; status, collection and publication continue.
compose "$release" run --rm --no-deps coding-controller github --config /etc/agentd/controller.json drain
for unit in $units; do
  if systemctl --user cat "$unit" >/dev/null 2>&1; then
    systemctl --user stop "$unit"
  fi
done
install -d -m 0700 /home/bened/.config/systemd/user
for unit in $units; do
  install -m 0644 "$release/deploy/systemd/$unit" "/home/bened/.config/systemd/user/$unit"
done
link="$base/.coding-current-$$"
ln -s "$release" "$link"
mv -Tf "$link" "$current"
systemctl --user daemon-reload
systemctl --user enable $units
if ! systemctl --user start $units; then
  if [ -n "$previous" ]; then
    ln -s "$previous" "$link"
    mv -Tf "$link" "$current"
    systemctl --user start $units
  fi
  echo 'activation failed; prior release restored where available; durable state retained' >&2
  exit 1
fi
printf '%s\n' 'Coding services started drained. Inspect health/status, then explicitly undrain.'
