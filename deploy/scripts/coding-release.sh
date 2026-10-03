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
controller_unit=agentd-coding-controller.service
case "${AGENTD_PROFILE:-goldenage}" in
  goldenage) ;;
  selfhost)
    base=/home/bened/.local/share/agentd-selfhost
    current="$base/coding-current"
    units='agentd-selfhost-controller.service agentd-selfhost-worker.service agentd-selfhost-publisher.service'
    controller_unit=agentd-selfhost-controller.service
    ;;
  *) echo 'AGENTD_PROFILE must be goldenage or selfhost' >&2; exit 2 ;;
esac
[ -f "$release/deploy/compose.coding.yaml" ] && [ -f "$release/release.env" ] && [ -f "$base/coding.env" ]
compose() {
  location=$1
  shift
  "$location/deploy/scripts/coding-compose.sh" "$@"
}
compose "$release" config --quiet
if [ "${AGENTD_PROFILE:-goldenage}" = selfhost ]; then
  compose "$release" config --format json | python3 "$release/deploy/security/validate_coding_compose.py"
fi
previous=''
if [ -L "$current" ]; then
  previous=$(readlink -f "$current")
  if ! systemctl --user is-active --quiet "$controller_unit"; then
    echo 'existing controller is inactive; restore its ownership and reconcile workers before release activation' >&2
    exit 1
  fi
  # Controller inactivity never proves that a remote worker has stopped.
  # Only its live, authenticated telemetry can authorize the stop below.
  compose "$previous" exec -T coding-controller agentd github --config /etc/agentd/controller.json drain
  report=$(compose "$previous" exec -T coding-controller agentd github --config /etc/agentd/controller.json health || true)
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["controller_live"] and d["draining"] and not d["unresolved_runs"] and d["workers"] and all(w["fresh"] and w["active_runs"] == 0 for w in d["workers"]), "still draining or ownership unresolved; let reconciliation finish before retry"' <<EOF
$report
EOF
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
  # A migration may already have committed. Keep the upgraded binaries and
  # drained durable state; old binaries cannot safely open an unknown schema.
  echo 'activation failed; upgraded release retained drained with durable state' >&2
  exit 1
fi
if [ "${AGENTD_AUTOMATIC_ACTIVATION:-0}" = 1 ]; then
  attempts=0
  while [ "$attempts" -lt 30 ]; do
    report=$(compose "$release" exec -T coding-controller agentd github --config /etc/agentd/controller.json health || true)
    if printf '%s' "$report" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["controller_live"] and not d["unresolved_runs"] and d["workers"] and all(w["fresh"] for w in d["workers"]) and d["source_fresh"] else 1)' 2>/dev/null; then
      compose "$release" exec -T coding-controller agentd github --config /etc/agentd/controller.json undrain
      printf '%s\n' 'Coding services ready; admission restored under the standing policy.'
      exit 0
    fi
    attempts=$((attempts + 1))
    sleep 2
  done
  echo 'new release did not become ready; retaining drain and durable state' >&2
  exit 1
fi
printf '%s\n' 'Coding services started drained. Inspect health/status, then explicitly undrain.'
